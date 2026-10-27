"""Core qwen -> self-consistency clustering loop.

Pure async function importable from a smoke test (no MCP protocol concepts).
Serializes pykx calls via a single-worker executor + ``asyncio.Lock``.

Replaces the old corrective-retry loop (ask qwen to fix its own mistakes
across sequential attempts) with self-consistency: draw several
*independent* candidates at a non-zero temperature in growing rounds,
cluster them by cross-seed execution-equivalence
(``mcp_server.self_consistency``), and return the shortest-code candidate
from the largest cluster. ``mcp_server/README.md`` § "Self-consistency
clustering" has the design rationale and the gate-by-gate walkthrough.
"""

from __future__ import annotations

# MUST come before any direct ``import pykx`` — sets PYKX_THREADING=1.
import mcp_qsql_shared.qlint_common  # noqa: F401

import asyncio
import concurrent.futures
import logging
import math
import os
from typing import Any, Callable

log = logging.getLogger(__name__)

from mcp_qsql_shared.constants import SYSTEM_PROMPT
from mcp_qsql_shared.extract import extract_qsql
from mcp_qsql_shared.io import build_user_prompt
from mcp_qsql_shared.qlint_common import _parse_safe, qlint_gate, resolve_qlint_gate
from mcp_qsql_shared.qlint_policy import QLINT_DISABLED_NOTE, QlintGate
from mcp_qsql_shared.qlint_kx import check_c9_qlint

from mcp_server import vllm_client
from mcp_server import qexec
from mcp_server.self_consistency import (
    Candidate,
    ClusterState,
    UniqueCandidate,
    live_bindings,
    parse_round_caps,
)


# --------------------------------------------------------------------------- #
# Self-consistency configuration — read once at process/session startup.
# --------------------------------------------------------------------------- #
# Deployer-fixed ceiling: a caller's `max_rounds` argument can only request
# FEWER rounds than this (cheaper), never more. Long-running HTTP server:
# fixed for the process's lifetime. stdio: fixed per spawned session
# (inherits the driver's env).
_SC_ROUND_CAPS_ENV = "MCP_QSQL_SC_ROUND_CAPS"
_SC_TEMPERATURE_ENV = "MCP_QSQL_SC_TEMPERATURE"
_DEFAULT_ROUND_CAPS_SPEC = "4,8,16"
_DEFAULT_TEMPERATURE = 0.8


def _load_round_caps() -> tuple[int, ...]:
    raw = os.environ.get(_SC_ROUND_CAPS_ENV, _DEFAULT_ROUND_CAPS_SPEC).strip()
    try:
        return parse_round_caps(raw)
    except ValueError as e:
        log.warning(
            "%s=%r invalid (%s) — falling back to default %r",
            _SC_ROUND_CAPS_ENV, raw, e, _DEFAULT_ROUND_CAPS_SPEC,
        )
        return parse_round_caps(_DEFAULT_ROUND_CAPS_SPEC)


def _load_temperature() -> float:
    raw = os.environ.get(_SC_TEMPERATURE_ENV, "").strip()
    if not raw:
        return _DEFAULT_TEMPERATURE
    try:
        temp = float(raw)
        # ``float()`` accepts "nan" / "inf", which would sail past the range
        # check below and only fail later as an unserializable JSON body on
        # the vLLM request. Reject them here, where the message names the var.
        if not math.isfinite(temp):
            raise ValueError("must be finite")
        # Negative is meaningless to every sampler; 0.0 is allowed on purpose
        # (greedy), even though it makes self-consistency degenerate — every
        # candidate comes back identical and the winning cluster is trivially
        # unanimous. That is a legitimate debugging configuration, so it is
        # not rejected here; MCP_RUNTIME_EXEC=0 is the knob for skipping the
        # clustering machinery outright.
        if temp < 0.0:
            raise ValueError("must be non-negative")
        return temp
    except ValueError as e:
        log.warning(
            "%s=%r invalid (%s) — falling back to default %r",
            _SC_TEMPERATURE_ENV, raw, e, _DEFAULT_TEMPERATURE,
        )
        return _DEFAULT_TEMPERATURE


SC_ROUND_CAPS: tuple[int, ...] = _load_round_caps()
SC_TEMPERATURE: float = _load_temperature()

