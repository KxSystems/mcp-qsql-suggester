# mcp-qsql-suggester

An MCP server that wraps a fine-tuned qSQL specialist (qwen) behind a
self-consistency pipeline — draw several independent candidates per call
(growing rounds, e.g. 4 -> 8 -> 16), gate each on raw q `parse` + KX
qlint C9, execute valid candidates against a dummy table built from the
caller's supplied `table_schemas` across multiple seeds, cluster by
cross-seed execution-equivalence, and return the shortest-code candidate
from the largest cluster. Designed for clients (e.g. Claude Code) that
want to delegate q/kdb+ qSQL drafting to a specialist model while still
catching structural errors — and reducing (not eliminating) the odds of
a single unlucky sample — before returning code to the user.

The server exposes a single tool, `generate_qsql`. See
[`mcp_server/README.md`](mcp_server/README.md) for tool semantics, the
self-consistency clustering pipeline, environment variables, and
customer-UX guidance.

## What the self-consistency pipeline buys you

Measured on a held-out set of 1078 qSQL tasks, holding the model fixed and
varying only the decoding strategy. A pass means the generated q executed against
dummy tables built from the task's declared schemas and its result matched the
task's reference implementation over the same tables; anything that failed to
parse, raised, timed out or returned a degenerate result counts as a failure. The
harness evaluated every task, so the denominator is 1078 exactly.

| decoding | pass | rate |
|---|---|---|
| single sample, greedy (temperature 0) | 606/1078 | 56.2% |
| single sample, temperature 0.8 | 555/1078 | 51.5% |
| **self-consistency (this server), temperature 0.8** | **690/1078** | **64.0%** |
| *pass@16 ceiling — any of 16 samples passes* | *850/1078* | *78.8%* |

