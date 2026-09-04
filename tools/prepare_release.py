#!/usr/bin/env python3
"""prepare_release.py — turn a merged checkpoint into a publishable one.

Four things have to happen to a freshly merged checkpoint before it can be
uploaded, and one of them is easy to forget, which is why this is a script and
not a list of commands in a runbook:

  1. inspect  — measure the checkpoint and assert the invariants a publishable
                checkpoint has to satisfy. Read-only; writes nothing.
  2. stage    — build the release directory: the small files, with the chat
                template's training tags stripped, plus the model card (draft
                prologue stripped) and the licence. No weights.
  3. reshard  — re-split the single large `model.safetensors` into shards of at
                most --max-shard-size, plus the index file. Expensive; opt-in.
  4. verify   — check the release directory the way a downloader receives it:
                shards against the source, template renders outside
                transformers, config + tokenizer load with an empty and offline
                HF cache, and that the card and licence are there with no
                placeholder left in them. `--deep` adds a per-tensor SHA-256
                compare of the shard bytes against the source bytes.

Run them in order, or run `all`. Stages 1 and 2 are cheap enough to re-run on
every candidate checkpoint while deciding which one to publish; stage 3 is the
one you only want to pay for once, on the winner; stage 4 is the one that says
whether stage 3 actually produced something loadable.

    ./tools/prepare_release.py inspect --checkpoint <dir>
    ./tools/prepare_release.py stage   --checkpoint <dir> --out <dir> \
                                       --card <card.md> --license <LICENSE>
    ./tools/prepare_release.py reshard --checkpoint <dir> --out <dir> --yes
    ./tools/prepare_release.py verify  --checkpoint <dir> --out <dir> [--deep]
    ./tools/prepare_release.py all     --checkpoint <dir> --out <dir> --yes

Design notes, so the choices here are not re-litigated later:

* **The source checkpoint is never written to.** Everything lands in --out.
  A merged checkpoint may well be the one your inference server is serving
  right now, and it may be the only copy of the training-time chat template.
  Copy-then-transform also keeps the result correct by construction: the input
  to the strip is the very template that trained the model, so there is no
  second copy of it to keep in step.

* **Resharding does not go through a model loader.** It copies tensor bytes
  from the source file into the shards, so it needs no torch, no accelerator,
  and RAM proportional to one tensor rather than to the whole model. More
  importantly it cannot silently rewrite `config.json`, re-cast a dtype, or
  drop a tensor a loader does not recognise: the only transformation is where
  the bytes live.

* **No default paths.** --checkpoint and --out are always explicit. The card
  and licence are passed the same way, so this script knows nothing about where
  a particular project keeps them.

* **The card's content stays hand-written; only the mechanical part is here.**
  What `stage` does to a card is strip a draft prologue and copy. That is in the
  script because it was previously an `awk` one-liner in a runbook, and a
  misspelt boundary makes `awk` emit an EMPTY file — which passed every check
  that existed. Deciding what the card says is not automatable, and none of it
  is attempted.

* **`verify` compares metadata; `verify --deep` compares content.** The default
  checks — key set, dtypes, shapes, byte totals, index `total_size`, and each
  shard's on-disk size against what its own header implies — catch truncation,
  mis-indexing and a dropped tensor, but they all pass if a tensor's bytes are
  replaced by different bytes of the same length. `--deep` closes that by
  hashing every tensor on both sides. It is opt-in only because it reads the
  weights twice — 57 s to 2 m 49 s for 18 GB on the same box, depending on how
  much of it the page cache still holds — not because it is ever the wrong
  thing to run: on the checkpoint you are actually publishing, run it.

Stdlib only, except that `stage` and `verify` use `jinja2` / `transformers`
for their strongest assertions when those are importable, and downgrade to a
WARN when they are not. Run from an env that has both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# --- what a checkpoint is made of -------------------------------------------

WEIGHTS_FILE = "model.safetensors"
TEMPLATE_FILE = "chat_template.jinja"

# Copied verbatim into the release directory (TEMPLATE_FILE is transformed, so
# it is handled separately). Absent entries are reported, not fatal: which of
# these a checkpoint carries depends on the transformers version that saved it
# — recent versions fold what used to live in `special_tokens_map.json` into
# `tokenizer_config.json`, for instance.
SMALL_FILES = [
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
    "preprocessor_config.json",
]

# The two training-only tags. They mark the assistant span so a trainer can
# compute assistant-only loss, and they render to zero characters — but
# `{% generation %}` is a transformers extension, not standard Jinja, so any
# renderer that does not go through transformers raises on it.
TRAIN_TAGS = ["{% generation %}", "{% endgeneration %}"]

# A wrapped (multimodal-prefixed) checkpoint has keys under this prefix. A
# text-only loader rejects them, so publishing one is a mistake worth catching
# before the upload rather than after.
WRAPPED_PREFIX = "model.language_model."

# A model card may be drafted with a prologue of notes that must not be
# published. The prologue ends at the first line starting with this string;
# everything below it is the card. Matching a prefix rather than the whole line
# lets the marker carry a trailing reminder to whoever edits the draft.
DRAFT_BOUNDARY = "# END OF DRAFT-ONLY SECTION"

# The canonical name for a --facts report. It carries the checkpoint name
# because screening several candidates writes several reports, and a shared
# name would silently overwrite one with another.
FACTS_NAME_FMT = "release_facts_{checkpoint}.json"

DEFAULT_MAX_SHARD_BYTES = 5 * 1000**3  # 5 GB, the convention on the Hub

# Markers that must not survive into a published README.md or NOTICE. Kept as an
# explicit list rather than a general "angle brackets" rule, because prose
# legitimately contains <...>; add a token here when you introduce one. The
# first two catch the model-card drafting conventions, the rest are the specific
# blanks the release artifacts carry.
PLACEHOLDER_TOKENS = (
    "<<",                          # <<DECISION n ...>> in the card draft
    "<DECISION",
    "<org>",                       # the HF account that owns the repo
    "OWNER/REPO",                  # GitHub org/repo in issue-template URLs
    "[name of copyright owner]",   # see the LICENSE exemption at the call site
    "[yyyy]",
    "<COPYRIGHT_HOLDER>",
    "<SECURITY_CONTACT>",
    "<PR_POLICY>",
)


# --- output helpers ---------------------------------------------------------

_PROBLEMS: list[str] = []


def info(msg: str) -> None:
    print(f"    {msg}")


def head(msg: str) -> None:
    print(f"\n[{msg}]")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def fail(msg: str) -> None:
    """Record a hard problem. Collected so one run reports every failure."""
    _PROBLEMS.append(msg)
    print(f"  FAIL  {msg}")


def human(n: int) -> str:
    return f"{n / 1000**3:.2f} GB" if n >= 1000**3 else f"{n / 1000**2:.1f} MB"


# --- reading a safetensors file without loading it --------------------------


def read_st_header(path: Path) -> tuple[dict, int]:
    """Return (header, data_start). Reads the header only, not the tensors.

    Layout: 8-byte little-endian header length, that many bytes of JSON, then
    the tensor data. Every tensor's `data_offsets` are relative to data_start.
    """
    with path.open("rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n))
    return header, 8 + n


def tensors_of(header: dict) -> dict:
    """The header minus its optional `__metadata__` entry."""
    return {k: v for k, v in header.items() if k != "__metadata__"}


def sha256_tensors(path: Path, chunk: int = 32 * 1024 * 1024) -> dict[str, str]:
    """SHA-256 of each tensor's bytes in one safetensors file.

    Keyed by tensor name, so the result is comparable across files that hold
    the same tensors at different offsets — which is exactly what resharding
    produces. Reads in file order so the pass is sequential, and holds at most
    `chunk` bytes at a time, so a 5 GB shard costs the same memory as a 5 MB
    one.
    """
    header, data_start = read_st_header(path)
    tensors = tensors_of(header)
    digests: dict[str, str] = {}
    with path.open("rb") as f:
        for k, t in sorted(tensors.items(),
                           key=lambda kv: kv[1]["data_offsets"][0]):
            a, b = t["data_offsets"]
            f.seek(data_start + a)
            h = hashlib.sha256()
            remaining = b - a
            while remaining:
                block = f.read(min(remaining, chunk))
                if not block:
                    raise EOFError(f"unexpected EOF reading {k} from {path.name}")
                h.update(block)
                remaining -= len(block)
            digests[k] = h.hexdigest()
    return digests


def tokenizer_len(tok_json: dict) -> int:
    """`len(tokenizer)` without importing transformers.

    The vocab, plus any added token whose id is not already in it. Validated
    against transformers on a real checkpoint; if it ever disagrees, trust
    transformers and fix this.
    """
    vocab = tok_json.get("model", {}).get("vocab", {})
    n = len(vocab)
    known = set(vocab.values())
    for added in tok_json.get("added_tokens", []):
        if added.get("id") not in known:
            n += 1
            known.add(added.get("id"))
    return n


# --- stage 1: inspect -------------------------------------------------------


def inspect(ckpt: Path, max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES) -> dict:
    """Measure the checkpoint and check the publishable invariants."""
    facts: dict = {"checkpoint_name": ckpt.name}

    head("files")
    for name in [WEIGHTS_FILE, TEMPLATE_FILE, "config.json", "tokenizer.json"]:
        p = ckpt / name
        if p.is_file():
            ok(f"{name} ({human(p.stat().st_size)})")
        else:
            fail(f"{name} missing")
    for name in SMALL_FILES:
        if name in ("config.json", "tokenizer.json"):
            continue
        p = ckpt / name
        info(f"{'present' if p.is_file() else 'absent '}  {name}")
    if _PROBLEMS:
        return facts

    head("weights")
    header, data_start = read_st_header(ckpt / WEIGHTS_FILE)
    tensors = tensors_of(header)
    total = sum(t["data_offsets"][1] - t["data_offsets"][0] for t in tensors.values())
    dtypes = sorted({t["dtype"] for t in tensors.values()})
    facts |= {
        "tensor_count": len(tensors),
        "tensor_bytes": total,
        "dtypes": dtypes,
        "file_bytes": (ckpt / WEIGHTS_FILE).stat().st_size,
        "st_metadata": header.get("__metadata__"),
    }
    ok(f"{len(tensors)} tensors, {human(total)} of tensor data, dtype(s) {dtypes}")

    # A tensor whose data_offsets duplicate another's is a shared/aliased
    # tensor. Splitting shards would silently write two copies, and some
    # loaders then complain about unexpected keys, so surface it.
    seen: dict[tuple, str] = {}
    shared = []
    for name, t in tensors.items():
        key = tuple(t["data_offsets"])
        if key in seen:
            shared.append((seen[key], name))
        else:
            seen[key] = name
    if shared:
        warn(f"{len(shared)} aliased tensor pair(s), e.g. {shared[0]} — "
             "resharding would duplicate the bytes")
    else:
        ok("no aliased tensors")
    facts["aliased_pairs"] = len(shared)

    wrapped = [k for k in tensors if k.startswith(WRAPPED_PREFIX)]
    if wrapped:
        fail(f"{len(wrapped)} keys under '{WRAPPED_PREFIX}' — this is the "
             "wrapped variant; publish the unwrapped one")
    else:
        ok(f"no '{WRAPPED_PREFIX}' keys (unwrapped variant)")

    head("config")
    cfg = json.loads((ckpt / "config.json").read_text())
    facts |= {
        "architectures": cfg.get("architectures"),
        "model_type": cfg.get("model_type"),
        "dtype": cfg.get("dtype") or cfg.get("torch_dtype"),
        "num_hidden_layers": cfg.get("num_hidden_layers"),
        "hidden_size": cfg.get("hidden_size"),
        "max_position_embeddings": cfg.get("max_position_embeddings"),
        "vocab_size": cfg.get("vocab_size"),
        "transformers_version": cfg.get("transformers_version"),
    }
    ok(f"{cfg.get('architectures')} / {cfg.get('model_type')} / "
       f"{facts['dtype']} / {cfg.get('num_hidden_layers')} layers")
    info(f"max_position_embeddings = {cfg.get('max_position_embeddings')}")
    if lt := cfg.get("layer_types"):
        counts: dict[str, int] = {}
        for t in lt:
            counts[t] = counts.get(t, 0) + 1
        info(f"layer_types = {counts}")
        facts["layer_types"] = counts

    # A config that declares extra prediction layers whose weights are not in
    # the file. Harmless for a loader that ignores the key, a hard error for one
    # that tries to build the layer — so it is a model-card note at minimum.
    n_mtp = cfg.get("mtp_num_hidden_layers") or 0
    if n_mtp:
        present = [k for k in tensors if "mtp" in k.lower()]
        if present:
            ok(f"mtp_num_hidden_layers={n_mtp}, {len(present)} mtp tensor(s) present")
        else:
            warn(f"config declares mtp_num_hidden_layers={n_mtp} but the weights "
                 "contain no mtp tensors — decide whether to document or drop the key")
        facts["mtp_declared"], facts["mtp_tensors"] = n_mtp, len(present)

    head("tokenizer")
    tok = json.loads((ckpt / "tokenizer.json").read_text())
    n_tok = tokenizer_len(tok)
    facts["tokenizer_len"] = n_tok
    vs = cfg.get("vocab_size")
    if isinstance(vs, int):
        pad = vs - n_tok
        if pad == 0:
            ok(f"vocab_size == len(tokenizer) == {vs}")
        elif pad > 0:
            # The safe direction: no id the tokenizer can emit is out of range
            # for the embedding matrix. Some tools still warn about it.
            ok(f"vocab_size {vs} > len(tokenizer) {n_tok} — {pad}-slot embedding "
               "pad (safe direction; note it in the model card)")
        else:
            fail(f"vocab_size {vs} < len(tokenizer) {n_tok} — the tokenizer can "
                 "emit ids the embedding matrix does not have")
        facts["embedding_pad"] = pad

    tok_cfg = json.loads((ckpt / "tokenizer_config.json").read_text())
    facts |= {"eos_token": tok_cfg.get("eos_token"), "pad_token": tok_cfg.get("pad_token"),
              "model_max_length": tok_cfg.get("model_max_length")}
    info(f"eos={tok_cfg.get('eos_token')!r} pad={tok_cfg.get('pad_token')!r} "
         f"model_max_length={tok_cfg.get('model_max_length')}")

    head("chat template")
    tpl = (ckpt / TEMPLATE_FILE).read_text()
    found = [t for t in TRAIN_TAGS if t in tpl]
    facts["train_tags_in_source"] = found
    if found:
        # Expected for a freshly merged checkpoint; `stage` removes them.
        ok(f"training tags present, will be stripped: {found}")
    else:
        warn("no training tags found — either already stripped, or the tags are "
             "spelled differently in this checkpoint (check before publishing: "
             "a trainer patch may use whitespace-control variants)")
    head("shard plan")
    plan = plan_shards(tensors, max_shard_bytes)
    facts["shard_plan"] = [{"file": f, "tensors": len(ks), "bytes": b}
                           for f, ks, b in plan]
    for fname, keys, nbytes in plan:
        info(f"{fname}  {len(keys):>4} tensors  {human(nbytes)}")
    info(f"(at --max-shard-size {human(max_shard_bytes)}; "
         f"reshard needs ~{human(total)} free in --out)")

    return facts


# --- stage 3 planning (shared with inspect, so the plan can be previewed) ---


def plan_shards(tensors: dict, max_bytes: int) -> list[tuple[str, list[str], int]]:
    """Greedy pack, preserving the source order, never splitting a tensor.

    Source order matters: it keeps each shard's byte ranges contiguous in the
    source file, so writing a shard is a sequential read.
    """
    groups: list[list[str]] = []
    sizes: list[int] = []
    cur: list[str] = []
    cur_bytes = 0
    for name, t in tensors.items():
        n = t["data_offsets"][1] - t["data_offsets"][0]
        if cur and cur_bytes + n > max_bytes:
            groups.append(cur)
            sizes.append(cur_bytes)
            cur, cur_bytes = [], 0
        cur.append(name)
        cur_bytes += n
    if cur:
        groups.append(cur)
        sizes.append(cur_bytes)

    total = len(groups)
    return [(f"model-{i + 1:05d}-of-{total:05d}.safetensors", g, s)
            for i, (g, s) in enumerate(zip(groups, sizes))]


# --- write guard ------------------------------------------------------------


def assert_safe_outdir(out: Path) -> None:
    """Refuse to write model artefacts into a tracked working tree.

    gitignore only stops `git add`; it does not stop tens of GB from landing
    inside a repo. So the check is: if --out is inside a git working tree, git
    must already ignore it. Outside any repo, anything goes.
    """
    probe = out if out.exists() else out.parent
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=probe,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return  # no git available: nothing to protect against
    if inside.returncode != 0:
        return  # not in a working tree
    root = inside.stdout.strip()
    ignored = subprocess.run(["git", "check-ignore", "-q", str(out)],
                             cwd=probe, capture_output=True)
    if ignored.returncode != 0:
        sys.exit(
            f"refusing to write to {out}\n"
            f"  it is inside the git working tree at {root} and git does not "
            f"ignore it.\n"
            f"  Point --out outside any repository, or add a .gitignore rule "
            f"first.")


def resolve_facts_path(raw: Path, ckpt: Path, out: Path | None) -> Path:
    """Where --facts writes, and where it must never write.

    Accepts a file path or a directory. A directory (or a path with a trailing
    separator) gets FACTS_NAME_FMT, which is per-checkpoint and is the shape
    this repo's .gitignore already covers.

    The second job is the one worth having a function for. --out is not a file
    list, it is the directory an upload sends wholesale (`hf
    upload-large-folder` takes a folder), so anything left in there is
    published. The facts report is internal: it records the absolute path and
    name of the checkpoint it measured, and its contents are already distilled
    into the card. Nothing downstream would catch it either — a leak scan runs
    on the git-push and export paths, never on an upload to a model hub. So a
    --facts path inside --out is refused here rather than warned about.
    """
    p = raw.expanduser()
    # A path naming the report itself ends `.json`; anything else is taken as a
    # directory to write the canonical name into. The rule is a suffix and not a
    # trailing separator because argparse's `type=Path` has already dropped the
    # separator by the time this sees it — the first run of this function wrote a
    # FILE called ~/hf-release for exactly that reason. `is_dir()` first, so an
    # existing directory wins however it is spelt.
    if p.is_dir() or p.suffix.lower() != ".json":
        p = p / FACTS_NAME_FMT.format(checkpoint=ckpt.name)
    p = p.resolve()
    if out is not None and p.is_relative_to(out):
        sys.exit(
            f"refusing to write the facts report to {p}\n"
            f"  it is inside --out ({out}), which is uploaded as a whole "
            f"directory, and the report is internal: it names the checkpoint "
            f"it measured.\n"
            f"  Point --facts at a path outside --out.")
    return p


# --- stage 2: stage ---------------------------------------------------------


def _mtp_tensor_names(tensors: dict) -> list[str]:
    return [k for k in tensors if "mtp" in k.lower()]


def _drop_orphan_mtp(ckpt: Path, out: Path) -> None:
    """Remove a multi-token-prediction declaration the weights cannot honour.

    A merged checkpoint can inherit `mtp_num_hidden_layers` from its base config
    without inheriting any MTP tensors. Nothing notices during ordinary serving
    — vLLM only consults the key when speculative decoding / MTP is switched on,
    and transformers does not model the field at all — which is exactly what
    makes it worth removing rather than documenting: it is a declaration that
    stays inert until somebody enables the feature it advertises, and then sends
    the loader looking for layers that were never published.

    Deleting is effective, not cosmetic: the call sites that decide anything
    (`vllm/transformers_utils/model_arch_config_convertor.py`,
    `vllm/config/speculative.py`) read it with a default of 0 / None, so an
    absent key means "no MTP" rather than "assume one layer".

    Conditional on purpose. If a checkpoint really does carry MTP weights the
    declaration is true and is left alone.
    """
    head("config: declarations with no weights behind them")
    cfg_path = out / "config.json"
    if not cfg_path.is_file():
        warn("config.json not in the release dir — skipped")
        return
    cfg = json.loads(cfg_path.read_text())
    declared = cfg.get("mtp_num_hidden_layers") or 0
    if not declared:
        info("no mtp_num_hidden_layers declaration — nothing to drop")
        return
    present = _mtp_tensor_names(tensors_of(read_st_header(ckpt / WEIGHTS_FILE)[0]))
    if present:
        ok(f"mtp_num_hidden_layers={declared} is backed by {len(present)} "
           "mtp tensor(s) — kept")
        return
    cfg.pop("mtp_num_hidden_layers")
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    ok(f"dropped mtp_num_hidden_layers={declared} — the weights contain no mtp "
       "tensors, so the declaration could only mislead a loader that acts on it")


def stage(ckpt: Path, out: Path, card: Path | None = None,
          licence: Path | None = None) -> None:
    """Copy the small files into --out, stripping what must not be published.

    Two of those files do not come from the checkpoint at all: the model card
    and the licence. They are staged here, from paths given on the command
    line, because they are small files that need a mechanical transformation —
    exactly what this stage is — and because folding them in means `all`
    produces a directory that is ready to upload, so `verify` can treat their
    absence as a failure rather than a warning.

    What stays hand-authored is the card's *content*. This only strips a draft
    prologue and copies; the decisions in the card are not derivable from the
    checkpoint and are not attempted here.
    """
    assert_safe_outdir(out)
    out.mkdir(parents=True, exist_ok=True)

    head("copy small files")
    copied = 0
    for name in SMALL_FILES:
        src = ckpt / name
        if not src.is_file():
            continue
        shutil.copy2(src, out / name)
        ok(f"{name} ({human(src.stat().st_size)})")
        copied += 1
    info(f"{copied} file(s) copied")

    _drop_orphan_mtp(ckpt, out)

    head("strip training tags from the chat template")
    tpl = (ckpt / TEMPLATE_FILE).read_text()
    stripped = tpl
    for tag in TRAIN_TAGS:
        stripped = stripped.replace(tag, "")
    (out / TEMPLATE_FILE).write_text(stripped)

    # Post-condition, not a hope: no tag of that shape survives. Matching on
    # the closing `generation %}` catches whitespace-control spellings
    # (`{%- generation %}`) that the literal replace above would have missed.
    residue = [ln for ln in stripped.splitlines() if "generation %}" in ln]
    if residue:
        fail(f"{len(residue)} line(s) still contain 'generation %}}' — the tags "
             f"in this checkpoint are spelled differently: {residue[0].strip()!r}")
    else:
        ok("no 'generation %}' remains")

    # The whole point of stripping is that a non-transformers renderer works.
    # Assert exactly that, when we can.
    try:
        import jinja2
    except ImportError:
        warn("jinja2 not importable — skipped the plain-Jinja render check, "
             "which is the assertion that actually matters here")
    else:
        try:
            rendered = jinja2.Template(
                stripped, trim_blocks=False, lstrip_blocks=False
            ).render(
                messages=[{"role": "system", "content": "S"},
                          {"role": "user", "content": "U"},
                          {"role": "assistant", "content": "A"}],
                add_generation_prompt=False)
        except jinja2.TemplateError as e:
            fail(f"plain jinja2 still cannot render the template: {e}")
        else:
            ok(f"plain jinja2 renders it: {rendered!r}")

    # And the rendered text must be unchanged by the strip, or we have altered
    # the prompt the model was trained on. Only checkable with transformers,
    # which knows the `generation` extension; skip quietly otherwise.
    _compare_rendered(tpl, stripped)

    _stage_docs(out, card, licence)


def _strip_draft_prologue(text: str, source_name: str) -> str | None:
    """The card below its draft prologue, or None if the source is unusable.

    The hazard this exists to remove: doing it with `awk` outside the script,
    which produces an EMPTY file when the boundary is misspelt — and an empty
    README.md satisfies both of the checks `verify` used to make (the file is
    present, and it contains no placeholder), so the mistake reached the upload
    silently. Every branch below is therefore a hard failure, not a warning.
    """
    lines = text.splitlines(keepends=True)
    marks = [i for i, ln in enumerate(lines) if ln.startswith(DRAFT_BOUNDARY)]

    if len(marks) > 1:
        fail(f"{len(marks)} {DRAFT_BOUNDARY!r} lines in the card source "
             f"(lines {', '.join(str(i + 1) for i in marks)}) — which one ends "
             f"the prologue is ambiguous, so nothing is published")
        return None
    if not marks:
        # A card with no prologue is legitimate; a file NAMED .draft with no
        # boundary is a misspelt marker, which is the failure worth catching.
        if ".draft" in source_name:
            fail(f"{source_name} is named as a draft but contains no line "
                 f"starting {DRAFT_BOUNDARY!r} — a misspelt boundary would "
                 f"otherwise publish the notes-to-ourselves prologue")
            return None
        info("no draft prologue to strip — the card is copied verbatim")
        return text

    body = "".join(lines[marks[0] + 1:])
    if not body.strip():
        fail(f"the card is empty below {DRAFT_BOUNDARY!r} (line "
             f"{marks[0] + 1} of {len(lines)}) — nothing to publish")
        return None
    if DRAFT_BOUNDARY in body:
        fail("the boundary marker survives in the stripped card")
        return None
    ok(f"stripped {marks[0] + 1} line(s) of draft prologue, "
       f"{len(body.splitlines())} line(s) of card remain")
    return body


def _check_frontmatter(card: str) -> bool:
    """Assert the Hub will actually see the card's metadata. False = do not ship.

    Returns True for a good block and for an absent one (a card without
    frontmatter is a degraded repo, not a broken file), False only for the
    shapes that are unambiguously a mistake.

    A hub reads `license:` / `base_model:` only from a literal `---` block at
    the very top of README.md. Get that wrong and the failure is quiet: the
    repo publishes with no licence tag and no link to its base model, and the
    YAML renders as body text. Worth checking here because no later stage can
    — `verify` looks for unresolved placeholders, which a mis-placed
    frontmatter block has none of.
    """
    lines = card.lstrip("\n").splitlines()
    if not lines or lines[0].strip() != "---":
        warn("the card does not open with a `---` frontmatter block — a model "
             "hub takes `license:` and `base_model:` from one, and without it "
             "the repo shows no licence and is not linked to its base model")
        return True
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"),
               None)
    if end is None:
        fail("the card's frontmatter block is never closed by a `---` line, so "
             "the whole card would be parsed as metadata")
        return False
    block = lines[1:end]
    if any(ln.startswith("```") for ln in block):
        fail("the card's frontmatter is inside a fenced code block — only a "
             "literal `---` block at the very top of README.md is parsed, so "
             "as staged this publishes with no license tag and no base_model "
             "link, and renders the YAML as a code sample")
        return False
    keys = [ln.split(":", 1)[0].strip() for ln in block
            if ln[:1] not in ("", " ", "-", "#") and ":" in ln]
    if not keys:
        fail("the card opens with an empty `---` block rather than "
             "frontmatter — no licence and no base-model link would be shown")
        return False
    shown = ", ".join(keys[:4]) + (", ..." if len(keys) > 4 else "")
    ok(f"frontmatter present, {len(keys)} key(s): {shown}")
    return True


def _stage_docs(out: Path, card: Path | None, licence: Path | None) -> None:
    """Stage the two files a model repo needs that the checkpoint cannot supply.

    Both flags are optional so that the cheap screening use — run `inspect` and
    `stage` against every candidate checkpoint to decide which to publish —
    does not have to name a card that is not written yet. Absent means skipped
    and said so, which is what `verify` then reports as not-ready.
    """
    head("stage the model card and licence")
    if licence is None:
        info("--license not given — skipped (verify will report it missing)")
    elif not licence.is_file():
        fail(f"--license {licence} is not a file")
    else:
        shutil.copy2(licence, out / "LICENSE")
        ok(f"LICENSE <- {licence} ({human(licence.stat().st_size)})")

    if card is None:
        info("--card not given — skipped (verify will report it missing)")
        return
    if not card.is_file():
        fail(f"--card {card} is not a file")
        return
    body = _strip_draft_prologue(card.read_text(), card.name)
    if body is None:
        return
    if not _check_frontmatter(body):
        # Deliberately do not write it. `verify` re-checks the frontmatter, but
        # leaving a card here that this stage has just rejected invites a
        # `verify` run to be read as a second opinion on it.
        info("README.md not written — fix the card and re-run `stage`")
        return
    (out / "README.md").write_text(body.lstrip("\n"))
    ok(f"README.md <- {card}")


def _compare_rendered(before: str, after: str) -> None:
    """Assert the strip did not change the rendered prompt (needs transformers)."""
    try:
        from transformers.utils.chat_template_utils import (  # type: ignore
            _compile_jinja_template,
        )
    except Exception:
        warn("transformers' Jinja environment not importable — skipped the "
             "before/after render comparison (the strip is documented as "
             "render-neutral; this would re-verify it for this checkpoint)")
        return
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
            {"role": "assistant", "content": "A"}]
    try:
        outs = [_compile_jinja_template(t).render(messages=msgs,
                                                  add_generation_prompt=True)
                for t in (before, after)]
    except Exception as e:
        warn(f"render comparison could not run: {e}")
        return
    if outs[0] == outs[1]:
        ok(f"rendered prompt byte-identical before/after: {outs[1]!r}")
    else:
        fail("the strip CHANGED the rendered prompt — do not publish this")


# --- stage 3: reshard -------------------------------------------------------


def reshard(ckpt: Path, out: Path, max_bytes: int, confirmed: bool) -> None:
    """Re-split model.safetensors into shards under --out, plus the index."""
    assert_safe_outdir(out)
    src = ckpt / WEIGHTS_FILE
    header, data_start = read_st_header(src)
    tensors = tensors_of(header)
    total = sum(t["data_offsets"][1] - t["data_offsets"][0] for t in tensors.values())
    plan = plan_shards(tensors, max_bytes)

    head("reshard")
    info(f"{len(tensors)} tensors, {human(total)} -> {len(plan)} shard(s)")
    free = shutil.disk_usage(out if out.exists() else out.parent).free
    info(f"free space at destination: {human(free)}")
    if free < total * 1.05:
        sys.exit(f"not enough free space: need ~{human(int(total * 1.05))}, "
                 f"have {human(free)}")
    if not confirmed:
        sys.exit("refusing to write ~%s without --yes (this is the expensive, "
                 "one-shot stage; run it on the checkpoint you have decided to "
                 "publish)" % human(total))

    out.mkdir(parents=True, exist_ok=True)
    weight_map: dict[str, str] = {}
    with src.open("rb") as fin:
        for fname, keys, nbytes in plan:
            # Rebuild the header with offsets relative to this shard.
            shard_header: dict = {}
            cursor = 0
            for k in keys:
                t = tensors[k]
                n = t["data_offsets"][1] - t["data_offsets"][0]
                shard_header[k] = {"dtype": t["dtype"], "shape": t["shape"],
                                   "data_offsets": [cursor, cursor + n]}
                cursor += n
                weight_map[k] = fname
            blob = json.dumps(shard_header, separators=(",", ":")).encode()
            blob += b" " * (-len(blob) % 8)  # 8-byte align the data section
            dest = out / fname
            with dest.open("wb") as fo:
                fo.write(struct.pack("<Q", len(blob)))
                fo.write(blob)
                for k in keys:
                    a, b = tensors[k]["data_offsets"]
                    fin.seek(data_start + a)
                    remaining = b - a
                    while remaining:
                        chunk = fin.read(min(remaining, 32 * 1024 * 1024))
                        if not chunk:
                            fail(f"unexpected EOF reading {k}")
                            return
                        fo.write(chunk)
                        remaining -= len(chunk)
            ok(f"{fname}  {len(keys):>4} tensors  {human(dest.stat().st_size)}")

    index = {"metadata": {"total_size": total}, "weight_map": weight_map}
    (out / f"{WEIGHTS_FILE}.index.json").write_text(json.dumps(index, indent=2))
    ok(f"{WEIGHTS_FILE}.index.json  ({len(weight_map)} entries)")

    head("verify shards")
    seen: dict[str, dict] = {}
    written = 0
    for fname, _, _ in plan:
        h, _ = read_st_header(out / fname)
        for k, t in tensors_of(h).items():
            seen[k] = t
            written += t["data_offsets"][1] - t["data_offsets"][0]
    if set(seen) != set(tensors):
        fail(f"key set changed: {len(seen)} written vs {len(tensors)} source")
    elif any(seen[k]["shape"] != tensors[k]["shape"]
             or seen[k]["dtype"] != tensors[k]["dtype"] for k in tensors):
        fail("a tensor's dtype or shape changed")
    elif written != total:
        fail(f"byte total changed: {written} written vs {total} source")
    else:
        ok(f"{len(seen)} tensors, {human(written)}, dtypes and shapes unchanged")


# --- stage 4: verify --------------------------------------------------------

# Run in a subprocess, because HF_HOME and the offline switches are read when
# transformers is imported — setting them after the fact does nothing. Loading
# config + tokenizer is enough: it is the step that would reach out to the Hub
# for a `base_model` reference the checkpoint failed to inline, and it needs no
# accelerator and no 18 GB of RAM.
_OFFLINE_PROBE = r"""
import json, os, sys
d = sys.argv[1]
res = {}
try:
    from transformers import AutoConfig, AutoTokenizer
    cfg = AutoConfig.from_pretrained(d)
    tok = AutoTokenizer.from_pretrained(d)
    res["arch"] = getattr(cfg, "architectures", None)
    res["vocab_size"] = getattr(cfg, "vocab_size", None)
    res["tokenizer_len"] = len(tok)
    res["tokenizer_class"] = type(tok).__name__
