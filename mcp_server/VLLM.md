# Serving qwen via vLLM — setup and gotchas

This doc covers everything needed to launch the local vLLM that backs
the MCP server (`mcp_server/`) and the `/qsql-coder` skill for the
fine-tuned `qwen3.5-9b`. CLAUDE.md has the quick-reference summary; the
full troubleshooting / config detail lives here on-demand (avoids
loading 80 lines of "what to do if vllm boot fails" into every Claude
Code conversation).

**No source patches are needed.** Earlier revisions of this doc
required three hand-edits to vllm 0.20.2's source to make it serve a
text-only Qwen3.5 (`Qwen3_5ForCausalLM` was absent from `registry.py`,
and `Qwen3_5ForCausalLMBase` lacked the `IsHybrid` mixin and the
`get_mamba_state_*_from_config` classmethods). **vllm 0.27.0 added all
three upstream**, verbatim — first release to carry them; 0.26.0 and
earlier still need the patches. Use vllm >= 0.27.0 and there is nothing
to patch. If you are pinned to an older vllm for unrelated reasons, see
"Serving on vllm < 0.27" at the bottom.

## Python env

Use a **dedicated env for vLLM**, separate from the one the MCP server
runs in, so vllm's pinned deps don't collide with pykx / mcp. With conda
(venv / pyenv work the same way — just activate it and drop the
`conda run` prefix):

```bash
conda create -y -n <your-vllm-env> python=3.12
conda run -n <your-vllm-env> pip install --no-cache-dir -r requirements-vllm.txt
```

That is the whole install — no post-install source edits.
[`requirements-vllm.txt`](../requirements-vllm.txt) (repo root) is the
single source of truth for the pinned version (`vllm==0.27.1`); this
repo's other two requirements files cover the MCP server
(`requirements-server.txt`) and the thin client
(`requirements-client.txt`) instead.

The verified set is vllm 0.27.1 + torch 2.13.0 + transformers 5.15.0 on
Python 3.12, about **8.2 GB** on disk. `vllm` pins its own torch and
transformers, so let it resolve them rather than pinning yourself —
that is why `requirements-vllm.txt` deliberately pins only the one line;
`--no-cache-dir` avoids leaving another ~5 GB of wheels in
`~/.cache/pip`.

You do **not** need `bitsandbytes` / `autoawq` here — both are broken
on this architecture (see "Known-broken combos" below).

**Verifying a clean install.** vllm ships a `RECORD` of sha256 hashes,
so you can prove no file was hand-edited:

```bash
conda run -n <your-vllm-env> python - <<'EOF'
import base64, csv, hashlib, os, vllm
sp = os.path.dirname(os.path.dirname(vllm.__file__))
rec = next(os.path.join(sp, d, "RECORD") for d in os.listdir(sp)
           if d.startswith("vllm-") and d.endswith(".dist-info"))
bad = []
for row in csv.reader(open(rec)):
    if len(row) < 2 or not row[1].startswith("sha256=") or not row[0].endswith(".py"):
        continue
    f = os.path.join(sp, row[0])
    if not os.path.exists(f):
        continue
    got = "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(open(f, "rb").read()).digest()).rstrip(b"=").decode()
    if got != row[1]:
        bad.append(row[0])
print("modified files:", bad or "none")
EOF
```

Expect `modified files: none`. (Harmless false positive: some envs
report a top-level `build_backend.py` as modified — `flashinfer` and
`torch_c_dlpack_ext` both install a file by that name at the
site-packages root and one overwrites the other. It is a PEP 517 build
hook, never imported at serve time.)

## Checkpoint

Load the **`*_merged_unwrapped/`** variant, NOT a plain `*_merged/` one.
The unwrapping is a one-time safetensors rewrite that:

- Strips `model.language_model.X` → `model.X` from every safetensors key
- Removes `mrope_section` / `mrope_interleaved` from `rope_parameters`
  in `config.json`

**Why:** The base model series ships safetensors keys saved from the
multimodal `Qwen3_5ForConditionalGeneration` wrapper, but
`architectures: ["Qwen3_5ForCausalLM"]` in `config.json` claims
text-only. The text-only loader expects unprefixed `model.X` keys, so
hitting the original wrapped checkpoint with vllm's text-only path
produces "missing weights" → re-init + OOM (transformers) or weight
mismatch (vllm).

Plain RoPE is mathematically equivalent to M-RoPE on text-only input,
so the M-RoPE config strip is safe.

