# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when
working with code in this repository.

## Project Overview

`mcp-qsql-suggester` is a standalone MCP server that wraps a
fine-tuned qSQL specialist (`qsql-qwen35-9b`, a Qwen3.5-9B fine-tune)
with a self-consistency
pipeline: draw several independent candidates per call at a non-zero
temperature (growing rounds, e.g. 4 -> 8 -> 16), gate each on raw q
`parse` + KX qlint C9, execute valid candidates against a dummy table
built from the caller's supplied `table_schemas` across multiple
seeds, cluster by cross-seed execution-equivalence, and return the
shortest-code candidate from the largest (winning) cluster. It exposes
a single tool, `generate_qsql`, intended for MCP clients that want to
delegate qSQL drafting to a specialist model.

The model is **not** part of this repo. The recommended checkpoint is
published at
[`KxSystems/qsql-qwen35-9b`](https://huggingface.co/KxSystems/qsql-qwen35-9b)
on Hugging Face; users may also supply their own fine-tune and serve it
via vLLM. The training pipeline that produced the recommended checkpoint
is not part of this repo either.

## Prerequisites

This project assumes **self-hosting**. Colleagues who only call an
already-running server need a strict subset — the first two bullets below plus
an endpoint URL in a project-scope `.mcp.json`; `requirements-client.txt` is
the pip half of that.

### Self-hosting the MCP server

- **q/kdb+** on `PATH` — `q --version` must succeed — with `QHOME` set as
  your q install requires; Bash-verify inherits it rather than guessing.
- **Python 3.10+** — the `/qsql-coder` skill calls
  `.claude/skills/qsql-coder/scripts/format_output.py` (stdlib-only).
- `pip install -e .` for `mcp` / `httpx` / `starlette`.
- **pykx** in the same Python env — embedded q via libq; pinned in and
  installed by [`requirements-server.txt`](requirements-server.txt), *not*
  by `pip install -e .` (intentionally not declared in `pyproject.toml`).
  The install is pip; the **licence** is what is supplied separately.
  Verify with `python -c "import pykx; print(pykx.__version__)"`.
- **KX `qlint.q_`** — *optional, but recommended*; powers the C9 lint gate.
  Licensed separately, so not shipped here: it is part of KX's **AxLibraries**
  collection, inside the EULA-gated [KX Developer
  download](https://code.kx.com/developer/). Unzip the archive, unzip the
  `ax-libraries.zip` inside it, copy `ax-libraries/ws/lib/<os>/*` into
  `ax-libraries/ws/lib/`, then export `QLINT_DIR=<...>/ax-libraries/ws`.
  KX's reference: [code.kx.com/analyst/libraries#download](https://code.kx.com/analyst/libraries/#download).
  `AXLIBRARIES_HOME` is not needed here (the loader chdirs into `QLINT_DIR`
  first) though it is harmless. Missing `qlint.q_` degrades to a two-gate
  stack rather than failing; see `MCP_QSQL_QLINT` in the env-var table. It
  is optional to keep a first run cheap, not because C9 is a formality —
  `mcp_server/README.md` gate 2 has the defect class it uniquely rejects and
  what its messages do that q's do not.
- **vLLM >= 0.27.0** serving `qsql-qwen35-9b` on
  `http://localhost:8000/v1`. See `## Serving qwen via vLLM` below for
  the quick-ref; full troubleshooting in `mcp_server/VLLM.md`.
- A **fine-tuned qSQL checkpoint** (e.g. `qsql_qwen35_9b_*_merged_unwrapped/`).

## Setup

```bash
pip install -r requirements-server.txt   # pinned, verified set
pip install -e .
```

`pyproject.toml`'s specifiers are unbounded and **pykx is intentionally
not declared** (licensed separately). Use
[`requirements-server.txt`](requirements-server.txt) to rebuild an env —
`pip install -e .` alone drifts to whatever is newest.

## Serving qwen via vLLM

Quick-reference (full detail in [`mcp_server/VLLM.md`](mcp_server/VLLM.md)):

- **Model**: `qsql-qwen35-9b`, a Qwen3.5-9B fine-tune.
- **Python env**: a dedicated env for vLLM, separate from the one this
  MCP server runs in. Verified set: vllm 0.27.1 + torch 2.13.0 +
  transformers 5.15.0 on Python 3.12, ~8.2 GB. `pip install
  --no-cache-dir -r requirements-vllm.txt` into it is the whole install
  — **no source patches**. That file pins only `vllm==0.27.1` on purpose
  (vllm resolves its own torch/transformers, and those wheels are
  CUDA-specific); it is the source of truth for the version. vllm 0.27.0
  was the first release to wire up text-only `Qwen3_5ForCausalLM`
  upstream; on 0.26.0 and earlier it must be hand-patched (see
  `VLLM.md` § "Serving on vllm < 0.27").
- **Checkpoint**: the **unwrapped** safetensors variant
  (`<...>_merged_unwrapped/`). The wrapped variant has multimodal-prefix
  keys (`model.language_model.X`) that vllm's text-only loader rejects.
- **Port**: 8000 (OpenAI-compatible).
- **Served model name**: `qsql-qwen35-9b`.
- **Recommended launch (fp8, single 24 GB GPU)**:
  ```bash
  CUDA_VISIBLE_DEVICES=<gpu> conda run --no-capture-output -n <your-vllm-env> \
      vllm serve <checkpoint> \
      --served-model-name qsql-qwen35-9b \
      --quantization fp8 \
      --max-model-len 32768 --gpu-memory-utilization 0.92
  ```
  No `--enforce-eager`: it was a workaround for the patched class
  hierarchy, and dropping it is worth +15% on batched (n=8) throughput
  — the shape self-consistency actually uses — with byte-identical
  greedy output. Costs ~140 s of boot time (CUDA-graph compilation),
  so wait for `Application startup complete`.
- **Verify**: `curl http://localhost:8000/v1/models`.

For why the `_unwrapped/` checkpoint is needed, hand-patching vllm <
0.27, known-broken combos (bnb + TP/PP, autoawq), GPU / KV-cache
constraints, harmless `[ERROR]` boot noise from transformers, and why
tool-calling is unsupported — see
[`mcp_server/VLLM.md`](mcp_server/VLLM.md).

## Launching the MCP server

[`SELF_HOSTING.md`](SELF_HOSTING.md) is the end-to-end runbook (both envs,
per-layer verification, troubleshooting by layer); this section is the
quick-reference.

The server listens on `0.0.0.0:9000` (endpoint path `/mcp`). It is not
auto-started; run once per host in a long-running session:

```bash
tmux new -s mcp-qsql -d
tmux send-keys -t mcp-qsql "cd <repo> && \
  QLINT_DIR=<dir> PYTHONPATH=<repo> \
  conda run --no-capture-output -n <your-env> \
  python -m mcp_server.server --transport streamable-http" Enter
```

(If you manage your Python env with venv / pyenv / etc. rather than
conda, activate it first and drop the `conda run` line.)

`--transport` is the only CLI flag `mcp_server.server` takes — every
other knob (self-consistency round caps/temperature/seeds/nrows,
vLLM endpoint, qlint dir, runtime-exec toggle) is env-var config read
once at process startup (see "Environment variables (server-side)"
below). A more fully-specified launch, overriding several defaults at
once:

```bash
tmux new -s mcp-qsql -d
tmux send-keys -t mcp-qsql "cd <repo> && \
  QLINT_DIR=<dir> PYTHONPATH=<repo> \
  VLLM_BASE_URL=http://localhost:8000/v1 VLLM_MODEL=qsql-qwen35-9b \
  MCP_QSQL_SC_ROUND_CAPS=4,8 MCP_QSQL_SC_TEMPERATURE=0.6 \
  MCP_QSQL_SC_SEEDS=42,43,44 MCP_QSQL_SC_NROWS=50 \
  conda run --no-capture-output -n <your-env> \
  python -m mcp_server.server --transport streamable-http" Enter
```

This example: caps self-consistency at 2 rounds (4 -> 8, skipping the
16-candidate round), lowers sampling temperature to 0.6, keeps the
default 3 seeds, and generates 50 rows per seed instead of the
default 20. The startup log line (`qlint.q_ + typeGen + eval_compare
loaded (round_caps=..., temperature=..., seeds=..., nrows=...,
split_requests=...)`)
echoes back the resolved config so you can confirm what actually took
effect — an invalid env value falls back to its default with a
logged warning rather than failing startup.

Verify on the host:

```bash
curl http://localhost:9000/health   # {"status":"ok"}
```

## MCP server layout

See [`mcp_server/README.md`](mcp_server/README.md) for the full file
map. Summary of the self-consistency pipeline inside `run_generate_loop`:

1. **Generate**: draw `n` independent candidates per round via
   `vllm_client.chat(..., n=...)` (one HTTP request per round — the
   prompt is prefilled once, not once per candidate; one request per
   candidate under `MCP_QSQL_SPLIT_REQUESTS=1`). Round caps default
   to `4 -> 8 -> 16` (`MCP_QSQL_SC_ROUND_CAPS`), stopping early once a
   round's winning cluster is large enough (`mcp_server.self_consistency
   .ROUND_MIN_WEIGHT`).
2. **Gate each candidate**: raw q `parse` (`mcp_qsql_shared.qlint_common
   ._parse_safe`) → KX qlint C9 (`mcp_qsql_shared.qlint_kx.check_c9_qlint`,
   schema-aware via `DECLARED_AFTER_USE` forgiveness; runs even when parse
   failed, purely to upgrade q's terse message — see `mcp_server/README.md`
   gate 2) → multi-seed
   execution (`mcp_server.qexec`) against a dummy table built per
   `table_schemas` entry (`mcp_server.type_gen.TYPE_GEN_STMTS`) across
   seeds (default `(42, 43, 44)`, `MCP_QSQL_SC_SEEDS`) with
   `MCP_QSQL_SC_NROWS` rows per seed (default `20`), under a `system
   "T 3"` timeout via
   `@[value;code;{"ERR:",x}]`. A candidate invalid on ANY seed (raises,
   times out, or produces a degenerate result — bare function / unexpected
   atom via `.eval.descVal`) is excluded from clustering.
3. **Cluster**: candidates that agree (`.eval.cmp`,
   `mcp_server/eval_compare.q`) on EVERY seed tested join the same
   cluster (`mcp_server.self_consistency`, union-find). The winning
   cluster (largest total weight) is the answer; the shortest-code
   candidate within it is returned.

**`eval_compare.q` exists twice and the two copies deliberately differ.**
`mcp_server/eval_compare.q` serves clustering above;
`.claude/skills/qsql-coder/eval_compare.q` serves the skill's Bash-verify and
additionally normalizes temporal resolution, so a `u` minute-of-day bucket
compares equal to the `p` timestamp bucket covering that minute. Clustering
asks "did independent samples agree?" — where a type difference *is*
disagreement, and keeping those candidates in separate clusters is what drops
`winning_cluster_fraction` below the skill's verify threshold so the
difference gets checked. Bash-verify asks "does this match the reference's
intent?", where the same difference is presentation. **Do not re-sync them**;
both file headers carry the full argument, and `internal/eval/README.md`
trap 11 records which copy the eval harness grades with.

`table_schemas` is **required** at the tool entry — the model was
trained with schemas in context and clustering needs them to build
dummy tables.

## Design stance: opt-in by design

This server is engineered for **explicit invocation only**; the
`TOOL_DESCRIPTION` in `mcp_server/server.py` is deliberately worded to
discourage primary-model auto-routing. The reason is not that the
gates are weak — they catch structural errors (`max` vs `mmax`, 5-arg
`!`, etc.) by excluding any candidate that raises on execution from
clustering entirely, and self-consistency further reduces (without
eliminating) the odds of a single unlucky sample winning. The reason is
a separate **semantic-intent gap** that no purely-internal gate — static,
runtime, or cross-candidate agreement — can fully close:

A draft can use a real q idiom (e.g. `wavg`) but combine it
incorrectly with adjacent ops (e.g. `wavg[size;price] % sum size`
double-divides because `wavg` already includes the `% sum size` step).
The code parses, lints, and runs against the dummy table; self-consistency
clustering can only catch this if *some* candidates make the mistake and
others don't — a systematic error qwen is prone to on a given instruction
can show up in every candidate and win the cluster unanimously. Catching
that requires an external reference (a computation derived independently
of the draft) and a verify step — both must be orchestrated by the
caller, not by this server.

Treat `passed=true` from this MCP as **"ran without raising, and
independent samples agreed"**, not **"matches intended semantics"**.
Winning-cluster agreement is a majority-vote signal over the model's own
samples, not a comparison against ground truth. Callers that depend on
semantic correctness should add their own reference-based verification on
top (in the `/qsql-coder` skill this is called Bash-verify).
Auto-routing strips that caller-side step and re-opens the gap, so
**do not** weaken the tool description to encourage primary-model
auto-invocation.

## RETRIEVE_EXPECTED_SCHEMA flag

`mcp_server.loop` reads a startup-time gate
`RETRIEVE_EXPECTED_SCHEMA` from the env var
`MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA` (`"1"` → on, anything else → off).
When on, the loop accepts the `MCP_QSQL_INJECT_EXPECTED_SCHEMA` env
var as a fallback for `expected_result_schema` — a driver can inject
a shape hint into qwen's prompt without exposing it to the caller's
prompt. The explicit `expected_result_schema` argument on the MCP
tool is per-request and unaffected by the gate.

**Multi-tenant safety**: leave `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA`
unset on any long-running HTTP server that more than one client talks
to. The env-var fallback only makes sense for the **stdio** transport,
where each MCP session spawns a fresh server subprocess that inherits
the driver's per-case env. Under HTTP the server is long-running and its
env is frozen at startup, so the fallback can't carry per-request data —
and turning it on would inject the same shape into every concurrent
client's prompt.

The intended consumer is a batch driver that invokes `claude -p
--mcp-config <its own config>` to rebind `kxqsql` to stdio for that
session only: the stdio subprocess gets
`MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA=1` from the config's `env:` block,
and the driver sets the per-case shape via
`MCP_QSQL_INJECT_EXPECTED_SCHEMA` in the spawned subprocess env.

## Testing

Four tiers, one per rung of the dependency ladder. Install what you have
access to and run a bare `pytest -q` — tiers 2 and 3 skip themselves when
their dependencies are absent, so the same command is correct at every rung.
Every skip prints its reason (`addopts = ["-ra"]`), and naming an unavailable
tier with `-m` is a usage error rather than a silent `0 passed, N skipped`
(`tests/_tiers.py`). Prefer `python -m pytest` over a bare `pytest`: the latter
resolves through `PATH` and may not be the env holding pykx.
Full detail in [`tests/README.md`](tests/README.md).

**Tier 1 — pure logic** (107 tests). No q, no kdb+ license, no pykx, no
`qlint.q_`, no GPU. Covers `extract_qsql`, the self-consistency decision
logic (round-cap validation, union-find, winning-cluster choice +
tie-break, shortest-code pick, `ROUND_MIN_WEIGHT` thresholds), the
`MCP_QSQL_QLINT` gate policy, the `vllm_client` request shaping (`n=N` vs `MCP_QSQL_SPLIT_REQUESTS`), and the skill renderer's case-A verdict
claims (`.claude/skills/.../format_output.py`). The one tier every CI
runs (`unit`).

**Tier 2 — the gates against real embedded q, model stubbed** (74 tests).
Needs pykx + a licence, but **no qlint, no GPU and no vLLM**:
`vllm_client.chat` is replaced with fixed completions, while parse,
multi-seed execution and clustering all run for real (22 tests), plus the
skill copy of `eval_compare.q` and its deliberate divergence from the server
copy (52 tests). CI-able only where a kdb+ licence can be supplied as a
secret.

**Tier 3 — the claims that need a real linter** (6 tests). Additionally
needs a working `qlint.q_` — a full AxLibraries `ws/` tree, not just the
loader stub. In no CI: the tree is far too large to inject as a secret.

```bash
pytest -q                                   # everything your box supports
pytest -q -m "not needs_q"                  # tier 1 only
pytest -q -m "needs_q and not needs_qlint"  # tier 2 only
pytest -q -m needs_qlint                    # tier 3 only
```

**Tier 4 — the two `python -m` suites.** Need everything above **plus**
vLLM up serving `qsql-qwen35-9b`. They bypass HTTP (use stdio
internally), so they do not need an MCP HTTP listener. Run by hand; not
wired into CI, because they sample the model at a non-zero temperature
and can go red on an unlucky draw rather than a code regression.

```bash
# Loop-only (fastest; no MCP protocol)
QLINT_DIR=<dir> PYTHONPATH=. python -m mcp_server.smoke_test

# Full MCP protocol round-trip
QLINT_DIR=<dir> PYTHONPATH=. python -m mcp_server.protocol_test
```

**Do not hoist the lazy pykx imports in `mcp_server/self_consistency.py`
back to module scope** — that would drag pykx into tier 1 and take
`tests/` out of CI's reach. The same goes for the lazy `httpx` import in
`mcp_server/vllm_client.py`: tier-1 CI installs pytest and nothing else.

## Environment variables (server-side)

Mirrors `mcp_server/README.md` § "Environment variables", which is the
canonical copy — kept in full here so a session does not have to open
another file. Add a variable there first, then here.

| Name | Required | Default | Purpose |
|---|---|---|---|
| `QLINT_DIR` | see `MCP_QSQL_QLINT` | — | Dir containing `qlint.q_` |
| `MCP_QSQL_QLINT` | no | `auto` | `auto` = use qlint if `QLINT_DIR` yields a loadable `qlint.q_`, else disable the C9 gate with a startup warning and `lint_gate: "disabled"` on every response. `required` = fail startup instead (set this on a shared deployment to guarantee the gate is live). `off` = skip C9 even when available, e.g. to measure what it contributes over parse + execution |
| `VLLM_BASE_URL` | no | `http://localhost:8000/v1` | vLLM endpoint |
| `VLLM_MODEL` | no | `qsql-qwen35-9b` | Served-model-name |
| `VLLM_MAX_TOKENS` | no | `2048` | Per-call generation cap |
| `VLLM_TIMEOUT_S` | no | `120` | httpx read timeout |
| `MCP_QSQL_SPLIT_REQUESTS` | no | unset | Set to `1` to draw each round's candidates as sequential `n=1` requests instead of one `n=N` request, for gateways that refuse `n>1`. Slower (prompt re-prefilled per request; caps are cumulative, so default caps = at most 16 requests) — see `mcp_server/README.md` for the trade-offs |
| `MCP_QSQL_SC_ROUND_CAPS` | no | `4,8,16` | Self-consistency round-cap preset, fixed at process/session startup. Restricted to validated presets: a single cap (any N), `4,8`, or `4,8,16`. Ceiling model — a caller's `max_rounds` tool arg can only request fewer rounds, never more |
| `MCP_QSQL_SC_TEMPERATURE` | no | `0.8` | Default sampling temperature for self-consistency candidates. Freely overridable per call via the tool's `temperature` arg |
| `MCP_QSQL_SC_SEEDS` | no | `42,43,44` | Comma-separated dummy-table seeds, fixed at process/session startup. No per-call arg. A single seed is accepted for debugging/latency but weakens the cross-seed agreement guarantee (see `mcp_server/README.md` § "Tuning self-consistency execution") |
| `MCP_QSQL_SC_NROWS` | no | `20` | Rows generated per seed per dummy table, fixed at process/session startup. No per-call arg |
| `MCP_RUNTIME_EXEC` | no | `1` | Set `0` to skip execution entirely (debug only) — collapses the call to a single parse+lint-only candidate, since clustering has nothing to compare without execution |
| `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA` | no | unset | Set to `1` to gate the env-var shape-injection fallback on. Only meaningful under stdio transport (per-session subprocess env); leave unset on the long-running HTTP server |
| `MCP_QSQL_INJECT_EXPECTED_SCHEMA` | no | unset | Out-of-band shape hint, read on each request when `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA=1`. Set per-case by the batch driver in the stdio MCP subprocess env |

## Repo layout

See [`README.md`](README.md) § "Repo layout" for the annotated module
map (`mcp_server/`, `mcp_qsql_shared/`, `tools/`, `.claude/skills/`).