except Exception as e:
    res["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(res))
"""


def _deep_compare(ckpt: Path, out: Path, shards: list[str]) -> None:
    """Per-tensor SHA-256, the release shards against the source file.

    The one check here that looks at tensor *content* rather than at the header
    describing it. Hash the shards first: they are the artifact under suspicion,
    and if one is unreadable there is no point paying for the source pass.
    """
    head("verify --deep: per-tensor SHA-256 against the source")
    info(f"reading the weights twice ({len(shards)} shard(s), then the source) "
         "— this is the slow check")
    try:
        got: dict[str, str] = {}
        for name in shards:
            got |= sha256_tensors(out / name)
        want = sha256_tensors(ckpt / WEIGHTS_FILE)
    except (EOFError, OSError) as e:
        fail(f"could not hash the weights: {type(e).__name__}: {e}")
        return

    shared = set(got) & set(want)
    if missing := sorted(set(want) - set(got)):
        fail(f"{len(missing)} source tensor(s) absent from the shards, "
             f"e.g. {missing[0]}")
    if extra := sorted(set(got) - set(want)):
        fail(f"{len(extra)} shard tensor(s) not in the source, e.g. {extra[0]}")
    if bad := sorted(k for k in shared if got[k] != want[k]):
        fail(f"{len(bad)} of {len(shared)} tensor(s) differ in CONTENT despite "
             f"matching metadata, e.g. {bad[0]}: shard {got[bad[0]][:16]}… vs "
             f"source {want[bad[0]][:16]}…")
    else:
        header, _ = read_st_header(ckpt / WEIGHTS_FILE)
        nbytes = sum(t["data_offsets"][1] - t["data_offsets"][0]
                     for t in tensors_of(header).values())
        ok(f"{len(shared)}/{len(want)} tensors byte-identical, "
           f"{nbytes} bytes ({human(nbytes)}) verified")


def verify(ckpt: Path, out: Path, deep: bool = False) -> None:
    """Check the release directory as a downloader would receive it.

    Everything here is a post-condition of the earlier stages, which is exactly
    why it lives in the same script: a separate checklist is a checklist that
    gets skipped. `deep` adds the content-level compare — see the design note
    at the top for why that one is opt-in rather than absent.
    """
    head("verify: shards against the source checkpoint")
    index = out / f"{WEIGHTS_FILE}.index.json"
    if not index.is_file():
        info(f"no {index.name} — reshard has not run against this directory, "
             "so there is nothing to compare (staged files only)")
        if deep:
            info("--deep also has nothing to hash without shards")
    else:
        src_tensors = tensors_of(read_st_header(ckpt / WEIGHTS_FILE)[0])
        idx = json.loads(index.read_text())
        wmap = idx.get("weight_map", {})
        shards = sorted(set(wmap.values()))
        seen: dict[str, dict] = {}
        written = 0
        missing = [s for s in shards if not (out / s).is_file()]
        if missing:
            fail(f"index references {len(missing)} missing shard(s): {missing[:3]}")
        else:
            # Header offsets are a *claim*; a truncated file still declares the
            # same ones, so compare them against the bytes actually on disk.
            # An interrupted write or a full disk produces exactly this.
            short = []
            for s in shards:
                header, data_start = read_st_header(out / s)
                shard_tensors = tensors_of(header)
                for k, t in shard_tensors.items():
                    seen[k] = t
                    written += t["data_offsets"][1] - t["data_offsets"][0]
                expect = data_start + max(
                    (t["data_offsets"][1] for t in shard_tensors.values()),
                    default=0)
                actual = (out / s).stat().st_size
                if actual != expect:
                    short.append((s, actual, expect))
            if short:
                s, a, e = short[0]
                fail(f"{len(short)} shard(s) are not the size their header "
                     f"implies, e.g. {s}: {a} bytes on disk vs {e} expected "
                     f"({'truncated' if a < e else 'trailing data'})")
            else:
                ok(f"all {len(shards)} shard file(s) are exactly the size their "
                   "headers imply")
            src_total = sum(t["data_offsets"][1] - t["data_offsets"][0]
                            for t in src_tensors.values())
            if set(seen) != set(src_tensors):
                only_src = sorted(set(src_tensors) - set(seen))[:3]
                only_out = sorted(set(seen) - set(src_tensors))[:3]
                fail(f"key set differs — {len(seen)} in shards vs "
                     f"{len(src_tensors)} in source; source-only {only_src}, "
                     f"shard-only {only_out}")
            elif bad := [k for k in src_tensors
                         if seen[k]["shape"] != src_tensors[k]["shape"]
                         or seen[k]["dtype"] != src_tensors[k]["dtype"]]:
                fail(f"{len(bad)} tensor(s) changed dtype or shape, e.g. {bad[0]}")
            elif written != src_total:
                fail(f"byte total differs: {written} in shards vs {src_total} "
                     "in source")
            else:
                ok(f"{len(shards)} shard(s), {len(seen)} tensors, {human(written)} "
                   "— key set, dtypes, shapes and byte total all match the source")
            declared = idx.get("metadata", {}).get("total_size")
            if declared == src_total:
                ok(f"index total_size matches ({declared})")
            else:
                fail(f"index total_size {declared} != {src_total}")
            if set(wmap) != set(seen):
                fail("weight_map does not name exactly the tensors in the shards")
            if deep:
                _deep_compare(ckpt, out, shards)

    head("verify: config declarations")
    cfg_path = out / "config.json"
    if not cfg_path.is_file():
        fail("config.json missing from the release dir")
    else:
        cfg = json.loads(cfg_path.read_text())
        declared = cfg.get("mtp_num_hidden_layers") or 0
        if not declared:
            ok("no orphan mtp_num_hidden_layers declaration")
        else:
            src_mtp = _mtp_tensor_names(
                tensors_of(read_st_header(ckpt / WEIGHTS_FILE)[0]))
            if src_mtp:
                ok(f"mtp_num_hidden_layers={declared}, backed by "
                   f"{len(src_mtp)} tensor(s)")
            else:
                fail(f"config declares mtp_num_hidden_layers={declared} with no "
                     "mtp tensors in the weights — stage should have dropped it")

    head("verify: chat template")
    tpl_path = out / TEMPLATE_FILE
    if not tpl_path.is_file():
        fail(f"{TEMPLATE_FILE} missing from the release dir")
    else:
        tpl = tpl_path.read_text()
        if "generation %}" in tpl:
            fail("the release template still contains a 'generation %}' tag")
        else:
            ok("no training tags")
        try:
            import jinja2
        except ImportError:
            warn("jinja2 not importable — cannot confirm a non-transformers "
                 "renderer accepts the template, which is the point of the strip")
        else:
            try:
                jinja2.Template(tpl).render(
                    messages=[{"role": "user", "content": "U"}],
                    add_generation_prompt=True)
            except jinja2.TemplateError as e:
                fail(f"plain jinja2 cannot render the release template: {e}")
            else:
                ok("plain jinja2 renders it")

    head("verify: offline load with an empty HF cache")
    cache = out.parent / f".{out.name}.hfcache-probe"
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True)
    env = dict(os.environ,
               HF_HOME=str(cache),
               HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1")
    try:
        proc = subprocess.run([sys.executable, "-c", _OFFLINE_PROBE, str(out)],
                              capture_output=True, text=True, env=env, timeout=600)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:
        warn(f"offline probe could not run ({type(e).__name__}: {e}) — run this "
             "stage from an env with transformers installed")
    else:
        if err := payload.get("error"):
            if "No module named" in err:
                warn(f"transformers not importable in this interpreter ({err}) — "
                     "offline load NOT verified")
            else:
                fail(f"offline load failed: {err}")
        else:
            ok(f"config + tokenizer load offline: {payload.get('arch')}, "
               f"vocab_size={payload.get('vocab_size')}, "
               f"len(tokenizer)={payload.get('tokenizer_len')} "
               f"({payload.get('tokenizer_class')})")
            # A file in the cache means something was fetched or resolved
            # remotely, i.e. the checkpoint is not self-contained.
            fetched = [p for p in cache.rglob("*") if p.is_file()]
            if fetched:
                fail(f"{len(fetched)} file(s) written to the empty HF cache — "
                     f"the checkpoint is not self-contained, e.g. "
                     f"{fetched[0].relative_to(cache)}")
            else:
                ok("0 files written to the empty HF cache — self-contained")
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    head("verify: upload readiness")
    # Not defects in the weights, but a repo missing either of these is not
    # publishable, and both are staged by `stage --card / --license`. This was a
    # WARN while they were added by hand after `all` had already run; now that
    # one command can produce them, an absence is a real not-ready.
    for name, what, flag in [("README.md", "the model card", "--card"),
                             ("LICENSE", "the weights licence", "--license")]:
        if (out / name).is_file():
            ok(f"{name} present ({what})")
        else:
            fail(f"{name} absent — {what}. Re-run `stage` with {flag}")
    # Re-checked here and not only in `stage`, because a card can be dropped in
    # by hand and this is the gate that runs last before an upload.
    if (out / "README.md").is_file():
        _check_frontmatter((out / "README.md").read_text())
    # Deliberately NOT applied to LICENSE: the canonical Apache-2.0 appendix
    # contains "Copyright [yyyy] [name of copyright owner]" as part of its
    # instructions for applying the licence, so that text is correct there and a
    # placeholder anywhere else is not.
    for name in ("README.md", "NOTICE"):
        f = out / name
        if not f.is_file():
            continue
        left = [ln.strip() for ln in f.read_text().splitlines()
                if any(tok in ln for tok in PLACEHOLDER_TOKENS)]
        if left:
            fail(f"{len(left)} unresolved placeholder(s) in {name}, "
                 f"first: {left[0][:70]!r}")
        else:
            ok(f"no unresolved placeholders in {name}")


# --- cli -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage",
                    choices=["inspect", "stage", "reshard", "verify", "all"])
    ap.add_argument("--checkpoint", required=True, type=Path,
                    help="merged, unwrapped checkpoint directory (read-only)")
    ap.add_argument("--out", type=Path,
                    help="release directory to create (required except for inspect)")
    ap.add_argument("--max-shard-size", type=int, default=DEFAULT_MAX_SHARD_BYTES,
                    help=f"bytes per shard (default {DEFAULT_MAX_SHARD_BYTES})")
    ap.add_argument("--facts", type=Path,
                    help="write the inspect measurements here as JSON. A path "
                         f"ending .json is the file; anything else is a "
                         f"directory and gets {FACTS_NAME_FMT!r}. Must not be "
                         "inside --out, which is uploaded wholesale")
    ap.add_argument("--card", type=Path,
                    help="stage/all: model-card source. Copied to "
                         "<out>/README.md with any draft prologue (everything "
                         f"up to and including a {DRAFT_BOUNDARY!r} line) "
                         "removed")
    ap.add_argument("--license", type=Path, dest="licence",
                    help="stage/all: licence file, copied verbatim to "
                         "<out>/LICENSE")
    ap.add_argument("--yes", action="store_true",
                    help="confirm the expensive reshard write")
    ap.add_argument("--deep", action="store_true",
                    help="verify: also compare every tensor's bytes by SHA-256 "
                         "(reads the weights twice, minutes for a 18 GB "
                         "checkpoint; run it on the one you are publishing)")
    args = ap.parse_args()

    ckpt: Path = args.checkpoint.expanduser().resolve()
    if not ckpt.is_dir():
        return f"not a directory: {ckpt}"  # type: ignore[return-value]
    if args.stage != "inspect" and not args.out:
        ap.error("--out is required for this stage")
    out = args.out.expanduser().resolve() if args.out else None
    if out and out == ckpt:
        ap.error("--out must differ from --checkpoint: this script never writes "
                 "to the source checkpoint")

    print(f"checkpoint : {ckpt}")
    if out:
        print(f"out        : {out}")

    facts = {}
    if args.stage in ("inspect", "all"):
        facts = inspect(ckpt, args.max_shard_size)
        if args.facts:
            dest = resolve_facts_path(args.facts, ckpt, out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(facts, indent=2))
            ok(f"facts written to {dest}")
    if args.stage in ("stage", "all") and not _PROBLEMS:
        stage(ckpt, out,  # type: ignore[arg-type]
              card=args.card.expanduser() if args.card else None,
              licence=args.licence.expanduser() if args.licence else None)
    if args.stage in ("reshard", "all") and not _PROBLEMS:
        reshard(ckpt, out, args.max_shard_size, args.yes)  # type: ignore[arg-type]
    if args.stage in ("verify", "all") and not _PROBLEMS:
        verify(ckpt, out, deep=args.deep)  # type: ignore[arg-type]

    print()
    if _PROBLEMS:
        print(f"NOT READY — {len(_PROBLEMS)} problem(s):")
        for p in _PROBLEMS:
            print(f"  - {p}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