vllm 0.27.1 still has no key remapping on the text-only path, so this
requirement has not gone away — but it is a **maintainer** concern,
not a user one: the published weights are already unwrapped, so anyone
serving the distributed checkpoint never encounters the wrapped
variant. Keep this section for whoever next re-merges a LoRA from the
base model and wonders why vllm reports missing weights.

### The chat template: strip the training tags before publishing

A freshly merged checkpoint carries the **training** chat template, which
contains `{% generation %}` / `{% endgeneration %}`. Those tags mark the
assistant span so training can compute assistant-only loss. They render to zero
characters, so the prompt the model sees is identical with or without them
(verified: byte-identical text *and* token ids across system+user,
+expected-schema, with-assistant-turn and multi-turn shapes, with and without
`add_generation_prompt`). There is no train/serve skew either way.

They are still wrong to ship, because `{% generation %}` is **not standard
Jinja** — it is an extension registered by transformers. vLLM works only because
it renders via transformers. Plain `jinja2` raises `TemplateSyntaxError:
Encountered unknown tag 'generation'`, and so does anything else that does not
go through transformers: GGUF conversion, llama.cpp, Ollama, TGI's Rust
renderer, the JS renderer behind the Hugging Face model page.

So before uploading, strip the two tags from the checkpoint's own copy:

```bash
sed -i -e 's/{% generation %}//' -e 's/{% endgeneration %}//' \
    <checkpoint>/chat_template.jinja

# must print nothing
grep -n 'generation %}' <checkpoint>/chat_template.jinja
```

Verified across 18 merged checkpoints spanning both ways the tags get there —
hand-written into the tokenizer directory, and inserted at train time by a
string patch — every one of them spells the tags exactly `{% generation %}` /
`{% endgeneration %}`, with no `{%-` whitespace-control variants, and every one
renders under plain `jinja2` after the strip.

**This repo deliberately does not keep its own copy of the template.** An
earlier revision did, as a "canonical inference variant", which was a mistake:
it created a second file to keep in step with the training template, and the
only way to know whether they had drifted was to remember to diff them.
Transforming the artifact you are already publishing has no such failure mode —
it is correct by construction, because the input *is* the template that trained
the model. The training variant stays canonical and versioned with the training
pipeline, where it belongs.

Anyone doing **continued training** from a published checkpoint has to put the
tags back. Whether they get told depends on their trainer: TRL >= 0.29 refuses
to start (`assistant_only_loss=True` with no assistant tokens in an example is a
`RuntimeError`), but `tokenizers.apply_chat_template` on its own returns an
all-zero mask with only a log line (measured: 3 of 12 assistant tokens masked
with the tags, 0 of 12 without). A hand-rolled training loop therefore gets no
warning worth the name. That belongs in the model card.

## Recommended launch (fp8, single L4)

```bash
CUDA_VISIBLE_DEVICES=<gpu> conda run --no-capture-output -n <your-vllm-env> \
    vllm serve <path to *_merged_unwrapped checkpoint> \
    --served-model-name qsql-qwen35-9b \
    --quantization fp8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.92
```

Verified on one L4 24 GB (~20 GB / 23 GB used, 7.3 GiB of that KV cache
= 224,411 tokens). No `--pipeline-parallel-size` /
`--tensor-parallel-size` needed — 9B in fp8 fits comfortably on a single
24 GB card. `CUDA_VISIBLE_DEVICES` is worth setting explicitly on any
multi-GPU host so vLLM does not land on a card someone else is using.

**Boot takes ~140 s** (of which ~59 s is CUDA-graph compilation).
That is not a hang — wait for `Application startup complete` rather
than assuming it wedged.

Endpoint: `http://localhost:8000/v1` (OpenAI-compatible). Verify with
`curl http://localhost:8000/v1/models` — should show
`{"id":"qsql-qwen35-9b", ...}`.

### Key flags explained

- **`--served-model-name qsql-qwen35-9b`** — critical: gives the
  `/v1/chat/completions` `model` field a short id instead of the full
  checkpoint path. The MCP server's `httpx` client sends this exact
  string (`VLLM_MODEL` env var, or the `mcp_server/vllm_client.py`
  default).
- **No `--enforce-eager`** — deliberately absent. Earlier revisions
  passed it because CUDA-graph capture was flaky on the *patched*
  class hierarchy; on stock 0.27.1 capture succeeds cleanly and is
  measurably faster. Measured on one L4, fp8, identical prompts:

  | | `--enforce-eager` | default (CUDA graphs) |
  |---|---|---|
  | decode, n=1 | 22.9 tok/s | **24.3 tok/s** (+6%) |
  | batch, n=8 | 154.8 tok/s | **178.4 tok/s** (+15%) |
  | boot | fast | ~140 s |

  Greedy (`temperature=0`) output is byte-identical either way, so
  this is throughput-only with no correctness cost. The n=8 column is
  the one that matters: self-consistency draws 4/8/16 candidates per
  round, so that is the real serving shape. Only re-add
  `--enforce-eager` if you hit a capture bug — and if you do, record
  the actual error here rather than restoring it as folklore.