# Dummy-table sizing for the runtime-exec gate + self-consistency clustering.
# Same startup-config model as round caps / temperature above: fixed for the
# process's lifetime (HTTP) or per spawned session (stdio, inherits the
# driver's env). No per-call ceiling — unlike round caps, seed count / row
# count aren't cost-scaling knobs a single caller should tune per request.
#
# Cross-seed clustering needs >1 seed so that "two candidates happen to agree
# on one dummy dataset by coincidence" doesn't get counted as real execution-
# equivalence — candidates must agree on EVERY seed tested. Setting
# MCP_QSQL_SC_SEEDS to a single seed weakens that guarantee; it's supported
# for debugging/latency tradeoffs, not recommended for production.
_SC_SEEDS_ENV = "MCP_QSQL_SC_SEEDS"
_SC_NROWS_ENV = "MCP_QSQL_SC_NROWS"


def _load_seeds() -> tuple[int, ...]:
    raw = os.environ.get(_SC_SEEDS_ENV, "").strip()
    if not raw:
        return qexec.DEFAULT_SC_SEEDS
    try:
        seeds = tuple(int(s.strip()) for s in raw.split(","))
        if not seeds:
            raise ValueError("empty seed list")
        return seeds
    except ValueError as e:
        log.warning(
            "%s=%r invalid (%s) — falling back to default %r",
            _SC_SEEDS_ENV, raw, e, qexec.DEFAULT_SC_SEEDS,
        )
        return qexec.DEFAULT_SC_SEEDS


def _load_nrows() -> int:
    raw = os.environ.get(_SC_NROWS_ENV, "").strip()
    if not raw:
        return qexec.DEFAULT_NROWS
    try:
        nrows = int(raw)
        if nrows <= 0:
            raise ValueError("must be positive")
        return nrows
    except ValueError as e:
        log.warning(
            "%s=%r invalid (%s) — falling back to default %r",
            _SC_NROWS_ENV, raw, e, qexec.DEFAULT_NROWS,
        )
        return qexec.DEFAULT_NROWS


SC_SEEDS: tuple[int, ...] = _load_seeds()
SC_NROWS: int = _load_nrows()


# Optional out-of-band channel for ``expected_result_schema``. When the caller
# (e.g. an Opus session driven by `claude -p`) does NOT pass the arg explicitly
# but this env var has a non-empty value, the env var content is used as the
# shape hint and forwarded to qwen via ``build_user_prompt``. Used by external
# batch drivers to inject shape into qwen WITHOUT letting the calling model see
# the shape in its own prompt.
# Priority: caller arg > env var > None.
#
# IMPORTANT: this fallback ONLY makes sense under the stdio transport, where
# each MCP session spawns a fresh server subprocess that inherits the
# driver's per-case env. Under streamable-http the server is long-running and
# its env is frozen at startup — the env var can't carry per-request data,
# so this fallback is effectively useless and the gate (below) should stay
# off on the production HTTP server.
_EXPECTED_SCHEMA_ENV_VAR = "MCP_QSQL_INJECT_EXPECTED_SCHEMA"

# Gate for the env-var fallback channel above, controlled by an env var so
# the same code can run on the production HTTP server (env unset → fallback
# dead → multi-tenant safe) AND on a per-session stdio server spawned by a
# batch driver (env=1 → fallback active → driver injects per-case shape).
# The explicit ``expected_result_schema`` argument on the MCP tool is
# always honoured regardless of this gate — caller-supplied per-request
# shape is safe in both transports.
_RETRIEVE_FLAG_ENV = "MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA"
RETRIEVE_EXPECTED_SCHEMA = os.environ.get(_RETRIEVE_FLAG_ENV, "0") == "1"

# Self-documenting note echoed alongside ``expected_result_schema_used`` so
# the caller (typically post-MCP Opus deciding case A/B/C/D / writing a fix)
# understands the semantic of the shape WITHOUT pre-MCP context — keeps Opus's
# pre-MCP prompt free of shape information (per the external batch-driver
# architecture that uses this channel).
_EXPECTED_SCHEMA_USED_NOTE = (
    "This is the canonical output shape requirement (column names + type "
    "chars) supplied as a hint to qwen during MCP generation. Use it as a "
    "reference when entering case C / case D rewrite — your fix's result "
    "should match this shape. The shape was NOT in your earlier prompt; "
    "this echo is the first time you see it."
)


