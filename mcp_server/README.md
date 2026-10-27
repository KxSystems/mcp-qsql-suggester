# mcp_server

MCP server exposing a single tool, `generate_qsql`, that wraps the
locally-served `qsql-qwen35-9b` (Qwen3.5-9B) fine-tune for qSQL code
generation. Runs as
a long-running HTTP service (one process serves all clients); intended
to be consumed by an MCP client (e.g. Claude Code) so the primary model
can delegate qSQL on demand.

Concurrent calls in that shape are isolated but not parallel: every pykx
operation is serialised through one worker (embedded q is single-threaded),
and each call's captured results are bound under a per-call name and
protected from the other calls' global cleanup — see the `_CALL_SEQ` note in
`self_consistency.py` for why both halves are needed and
`tests/test_pipeline_gates.py::test_concurrent_calls_do_not_collide` for the
regression test.

## Design stance: opt-in, customer-facing UX via skill

Copying the tracked `.mcp.json.example` to `.mcp.json` at the repo root
registers this server with Claude Code, and the tool then appears on the
next Claude Code start (`.mcp.json` itself is gitignored, so an endpoint
cannot be committed by accident). Customer UX is
via the project-level skill **`/qsql-coder`**
(`.claude/skills/qsql-coder/SKILL.md`), which gathers params,
calls the MCP, and presents the verdict. The `TOOL_DESCRIPTION` in
`server.py` remains worded to discourage auto-routing — the tool is not
meant to be reached except through a skill.

**The two layers moved apart.** The *skills* do auto-invoke: their
descriptions route on finance-qSQL wording, so the customer need not type
a slash command. The *tool description* still does not, and that is the
distinction the rest of this section is about — what must not happen is the
primary model calling `generate_qsql` and presenting the result with no
reference check, which is precisely what the skills exist to do. A skill
firing on its own keeps that step; the tool firing on its own removes it.

**Why opt-in (rationale):**

1. **Structural-error gap (closed by execution gating).** The internal
   gate used to be static-only (parse + KX qlint C9). Errors like `max`
   vs `mmax`, `sum` vs `sums`, 5-arg `!` produce valid-but-wrong q that
   passed cleanly, so a primary model with shell access to `q` was
   strictly more rigorous than this MCP. Multi-seed execution (see
   "Self-consistency clustering" below) catches these: q raises `'rank` /
   `'type` etc. against the dummy table, and a candidate that fails on
   any seed is excluded from clustering entirely rather than being
   allowed to win.
2. **Semantic-intent gap (NOT fully closed by any gate — narrowed by
   self-consistency, backstopped by the skill).** A draft can parse, lint,
   and run against dummy data while still not computing what was asked.
   Self-consistency clustering (see "Self-consistency clustering" below)
   reduces the odds of this shipping unnoticed — a majority-cluster answer
   is *more likely* correct than a single greedy sample — but a systematic
   mistake qwen is prone to on a given instruction can still show up in
   every candidate and win the cluster unanimously. Winning-cluster
   agreement is a majority-vote signal, NOT an **external reference**
   compare. The skill `/qsql-coder` backstops the remaining gap via
   conditional Bash-verify (see "Customer-facing skill" below).

Net: the opt-in default remains, but its load-bearing reason has
shifted from "gate rigor insufficient" → "semantic-intent verification
requires the skill's orchestration of an independent reference".
Flipping the **tool** to auto-routing would re-open the semantic-intent gap
because Opus wouldn't be in the loop to draft the reference. Skill-level
auto-invocation does not, for the reason above — the loop is still there,
which is why the two layers are worded differently.

## Self-consistency clustering