- No `--override-generation-config` — the MCP server's self-consistency
  sampling always sends its own `temperature` per call, so server-side
  sampling defaults only matter for clients that send none (e.g. raw
  `curl`). Set it if you have such a client.

## Known-broken combos

Architecture (`Qwen3_5*`)-level facts rather than size-specific ones,
established on an earlier vllm while validating a multi-GPU setup. The
single-GPU fp8 launch above doesn't hit them (no TP/PP in play), but they
are worth knowing before reaching for quantization or parallelism. Not
re-tested on 0.27.1; treat as "assume still broken until someone checks":

| Combo | Failure |
|---|---|
| `bnb + TP ≥ 2` | `NotImplementedError: Shard id with multiple indices is not supported for BNB quantization with TP yet.` |
| `bnb + PP ≥ 2` | Loads cleanly but **generates garbage** (all `!`). Silent correctness failure — do NOT use. |
| `autoawq` | Rejects `qwen3_5_text` (`TypeError: qwen3_5_text isn't supported yet`). Library deprecated; `vllm-project/llm-compressor` is the eventual successor, not yet validated on this arch. |

## Other gotchas

- **KeyError under PP first-request**: if you hit `KeyError` in
  `_update_states` on the first request after boot (only relevant if
  you go back to multi-GPU PP), add `--no-async-scheduling`.
- **Max position embeddings**: 262144 (256K) natively on this
  architecture family. Cap `--max-model-len` based on your KV-cache
  budget — 32768 for the single-GPU launch above.
- **Tool calls are not supported by this fine-tune.** vLLM can be told to
  parse them (`--enable-auto-tool-choice --tool-call-parser qwen3_xml`),
  but the LoRA was trained on qSQL data only, with no tool-calling
  examples and a chat template that never renders tool definitions — so
  passing `tools` to `/v1/chat/completions` is silently ignored rather
  than erroring. The MCP server never passes `tools`.
- **Alarming-but-harmless boot noise**: transformers 5.15 prints two
  lines tagged `[ERROR]` during boot —
  ``[ERROR] `min_frames` is part of Qwen3VLVideoProcessorInitKwargs,
  but not documented`` and the same for `max_frames`. They come from
  transformers' own docstring checker for the Qwen3-VL *video*
  processor, have nothing to do with this text-only model, and do not
  affect serving. Ignore them. (They are also broad enough to trip a
  naive `grep -i error` boot-health check — match on
  `Application startup complete` or poll `/v1/models` instead.)
- **`--mamba-cache-mode`**: Qwen3.5 is a hybrid architecture
  (`layer_types` alternates `linear_attention` / `full_attention`), and
  `qwen3_5.py` raises `NotImplementedError` if this is set to `all`.
  The default is `none` and vllm auto-selects `align` when prefix
  caching is on, so you only hit this by explicitly asking for `all`.
  Noted because it looks dangerous and is not — saves re-deriving it.

## Serving on vllm < 0.27

Only relevant if something unrelated pins you below 0.27.0. The
text-only Qwen3.5 wiring is missing there and must be added by hand —
47 purely additive lines, no deletions, across two files in
`vllm/model_executor/models/`:

1. `registry.py` — add to the text-generation arch dict:
   ```python
   "Qwen3_5ForCausalLM": ("qwen3_5", "Qwen3_5ForCausalLM"),
   "Qwen3_5MoeForCausalLM": ("qwen3_5", "Qwen3_5MoeForCausalLM"),
   ```
2. `qwen3_5.py` — add `IsHybrid` to `Qwen3_5ForCausalLMBase`'s bases.
3. `qwen3_5.py` — add `get_mamba_state_dtype_from_config`,
   `get_mamba_state_shape_from_config` and `get_mamba_state_copy_func`
   as classmethods on `Qwen3_5ForCausalLMBase`. The multimodal
   `Qwen3_5ForConditionalGeneration` in the same file already has all
   three; copy them.

The simplest way to obtain the exact text is to diff v0.27.0's copies
of those two files against your version. Note that these edits are
erased by any `pip install -U vllm`, which is the main reason to just
upgrade instead.
