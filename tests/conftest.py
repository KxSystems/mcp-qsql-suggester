"""Shared scaffolding for the embedded-q tiers (2 and 3).

Everything pykx-dependent is imported **inside** the fixtures rather than at
module scope. conftest.py is loaded for every pytest run, including a tier-1-only
run on a machine with no pykx and no licence, so a module-scope
``import pykx`` here would take tier 1 down with it. The fixtures only execute
for tests that request them, and only tier 2/3 tests do.

The tier-2/3 test modules keep their own module-level availability skips (see
``tests/test_pipeline_gates.py``); this file only supplies what they share.
"""

from __future__ import annotations

import asyncio
from typing import Iterator

import _tiers
import pytest


def pytest_configure(config):
    """Reject a run that named a tier this box cannot run.

    Before collection, so an unrunnable ``-m needs_q`` / ``-m needs_qlint``
    exits with one printed message (status 4) instead of the ``2 skipped, 87
    deselected`` that reads exactly like the documented self-skip. Why the two
    reactions differ, and why a bare ``pytest`` still skips:
    ``tests/_tiers.py``.
    """
    _tiers.check_selected_tiers(config)


#: The one dummy schema every gate test uses. Plain data — safe at module scope.
TRADE = {"trade": "`time`sym`price`size!`t`s`f`j"}


@pytest.fixture(scope="session")
def run_async():
    """Run every coroutine in ONE event loop for the whole session.

    ``mcp_server.loop`` holds a module-level ``asyncio.Lock`` that binds to the
    first loop that awaits it; a per-test ``asyncio.run()`` would bind it to a
    dead loop and every test after the first would fail with "is bound to a
    different event loop". The q-side globals (qlint, typeGen, eval_compare) are
    loaded once here for the same reason embedded q is single-threaded.

    Session scope spans **all** files, so tiers 2 and 3 share one loop and one
    q session even though they live in separate modules.
    """
    from mcp_server import loop as qsql_loop
    from mcp_server import vllm_client

    ev = asyncio.new_event_loop()
    try:
        ev.run_until_complete(qsql_loop.ensure_qlint_loaded())
        ev.run_until_complete(qsql_loop.ensure_typegen_loaded())
        ev.run_until_complete(qsql_loop.ensure_evalcmp_loaded())
        yield ev.run_until_complete
    finally:
        try:
            ev.run_until_complete(vllm_client.aclose())
        finally:
            ev.close()


@pytest.fixture
def fake_chat(monkeypatch):
    """Replace ``vllm_client.chat`` with one returning fixed completions,
    cycled round-robin to fill whatever ``n`` the round asks for."""
    from mcp_server import vllm_client

    def _install(completions: list[str]) -> None:
        async def _chat(messages, *, temperature=0.2, top_p=0.95,
                        max_tokens=None, n=1):
            it: Iterator[str] = iter(completions)
            out = []
            for _ in range(n):
                try:
                    out.append(next(it))
                except StopIteration:
                    it = iter(completions)
                    out.append(next(it))
            return out
        monkeypatch.setattr(vllm_client, "chat", _chat)
    return _install


@pytest.fixture
def generate(run_async):
    """Run one full ``run_generate_loop`` against ``TRADE``, one round only."""
    from mcp_server import loop as qsql_loop

    def _run(**kwargs) -> dict:
        return run_async(qsql_loop.run_generate_loop(
            table_schemas=TRADE, max_rounds=1, **kwargs,
        ))
    return _run


@pytest.fixture
def lint_gate_disabled(monkeypatch):
    """Force the resolved C9 gate to 'disabled' for one test.

    Patches the cached decision in ``qlint_common`` rather than the environment:
    the gate is resolved once per process (at ``run_async``'s
    ``ensure_qlint_loaded``), so by the time a test runs, re-reading
    ``MCP_QSQL_QLINT`` would change nothing.
    """
    from mcp_qsql_shared import qlint_common
    from mcp_qsql_shared.qlint_policy import QLINT_AUTO, QlintGate

    monkeypatch.setattr(
        qlint_common, "_qlint_gate",
        QlintGate(mode=QLINT_AUTO, enabled=False, reason="disabled by test"),
    )