`run_generate_loop` no longer asks qwen to retry-and-fix a single draft.
Instead, per call it draws several **independent** candidates at a
non-zero temperature (`vllm_client.chat(..., n=...)`, one HTTP request per
round, or one per candidate under `MCP_QSQL_SPLIT_REQUESTS=1`) in growing rounds — round caps default to `4 -> 8 -> 16`
(`MCP_QSQL_SC_ROUND_CAPS`) and stop early once one round's winning cluster
is large enough (weight ≥ 4 at cap 4, ≥ 6 at cap 8 — these two thresholds
are the only ones empirically validated; see
`mcp_server/self_consistency.py`'s `ROUND_MIN_WEIGHT`). Each candidate is
gated in order:

1. **Raw q `parse`** (`mcp_qsql_shared.qlint_common._parse_safe`) — syntax check.
2. **KX qlint C9** (`mcp_qsql_shared.qlint_kx.check_c9_qlint`) —
   static rule checks; schema-aware via `DECLARED_AFTER_USE` forgiveness.
   Optional, but recommended — see `MCP_QSQL_QLINT` below. This gate earns
   its place twice
   over, and the second way is easy to miss:

   *It rejects things the other two gates cannot.* `{x::1} peach til 3`
   parses cleanly and **executes** cleanly against the dummy table — the
   embedded q is single-threaded, so the global assignment is harmless there
   — and it wins its cluster 4/4 with C9 off. Under a real `-s N` process it
   is a genuine defect. Measured: `MCP_QSQL_QLINT=off` turns that candidate
   from `passed=false` into `passed=true`. Only 1 of qlint's 15 error-level
   rules was found to be reachable by parseable code, so the effect is
   narrow — but it is not zero, and the class it catches (unsafe under
   parallelism, invisible single-threaded) is one no amount of dummy-table
   execution or cross-candidate agreement can find.

   *It translates q's error messages.* This gate therefore runs **even when
   gate 1 failed**. q's parse errors are extremely terse — a function with
   too many globals reports the single word `globals`, a bad escape reports
   the offending token — while qlint names the rule and often the fix
   (`INVALID_ESCAPE: Valid escape sequences are: ...`). Most error-level
   rules also fail q's parse, so linting only parseable code discarded the
   better message in exactly the cases where it mattered. The candidate is
   still rejected on the parse failure; both messages reach the caller in
   `final_issues`, parse error first. (qlint's own `CANNOT_PARSE` is dropped
   in that case — it restates what q already said.)
3. **Multi-seed runtime execution** (`mcp_server.qexec`) — builds a dummy
   table per `table_schemas` entry (via `typeGen` / `parseSchema0` /
   `genTable0`, `mcp_server.type_gen.TYPE_GEN_STMTS`) once per seed
   (`DEFAULT_SC_SEEDS = (42, 43, 44)`), evaluates the candidate under a
   `system "T 3"` timeout via `@[value;code;{"ERR:",x}]`, and flags a
   result as degenerate (bare function / unexpected atom, via
   `.eval.descVal`) if it isn't a real query output. Each execution starts
   from the same state (`qexec.isolated_candidate`): the seed's tables are
   restored from a pristine copy first, and any root global the candidate
   defined is deleted afterwards. Otherwise a candidate could pass only
   because an earlier one defined a helper it uses, or agree with one only
   because that one had filtered the shared table.

Candidates that parse+lint+execute cleanly (non-degenerate, on **every**
seed tested) are clustered by cross-seed output equivalence
(`.eval.cmp`, `mcp_server/eval_compare.q`) — two candidates only join a
cluster if their results agree on every seed, which rules out "agreed by
coincidence on one dummy dataset". Note the file: this is the **server's**
`.eval.cmp`, which counts a type difference as disagreement. The skill's copy
of the same name does not, and that is the point — clustering wants two
candidates that returned differently-typed results kept apart, because the
resulting low `winning_cluster_fraction` is what sends the difference to
Bash-verify to be looked at. The **winning cluster** (largest total
weight, duplicate candidate texts counted) is the answer; within it, the
**shortest-code** candidate is returned. Duplicate candidate texts are
deduped before execution (no re-running byte-identical code), and later
rounds only pay execution cost for genuinely new candidate texts —
earlier rounds' captured results stay bound in the embedded q session for
the rest of the call.

`table_schemas` is **required** at the tool entry — qwen was trained
with schemas in context and clustering needs them to build dummy tables.
The MCP tool refuses calls without it (the customer-facing skill
`/qsql-coder` is responsible for collecting or drafting a schema before
calling the MCP). If `table_schemas` can't actually build dummy tables
(preflight check fails), or `MCP_RUNTIME_EXEC=0`, the whole
self-consistency machinery is skipped up front — generating a full round's
worth of candidates that can never be clustered would be pure waste — and
the call degrades to a single parse+lint-only candidate.

## The response payload

What `generate_qsql` returns. Every field below is at the **top level** except
where noted, so a caller never has to descend into `candidates` to decide
whether to trust the answer.

| field | type | meaning |
|---|---|---|
| `q_code` | str | The answer. On `passed=false` this is the *first-generated* candidate's code, returned for caller fallback rather than as a recommendation. |
| `passed` | bool | A candidate parsed, cleared the lint gate, and executed cleanly on every seed — **and** independent samples agreed on it. Not a claim about intended semantics; see "Design stance" above. |
| `final_issues` | list[str] | Empty when `passed=true`. Otherwise why the first candidate is unusable: q parse error (with the lint messages appended, because those are the actionable half), lint issues, a degenerate-result description, or a q runtime error against the dummy table. |
| `lint_gate` | str | `"kx_qlint_c9"` (gate live) or `"disabled"` (no loadable `qlint.q_`, or `MCP_QSQL_QLINT=off`). A `disabled` response was filtered by two gates, not three. |
| `request_mode` | str | How candidates were drawn: `"n"` (one `n=N` request per round), `"split"` (`MCP_QSQL_SPLIT_REQUESTS=1`) or `"split_auto"` (the endpoint returned fewer choices than requested, so it was switched to sequential `n=1` requests for the rest of the process). A `split*` mode is slower per call; `split_auto` is the reason a deployment got slower without a config change. |
| `rounds_used` | int | How many round caps were consumed (1-3 with the default `4,8,16`). |
| `candidates_generated` | int | Total samples drawn across those rounds. |
| `winning_cluster_size` | int | Weight of the winning cluster — how many *generated* candidates agree with `q_code`, counting duplicates. `0` when nothing survived. |
| `winning_cluster_fraction` | float | `winning_cluster_size / candidates_generated`. `1.0` means unanimous. |
| `n_clusters` | int | Distinct execution-equivalence clusters among the valid candidates. High with a low fraction = the model is guessing. |
| `tie_occurred` | bool | Two or more clusters shared the top weight. The winner was then chosen by tie-break, so agreement is weaker than the fraction alone suggests. |
| `winning_candidate_index` | int \| None | Index into `candidates` of the returned representative. `None` when `passed=false`. |
| `winning_cluster_id` | int \| None | The winning cluster's id, matching `candidates[].cluster_id`. **Absent entirely** in the degraded no-execution path (see below) — use `.get()`. |
| `expected_result_schema_used` | str \| None | The shape actually forwarded to qwen, whether it came from the tool argument or from the deployer's out-of-band channel. `None` if no shape was used. |
| `expected_result_schema_note` | str \| None | Set only when the previous field is non-None: prose explaining what that shape is, for a caller that is seeing it for the first time in this response. |
| `candidates` | list[dict] | Per-candidate detail. Omitted when the caller passes `include_candidates=false`. |

Each entry in `candidates`:

| field | meaning |
|---|---|
| `index` | Generation order across all rounds. |
| `raw_completion` / `extracted` | The model's raw text, and the q pulled out of it by `extract_qsql`. |
| `parse_ok` / `parse_err` | Gate 1. |
| `lint_issues` | Gate 2. Populated even when `parse_ok=false` — C9's message is often the usable half of a parse failure. |
| `exec_ran` / `exec_passed` / `exec_err` | Gate 3. `exec_ran=false` means execution was never attempted (this candidate deduped onto another, or execution is off), so `exec_passed` says nothing — check `exec_ran` first. |
| `degenerate` | Non-None if the result was a bare function or an unexpected atom (`.eval.descVal`); such a candidate is excluded from clustering even though it did not raise. |
| `dedup_group_id` | Byte-identical extracted code. An efficiency detail. |
| `cluster_id` | Executed to equal results on **every** seed tested — the semantically meaningful grouping, and the one that answers "does this candidate agree with `q_code`". `None` if the candidate never executed or was invalid. Two different `dedup_group_id`s can share one `cluster_id` (a comment or a renamed local changes the text but not the result). |
| `exec_skipped_reason` | Present only in the no-execution path below. |

**The degraded path.** With `MCP_RUNTIME_EXEC=0`, or when `table_schemas`
fails the dummy-table preflight, the call collapses to one parse+lint-only
candidate. The payload keeps the same shape so callers need no special case,
but three things differ: `winning_cluster_id` is not present at all;
`candidates[0].exec_ran` is `false` while `exec_passed` is `true` (nothing ran,
so nothing failed); and `candidates[0].exec_skipped_reason` says why. A
`passed=true` from this path means "parsed and linted" only — there was no
execution and no agreement, so the two strongest gates did not run.

## Files

- `server.py` — entrypoint; registers the FastMCP tool and runs the transport chosen by `--transport` (`stdio` is the argparse default; a deployment passes `streamable-http`). Preloads qlint + typeGen + eval_compare on startup.
- `loop.py` — async `run_generate_loop()`; orchestrates qwen (via `n=` multi-sample requests) → self-consistency clustering (`self_consistency.py`) → winner selection. Serializes pykx calls via an asyncio lock + single-worker thread executor. Also owns the `MCP_QSQL_SC_ROUND_CAPS` / `MCP_QSQL_SC_TEMPERATURE` / `MCP_QSQL_SC_SEEDS` / `MCP_QSQL_SC_NROWS` startup config and the `MCP_RUNTIME_EXEC=0` / preflight-failure short-circuit.
- `self_consistency.py` — the clustering engine: dedupes candidate texts, executes new ones across all seeds, unions execution-equivalent candidates (`.eval.cmp`), tracks the winning cluster + round thresholds (`ROUND_MIN_WEIGHT`), picks the shortest-code representative.
- `qexec.py` — execution primitives used by `self_consistency.py`: dummy-table build/drop per seed, candidate exec+capture with error/timeout trapping, degenerate-result detection (`.eval.descVal`), cross-candidate equality (`.eval.cmp`), and root-namespace leaked-global cleanup (candidate code routinely assigns intermediate helpers that would otherwise accumulate across the server's lifetime).
- `eval_compare.q` — mcp_server's own copy of `.eval.cmp` / `.eval.eqEps` / `.eval.descVal` / `.eval.fmtCols`. Deliberately NOT loaded from `.claude/skills/qsql-coder/eval_compare.q`, for two independent reasons. Packaging: mcp_server is meant to be deployable standalone (see Prerequisites in the repo's `CLAUDE.md`), so it carries its own copy rather than depending on `.claude/skills/` being present. Semantics: since 2026-09-08 the two are **not** the same function — the skill copy normalizes temporal resolution (a `u` minute-of-day bucket equals the `p` timestamp bucket covering that minute) and this one does not. Do not "re-sync" them; the file headers carry the argument.
- `vllm_client.py` — module-level `httpx.AsyncClient` wrapping vLLM `/v1/chat/completions`. Disables Qwen3.5 thinking template explicitly; `chat(..., n=...)` returns all `n` completions from a single request (cheaper than `n` separate calls — the prompt is only prefilled once). `MCP_QSQL_SPLIT_REQUESTS=1` swaps that for `n` separate `n=1` requests, for gateways that refuse `n>1` — sequential by default, up to `MCP_QSQL_SPLIT_CONCURRENCY` in flight otherwise. An endpoint that silently ignores `n` (e.g. `mlx_lm.server`, which returns one choice) is detected on the first short response: that call is topped up with `n=1` requests, a warning is logged once, and the rest of the process runs as if the variable were `1`.
- `smoke_test.py` — loop-only regression check (no MCP protocol). Covers happy-path, schema-aware lint, runtime-exec-catches-bug (every candidate invalid), exhaustion (every candidate unparseable), clustering-picks-majority (3-vs-1 split), and the `MCP_RUNTIME_EXEC=0` short-circuit.
- `protocol_test.py` — full MCP protocol regression check. Spawns the server as a subprocess and drives JSON-RPC over stdio; surfaces `rounds_used`, `candidates_generated`, `winning_cluster_size`, `n_clusters` and candidate[0]'s gate results.

## Internal module map

- `mcp_qsql_shared.qlint_common._ensure_qlint` / `_parse_safe` — qlint.q_ loader + safe q `parse`.
- `mcp_qsql_shared.qlint_kx.check_c9_qlint` — KX qlint C9 (schema-aware via `DECLARED_AFTER_USE` forgiveness).
- `mcp_server/type_gen.q` — **server-side** q-side `typeGen` / `parseSchema0` / `genTable0` for dummy-table generation. The column-type vocabulary matches what the model saw at training / evaluation time. Consumed by `qexec.py` (self-consistency execution) and the `/qsql-coder` skill's `pre-derive.md` (those two must share dummy bytes so the shape hint matches what qwen sees at runtime). The skill's Bash-verify reference compare uses a separate verify-tuned dummy — see `.claude/skills/qsql-coder/verify_gen.q` (same API, denser data: 1000 rows / 3 syms / 2h span for grouped-query rigor).
- `mcp_server.type_gen.TYPE_GEN_STMTS` — thin Python loader exposing one element, `\l <abs-path-to-type_gen.q>`, so `qexec._ensure_typegen` can iterate-and-send unchanged.
- The skill-side q helpers (tolerant equality, shape descriptor, divergence and wrap diagnostics) live with the skill at `.claude/skills/qsql-coder/eval_describe.q` and `.claude/skills/qsql-coder/eval_compare.q`. They are NOT imported by anything in `mcp_server/` (which has its own independent copy of the subset it needs — `mcp_server/eval_compare.q`, see "Files" above); the skill `cat`s its own copies into ad-hoc verify scripts. The two `eval_compare.q` files have also **diverged in semantics**, not only in location — the skill's is the more tolerant of the two.
- `mcp_qsql_shared.extract.extract_qsql` — pull q code from model output (fenced-block → heuristic → fallback).
- `mcp_qsql_shared.constants.SYSTEM_PROMPT` — same prompt used at training time.

## Why is the server Python?

The MCP server is **not a pure vLLM proxy** — a one-language proxy would
defeat the design. Its value-add is the **gate stack + self-consistency
clustering** that wraps every qwen call. Two of those gates rely on the
q runtime:

- `parse` and KX qlint C9 require q's parser / qlint-installed q process.
- Runtime exec builds dummy tables and evaluates the candidate q against
  them.

We host q in-process via **pykx** (the Python binding for embedded q),
which is **Python-only** — there is no Go / Node / Bash binding to libq
that gives us the same in-process loading speed. Spawning the `q`
binary per gate (~100 ms cold start each) would inflate latency. So the
server is Python because pykx is Python.

The "talk to vLLM" half (`vllm_client.py`) is just an `httpx` POST — any
language would do that part. But once we want gates + clustering to
share code with q-side utilities (`mcp_qsql_shared.qlint_common`,
`mcp_qsql_shared.qlint_kx`, `mcp_server.type_gen.TYPE_GEN_STMTS`),
Python is the lowest-friction choice.

If you wanted a pure-proxy MCP without gates (just relay Claude Code's
arg → vLLM → response), Python is unnecessary — `curl` would do. But
you'd lose the whole class of bug coverage the gates provide (code that
parses and lints but raises on execution, and candidates that disagree
with each other); see "Design stance" above for why we kept them.

