"""MCP server exposing the ``generate_qsql`` tool.

Run as: ``python -m mcp_server.server [--transport stdio|streamable-http]``

Default transport is ``stdio`` (per-session subprocess, the original model).
Pass ``--transport streamable-http`` to run as a long-running HTTP service
on ``0.0.0.0:9000`` (endpoint path ``/mcp``) — see CLAUDE.md for the
multi-tenant launch workflow.
"""

from __future__ import annotations

# MUST be first import so PYKX_THREADING=1 is set before any pykx import.
from mcp_server import loop as _loop  # noqa: F401

import argparse
import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_qsql_shared.qlint_policy import (
    QlintGate,
    QlintUnavailable,
    banner,
    missing_qlint_message,
)


logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mcp_server")


TOOL_DESCRIPTION = (
    "Generate q/kdb+ qSQL code using a fine-tuned q-specialist model served "
    "locally via vLLM. The server draws several independent candidates per "
    "call (self-consistency: growing rounds, e.g. 4 -> 8 -> 16) at a non-zero "
    "temperature, executes each against a dummy table built from the "
    "supplied `table_schemas` across multiple seeds, clusters candidates by "
    "cross-seed execution-equivalence, and returns the shortest-code "
    "candidate from the largest (winning) cluster. Each candidate is also "
    "gated on raw q `parse` and KX qlint C9 before being considered — the "
    "response's `lint_gate` field says which: `kx_qlint_c9` when the lint "
    "gate ran, `disabled` when this deployment has no usable `qlint.q_` and "
    "candidates were filtered by parse plus execution only.\n\n"
    "IMPORTANT LIMITATION: agreement across the winning cluster reduces but "
    "does NOT eliminate the risk of a semantically-wrong-but-consistent "
    "answer (e.g. the whole cluster making the same `max` vs `mmax` "
    "mistake). Do not treat `passed=True` as runtime-correct — always "
    "verify the returned `q_code` by executing it against a sample table "
    "before committing.\n\n"
    "INVOKE ONLY WHEN: (1) the user explicitly asks to use this tool / the "
    "specialist model / qwen, OR (2) generating a long batch of qSQL where "
    "offloading meaningfully saves primary-model tokens.\n\n"
    "DO NOT invoke for: routine qSQL that you can write yourself (especially "
    "when you have shell access to `q` and can verify by execution), IPC "
    "(handles, `hopen`), file I/O (`load`, `get`, `set`, `\\l`), system "
    "commands (`\\d`, `\\p`, `\\t`), or any non-q/kdb+ task.\n\n"
    "Pass `table_schemas` as {name: \"`col1`col2!`t1`t2\"} — the model was "
    "trained on that exact shape.\n\n"
    "Optional `expected_result_schema` (string) — when supplied, appended "
    "to qwen's user prompt as `Expected result schema: ...`. Set by "
    "the /qsql-coder skill when the user supplied a q reference it can "
    "derive the shape from; caller derives the shape externally and "
    "passes the resulting string in.\n\n"
    "Set `include_candidates=False` to omit the per-candidate `candidates` "
    "list from the response and save tokens on the calling side — every "
    "field needed to decide whether to trust the answer (`q_code`, "
    "`passed`, `lint_gate`, `final_issues`, `rounds_used`, "
    "`candidates_generated`, `winning_cluster_size`, "
    "`winning_cluster_fraction`, `tie_occurred`) "
    "stays at the top level regardless; `candidates` is debugging detail."
)