The comparison is paired — every row ran on the same tasks — and the margin
against either single-sample baseline is far outside sampling noise. The
significance tests, what the table implies beyond the headline rate, the caveats
behind it and where the tasks came from are in
[How the benchmark was run](#how-the-benchmark-was-run) below.

**What it does not show is semantic correctness.** `passed=true` means "ran
without raising, and independent samples agreed", never "matches what you asked
for" — see [`mcp_server/README.md`](mcp_server/README.md) for why closing that
gap is the caller's job, and why the `generate_qsql` *tool* is opt-in rather
than auto-routed. The two `/qsql-coder` skills are the opposite — they carry
that caller-side step, so Claude reaches for them on its own; see
[Using the skills](#using-the-skills).

## What is and is not in this release

**In**: the MCP server, the shared gate helpers, the client-side
`/qsql-coder` skills, the test suite, and `tools/prepare_release.py`.

**Not in, and not planned**:

- **The training data and the fine-tuning pipeline.** Neither is part of
  this release and there are no plans to publish them, so please do not
  open an issue asking for them — an issue asking for training data will
  be closed with a link to this section.
- **The model weights themselves**, which are distributed separately —
  see [Prerequisites](#prerequisites) and
  [`KxSystems/qsql-qwen35-9b`](https://huggingface.co/KxSystems/qsql-qwen35-9b)
  on Hugging Face. The server is not tied to them though —
  [`SELF_HOSTING.md`](SELF_HOSTING.md) § "Using a different model" covers
  pointing it at any OpenAI-compatible endpoint, and what you give up by
  doing so.
- **kdb+/q, pykx and KX's `qlint.q_`**, which are licensed separately.
  Supply your own copies.

## Prerequisites

Which file owns which requirement — there are no version numbers repeated
here on purpose, so there is only ever one place to change them:

| What you are setting up | Pinned versions live in |
|---|---|
| The MCP server | [`requirements-server.txt`](requirements-server.txt) |
| The vLLM model host | [`requirements-vllm.txt`](requirements-vllm.txt) |
| A colleague's machine that only calls your server | [`requirements-client.txt`](requirements-client.txt) |
| Everything that is not a pip install (q/kdb+, a kdb+ licence, qlint, GPU) | the list below |

This project assumes you are **self-hosting**. There is no public endpoint to
point a client at; the only client-side setup that matters is the one you hand
to colleagues once your own server is up (see
["Point your client at your server"](#point-your-client-at-your-server)).

### If you are hosting the server

- **q/kdb+** on `PATH` (`q --version` should work), with `QHOME` set as
  your q install requires — the `/qsql-coder` skill's Bash-verify step
  runs q locally and inherits your environment rather than guessing a
  layout.
- **Python 3.10+** — the skill also calls
  `.claude/skills/qsql-coder/scripts/format_output.py` (stdlib-only, no
  `pip install` needed).
- `pip install -e .` for `mcp` / `httpx` / `starlette`.
- **pykx** in the same Python env — pinned in and installed by
  [`requirements-server.txt`](requirements-server.txt), *not* by
  `pip install -e .`: `pyproject.toml` deliberately does not declare it.
  The install is pip; what you have to supply separately is the **licence**
  (a kdb+ licence, plus `QLIC` and `QHOME`). Verify with
  `python -c "import pykx; print(pykx.__version__)"`.
- **KX `qlint.q_`** — *optional, but recommended*; it powers the C9 lint
  gate. Licensed separately from this repo, so it is not shipped here. It is
  part of KX's **AxLibraries** collection, which ships inside the KX
  Developer download
  ([code.kx.com/developer](https://code.kx.com/developer/) — EULA-gated). KX
  Analyst is the larger product and ships the same AxLibraries; Developer is
  the smaller download and is sufficient here.

  1. unzip the KX Developer archive
  2. unzip the `ax-libraries.zip` contained inside it
  3. copy `ax-libraries/ws/lib/<your-os>/*` into `ax-libraries/ws/lib/`
     (the shared objects qlint loads at runtime)
  4. `export QLINT_DIR=<...>/ax-libraries/ws`

  KX's own step-by-step reference is
  [code.kx.com/analyst/libraries#download](https://code.kx.com/analyst/libraries/#download).
  Those instructions also have you set `AXLIBRARIES_HOME`; this server does
  not need it (the loader chdirs into `QLINT_DIR` before `\l qlint.q_`, so
  qlint's relative loads resolve either way), but setting it is harmless and
  other AxLibraries tools do expect it.

  Without `qlint.q_` the server still runs: the lint gate is skipped,
  candidates are filtered on q `parse` plus execution, and every response
  reports `lint_gate: "disabled"`. Set `MCP_QSQL_QLINT=required` to make a
  missing `qlint.q_` a startup failure instead.

  It is optional so that a first run does not depend on getting through a
  EULA-gated download — not because the gate is a formality. C9 rejects a
  class of defect the other two gates structurally cannot (code that is
  unsafe under a real `-s N` process but harmless in the single-threaded
  embedded q), and it replaces q's one-word parse errors with named rules
  and often the fix, which is the difference between a caller being able to
  act on a rejection and not. Both are shown in
  [`mcp_server/README.md`](mcp_server/README.md) gate 2. Install it if you
  can.
- **vLLM >= 0.27.0** serving `qsql-qwen35-9b` (Qwen3.5-9B fine-tune) on
  `http://localhost:8000/v1`. `pip install -r requirements-vllm.txt` in
  a dedicated env is the whole setup — no source patches. (0.27.0 was the first
  release to support text-only `Qwen3_5ForCausalLM`; earlier versions
  need hand-patching.) See
  [`mcp_server/VLLM.md`](mcp_server/VLLM.md) for the recommended
  launch, unwrapped-checkpoint rationale, GPU constraints, and
  known-broken combos.
- A **fine-tuned qSQL checkpoint** —
  [`KxSystems/qsql-qwen35-9b`](https://huggingface.co/KxSystems/qsql-qwen35-9b)
  on Hugging Face, or your own `qsql_qwen35_9b_*_merged_unwrapped/`
  fine-tune. Not shipped with this repo either way.

### Colleagues who only call your server

A strict subset of the list above, for someone inside your organisation whose
machine never runs the server. Two things, plus one conditional third:

- **Python 3.10+** and an MCP client (Claude Code).
- **Your colleague's endpoint URL** in a project-scope `.mcp.json` —
  § [Point your client at your server](#point-your-client-at-your-server)
  has the four lines.
- **For `/qsql-coder` only: q/kdb+ on `PATH` with `QHOME` set, *and* a kdb+
  licence on your own machine.** That skill's Bash-verify step runs q
  **locally, on the caller's machine** — it derives an independent reference
  and compares, which is the whole reason it catches errors the server
  cannot (§ [What the self-consistency pipeline buys
  you](#what-the-self-consistency-pipeline-buys-you)). So it needs a working
  q, not just a reachable server. Without it `/qsql-coder` still answers —
  it degrades to presenting the draft unverified, and its verdict line says
  *"Not verified — `q` is not on PATH"* rather than implying nothing needed
  checking. `/qsql-coder-quick` never invokes q, so it works without any of
  that.

No pykx, no qlint, no GPU and no checkpoint either way — those are the
server's problem, not yours.

**Nothing here requires cloning this repo.** If a colleague has given you a
URL, the whole flow is four steps:

1. `/plugin marketplace add KxSystems/mcp-qsql-suggester` then
   `/plugin install mcp-qsql-suggester@mcp-qsql-suggester` —
   § [Installing them as a plugin](#installing-them-as-a-plugin).
2. Put their URL in a `.mcp.json` at the root of your own project —
   § [Point your client at your server](#point-your-client-at-your-server).
   If they told you to forward a port over SSH, do that first and the URL
   stays `http://localhost:9000/mcp`.
3. Restart your client and confirm `/mcp` lists `kxqsql`. The server never
   reads that file, so it cannot warn you; the skills do, by stopping with a
   message rather than answering with q of their own.
4. Ask for a draft with `/qsql-coder-quick` —
   § [A first run](#a-first-run) has a task worth copying, and explains
   what its verdict line does and does not claim.

[`requirements-client.txt`](requirements-client.txt)
is a self-hoster's convenience rather than something you need — it exists to
run [`scripts/qsql_query.py`](scripts/qsql_query.py), which lives in a clone
of this repo and lets you exercise the server with no MCP client at all.

## Using the skills

Two Claude Code skills drive the server: `/qsql-coder`, which Bash-verifies
qwen's draft against an independently-derived reference, and
`/qsql-coder-quick`, which presents the draft directly.

**You do not have to name either one.** Describe the query you want and Claude
routes to one of them on its own whenever the request looks like finance qSQL —
a single table query over market or trading data. Typing `/qsql-coder` or
`/qsql-coder-quick` is how you *override* that choice, not how you reach the
skills.

Which one it picks turns on two properties of the request. As-of or
time-window semantics ("the prevailing quote at each trade", "1-minute bars",
"rolling 20-day"), or a formula / q snippet / worked example to check the
draft against, send it to `/qsql-coder`; anything simpler goes to
`/qsql-coder-quick`. That routing reads your wording, so it is best-effort
rather than a guarantee — name `/qsql-coder` explicitly when you want the
verified path for certain.

The **MCP tool description** is a separate thing and stays deliberately
opt-in: it discourages the primary model from calling `generate_qsql`
directly and skipping the skills, for the reason in [`CLAUDE.md`](CLAUDE.md)
§ "Design stance: opt-in by design". Auto-invoking a *skill* does not
re-open that gap — the skills are where the caller-side verification that
stance is about actually lives.

### Installing them as a plugin

The skills live in [`.claude/skills/`](.claude/skills/), which is *project*
scope — Claude Code loads them only when it runs inside this repo. To use them
from your own q project, install this repo as a plugin:

```bash
/plugin marketplace add KxSystems/mcp-qsql-suggester
/plugin install mcp-qsql-suggester@mcp-qsql-suggester
```

The repeated name is not a typo: the marketplace and its one plugin are both
named after the repo. `add` takes the GitHub path, `install` takes
`<plugin>@<marketplace>`. One plugin carries both skills.

If you copy the skills by hand instead of installing the plugin, copy **both**
`qsql-coder/` and `qsql-coder-quick/`: the quick skill renders its output with
`qsql-coder/scripts/format_output.py`, and stops with a message naming the
missing file when the sibling folder is absent.

The plugin serves them straight out of `.claude/skills/` —
[`.claude-plugin/plugin.json`](.claude-plugin/plugin.json)'s `skills` field
points at that directory, so there is no second copy to keep in sync.

**The plugin does not register the MCP server.** You still need `kxqsql` in a
`.mcp.json` in whichever project you invoke the skills from — § [Point your
client at your server](#point-your-client-at-your-server) next. That is
deliberate rather than an omission: a plugin-supplied MCP server is namespaced
`plugin:<plugin>:<server>`, which would change the tool name out from under the
skills (they call `mcp__kxqsql__generate_qsql`), and the endpoint URL has to be
yours rather than a baked-in `localhost`.

### Point your client at your server

The skills reach the server through an MCP client entry named `kxqsql`,
which lives in a `.mcp.json` at the **root of whichever project you launch
your client from** — this repo if you are self-hosting and working inside
it, your own q project if you installed the skills as a plugin. Four lines:

```json
{
  "mcpServers": {
    "kxqsql": {
      "type": "http",
      "url": "http://localhost:9000/mcp"
    }
  }
}
```

`localhost:9000` is right only when the server runs on the same machine as
your client. If a colleague hosts it, use the URL they give you — that is
usually a local port they told you to forward over SSH, so it still reads
as `localhost`; [`SELF_HOSTING.md`](SELF_HOSTING.md) § "Letting colleagues
use your server" is the host's side of that arrangement.

Inside a clone of this repo those same four lines are tracked as
[`.mcp.json.example`](.mcp.json.example), so `cp .mcp.json.example
.mcp.json` does it. Both are dotfiles — `ls -a` rather than `ls` if you are
looking for them. `.mcp.json` itself is gitignored on purpose, so a
deployment endpoint can never be committed by accident, which is also why
the example is a separate tracked file rather than a ready-to-use
`.mcp.json`.

Restart your MCP client afterwards (the file is read once at startup), then
confirm the registration — in Claude Code, `/mcp` should list `kxqsql`. That
check is worth doing, because nothing on the server side can flag the
omission: the file is read by the client and never by the server, so the
server boots, `/health` returns `ok`, and the tool simply does not exist on
the client side.

Keeping that file correct is yours to do — the plugin cannot supply it (§
[Installing them as a plugin](#installing-them-as-a-plugin) says why). If a
skill fires without `kxqsql` registered, it stops and tells you so; it will
not quietly fall back to writing q itself, because then you would have no way
to tell which model answered. Adding the file mid-session does not help
either — restart, since it is read once at startup.

### A first run

With `kxqsql` registered, ask for a draft. `/qsql-coder-quick` is the one to
try first: it needs nothing beyond the plugin and the endpoint, whereas
`/qsql-coder`'s Bash-verify step also wants `q` on `PATH`.

```
/qsql-coder-quick

Task: Compute 5-minute OHLCV bars from tick data. Table `trade`
(time:p, sym:s, price:f, size:j). Result columns: bucket (the 5-minute
bucket start, as a timestamp), sym, open (first price), high (max
price), low (min price), close (last price), volume (sum size).
```

The leading `/qsql-coder-quick` is shown to make the example reproducible;
drop it and paste the `Task:` block on its own and Claude routes there anyway —
this request has no as-of or time-window semantics and no reference to check
against. Name the skill when you want a specific one for certain: `/qsql-coder`
to force the verified path on a request that would otherwise route to quick
mode, `/qsql-coder-quick` to skip Bash-verify on one that would not.

Naming the table's columns and q type characters is not optional garnish —
`table_schemas` is a required argument, because the model was trained with
schemas in context and the server builds its dummy tables from them. Give the
schema in the task and the skill passes it straight through; leave it out and
the skill has to stop and ask.

**"as a timestamp" is doing real work too.** Without it, this instruction has
two defensible readings and the model splits between them: `0D00:05 xbar time`
keeps the bucket a timestamp, while `5 xbar time.minute` reduces it to a
minute-of-day — which quietly folds 09:30 on Monday together with 09:30 on
Tuesday. Both parse, both run, and they cluster separately because they really
do return different things. Saying which one you want costs four words and is
the general lesson: pin down the result *type*, not just the column names.

A real run of exactly the above returned:

> ### Instruction interpreted as
>
> Bucket trades into 5-minute windows and aggregate per (bucket, sym): opening,
> highest, lowest and closing price, plus total traded size.
>
> ### Schemas used
>
> - `trade`: `` `time`sym`price`size!`p`s`f`j ``
>
> ### TOOL CODE (suggested)
>
> ```q
> select open:first price, high:max price, low:min price, close:last price, volume:sum size by bucket:0D00:05 xbar time, sym from trade
> ```
>
> **Verdict**: passed=true, 4/4 candidates agreed (round 1). Not verified —
> quick mode does not verify against a reference.
>
> **Idiom note**: Treat `passed=true` as 'ran without raising on the dummy
> table', not 'matches intended semantics'. For a draft verified against an
> independent reference, use `/qsql-coder`.

That verdict line is the part worth reading. All four candidates in the first
round landed in one cluster, which clears the early-finalize threshold for a
round of 4, so the server stopped there instead of drawing the 8 and 16 its
default preset allows. `passed=true` means those four parsed, cleared KX qlint
C9, and returned the same result on every dummy-table seed — **not** that the
code means what you asked for. This task is a safe one to take at face value
(`first` / `max` / `min` / `last` / `sum` over a bucket group compose the
obvious way); switch to `/qsql-coder` when being subtly wrong would cost you
something, and see
[What the self-consistency pipeline buys you](#what-the-self-consistency-pipeline-buys-you)
for the failure class that only a reference catches.

Your own run will not match this one number for number, and a unanimous first
round is the good case rather than the typical one — the server samples at a
non-zero temperature, so both the cluster sizes and the round it settles on move
between calls. Eight runs of this exact task on a default `4,8,16` server
finished twice in round 1 at 4/4 and six times in round 3, with winning clusters
from 8 to 10 out of 16. One of the two unanimous rounds picked the minute-of-day
form anyway, which is precisely why the paragraph above says what it says: a
cluster is agreement among the model's own samples, not a check against your
intent.

## Self-hosting the MCP server

[`SELF_HOSTING.md`](SELF_HOSTING.md) is the full step-by-step runbook —
both Python envs, launching and verifying each layer, configuration, and
troubleshooting grouped by which layer is failing. What follows here is the
short version.

### Install

```bash
pip install -r requirements-server.txt   # pinned, verified set — includes pykx
pip install -e .                         # mcp / httpx / starlette
```

`pyproject.toml` declares `mcp`, `httpx` and `starlette` with unbounded
specifiers, and **deliberately does not declare pykx** (it is licensed
separately). So `pip install -e .` alone gives you an env without pykx that
drifts to whatever is newest — [`requirements-server.txt`](requirements-server.txt)
is the file that rebuilds a known-good one. Don't `pip install pykx` by hand
first: the pinned install would then silently change the version you just
chose.

### Launch

The server listens on `0.0.0.0:9000` (endpoint path `/mcp`). Run it in
a long-running session (tmux / systemd / your choice) so client
connections survive shell disconnects:

```bash
cd <repo>
QLINT_DIR=<dir containing qlint.q_> PYTHONPATH=<repo> \
  conda run --no-capture-output -n <your-env> \
  python -m mcp_server.server --transport streamable-http
```

(If you manage your Python env with venv / pyenv / etc. rather than
conda, activate it first and drop the `conda run` line.)

`--transport` is the only CLI flag `mcp_server.server` takes — every
other knob is env-var config read once at startup. A more fully-specified
launch, overriding the vLLM endpoint, self-consistency round caps /
temperature, and the dummy-table seeds / row count:

```bash
cd <repo>
QLINT_DIR=<dir containing qlint.q_> PYTHONPATH=<repo> \
  VLLM_BASE_URL=http://localhost:8000/v1 VLLM_MODEL=qsql-qwen35-9b \
  MCP_QSQL_SC_ROUND_CAPS=4,8 MCP_QSQL_SC_TEMPERATURE=0.6 \
  MCP_QSQL_SC_SEEDS=42,43,44 MCP_QSQL_SC_NROWS=50 \
  conda run --no-capture-output -n <your-env> \
  python -m mcp_server.server --transport streamable-http
```

See [`mcp_server/README.md`](mcp_server/README.md) § "Environment
variables" for the full list and defaults, and § "Tuning
self-consistency execution" for what `MCP_QSQL_SC_SEEDS` /
`MCP_QSQL_SC_NROWS` trade off.

Verify on the host:

```bash
curl http://localhost:9000/health   # {"status":"ok"}
curl -i http://localhost:9000/mcp   # HTTP 405/406 — server is up
```

The server binds `0.0.0.0` with no authentication of its own and executes
model-generated q in-process, so keep it on loopback or behind something
that authenticates, and don't expose it to callers you don't trust. To let
colleagues reach it, see [`SELF_HOSTING.md`](SELF_HOSTING.md)
§ "Letting colleagues use your server".

Then register the endpoint with your client — § [Point your client at your
server](#point-your-client-at-your-server) above.

### Tests

Both tests assume vLLM is up and serving the qSQL specialist model.

```bash
# Loop-only (bypasses the MCP protocol, fastest)
QLINT_DIR=<dir> PYTHONPATH=. python -m mcp_server.smoke_test

# Full MCP protocol round-trip
QLINT_DIR=<dir> PYTHONPATH=. python -m mcp_server.protocol_test
```

## Repo layout

```
.
├── mcp_server/
│   ├── server.py          — FastMCP entrypoint; registers generate_qsql + /health
│   ├── loop.py            — async run_generate_loop (qwen n= sampling → self-consistency clustering)
│   ├── self_consistency.py — clustering engine: dedupe, union-find over cross-seed equivalence, winning cluster + round thresholds
│   ├── qexec.py           — execution primitives (pykx + dummy tables, multi-seed exec+capture, degenerate check, global cleanup)
│   ├── eval_compare.q     — mcp_server's own .eval.cmp / .eval.eqEps / .eval.descVal / .eval.fmtCols (standalone copy, and deliberately STRICTER than the skill's: see either file's header)
│   ├── vllm_client.py     — async httpx client to vLLM /v1/chat/completions (chat(..., n=...) for multi-sample requests)
│   ├── type_gen.q         — server-side typeGen / parseSchema0 / genTable0 (self-consistency exec + pre-derive)
│   ├── type_gen.py        — thin loader exposing TYPE_GEN_STMTS = ["\\l type_gen.q"]
│   ├── smoke_test.py      — loop-only regression
│   ├── protocol_test.py   — full MCP stdio round-trip
│   ├── README.md          — self-consistency clustering, env vars, internal module map
│   └── VLLM.md            — vLLM setup, GPU notes, troubleshooting
├── mcp_qsql_shared/
│   ├── qlint_common.py  — qlint.q_ loader + safe q parse (load-bearing: import before `import pykx`)
│   ├── qlint_kx.py      — KX qlint C9 wrapper (check_c9_qlint)
│   ├── constants.py     — SYSTEM_PROMPT
│   ├── extract.py       — extract_qsql (pull q code from model output)
│   └── io.py            — build_user_prompt
├── scripts/
│   └── qsql_query.py      — call generate_qsql directly, no Claude in the loop (HTTP or --stdio); see SELF_HOSTING.md § Step 8
├── tests/
│   ├── test_extract.py               — tier 1: extract_qsql over raw model completions
│   ├── test_self_consistency.py      — tier 1: round-cap presets, union-find, winning cluster + tie-break, shortest-code pick
│   ├── test_qlint_policy.py          — tier 1: the MCP_QSQL_QLINT gate policy (auto / required / off)
│   ├── test_format_output.py         — tier 1: the skill renderer's case-A verdict claims match its own candidate counts
│   ├── test_pipeline_gates.py        — tier 2: the gates against real embedded q, model stubbed (needs pykx + a licence)
│   ├── test_pipeline_gates_qlint.py  — tier 3: claims about qlint's own output (additionally needs a working qlint.q_)
│   ├── conftest.py                   — the needs_q / needs_qlint markers and their skip logic
│   └── README.md                     — what each tier covers, and what CI cannot reach
├── tools/
│   └── prepare_release.py — maintainer tool: merged checkpoint → publishable weights (inspect / stage / reshard / verify)
├── .claude-plugin/
│   ├── plugin.json        — plugin manifest; its `skills` field points at .claude/skills/ so the skills have one home, not two
│   └── marketplace.json   — one-plugin marketplace, so this repo can be added with /plugin marketplace add
└── .claude/skills/
    ├── qsql-coder/
    │   ├── SKILL.md           — full flow: params → MCP → gate-aware Bash-verify → case A/B/C/D
    │   ├── pre-derive.md      — dev-only A/B-test protocol (expected-schema hint); not auto-loaded
    │   ├── verify_gen.q       — verify-tuned dummy generator (1000 rows / 3 syms / 2h span) for Bash-verify reference compare
    │   ├── eval_describe.q    — .eval.fmtCols / .eval.descVal (single-value shape introspection; shared with pre-derive)
    │   ├── eval_compare.q     — .eval.cmp / .eval.eqEps / .eval.tNorm / .eval.flatNumeric / .eval.diag / .eval.wrapInfo (tolerant compare incl. temporal-resolution normalization, + divergence and wrap diagnostics)
    │   └── scripts/
    │       └── format_output.py — Markdown renderer; single source of truth, used by both skills
    └── qsql-coder-quick/
        └── SKILL.md           — strict-subset variant: case A only, no verify, no rewrite
```

Root files, since `ls` hides the first one and it is the only file a
client-only user has to find:

| file | what it is |
|---|---|
| `.mcp.json.example` | the four-line MCP client entry to copy to `.mcp.json` — § [Point your client at your server](#point-your-client-at-your-server) |
| `requirements-server.txt` | pinned, verified deps for the MCP server env (includes pykx) |
| `requirements-vllm.txt` | pins `vllm` for the separate model-serving env |
| `requirements-client.txt` | the pip half of a client-only box; only needed to run `scripts/qsql_query.py` |
| `pyproject.toml` | the `mcp` / `httpx` / `starlette` deps, unbounded on purpose; pykx deliberately absent |
| `SECURITY.md`, `CONTRIBUTING.md`, `LICENSE` | reporting a vulnerability, why we do not take pull requests, Apache-2.0 |
| `.github/` | CI (tier 1 only), the two issue forms, and the workflow that closes pull requests |

## How the benchmark was run

Detail behind
[What the self-consistency pipeline buys you](#what-the-self-consistency-pipeline-buys-you)
above.

Every row ran on the same tasks, so the comparison is paired: the pipeline wins
105 tasks and loses 21 against the greedy sample (exact McNemar *p* ~= 1 x
10^-14), and wins 162 against 27 versus the first sample of its own
temperature-0.8 draw (*p* ~= 1 x 10^-24). The greedy row is deterministic — two
independent runs of it produced byte-identical completions on all 1078 tasks.

Three things in that table matter more than the headline:

- **Turning the temperature up costs 4.7 points on its own** (56.2% -> 51.5%,
  greedy against one sample at 0.8). Diverse candidates are not free, and the
  clustering has to pay that back before it earns anything. The net over the
  better single-sample baseline is +7.8 points.
- **The wrapper closes 34% of the distance to the pass@16 ceiling.** A majority
  vote over the model's own samples recovers about a third of what a perfect
  oracle picker would.
- **Early stopping cost nothing here.** Finalising a round when its winning
  cluster is unanimous at 4, or reaches weight 6 at 8, cut sample generation by
  34% while passing the same number of tasks as running all 16 — so the `4,8,16`
  round-cap default is not only a latency knob.

Two caveats, because this is an offline reconstruction rather than a recording of
the live server: the rounds were simulated by re-windowing a single 16-sample
draw, where the server draws fresh candidates each round; and the harness applied
the parse and execution gates but not the optional KX qlint C9 check, whose
contribution is narrow but not nil.

**Where the tasks come from, and why they are not published.** They are
synthetic: finance-domain instructions over generated table schemas, each paired
with a reference q implementation, kept only where that reference executed and
passed a correctness judge. So a "pass" means agreement with a generated-then-
filtered reference, not with a q expert. They are held back because releasing
them would put them into the next model's training data and retire the
benchmark.

## Further reading

- [`SELF_HOSTING.md`](SELF_HOSTING.md) — the full runbook: prerequisites,
  both Python envs, launching and verifying each layer, troubleshooting.
- [`mcp_server/README.md`](mcp_server/README.md) — design stance,
  self-consistency clustering pipeline, env vars, internal module map.
- [`mcp_server/VLLM.md`](mcp_server/VLLM.md) — vLLM launch, env setup,
  GPU constraints, known-broken combos.
- [`.claude/skills/qsql-coder/SKILL.md`](.claude/skills/qsql-coder/SKILL.md)
  — the Claude Code skill that drives this server: which parameters it
  sends, the gate-aware Bash-verify step it adds on top of the tool's
  own gates, and how it decides to adopt, rewrite or reject a draft.
- [`CLAUDE.md`](CLAUDE.md) — project guidance for Claude Code
  sessions working in this repo.
- [`tests/README.md`](tests/README.md) — the four test tiers, what each
  one needs installed, and why tiers 2 and 3 skip themselves rather
  than fail when their dependencies are absent.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — filing issues, running the
  four test tiers, what an outside contributor can verify, and why this
  project does not take pull requests.
- [`SECURITY.md`](SECURITY.md) — what this server does by design, and
  how to report something privately.

## Support

- Questions or feedback on this project: open an issue, or write to
  [`incubation@kx.com`](mailto:incubation@kx.com).
- Support for licensed kdb+ subscribers:
  [support.kx.com](https://client.support.kx.com/).
- Security reports: privately, per [SECURITY.md](SECURITY.md) — never as a
  public issue.

An issue is preferred for anything that is not sensitive, because the answer then
stays visible to the next person who hits the same thing.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

The model weights
([`KxSystems/qsql-qwen35-9b`](https://huggingface.co/KxSystems/qsql-qwen35-9b))
are distributed separately under the same licence, and are a derivative of
[`Qwen/Qwen3.5-9B`](https://huggingface.co/Qwen/Qwen3.5-9B) (Apache-2.0).
kdb+/q, pykx and KX's `qlint.q_` are **not** redistributed here and are
licensed separately; supply your own copies.