## Setup

### Server launch (once per host)

The MCP server runs as a long-running HTTP service on `0.0.0.0:9000`
(endpoint path `/mcp`; bind address set by `MCP_QSQL_HOST`); it is NOT auto-started. Run once on the host in
a tmux session so it survives shell disconnects:

```bash
tmux new -s mcp-qsql -d
tmux send-keys -t mcp-qsql "cd <repo> && \
  QLINT_DIR=<dir containing qlint.q_> PYTHONPATH=<repo> \
  conda run --no-capture-output -n <your-env> \
  python -m mcp_server.server --transport streamable-http" Enter
tmux attach -t mcp-qsql       # view logs / detach with C-b d
tmux kill-session -t mcp-qsql # stop
```

Verify on the host: `curl -i http://localhost:9000/mcp` (HTTP 405/406 =
server is up and reachable on loopback).

### Client setup

Copy the tracked `.mcp.json.example` to `.mcp.json` at the repo root — it
registers `kxqsql` against `http://localhost:9000/mcp`, i.e. the server
you launched above. Start Claude Code and `/mcp` should show
`kxqsql: connected`.

`.mcp.json` itself is gitignored on purpose, so a deployment endpoint
cannot be committed by accident. If the server lives on another host, an
SSH tunnel (`ssh -N -L 9000:localhost:9000 <user>@<host>`) gives the
client a `localhost:9000` to talk to; point the config at an HTTPS URL
instead if you front the server with a reverse proxy.