_PYKX_LOCK = asyncio.Lock()
_PYKX_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="pykx"
)


async def _run_pykx(fn: Callable, *args: Any) -> Any:
    """Serialize all pykx calls — embedded q is single-threaded."""
    async with _PYKX_LOCK:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_PYKX_EXEC, fn, *args)


async def ensure_qlint_loaded() -> QlintGate:
    """Resolve the C9 gate once at server startup and return the decision.

    Fail-fasts only under ``MCP_QSQL_QLINT=required``; under the default
    ``auto`` a missing qlint.q_ yields a disabled gate whose ``reason`` the
    caller is expected to warn about (``mcp_server.server`` prints a banner).
    """
    return await _run_pykx(resolve_qlint_gate)


def lint_gate_status() -> str:
    """``"kx_qlint_c9"`` or ``"disabled"`` — reported on every tool response."""
    return qlint_gate().status


async def ensure_typegen_loaded() -> None:
    """Load typeGen / parseSchema0 / genTable0 into the embedded q at startup."""
    await _run_pykx(qexec._ensure_typegen)


async def ensure_evalcmp_loaded() -> None:
    """Load ``.eval.cmp`` / ``.eval.descVal`` into the embedded q at startup."""
    await _run_pykx(qexec._ensure_evalcmp)


def _candidate_summary(c: Candidate, state: ClusterState, cluster_roots: dict[int, int]) -> dict:
    uniq = state.uniques[c.uniq_idx]
    return {
        "index": c.orig_idx,
        "raw_completion": c.raw,
        "extracted": c.code,
        "parse_ok": c.parse_ok,
        "parse_err": c.parse_err,
        "lint_issues": c.lint_issues,
        "exec_ran": uniq.parse_lint_ok,
        "exec_passed": uniq.valid,
        "exec_err": uniq.exec_err,
        "degenerate": uniq.degenerate_desc,
        # dedup_group_id: candidates sharing this id have byte-identical
        # extracted code (pre-execution dedup, an efficiency detail).
        # cluster_id: candidates sharing this id executed to equal results
        # on every seed tested (the semantically meaningful grouping) —
        # None if this candidate never executed / was invalid. Two
        # different dedup_group_id values CAN share the same cluster_id
        # (e.g. a stray comment or renamed column makes the text differ
        # but not the result) — cluster_id is the one that answers "does
        # this candidate agree with the winning answer".
        "dedup_group_id": uniq.uniq_idx,
        "cluster_id": cluster_roots.get(uniq.uniq_idx),
    }


def _final_issues_for(candidate: Candidate, uniq: UniqueCandidate) -> list[str]:
    """Issues explaining why one specific candidate is not usable — used both
    for the passing-attempt case (empty) and the no-valid-candidates fallback."""
    if not candidate.parse_ok:
        # Both, in that order: the parse error says the code is unusable, the
        # lint issues (when qlint had something to say) explain it in terms a
        # caller can act on. Returning only the parse error used to discard the
        # more useful half.
        return [f"q parse error: {candidate.parse_err}"] + list(candidate.lint_issues)
    if candidate.lint_issues:
        return list(candidate.lint_issues)
    if uniq.degenerate_desc:
        return [f"degenerate result: {uniq.degenerate_desc}"]
    if uniq.exec_err:
        return [f"q runtime error against dummy data: {uniq.exec_err}"]
    return []


def _build_messages(instruction: str, table_schemas: dict[str, str],
                     expected_result_schema: str | None) -> list[dict]:
    user0 = build_user_prompt(
        {"instruction": instruction, "metadata": {"table_schemas": table_schemas}},
        output_schema=expected_result_schema,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user0},
    ]


