#!/usr/bin/env python3
r"""Call the ``kxqsql`` MCP server's ``generate_qsql`` tool directly, bypassing
Claude entirely (no LLM tokens spent on the orchestration side — only the
server's own vLLM calls).

Usage (HTTP server, the default -- ``--url`` defaults to the same
``http://localhost:9000/mcp`` that ``.mcp.json.example`` points at)::

    python scripts/qsql_query.py \\
        --instruction "Compute 5-minute OHLCV bars from tick data..." \\
        --schema trade='`time`sym`price`size!`p`s`f`j'

Usage (spawn a local stdio server instead of talking to one over HTTP)::

    QLINT_DIR=<dir containing qlint.q_> python scripts/qsql_query.py --stdio \\
        --instruction "..." --schema trade='...'

``--schema name=value`` may be repeated for multiple tables. Requires the
``mcp`` package -- see ``requirements-client.txt`` at the repo root
(``pip install -r requirements-client.txt``).

1-table example::
    python scripts/qsql_query.py \\
    --instruction "Compute 5-minute OHLCV bars from tick data. Table \`trade\` (time:p, sym:s, price:f, size:j). Result columns: bucket (5-min bucket start), sym, open (first price), high (max price), low (min price), close (last price), volume (sum size)." \\
    --schema 'trade=`time`sym`price`size!`p`s`f`j'

2-table example::
    Repeat ``--schema`` once per table. To left-join ``trade`` with a
    second table ``instrument`` (keyed by ``sym``), pass both schemas
    and describe the join in the instruction:

    python scripts/qsql_query.py \\
    --instruction "Left join instrument onto trade by sym to attach each trade's sector, then select time, sym, sector, price, size from the joined result." \\
    --schema 'trade=`time`sym`price`size!`p`s`f`j' \\
    --schema 'instrument=`sym`sector!`s`s'

More-options example (self-consistency knobs)::
    ``--max-rounds`` caps the round-cap ceiling for THIS call only (it can
    request fewer rounds than the server's ``MCP_QSQL_SC_ROUND_CAPS``,
    never more); ``--temperature`` overrides the per-call sampling
    temperature; ``--no-candidates`` trims the per-candidate debug list
    from the response (q_code / passed / rounds_used etc. stay at the
    top level regardless):

    python scripts/qsql_query.py \\
    --instruction "Compute 5-minute OHLCV bars from tick data. Table \`trade\` (time:p, sym:s, price:f, size:j). Result columns: bucket (5-min bucket start), sym, open (first price), high (max price), low (min price), close (last price), volume (sum size)." \\
    --schema 'trade=`time`sym`price`size!`p`s`f`j' \\
    --max-rounds 3 --temperature 0.8 --no-candidates

    ``table_schemas``-derived dummy-table sizing (seed count, rows per
    seed) is server-side startup config, not a per-call tool arg, so
    there's no ``--seeds`` / ``--nrows`` flag here. With ``--stdio`` the
    spawned server subprocess inherits your shell's env, so set
    ``MCP_QSQL_SC_SEEDS`` / ``MCP_QSQL_SC_NROWS`` before invoking this
    script if you want to test against a different dummy-table shape
    (see ``mcp_server/README.md`` § "Environment variables"):

    QLINT_DIR=<dir containing qlint.q_> MCP_QSQL_SC_SEEDS=42 MCP_QSQL_SC_NROWS=50 \\
    python scripts/qsql_query.py --stdio \\
    --instruction "..." --schema 'trade=`time`sym`price`size!`p`s`f`j'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://localhost:9000/mcp"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instruction", required=True, help="Natural-language qSQL task")
    p.add_argument(
        "--schema",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "Table schema entry, e.g. trade='`time`sym`price`size!`p`s`f`j'. "
            "Repeatable -- pass one --schema per table (see module docstring "
            "for a two-table left-join example)."
        ),
    )
    p.add_argument("--max-rounds", type=int, default=None,
                    help="Self-consistency round-cap ceiling (fewer than the "
                         "server's MCP_QSQL_SC_ROUND_CAPS is honored; more is clamped)")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--expected-result-schema", default=None)
    p.add_argument(
        "--no-candidates", action="store_true",
        help="Omit the per-candidate 'candidates' list from the response "
             "(raw completions + per-candidate gate results). Every field "
             "needed to judge whether to trust the answer (q_code, passed, "
             "final_issues, rounds_used, candidates_generated, "
             "winning_cluster_size, winning_cluster_fraction, tie_occurred) "
             "stays at the top level either way -- this only trims debug "
             "detail. Default: included.",
    )
    p.add_argument("--url", default=DEFAULT_URL, help="Streamable-HTTP MCP endpoint")
    p.add_argument(
        "--stdio", action="store_true", help="Spawn a local stdio server instead of --url"
    )
    return p.parse_args()


async def _call(args: argparse.Namespace) -> dict:
    table_schemas = dict(kv.split("=", 1) for kv in args.schema)
    arguments = {"instruction": args.instruction, "table_schemas": table_schemas}
    if args.max_rounds is not None:
        arguments["max_rounds"] = args.max_rounds
    if args.temperature is not None:
        arguments["temperature"] = args.temperature
    if args.expected_result_schema is not None:
        arguments["expected_result_schema"] = args.expected_result_schema
    if args.no_candidates:
        arguments["include_candidates"] = False

    if args.stdio:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_server.server", "--transport", "stdio"]
        )
        ctx = stdio_client(params)
    else:
        ctx = streamablehttp_client(args.url)

    async with ctx as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("generate_qsql", arguments)
            text = next((b.text for b in result.content if b.type == "text"), None)
            if text is None:
                raise RuntimeError(f"No text content in result: {result}")
            if result.isError:
                raise RuntimeError(f"generate_qsql tool error: {text}")
            return json.loads(text)


def main() -> int:
    args = _parse_args()
    payload = asyncio.run(_call(args))
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