Note that the server binds `0.0.0.0` by default and does no authentication of its
own, and that it executes model-generated q in-process. Keep it on
loopback (`MCP_QSQL_HOST=127.0.0.1`) or behind something that authenticates, and do not expose it to
callers you don't trust.

### Python env contents

The env must contain:

| Package | How to get it |
|---|---|
| `pykx` (embedded q via libq) | `pip install -r requirements-server.txt` from repo root — it is pinned there. `pyproject.toml` deliberately does NOT declare it, so `pip install -e .` alone will not bring it in. The install is pip; the licence is separate. Verify with `python -c "import pykx; print(pykx.__version__)"`. |
| `mcp >= 1.27`, `httpx >= 0.28`, `starlette >= 0.40` | `pip install -e .` from repo root (declared in `pyproject.toml`). |

If any of these is missing, the MCP server boot will fail at import
time and `/mcp` in Claude Code will show `kxqsql: failed`. The
specific missing package appears in the server's stderr.

### Shell env vars — stdio only

`QLINT_DIR` (and every other variable in § "Environment variables") is read
by the **server** process at startup. Under `stdio` the client spawns that
process, so the client's own environment is what it inherits:

```bash
export QLINT_DIR=<dir containing qlint.q_>   # then launch Claude Code
```

Under `streamable-http` — the transport recommended above, and the one a
shared deployment uses — the server is launched separately and the client's
environment is irrelevant. Exporting it in the client's shell there is the
quiet way to end up with `lint_gate: "disabled"` on every response despite
a correct qlint install; put it on the server's launch line instead
(`../SELF_HOSTING.md` Steps 5 and 6).