async def _single_shot_no_exec(
    messages: list[dict], temperature: float, schema_names: set[str], skip_reason: str,
) -> dict:
    """Parse+lint-only path — used when runtime exec is unavailable
    (``MCP_RUNTIME_EXEC=0``, or the case's ``table_schemas`` can't actually
    build dummy tables). Clustering needs execution to compare candidates,
    so with exec off there's nothing to cluster: generate exactly one
    candidate instead of wasting the round-cap budget on candidates that
    can never be compared.
    """
    raws = await vllm_client.chat(messages, temperature=temperature, n=1)
    raw = raws[0]
    code = extract_qsql(raw)
    parse_ok, parse_err = await _run_pykx(_parse_safe, code)
    # Lint unconditionally — see the note in ``self_consistency.add_round``:
    # a parse failure still fails the candidate, but qlint's message is the one
    # worth handing back.
    lint = await _run_pykx(check_c9_qlint, code, schema_names, not parse_ok)
    lint_pass = bool(lint["pass"])
    lint_issues = list(lint["issues"])

    passed = parse_ok and lint_pass
    final_issues: list[str] = []
    if not parse_ok:
        final_issues.append(f"q parse error: {parse_err}")
    final_issues.extend(lint_issues)

    return {
        "q_code": code,
        "passed": passed,
        "lint_gate": lint_gate_status(),
        "request_mode": vllm_client.request_mode(),
        "rounds_used": 1,
        "candidates_generated": 1,
        "winning_candidate_index": 0 if passed else None,
        "winning_cluster_size": 1 if passed else 0,
        "winning_cluster_fraction": 1.0 if passed else 0.0,
        "n_clusters": 1 if passed else 0,
        "tie_occurred": False,
        "final_issues": final_issues,
        "candidates": [{
            "index": 0,
            "raw_completion": raw,
            "extracted": code,
            "parse_ok": parse_ok,
            "parse_err": None if parse_ok else str(parse_err),
            "lint_issues": lint_issues,
            "exec_ran": False,
            "exec_passed": True,
            "exec_err": None,
            "degenerate": None,
            "dedup_group_id": 0,
            "cluster_id": 0 if passed else None,
            "exec_skipped_reason": skip_reason,
        }],
    }


async def run_generate_loop(
    instruction: str,
    table_schemas: dict[str, str],
    max_rounds: int = len(SC_ROUND_CAPS),
    temperature: float = SC_TEMPERATURE,
    expected_result_schema: str | None = None,
) -> dict:
    """Drive the qwen self-consistency generation + clustering pipeline.

    ``table_schemas`` must be non-empty: qwen was trained with schemas in
    context and the clustering step needs them to build dummy tables.
    Raises ``ValueError`` otherwise.

    ``max_rounds``: how many of the server's configured round caps
    (``MCP_QSQL_SC_ROUND_CAPS``, e.g. ``4,8,16``) this call is allowed to
    use. Ceiling model — a caller can request fewer rounds (cheaper) but
    never more than the server allows; clamped via ``min()``.

    ``temperature``: sampling temperature for candidate generation. Self-
    consistency needs genuine diversity across the ``n`` samples in a
    round, so this should stay well above 0 — the default
    (``MCP_QSQL_SC_TEMPERATURE``, 0.8) is tuned for that; a caller passing
    something near 0 will silently collapse most rounds to a single
    (n_clusters==1) cluster.

    ``expected_result_schema``: optional shape hint string (e.g.
    ``"table: time(t), sym(s), price(f)"``). When supplied, appended to
    the user prompt as ``Expected result schema: ...``. Caller
    (typically the /qsql-coder skill) is responsible for
    deriving the shape from a user-supplied q reference.

    Fallback: if ``expected_result_schema`` is None AND the server was
    started with ``MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA=1``, a non-empty
    ``MCP_QSQL_INJECT_EXPECTED_SCHEMA`` env var is used instead. That lets
    a driver that spawns this server over stdio supply a shape per session
    without putting it in the calling model's prompt; the gate is off by
    default and must stay off on a shared HTTP server (``CLAUDE.md``
    § "RETRIEVE_EXPECTED_SCHEMA flag").

    Returns a structured payload — see ``mcp_server/README.md``
    § "The response payload" for the full field list. On exhaustion (no
    candidate in any round executed cleanly), ``passed=False`` and the
    first-generated candidate's ``q_code`` / issues are returned for
    caller fallback.
    """
    if not table_schemas:
        raise ValueError(
            "table_schemas is required and must be non-empty — qwen was "
            "trained with schemas in context. Ask the user for a schema "
            "(or draft one and have them confirm) before calling this loop."
        )
    round_caps = SC_ROUND_CAPS[: max(1, min(int(max_rounds), len(SC_ROUND_CAPS)))]

    if expected_result_schema is None and RETRIEVE_EXPECTED_SCHEMA:
        env_val = os.environ.get(_EXPECTED_SCHEMA_ENV_VAR, "").strip()
        log.info("env-var read: %s=%r", _EXPECTED_SCHEMA_ENV_VAR, env_val)
        if env_val:
            expected_result_schema = env_val

    messages = _build_messages(instruction, table_schemas, expected_result_schema)
    schema_names = set(table_schemas.keys())

    runtime_exec_enabled = os.environ.get("MCP_RUNTIME_EXEC", "1") != "0"
    skip_reason = None if runtime_exec_enabled else "MCP_RUNTIME_EXEC=0"
    if runtime_exec_enabled:
        skip_reason = await _run_pykx(qexec.preflight_check, table_schemas, SC_NROWS)
        runtime_exec_enabled = skip_reason is None

    if not runtime_exec_enabled:
        result = await _single_shot_no_exec(messages, temperature, schema_names, skip_reason)
    else:
        result = await _run_self_consistency(messages, table_schemas, schema_names,
                                              round_caps, temperature)

    result["expected_result_schema_used"] = expected_result_schema
    result["expected_result_schema_note"] = (
        _EXPECTED_SCHEMA_USED_NOTE if expected_result_schema else None
    )
    return result