# host/port are consulted only by the streamable-http transport; ignored for
# stdio. Safe to set unconditionally so both modes work from the same object.
mcp = FastMCP("qsql-generator", host="0.0.0.0", port=9000)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool(description=TOOL_DESCRIPTION)
async def generate_qsql(
    instruction: str,
    table_schemas: dict[str, str],
    max_rounds: int = len(_loop.SC_ROUND_CAPS),
    temperature: float = _loop.SC_TEMPERATURE,
    expected_result_schema: str | None = None,
    include_candidates: bool = True,
) -> dict:
    """Generate qSQL via fine-tuned qwen using self-consistency clustering.

    ``table_schemas`` is required and must be non-empty — qwen was trained
    with schemas in context and the clustering step needs them to build
    dummy tables. Callers that lack a schema should ask the user (or
    draft + confirm) before calling this tool.

    ``max_rounds``: how many of the server's configured round caps
    (``MCP_QSQL_SC_ROUND_CAPS``) this call may use — ceiling model, a
    caller can request fewer (cheaper) but never more than the server
    allows.

    ``expected_result_schema`` is optional. When non-None, it is appended
    to qwen's user prompt as ``Expected result schema: <string>``. Set by
    /qsql-coder when the user supplied a q reference to derive the shape
    from (that skill calls such a reference "priority-1").

    When it is None, ``mcp_server.loop`` may fall back to the
    ``MCP_QSQL_INJECT_EXPECTED_SCHEMA`` env var — but ONLY if the server was
    started with ``MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA=1``, which is off by
    default and is meaningful under stdio only (see ``CLAUDE.md``
    § "RETRIEVE_EXPECTED_SCHEMA flag" for why it must stay off on a shared
    HTTP server). Deliberately not described in ``TOOL_DESCRIPTION``: a
    calling model cannot set env vars, and the response carries
    ``expected_result_schema_note`` to explain the shape at the point it
    first becomes visible to that caller.

    ``include_candidates``: whether the returned payload includes the
    per-candidate ``candidates`` list (raw completions, per-candidate
    gate results, dedup/cluster ids). Every field a caller needs to decide
    whether to trust the answer (``q_code``, ``passed``, ``lint_gate``,
    ``final_issues``, ``rounds_used``, ``candidates_generated``,
    ``winning_cluster_size``, ``winning_cluster_fraction``,
    ``tie_occurred``) is at the top level
    regardless of this flag — ``candidates`` is debugging/audit detail, not
    needed for that decision. Set ``False`` to save tokens on the calling
    side (e.g. the /qsql-coder skill, which never reads per-candidate
    detail). Defaults to ``True`` to keep the payload fully self-describing
    for ad-hoc / debugging callers.
    """
    log.info(
        "generate_qsql: instruction=%r tables=%s max_rounds=%d temp=%.2f "
        "expected_result_schema=%s include_candidates=%s",
        instruction[:80],
        list(table_schemas.keys()),
        max_rounds,
        temperature,
        bool(expected_result_schema),
        include_candidates,
    )
    result = await _loop.run_generate_loop(
        instruction=instruction,
        table_schemas=table_schemas,
        max_rounds=max_rounds,
        temperature=temperature,
        expected_result_schema=expected_result_schema,
    )
    log.info(
        "generate_qsql: passed=%s rounds_used=%d candidates_generated=%d "
        "winning_cluster_size=%d/%d",
        result["passed"],
        result["rounds_used"],
        result["candidates_generated"],
        result["winning_cluster_size"],
        result["candidates_generated"],
    )
    if not include_candidates:
        result.pop("candidates", None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_server.server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport. 'stdio' (default) for per-session subprocess; "
             "'streamable-http' for long-running HTTP service on 0.0.0.0:9000.",
    )
    args = parser.parse_args()

    async def _preload() -> QlintGate:
        gate = await _loop.ensure_qlint_loaded()
        await _loop.ensure_typegen_loaded()
        await _loop.ensure_evalcmp_loaded()
        return gate

    try:
        gate = asyncio.run(_preload())
    except QlintUnavailable as e:
        # A misconfiguration, not a crash: show the message and nothing else.
        # Every other startup failure keeps its traceback, which is why this
        # catches one specific type rather than Exception.
        log.error("\n%s", banner(str(e)))
        raise SystemExit(1) from None

    # A missing lint gate is a legitimate deployment (qlint.q_ is licensed
    # separately), but it must never be discovered by surprise: shout once
    # here, and mark it on every response via `lint_gate`.
    if not gate.enabled:
        log.warning(
            "\n%s",
            banner(missing_qlint_message(gate.reason or "qlint.q_ unavailable",
                                         gate.mode)),
        )

    log.info(
        "%s + typeGen + eval_compare loaded (round_caps=%s, temperature=%.2f, "
        "seeds=%s, nrows=%d); starting %s MCP server",
        "qlint.q_" if gate.enabled else "NO qlint.q_ (lint gate disabled)",
        _loop.SC_ROUND_CAPS, _loop.SC_TEMPERATURE, _loop.SC_SEEDS, _loop.SC_NROWS,
        args.transport,
    )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