### vLLM prerequisite

vLLM **>= 0.27.0** must be serving `qsql-qwen35-9b` on
`http://localhost:8000/v1`. Install is just `pip install -r
requirements-vllm.txt` into a dedicated env, kept separate from the MCP
server's own env — no source patches on 0.27+. Recommended launch +
hand-patching older vllm + known-broken combos + GPU constraints all live
in [`VLLM.md`](VLLM.md) (the CLAUDE.md `## Serving qwen via vLLM` section
is just a one-screen quick-reference pointing here).

## Tests

Four tiers by dependency — see [`../tests/README.md`](../tests/README.md).

```bash
# Tier 1 — pure logic, pytest only. CI job `unit`.
pytest -q -m "not needs_q"

# Tier 2 — real embedded q, model stubbed. No qlint, no GPU, no vLLM. CI job `integration`.
pytest -m "needs_q and not needs_qlint" -q

# Tier 3 — additionally needs a working qlint.q_ (full AxLibraries ws/ tree).
QLINT_DIR=<dir containing qlint.q_> pytest -m needs_qlint -q

# Tier 4 — needs vLLM running. Run by hand; not in CI.
QLINT_DIR=<dir containing qlint.q_> PYTHONPATH=. conda run --no-capture-output \
    -n <your-env> python -m mcp_server.smoke_test          # loop only
QLINT_DIR=<dir containing qlint.q_> PYTHONPATH=. conda run --no-capture-output \
    -n <your-env> python -m mcp_server.protocol_test       # MCP round-trip
```

