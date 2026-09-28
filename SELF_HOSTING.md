# Self-hosting the qSQL MCP server

End-to-end setup for running `mcp-qsql-suggester` on your own host: a local
vLLM serving the fine-tuned qSQL model, plus the MCP server that gates and
clusters its output.

Do the steps in order. Each one ends with a verification command, because
every failure mode in the Troubleshooting section is much easier to place
when you know which layer was last known-good.

**Covered**: hardware and OS prerequisites, obtaining each artifact, both
Python envs, launching and verifying both processes, pointing a client at
them, configuration knobs, and troubleshooting by layer.

**Not covered**: how the model was fine-tuned (the training data and pipeline
are not part of this release); the client-side `/qsql-coder` skill (see
`.claude/skills/qsql-coder/SKILL.md`); TLS termination and public-internet
exposure — this guide assumes loopback or an SSH tunnel, and the server has
no authentication of its own.

## Architecture

Two long-running processes. The runbook below puts both on one host, which
is the simplest arrangement and the one every command assumes, but they
talk over HTTP: set `VLLM_BASE_URL` to a remote endpoint and the MCP
server needs no GPU at all (it still needs pykx and a kdb+ licence, since
the gates run q in-process).

```
  MCP client  (e.g. Claude Code, via .mcp.json)
       |
       |  HTTP, streamable-http transport
       v
  +-------------------------------+        +---------------------------+
  | mcp_server            :9000   |  HTTP  | vLLM              :8000   |
  |                               | -----> |                           |
  |  run_generate_loop:           |        |  serves qsql-qwen35-9b    |
  |   - draw n candidates/round   |        |  OpenAI-compatible API    |
  |   - gate each:                |        |  (its own Python env)     |
  |       1. q parse              |        +---------------------------+
  |       2. KX qlint C9 (opt.)   |
  |       3. multi-seed execution |
  |   - cluster by execution-     |
  |     equivalence, return the   |
  |     shortest code in the      |
  |     winning cluster           |
  |                               |
  |  embedded q via pykx          |
  +-------------------------------+
```

The MCP server runs q **in-process** via pykx (KX's Python↔q binding, which
loads `libq` into the Python process) rather than spawning a `q` subprocess
per gate — at three gates × several candidates × several seeds per call, the
~100 ms subprocess cold-start would dominate. That is also why embedded q is
single-threaded here and every pykx call is serialised through one worker.

The two processes need **separate Python envs**: vLLM pins its own torch and
transformers, which will fight with pykx and `mcp` if installed together.

## Prerequisites

### Hardware / OS

- **Linux.** macOS and Windows are untested.
- **One CUDA-capable GPU with ≥24 GB VRAM** for the recommended fp8 launch.
  Verified on an NVIDIA L4 24 GB: ~20 GB used, of which 7.3 GiB is KV cache
  (224,411 tokens) at `--max-model-len 32768`. No multi-GPU parallelism is
  needed — see "If your hardware differs" below if yours is smaller.
- **Working GPU driver + CUDA.** `nvidia-smi` must work. Installing these is
  distro- and card-specific and out of scope here.
- **~30 GB free disk**: ~17 GB for the checkpoint, ~8 GB for the vLLM env,
  plus pip/conda caches. Using the Hugging Face repo id (Step 3) puts that
  17 GB in `~/.cache/huggingface/hub` instead of wherever you place a manual
  download — doing both costs it twice (~34 GB). Resharded checkpoints and a
  second env variant will want more.

### Software and artifacts