def _cleanup_globals(before: set[str], schema_names: set[str]) -> None:
    """``cleanup_new_globals`` with other calls' live results protected."""
    qexec.cleanup_new_globals(before, schema_names | live_bindings())


async def _run_self_consistency(
    messages: list[dict],
    table_schemas: dict[str, str],
    schema_names: set[str],
    round_caps: tuple[int, ...],
    temperature: float,
) -> dict:
    state = ClusterState(
        table_schemas=table_schemas,
        schema_names=schema_names,
        seeds=SC_SEEDS,
        nrows=SC_NROWS,
        timeout_s=qexec.DEFAULT_TIMEOUT_S,
    )
    before_globals = await _run_pykx(qexec.snapshot_globals)

    rounds_used = 0
    winning = None
    try:
        prev_cap = 0
        for round_idx, cap in enumerate(round_caps):
            n_new = cap - prev_cap
            raws = await vllm_client.chat(messages, temperature=temperature, n=n_new)
            await _run_pykx(state.add_round, raws)
            rounds_used = round_idx + 1
            prev_cap = cap

            winning = state.winning_cluster()
            is_last = round_idx == len(round_caps) - 1
            if state.meets_threshold(cap, winning.weight, is_last):
                break

        cluster_roots = state.cluster_roots()

        if winning and winning.members:
            rep = state.pick_shortest(winning.members)
            result = {
                "q_code": rep.text,
                "passed": True,
                "winning_candidate_index": rep.orig_idxs[0],
                "winning_cluster_id": winning.root,
                "final_issues": [],
            }
        else:
            first = state.candidates[0]
            first_uniq = state.uniques[first.uniq_idx]
            result = {
                "q_code": first.code,
                "passed": False,
                "winning_candidate_index": None,
                "winning_cluster_id": None,
                "final_issues": _final_issues_for(first, first_uniq),
            }

        result.update({
            "lint_gate": lint_gate_status(),
            "request_mode": vllm_client.request_mode(),
            "rounds_used": rounds_used,
            "candidates_generated": state.n_generated,
            "winning_cluster_size": winning.weight if winning else 0,
            "winning_cluster_fraction": (
                winning.weight / state.n_generated
                if winning and state.n_generated else 0.0
            ),
            "n_clusters": winning.n_clusters if winning else 0,
            "tie_occurred": winning.tie_occurred if winning else False,
            "candidates": [_candidate_summary(c, state, cluster_roots) for c in state.candidates],
        })
        return result
    finally:
        await _run_pykx(state.cleanup)
        # `live_bindings()` is read on the pykx thread, after this call's own
        # bindings have just been dropped, so it holds exactly the captured
        # results of OTHER in-flight calls. Without it, one call's cleanup
        # deletes another's results mid-flight (the snapshot diff cannot tell
        # them from a leaked helper global) — see the `_CALL_SEQ` note in
        # `mcp_server.self_consistency`.
        await _run_pykx(_cleanup_globals, before_globals, set(table_schemas.keys()))
