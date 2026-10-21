"""Tier 4 — live-model smoke test for ``mcp_server.loop.run_generate_loop``.

Runs the scenarios that need a real generation against the live vLLM
endpoint (does NOT go through the MCP stdio protocol). Prints a PASS/FAIL
line per scenario; exits non-zero on any failure.

This is a smoke test in the operational sense — "is vLLM up, is the served
checkpoint still producing usable qSQL" — not a code regression suite. Every
scenario here samples the model at a non-zero temperature, so a failure can
mean an unlucky draw rather than a bug. Do not gate CI on it.

The deterministic half of this file (the scenarios that stubbed out
``vllm_client.chat`` with fixed completions) now lives in
``tests/test_pipeline_gates.py`` as tier-2 pytest tests: same gates, same
embedded q, no model. Run those with ``QLINT_DIR=<dir> pytest -m needs_q``.
See ``tests/README.md`` for the tier split.

Usage::

    QLINT_DIR=<dir containing qlint.q_> conda run -n <your-env> \\
        python -m mcp_server.smoke_test
"""

from __future__ import annotations

import asyncio
import sys

from mcp_server import loop, vllm_client


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _ok(msg: str) -> None:
    print(_color("PASS", "32") + " " + msg)


def _fail(msg: str) -> None:
    print(_color("FAIL", "31") + " " + msg)


async def scenario_happy_path() -> bool:
    result = await loop.run_generate_loop(
        instruction="Select the average price per sym from the trade table.",
        table_schemas={"trade": "`time`sym`price`size!`t`s`f`j"},
        max_rounds=1,
    )
    winner = next(
        (c for c in result["candidates"] if c["exec_passed"]), None
    )
    if (
        result["passed"]
        and result["candidates_generated"] >= 1
        and result["rounds_used"] >= 1
        and winner is not None
    ):
        _ok(
            f"happy_path: passed, {result['candidates_generated']} candidate(s), "
            f"{result['rounds_used']} round(s), winning_cluster="
            f"{result['winning_cluster_size']}/{result['candidates_generated']}"
        )
        print(f"     q_code: {result['q_code']}")
        return True
    _fail(
        f"happy_path: passed={result['passed']} "
        f"candidates_generated={result['candidates_generated']} "
        f"winning_cluster_size={result['winning_cluster_size']} "
        f"issues={result['final_issues']}"
    )
    print(f"     q_code: {result['q_code']!r}")
    return False


async def scenario_schema_aware_lint() -> bool:
    result = await loop.run_generate_loop(
        instruction="From the alloc table, sum allocQty grouped by allocAcct.",
        table_schemas={"alloc": "`tradeId`allocAcct`allocQty!`j`s`j"},
        max_rounds=1,
    )
    if not result["passed"]:
        _fail(f"schema_aware_lint: passed=False issues={result['final_issues']}")
        return False
    flagged = any(
        "DECLARED_AFTER_USE" in issue
        for c in result["candidates"] for issue in c["lint_issues"]
    )
    if flagged:
        _fail(f"schema_aware_lint: DECLARED_AFTER_USE not forgiven; issues={result['final_issues']}")
        return False
    _ok("schema_aware_lint: alloc accepted as declared table")
    print(f"     q_code: {result['q_code']}")
    return True


async def scenario_runtime_exec_disabled() -> bool:
    """``MCP_RUNTIME_EXEC=0`` should short-circuit to a single parse+lint-only
    candidate rather than spending the round-cap generation budget on
    candidates that can never be clustered."""
    import os
    original = os.environ.get("MCP_RUNTIME_EXEC")
    os.environ["MCP_RUNTIME_EXEC"] = "0"
    try:
        result = await loop.run_generate_loop(
            instruction="Select the average price per sym from the trade table.",
            table_schemas={"trade": "`time`sym`price`size!`t`s`f`j"},
        )
    finally:
        if original is None:
            os.environ.pop("MCP_RUNTIME_EXEC", None)
        else:
            os.environ["MCP_RUNTIME_EXEC"] = original

    c0 = result["candidates"][0]
    ok = (
        result["candidates_generated"] == 1
        and c0["exec_ran"] is False
        and c0.get("exec_skipped_reason") == "MCP_RUNTIME_EXEC=0"
    )
    if ok:
        _ok(f"runtime_exec_disabled: candidates_generated=1, passed={result['passed']}")
        return True
    _fail(f"runtime_exec_disabled: candidates_generated={result['candidates_generated']} "
          f"exec_ran={c0.get('exec_ran')} skipped_reason={c0.get('exec_skipped_reason')!r}")
    return False


async def main() -> int:
    gate = await loop.ensure_qlint_loaded()
    await loop.ensure_typegen_loaded()
    await loop.ensure_evalcmp_loaded()
    # Mirror mcp_server.server: never claim qlint loaded when the gate is off.
    if not gate.enabled:
        print(f"WARNING: lint gate disabled ({gate.reason or 'qlint.q_ unavailable'})")
    print(f"{'qlint.q_' if gate.enabled else 'NO qlint.q_ (lint gate disabled)'}"
          " + typeGen + eval_compare loaded — starting smoke scenarios\n")
    scenarios = [
        ("happy_path", scenario_happy_path),
        ("schema_aware_lint", scenario_schema_aware_lint),
        ("runtime_exec_disabled", scenario_runtime_exec_disabled),
    ]
    n_pass = 0
    for name, fn in scenarios:
        print(f"--- {name} ---")
        try:
            if await fn():
                n_pass += 1
        except Exception as e:
            _fail(f"{name}: exception {type(e).__name__}: {e}")
        print()

    await vllm_client.aclose()
    total = len(scenarios)
    print(f"{n_pass}/{total} scenarios passed")
    return 0 if n_pass == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