## Environment variables

This is the full list — the copies in `../README.md` § Launch and
`../SELF_HOSTING.md` § Configuration are audience-scoped subsets that point
back here, so add a new variable here first.

| Name | Required | Default | Purpose |
|---|---|---|---|
| `QLINT_DIR` | see `MCP_QSQL_QLINT` | — | Dir containing `qlint.q_` |
| `MCP_QSQL_HOST` | no | `0.0.0.0` | Bind address for the `streamable-http` transport (ignored under stdio). `127.0.0.1` gives a loopback-only listener, the safe choice when callers reach the server over an SSH tunnel or a same-host proxy. On a loopback address the MCP SDK also enables DNS-rebinding protection and rejects requests whose `Host` header is not `localhost` / `127.0.0.1` / `[::1]`, so a same-host reverse proxy must forward one of those rather than the public hostname. The startup log line echoes the bind address |
| `MCP_QSQL_QLINT` | no | `auto` | Policy for the C9 gate when `qlint.q_` (licensed separately, so legitimately absent) cannot be loaded. `auto`: disable the gate, warn once at startup, and mark every response `lint_gate: "disabled"`. `required`: fail startup — use this where you need a hard guarantee the gate is live. `off`: skip C9 even when present, which is how you measure what it adds over parse + execution |
| `VLLM_BASE_URL` | no | `http://localhost:8000/v1` | vLLM endpoint |
| `VLLM_MODEL` | no | `qsql-qwen35-9b` | Served-model-name |
| `VLLM_MAX_TOKENS` | no | `2048` | Per-call generation cap |
| `VLLM_TIMEOUT_S` | no | `120` | httpx read timeout |
| `VLLM_API_KEY` | no | unset | Sent as `Authorization: Bearer <key>` when set, for an endpoint behind an authenticating gateway or vLLM's own `--api-key`. Unset sends no `Authorization` header, which is what a bare local vLLM expects. Never logged |
| `MCP_QSQL_SPLIT_REQUESTS` | no | unset | Set to `1` to draw each round's candidates as that many sequential `n=1` requests instead of one `n=N` request. For OpenAI-compatible gateways that refuse `n>1` (e.g. because they meter per completion), so the server can sit behind one rather than only a bare vLLM. Costs: the prompt is prefilled once per request instead of once per round, and total latency grows roughly with the candidate count (caps are cumulative, so the default `4,8,16` is at most 16 requests and `4,8` at most 8) — consider `MCP_QSQL_SC_ROUND_CAPS=4,8` and check your MCP client's tool-call timeout. Requests are sequential by default (`MCP_QSQL_SPLIT_CONCURRENCY` below), so a failure raises exactly as the single request does and one call never bursts a rate-limited gateway (concurrent calls under the HTTP transport still add up). Any value other than `1` leaves it off; a non-empty one also logs a warning. The startup log line echoes `split_requests=`. Not needed for an endpoint that accepts `n>1` but **ignores** it and returns fewer choices: that is detected on the first short response, topped up the same way with a one-time warning, and split for the rest of the process. Setting `1` up front just skips that first wasted request |
| `MCP_QSQL_SPLIT_CONCURRENCY` | no | `1` | How many of those split-mode `n=1` requests may be in flight at once, whether split mode came from `MCP_QSQL_SPLIT_REQUESTS=1` or from detecting an endpoint that ignores `n`. `1` keeps them sequential. Raise it (e.g. `4`) for a server that batches concurrent requests, such as `mlx_lm.server`: a round then costs roughly one batched decode instead of N back-to-back ones. Leave it at `1` for a rate-limited gateway, which is what sequential exists to protect. Order is unchanged (list order is still generation order), and the first failure cancels the requests still pending and raises. Has no effect on an endpoint that honours `n`. A non-positive or non-integer value falls back to `1` with a logged warning; the startup log line echoes `split_concurrency=` |
| `MCP_QSQL_SC_ROUND_CAPS` | no | `4,8,16` | Self-consistency round-cap preset for this process/session. Restricted to validated presets: a single cap (any N, e.g. `16`), `4,8`, or `4,8,16` — anything else falls back to the default with a logged warning. Ceiling: a caller's `max_rounds` tool arg can only shrink this (fewer rounds), never extend it |
| `MCP_QSQL_SC_TEMPERATURE` | no | `0.8` | Default sampling temperature for self-consistency candidates. Freely overridable per call via the tool's `temperature` arg (no ceiling — unlike round caps, temperature isn't a cost-scaling knob) |
| `MCP_QSQL_SC_SEEDS` | no | `42,43,44` | Comma-separated dummy-table seeds for the runtime-exec gate + clustering. Fixed for the process/session lifetime — no per-call arg. A single seed (e.g. `42`) is supported for debugging/latency but weakens the cross-seed agreement guarantee (see "Tuning self-consistency execution" below) |
| `MCP_QSQL_SC_NROWS` | no | `20` | Rows generated per seed per dummy table. Fixed for the process/session lifetime — no per-call arg |
| `MCP_RUNTIME_EXEC` | no | `1` | Set `0` to skip execution entirely (debug only) — self-consistency clustering is impossible without execution, so this short-circuits the whole call to a single parse+lint-only candidate rather than wasting the round-cap generation budget |
| `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA` | no | unset | Set to `1` to gate the env-var shape-injection fallback on. Only meaningful under `stdio`, where each session gets a fresh server subprocess carrying the driver's env; leave it unset on a long-running HTTP server, whose env is frozen at startup and shared by every concurrent client (`../CLAUDE.md` § "RETRIEVE_EXPECTED_SCHEMA flag") |
| `MCP_QSQL_INJECT_EXPECTED_SCHEMA` | no | unset | The out-of-band shape hint itself, read on each request when the gate above is `1`. Ignored otherwise |