| Item | Where to get it | Notes |
|---|---|---|
| **q/kdb+** | KX, under your q licence | Must be on `PATH` — `q --version` should work. |
| **pykx** | `pip install -r requirements-server.txt`, which pins it; the licence is separate | Embedded q via `libq`. Deliberately **not** declared in this repo's `pyproject.toml`, so `pip install -e .` alone will not bring it in. Do not `pip install pykx` by hand first — the pinned install would then silently change the version you just chose. |
| **A kdb+ licence** | KX | Needed for pykx to run embedded q. See "pykx says Unlicensed" in Troubleshooting — a headless host needs `$QLIC/kc.lic` on disk, not an env var. |
| **KX `qlint.q_`** | *Optional, but recommended.* Part of KX's AxLibraries, inside the KX Developer download ([code.kx.com/developer](https://code.kx.com/developer/), EULA-gated) | Powers the C9 lint gate. KX Analyst ships the same AxLibraries, but Developer is the smaller download and is sufficient. Install steps in [`README.md`](README.md) § Prerequisites; KX's own reference is [code.kx.com/analyst/libraries#download](https://code.kx.com/analyst/libraries/#download). Without it the server still runs with a two-gate stack — optional to keep a first run cheap, not because C9 is a formality; see [`mcp_server/README.md`](mcp_server/README.md) gate 2. |
| **Python 3.10+** | distro / pyenv / conda | 3.12 is the verified version for both envs. |
| **conda** (or venv / pyenv) | [docs.conda.io](https://docs.conda.io/) | Examples below use conda; with venv, activate the env and drop the `conda run` prefix. |
| **The fine-tuned checkpoint** | [`KxSystems/qsql-qwen35-9b`](https://huggingface.co/KxSystems/qsql-qwen35-9b) on Hugging Face, or your own — not shipped with this repo either way; see [`README.md`](README.md) § Prerequisites | Must be the **`*_merged_unwrapped/`** safetensors layout. A wrapped checkpoint has multimodal-prefix keys (`model.language_model.X`) that vLLM's text-only loader rejects; see [`mcp_server/VLLM.md`](mcp_server/VLLM.md) § Checkpoint. |
| **This repo** | `git clone <repo url>` | |

## Step 1 — Clone, and (optionally) fetch the checkpoint

```bash
git clone <repo url>
cd mcp-qsql-suggester
```

**Downloading the checkpoint yourself is optional.** `vllm serve` in Step 3
also accepts a Hugging Face repo id directly —
`vllm serve KxSystems/qsql-qwen35-9b` resolves it and downloads on first run
into `~/.cache/huggingface/hub`. If that is fine for you, skip ahead to Step
2. Keep reading if you want a local copy instead — e.g. your own fine-tune,
or the files somewhere other than the HF cache.

The checkpoint can live anywhere — vLLM is pointed at it explicitly at
launch. Confirm it is the unwrapped variant and that its files are readable:

```bash
ls <checkpoint>/           # expect config.json, tokenizer.json, *.safetensors
```

## Step 2 — Create the vLLM env

```bash
conda create -y -n <your-vllm-env> python=3.12
conda run -n <your-vllm-env> pip install --no-cache-dir -r requirements-vllm.txt
```

That is the whole install — **no source patches**. `requirements-vllm.txt`
pins `vllm==0.27.1`, the first release with native text-only
`Qwen3_5ForCausalLM` support, and deliberately pins nothing else: vLLM
resolves its own torch and transformers, and those wheels are CUDA-specific.
Do **not** add `bitsandbytes` or `autoawq` — both are broken on this
architecture ([`mcp_server/VLLM.md`](mcp_server/VLLM.md) § Known-broken
combos).

Verify:

```bash
conda run -n <your-vllm-env> python -c "import vllm; print(vllm.__version__)"
```

## Step 3 — Launch vLLM

```bash
tmux new -s vllm -d
tmux send-keys -t vllm "CUDA_VISIBLE_DEVICES=<gpu> \
  conda run --no-capture-output -n <your-vllm-env> \
  vllm serve <checkpoint> \
  --served-model-name qsql-qwen35-9b \
  --quantization fp8 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.92" Enter
tmux attach -t vllm      # watch the log; detach with Ctrl-b d
```

`<checkpoint>` is either a local directory or a Hugging Face repo id —
`KxSystems/qsql-qwen35-9b` works here directly (see Step 1). The model
card has the pinning / `HF_HUB_OFFLINE` details for a reproducible load.

Two flags worth understanding:

- **`--served-model-name qsql-qwen35-9b`** is a contract. The MCP server
  sends this exact string in the `model` field of every
  `/v1/chat/completions` request. If you change it here, set `VLLM_MODEL` to
  match on the MCP server side.
- **No `--enforce-eager`.** Its absence is deliberate: letting vLLM capture
  CUDA graphs is worth ~15% on batched (`n=8`) throughput, which is the shape
  self-consistency actually uses, with byte-identical greedy output. The cost
  is **~140 s of boot time** for graph compilation.

**Boot takes ~140 s. That is not a hang.** Wait for
`Application startup complete` rather than assuming it wedged, and note that
transformers prints two harmless lines tagged `[ERROR]` about
`Qwen3VLVideoProcessorInitKwargs` during boot — they come from a docstring
checker for an unrelated video processor. Do not health-check this server by
grepping for `error`.

Verify:

```bash
curl http://localhost:8000/v1/models     # expect "id":"qsql-qwen35-9b"
```

### If your hardware differs

- **Less than 24 GB VRAM**: lower `--max-model-len` (KV cache is the flexible
  part of the budget; 32768 is not a hard requirement — the architecture
  supports up to 262144, and this server's prompts are far shorter).
- **More than one GPU**: you do not need parallelism for a 9B in fp8, and
  adding it costs you cross-GPU traffic for nothing. If you have a reason to
  split anyway, note that tensor parallelism requires the vocab size (248320)
  to be divisible by the TP degree, and that some quantization × parallelism
  combinations load cleanly but **silently corrupt generation** — so validate
  actual output, not just that the server booted.
- **Multi-tenant host**: set `CUDA_VISIBLE_DEVICES` explicitly so vLLM does
  not land on a card someone else is using.

Full detail, including hand-patching vLLM older than 0.27:
[`mcp_server/VLLM.md`](mcp_server/VLLM.md).

## Step 4 — Create the MCP server env

A second env, separate from the vLLM one.

```bash
conda create -y -n <your-server-env> python=3.12
conda run -n <your-server-env> pip install -r requirements-server.txt
conda run -n <your-server-env> pip install -e .
```

`requirements-server.txt` is the pinned, verified set. `pip install -e .`
alone is **not** enough: `pyproject.toml`'s specifiers are unbounded, so a
fresh install drifts to whatever is newest, and pykx is not declared there at
all (it is licensed separately and installed by `requirements-server.txt`
explicitly).

Verify pykx can actually run q, not merely import:

```bash
conda run -n <your-server-env> python -c \
  "import pykx as kx; assert kx.licensed, 'unlicensed'; print(kx.q('1+1'))"
```

`import pykx` succeeding proves nothing — pykx falls back to an unlicensed
mode in which embedded q is unavailable. The `kx.licensed` assertion is the
check that matters.

## Step 5 — Point `QLINT_DIR` at qlint.q_ (optional, but recommended)

```bash
export QLINT_DIR=<...>/ax-libraries/ws
ls $QLINT_DIR/qlint.q_          # should exist
```

Skip this if you do not have AxLibraries. The server then starts with the C9
lint gate disabled, prints a warning saying so once, and marks every response
`lint_gate: "disabled"`; candidates are filtered on q `parse` plus execution.
Set `MCP_QSQL_QLINT=required` if you would rather a missing `qlint.q_` be a
startup failure than a silent reduction in filtering.

Do come back to this step, though. It is optional so that a first run does not
depend on getting through a EULA-gated download, not because C9 is a formality:
[`mcp_server/README.md`](mcp_server/README.md) gate 2 has the defect class it
uniquely rejects, and what its error messages do that q's terse ones do not.

## Step 6 — Launch the MCP server

```bash
tmux new -s mcp-qsql -d
tmux send-keys -t mcp-qsql "cd $(pwd) && \
  QLINT_DIR=$QLINT_DIR PYTHONPATH=$(pwd) \
  conda run --no-capture-output -n <your-server-env> \
  python -m mcp_server.server --transport streamable-http" Enter
tmux attach -t mcp-qsql
```

The server binds **`0.0.0.0:9000`**, endpoint path `/mcp`, with **no
authentication of its own**, and it executes model-generated q in-process.
Keep it on loopback or behind something that authenticates, and do not expose
it to callers you do not trust.

`--transport` is the only CLI flag. Everything else is env-var config read
once at startup; the startup log echoes back the resolved values so you can
confirm what actually took effect.

tmux is the shortest thing that survives a disconnect; for a host that should
bring the server back after a reboot, a user unit is the same command with the
env moved into the file:

```ini
# ~/.config/systemd/user/mcp-qsql.service — systemctl --user enable --now mcp-qsql
[Unit]
Description=qSQL MCP server
After=network-online.target

[Service]
WorkingDirectory=%h/mcp-qsql-suggester
Environment=PYTHONPATH=%h/mcp-qsql-suggester
Environment=QLINT_DIR=%h/ax-libraries/ws
Environment=QHOME=%h/q
Environment=QLIC=%h/q
ExecStart=%h/miniconda3/envs/<your-server-env>/bin/python -m mcp_server.server --transport streamable-http
Restart=on-failure

[Install]
WantedBy=default.target
```

`ExecStart` names the env's interpreter directly rather than going through
`conda run`, which is what makes `Restart=` and `systemctl status` report the
server rather than a wrapper. `QLIC` matters here for the reason in
Troubleshooting's "pykx says Unlicensed": a service is headless, so the
licence has to be on disk. Run `loginctl enable-linger <user>` if the unit
should survive logout.

Verify:

```bash
curl http://localhost:9000/health     # {"status":"ok"}
curl -i http://localhost:9000/mcp     # HTTP 405/406 — alive, rejecting bare GETs
```

## Step 7 — Point your client at it

```bash
cp .mcp.json.example .mcp.json          # registers kxqsql at http://localhost:9000/mcp
```

Edit the URL if your server is elsewhere. `.mcp.json` is gitignored on
purpose, so an endpoint cannot be committed by accident. Start your MCP
client — the file is read once at client startup, so restart it if it was
already running — and in Claude Code, `/mcp` should show
`kxqsql: connected`. Check that before moving on: nothing on the server
side reports a missing `.mcp.json`, because the server never reads it.

If the client runs on a different host, tunnel instead of exposing the port:

```bash
ssh -N -L 9000:localhost:9000 <user>@<server-host>
```

### Letting colleagues use your server

The server has no authentication and no notion of a caller, and a caller who
can reach `/mcp` can in practice get arbitrary q to run in its embedded q
session — so treat reaching the endpoint as equivalent to shell access on
this host ([`SECURITY.md`](SECURITY.md) states the threat model). That leaves
two arrangements worth using, and one to avoid:

- **An SSH tunnel per colleague — the supported default.** Each one runs the
  `ssh -N -L 9000:localhost:9000 <user>@<this-host>` line above and then
  points their own `.mcp.json` at `http://localhost:9000/mcp`, unchanged.
  Authentication is SSH's, so it needs a shell account on this host per
  colleague and nothing else; the server stays on loopback. Their config is
  then byte-identical to yours, which is why the client instructions do not
  branch.
- **A reverse proxy, if a shell account each is not on the table.** It has to
  authenticate every request itself and forward only to `127.0.0.1:9000` —
  the server will trust whatever reaches it. Keep the port itself off any
  interface the proxy does not own.
- **Do not just bind it to a routable interface.** It already binds
  `0.0.0.0`, so "exposing it" is only a firewall change away, and that is
  exactly the thing not to do.

**Capacity.** One server process holds one embedded q session and every q
operation is serialised through a single worker, so concurrent calls share
one q thread: they do not corrupt each other (each call's captured results
are named and protected per call), but their q work queues. A call also
occupies a vLLM slot for each of its sampling rounds. In practice a handful
of colleagues on one server is fine because the calls are short and mostly
spent waiting on vLLM; a team hammering it concurrently will feel the queue
before anything breaks, and the cheap lever is
`MCP_QSQL_SC_ROUND_CAPS=4,8` (§ Configuration) rather than a second server
on the same GPU.

## Step 8 — Smoke-test the whole stack

Both suites need vLLM up. They bypass HTTP (stdio internally), so they do not
need the listener from Step 6 — they exercise the loop and the MCP protocol
directly.

```bash
# Loop only — fastest, no MCP protocol. Expect 3/3.
QLINT_DIR=$QLINT_DIR PYTHONPATH=. conda run --no-capture-output \
    -n <your-server-env> python -m mcp_server.smoke_test

# Full MCP protocol round-trip
QLINT_DIR=$QLINT_DIR PYTHONPATH=. conda run --no-capture-output \
    -n <your-server-env> python -m mcp_server.protocol_test
```

These are **operational** smoke tests — "is vLLM up, is the served checkpoint
still producing usable qSQL" — not regression tests. They sample the model at
a non-zero temperature, so an occasional failure can be an unlucky draw
rather than a bug. For deterministic checks that need no GPU, run the pytest
tiers instead (see [`tests/README.md`](tests/README.md)):

```bash
pytest -q          # every tier your box supports; tiers 2-3 skip themselves
                   # when pykx / a licence / qlint.q_ are absent
```

### Exercising `generate_qsql` by hand

[`scripts/qsql_query.py`](scripts/qsql_query.py) calls the tool directly,
bypassing Claude — so it spends no tokens on the orchestration side, only the
server's own vLLM calls. It is the quickest way to answer "is my server
answering sensibly" with an instruction of your own, and the way to reproduce
a bad answer without a skill in the loop.

```bash
# Against the HTTP listener from Step 6 (--url defaults to localhost:9000/mcp)
python scripts/qsql_query.py \
    --instruction "Compute 5-minute OHLCV bars from tick data. ..." \
    --schema 'trade=`time`sym`price`size!`p`s`f`j'

# Or spawn a throwaway stdio server instead of using the listener
QLINT_DIR=$QLINT_DIR python scripts/qsql_query.py --stdio \
    --instruction "..." --schema 'trade=...'
```

Repeat `--schema name=value` once per table. It needs only the `mcp` package:
`pip install -r requirements-client.txt`. The script's own docstring has
multi-table and join examples.

### What a call should cost

So you can tell slow from broken. Measured on one L4 serving the 9B at fp8,
with the default `4,8,16` caps, three seeds and 20 rows:

| call | wall clock |
|---|---|
| finishes in round 1 (4/4 agree) | **~0.5–1 s** |
| runs all three rounds (16 candidates) | **~7 s** |

Generation dominates; the q side (three dummy tables per round, up to 16
candidates × 3 seeds, each under `system "T 3"`) is a small fraction of it.
Two consequences worth knowing:

- **A call that has not returned in a minute is not a slow call.** The
  likely causes are vLLM still compiling CUDA graphs at boot (~140 s, see
  Step 3), a candidate hitting the 3-second q timeout on every seed, or
  `VLLM_TIMEOUT_S` (default 120) about to fire on an endpoint that is not
  answering.
- **The cheap lever is rounds, not seeds.** `MCP_QSQL_SC_ROUND_CAPS=4,8`
  drops the 16-candidate round, which is most of the 7 s in the bad case.
  Cutting `MCP_QSQL_SC_SEEDS` saves much less and weakens the cross-seed
  agreement guarantee — see `mcp_server/README.md` § "Tuning
  self-consistency execution".

Harder instructions cost more because they finish in later rounds, not
because any single round is slower — a round is one batched request to vLLM
regardless of `n`.

## Configuration

Full table with defaults: [`mcp_server/README.md`](mcp_server/README.md)
§ "Environment variables". The ones you are most likely to set at launch:

| Variable | Why you would set it |
|---|---|
| `QLINT_DIR` | Where your `qlint.q_` is (Step 5). |
| `MCP_QSQL_QLINT` | `required` to refuse to start without the lint gate; `off` to skip it even when present. Default `auto`. |
| `VLLM_BASE_URL` / `VLLM_MODEL` | If vLLM is not on `localhost:8000` or you changed `--served-model-name`. |
| `MCP_QSQL_SC_ROUND_CAPS` | Cheaper calls: `4,8` skips the 16-candidate round. Restricted to validated presets. |
| `MCP_QSQL_SC_TEMPERATURE` | Sampling temperature for candidates (default `0.8`); also overridable per call. |
| `MCP_QSQL_SC_SEEDS` / `MCP_QSQL_SC_NROWS` | Dummy-table shape for the execution gate. Fewer seeds is faster but weakens the cross-seed agreement guarantee — see `mcp_server/README.md` § "Tuning self-consistency execution". |

Leave `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA` unset on any HTTP server more than
one client talks to; it is a per-session stdio feature and under HTTP it would
inject one caller's shape hint into every concurrent caller's prompt.

### Using a different model

Nothing in the gate stack is specific to the fine-tune. `VLLM_BASE_URL` and
`VLLM_MODEL` point the server at any OpenAI-compatible
`/v1/chat/completions` endpoint, and everything downstream of generation —
q `parse`, KX qlint C9, multi-seed execution against the dummy tables, and
the cross-seed clustering — only ever sees the extracted q code. So a
general-purpose model, or your own fine-tune, works:

```bash
VLLM_BASE_URL=http://localhost:8000/v1 VLLM_MODEL=<your-served-name> \
  python -m mcp_server.server --transport streamable-http
```

Two things to know before you do.

**The prompt is a contract with the fine-tune, not a general prompt.**
`mcp_qsql_shared/constants.py`'s `SYSTEM_PROMPT` is one line and
`mcp_qsql_shared/io.py`'s `build_user_prompt` is a dozen — they are
deliberately minimal because they reproduce, byte for byte, the prompts the
recommended checkpoint was trained on. A model that was not trained on them
gets no task framing, no few-shot examples and no output-format
instruction, so expect it to wrap answers in prose, explain itself, or
emit a language other than q. `mcp_qsql_shared/extract.py` pulls q out of
a fenced block and is fairly tolerant, but it is not a parser for arbitrary
chatter. If you are serving a general model, editing those two files to
give it real instructions is the first thing to try — and note you are then
no longer running the prompt contract the published numbers were measured
against.

**The gates still bite, which is the point, but they are a filter and not a
fix.** A weaker model produces more candidates that fail parse or execution,
so they are excluded from clustering and the winning cluster shrinks. You
will see lower `winning_cluster_fraction`, more rounds used, and more
`passed=false`. That is the pipeline working — see the README's
§ "What the self-consistency pipeline buys you" for what clustering does
and does not buy, measured on the recommended checkpoint.

## Stop, restart, update

```bash
# Stop
tmux kill-session -t mcp-qsql
tmux kill-session -t vllm

# Update the repo, then re-launch per Step 6
git pull

# Update vLLM — no patches to re-apply on 0.27+, but re-verify the version
conda run -n <your-vllm-env> pip install -r requirements-vllm.txt
```

Restart the MCP server after changing any of its env vars: all of them are
read once at process startup, so editing your shell profile does nothing to a
server that is already up.

## Troubleshooting

Grouped by layer. Work from the bottom of the stack up — a client-side
symptom is usually a server-side cause.

### vLLM will not boot

| Symptom | Cause and fix |
|---|---|
| "missing weights" / weight-mismatch | You pointed at a wrapped checkpoint. Use the `*_merged_unwrapped/` variant. |
| `Qwen3_5ForCausalLM` not recognised | vLLM older than 0.27.0. Upgrade; or hand-patch per [`mcp_server/VLLM.md`](mcp_server/VLLM.md) § "Serving on vllm < 0.27". |
| OOM at load | Lower `--max-model-len`, or `--gpu-memory-utilization`, or check whether another process already holds VRAM (`nvidia-smi`). |
| Looks hung for ~2 minutes | Expected: CUDA-graph compilation. Wait for `Application startup complete`. |
| `[ERROR] min_frames / max_frames …` | Harmless transformers docstring-checker noise about an unrelated video processor. Ignore. |

### vLLM boots but output is wrong

Validate generated text, not just HTTP 200. If you added quantization or
parallelism beyond the recommended launch, suspect that first: some
combinations load cleanly and silently produce garbage (all `!`, for
instance). Revert to the Step 3 config and re-test.

### MCP server will not boot

| Symptom | Cause and fix |
|---|---|
| `ImportError: pykx` | pykx is not in the env you launched from. `pip install -r requirements-server.txt` into it. |
| `QLINT_DIR is not set` **and startup fails** | You set `MCP_QSQL_QLINT=required`. Either install AxLibraries (Step 5) or drop back to `auto`. Under `auto` this is a warning, not a failure. |
| pykx says "Unlicensed user" | pykx only reads a base64 licence env var from an interactive session; in any headless process it gives up before looking. Write the licence to `$QLIC/kc.lic` yourself — that is the path q resolves natively (it tries `$QLIC/kx.lic`, then `$QLIC/kc.lic`). |
| `Address already in use` | An earlier server is still up. `tmux ls`, then `tmux kill-session -t <name>`. |

### Client shows no `kxqsql` at all

Not the same symptom as the next one, and it is the more likely of the two
on a first run: you skipped `cp .mcp.json.example .mcp.json` in Step 7, ran
it outside the repo root, or have not restarted the client since. There is
**no error message anywhere** for this — `.mcp.json` is read by the MCP
client, never by the server, so the server boots, `/health` returns `ok`,
and a missing config is indistinguishable from a healthy one from its side.
The client simply loads no project-scope server, so the `generate_qsql`
tool does not exist.

That matters because a primary model with no `kxqsql` tool available will
often just answer the q question itself, and the caller gets a draft that
never went through any of the gates. So confirm `/mcp` lists `kxqsql`
before trusting a result.

```bash
ls -a | grep mcp                       # dotfiles: bare `ls` shows neither
cp .mcp.json.example .mcp.json         # if only the .example is there
```

Then restart the MCP client. If `.mcp.json` is present at the repo root and
`kxqsql` still does not appear, check that the client was started with this
repo as its working directory — project-scope config is resolved from there.

### Client shows `kxqsql: failed`

Registered but unreachable — a different problem from the one above.
Check the server's tmux log first — a boot failure surfaces here as a
connection failure. If the server is healthy (`/health` returns `ok`), the
`.mcp.json` URL is wrong: check the port, the `/mcp` path component, and any
trailing slash.

### Calls succeed but results look degraded

| Symptom | What it means |
|---|---|
| `lint_gate: "disabled"` | No usable `qlint.q_`; candidates were filtered on parse plus execution only. Expected if you skipped Step 5. |
| `lint_gate: "disabled"` **but you did install qlint** | Read the startup warning: it names the reason. The usual cause is an incomplete AxLibraries install — `qlint.q_` is a ~2 KB loader stub that needs its `.qlint.*.q_` / `.ax*.q_` siblings **and** `ws/lib/<os>/*` copied into `ws/lib/` beside it. Point `QLINT_DIR` at the real `ws/` directory rather than at a copy of the single file. The server probes `.qlint.lintItem` at startup precisely because q reports the stub's failed sub-loads on stderr *without* failing the load. |
| `exec_ran=false` with a skip reason | Usually a malformed `table_schemas` — the execution gate cannot build a dummy table without a valid schema dict, so clustering has nothing to compare. |
| `passed=false` with `'timeout` | A candidate exceeded q's per-call timeout against a 20-row dummy table. That is essentially always runaway recursion in the draft, not legitimate slowness. |
| `passed=true` but the code is wrong | Expected and documented. `passed=true` means "ran without raising, and independent samples agreed" — not "matches your intent". See [`mcp_server/README.md`](mcp_server/README.md) § "Design stance" for why closing that gap requires caller-side verification against an independent reference. |

## Further reading

- [`README.md`](README.md) — overview, prerequisites, repo layout.
- [`mcp_server/README.md`](mcp_server/README.md) — the gate stack and
  clustering pipeline, full env-var table, design stance, module map.
- [`mcp_server/VLLM.md`](mcp_server/VLLM.md) — vLLM setup in depth: the
  unwrapped-checkpoint rationale, older-vLLM patches, known-broken combos,
  GPU and KV-cache constraints.
- [`tests/README.md`](tests/README.md) — the four test tiers and what each
  one needs.
- [`CLAUDE.md`](CLAUDE.md) — guidance for Claude Code sessions working inside
  this repo.