## Customer-facing skill

`.claude/skills/qsql-coder/SKILL.md` is the user-facing UX
layer. Customers type `/qsql-coder` (or autocomplete `/q`), or let the
primary model route there off the request's wording; either way the skill
body orchestrates a single MCP call, then presents the drafted q_code +
verdict + optional idiom note. The skill conditionally
Bash-verifies the draft against an independent reference when (a) the q uses
a high-risk named idiom (`wavg`, `mavg`, `fby`, `xexp`, etc. — easy to
over-apply), (b) the functional form `![...]` / `?[...]` is used,
(c) `winning_cluster_fraction < 0.6` or `tie_occurred=true` (candidates split
— qwen was uncertain; the tie clause is redundant above `0.5` and kept
deliberately, see the SKILL.md note), or (d) the user supplied a q snippet or
formula to check against. Execution + clustering covers "does it
run, and do independent samples agree"; the skill's Bash-verify covers
"does it match the intended answer" (catches the semantic-misapplication
class that agreement alone cannot detect without a reference). On
`passed=false`, the skill has Opus rewrite + Bash-verify before
presenting; qwen's failed draft is shown as secondary context, not the
primary answer.

## Tuning self-consistency execution

`qexec` defaults match the maintainer-side offline clustering analysis
this pipeline was ported from (not part of this repo): `NROWS=20`,
`DEFAULT_SC_SEEDS=(42, 43, 44)`, q `system "T 3"` per-candidate timeout.
`NROWS` / seeds are env-configurable at process/session startup —
`MCP_QSQL_SC_NROWS` / `MCP_QSQL_SC_SEEDS` — same startup-config model
as round caps and temperature (fixed for the process's lifetime under
streamable-http, or per spawned session under stdio; no per-call
override). The per-candidate timeout (`DEFAULT_TIMEOUT_S` in
`qexec.py`) is not exposed — change it there and restart if a longer
timeout is needed.
