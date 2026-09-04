"""Tier 2 — pipeline gates against embedded q, with the model stubbed.

The deterministic half of the old ``mcp_server.smoke_test``: every candidate is
a fixed string supplied by the ``fake_chat`` fixture, so nothing here depends
on what qwen samples on the day. Embedded q is real though — each candidate is
genuinely parsed, executed against dummy tables across all seeds, and
clustered.

Needs pykx + a kdb+ licence. Deliberately does **not** need ``qlint.q_``: it is
licensed separately, the server treats it as optional
(``MCP_QSQL_QLINT=auto``), and this is the configuration most outside
contributors with a licence will have — so it is the one worth guarding. The
tests whose claims are *about* qlint's own output live in
``test_pipeline_gates_qlint.py`` (tier 3).

    pytest -m "needs_q and not needs_qlint" -q      # this file

Shared fixtures (``run_async``, ``fake_chat``, ``generate``,
``lint_gate_disabled``, ``TRADE``) come from ``conftest.py``. See
``tests/README.md`` for the four-tier split.
"""

from __future__ import annotations

import _tiers
import pytest
from conftest import TRADE


pytestmark = pytest.mark.needs_q


# --------------------------------------------------------------------------- #
# tier-2 availability gate
#
# The probe (pykx present? licensed? which interpreter?) lives in `_tiers` so
# tier 3 and conftest's `-m` check share one answer and one wording. Skipping
# here is for a bare `pytest`; a run that named this tier gets a usage error
# from `_tiers.check_selected_tiers` instead, before collection reaches this
# line.
# --------------------------------------------------------------------------- #

_unavailable = _tiers.pykx_reason()
if _unavailable:
    pytest.skip(f"tier 2: {_unavailable}", allow_module_level=True)


# --------------------------------------------------------------------------- #
# the execution gate
# --------------------------------------------------------------------------- #

def test_runtime_error_leaves_no_cluster(fake_chat, generate):
    """``price + sym`` is float + symbol: it parses and lints clean, so only
    executing it against real dummy data catches the bug. Every candidate
    raises on every seed, so nothing is eligible for clustering and the call
    must report failure rather than returning code that cannot run."""
    fake_chat(["```q\nselect bad:price+sym from trade\n```"])
    result = generate(
        instruction="Runtime-error demo — should fail at execution only.",
    )
    assert result["passed"] is False
    assert result["winning_cluster_size"] == 0
    assert any("runtime error" in i.lower() for i in result["final_issues"])


def test_parse_error_leaves_no_cluster(fake_chat, generate):
    """Unbalanced paren — rejected by the first gate, before exec."""
    fake_chat(["```q\nselect from trade where (\n```"])
    result = generate(instruction="Select rows where price exceeds size.")

    assert result["passed"] is False
    assert result["winning_cluster_size"] == 0
    assert any("parse error" in i.lower() for i in result["final_issues"])


# --------------------------------------------------------------------------- #
# clustering
# --------------------------------------------------------------------------- #

def test_majority_cluster_wins_when_round_threshold_is_unreachable(
    fake_chat, generate,
):
    """3 candidates compute ``avg price by sym``, 1 computes ``sum price by
    sym`` — execution-inequivalent, so two clusters form.

    A round of 4 needs unanimity (``ROUND_MIN_WEIGHT[4] == 4``) to finalize
    early, which a 3-1 split cannot reach. But ``max_rounds=1`` makes this the
    last round available, so the call must still finalize on the size-3
    majority rather than bailing out because the threshold was missed.
    """
    fake_chat(
        ["```q\nselect avg price by sym from trade\n```"] * 3
        + ["```q\nselect sum price by sym from trade\n```"]
    )
    result = generate(instruction="Average price per sym.")

    assert result["passed"] is True
    assert result["candidates_generated"] == 4
    assert result["winning_cluster_size"] == 3
    assert result["n_clusters"] == 2
    assert result["tie_occurred"] is False
    assert "avg price" in result["q_code"]


def test_identical_candidates_form_one_cluster(fake_chat, generate):
    """Four byte-identical completions dedupe to one unique candidate that is
    executed once, yet still carry weight 4 — enough to meet the round-of-4
    unanimity threshold."""
    fake_chat(["```q\nselect avg price by sym from trade\n```"])
    result = generate(instruction="Average price per sym.")

    assert result["passed"] is True
    assert result["candidates_generated"] == 4
    assert result["winning_cluster_size"] == 4
    assert result["n_clusters"] == 1
    assert result["rounds_used"] == 1


def test_cosmetically_different_equivalent_code_clusters_together(
    fake_chat, generate,
):
    """Distinct texts that compute the same thing must land in ONE cluster —
    this is execution-equivalence via ``.eval.cmp``, not text comparison, and
    it is what lets agreement accumulate across differently-phrased samples."""
    fake_chat([
        "```q\nselect avg price by sym from trade\n```",
        "```q\nselect avg price by sym from trade where 1b\n```",
    ])
    result = generate(instruction="Average price per sym.")

    assert result["passed"] is True
    assert result["candidates_generated"] == 4
    assert result["n_clusters"] == 1
    assert result["winning_cluster_size"] == 4


# --------------------------------------------------------------------------- #
# the lint gate in its ABSENT state
#
# qlint.q_ is licensed separately, so a deployment without it is legitimate
# (MCP_QSQL_QLINT=auto) rather than broken. What must never happen is that it
# degrades silently: the pipeline still has to work, the remaining gates still
# have to bite, and the response has to say the gate did not run. None of that
# needs a real linter to test — hence tier 2, not tier 3.
# --------------------------------------------------------------------------- #

def test_pipeline_works_with_the_lint_gate_disabled(
    fake_chat, generate, lint_gate_disabled,
):
    """No qlint.q_ must not break the call: parse and execution still gate, the
    candidates still cluster, and a good answer still passes."""
    fake_chat(["```q\nselect avg price by sym from trade\n```"])
    result = generate(instruction="Average price per sym.")

    assert result["passed"] is True
    assert result["winning_cluster_size"] == 4
    assert result["lint_gate"] == "disabled"


def test_execution_gate_still_bites_with_the_lint_gate_disabled(
    fake_chat, generate, lint_gate_disabled,
):
    """The remaining gates must not be weakened by dropping C9: `price+sym` is
    float + symbol, which only execution catches, and it must still be excluded
    from clustering."""
    fake_chat(["```q\nselect bad:price+sym from trade\n```"])
    result = generate(instruction="Runtime-error demo.")

    assert result["passed"] is False
    assert result["winning_cluster_size"] == 0
    assert result["lint_gate"] == "disabled"


def test_disabled_gate_skips_rather_than_passes_silently(lint_gate_disabled):
    """`check_c9_qlint` must distinguish 'linted clean' from 'never linted'.
    Both report pass=True — a candidate cannot be rejected by a gate that did
    not run — so `skipped` is the only thing that carries the difference."""
    from mcp_qsql_shared.qlint_kx import check_c9_qlint

    result = check_c9_qlint("select avg price by sym from trade", {"trade"})

    assert result["pass"] is True
    assert result["skipped"] is True
    assert "disabled" in result["notes"]


# --------------------------------------------------------------------------- #
# concurrent calls in one process (the HTTP transport's actual shape)
# --------------------------------------------------------------------------- #

def test_concurrent_calls_do_not_collide(monkeypatch, run_async):
    """Two overlapping calls must each cluster against their OWN results.

    `_run_pykx` serializes each individual pykx operation but releases its
    lock across the awaited `vllm_client.chat(...)`, so under the HTTP
    transport ("one process serves all clients") two calls interleave at
    round boundaries. The captured results deliberately outlive a round, so
    without per-call naming both calls bind `sc0_s42`, each silently
    overwrites the other, and the round-2 cross-round `.eval.cmp` compares
    the wrong pair; without `live_bindings()` in the protect set, whichever
    call finishes first deletes the other's still-live bindings.

    Both failure modes are silent, which is why this asserts on the cluster
    weights rather than on an exception. Each call's third text is
    execution-equivalent to its first, so the correct winner is
    weight 2 (round 1) + 4 (round 2) = 6 out of 8 generated.
    """
    from mcp_server import loop as qsql_loop
    from mcp_server import vllm_client

    quote = {"quote": "`time`sym`bid`ask!`t`s`f`f"}
    scripts = {
        # marker: [round-1 texts (cycled to n=4), round-2 texts]
        "trade": (
            ["```q\nselect avg price by sym from trade\n```",
             "```q\nselect max price by sym from trade\n```"],
            ["```q\nselect avg price by sym from trade where 1b\n```"],
        ),
        # Two value columns, not one: the dummy tables for both calls are
        # built from the same seeds, so a single `f` column would generate
        # byte-identical data and a cross-contaminated comparison could come
        # out EQUAL by coincidence rather than because isolation held.
        "quote": (
            ["```q\nselect avg bid, avg ask by sym from quote\n```",
             "```q\nselect max bid, max ask by sym from quote\n```"],
            ["```q\nselect avg bid, avg ask by sym from quote where 1b\n```"],
        ),
    }

    # Release round r only once BOTH calls have reached it, so the interleave
    # is deterministic rather than dependent on how the event loop happens to
    # schedule two coroutines.
    arrived: dict[int, int] = {}
    events: dict[int, object] = {}
    rounds: dict[str, int] = {"trade": 0, "quote": 0}

    async def _chat(messages, *, temperature=0.2, top_p=0.95,
                    max_tokens=None, n=1):
        import asyncio

        marker = "trade" if "trade" in messages[1]["content"] else "quote"
        r = rounds[marker]
        rounds[marker] += 1

        ev = events.setdefault(r, asyncio.Event())
        arrived[r] = arrived.get(r, 0) + 1
        if arrived[r] >= 2:
            ev.set()
        await ev.wait()

        texts = scripts[marker][r]
        return [texts[i % len(texts)] for i in range(n)]

    monkeypatch.setattr(vllm_client, "chat", _chat)

    async def _both():
        import asyncio

        return await asyncio.gather(
            qsql_loop.run_generate_loop(
                instruction="Average price by sym.", table_schemas=TRADE,
                max_rounds=2,
            ),
            qsql_loop.run_generate_loop(
                instruction="Average bid by sym.", table_schemas=quote,
                max_rounds=2,
            ),
        )

    a, b = run_async(_both())

    for name, result in (("trade", a), ("quote", b)):
        assert result["passed"] is True, f"{name}: {result['final_issues']}"
        assert result["rounds_used"] == 2, name
        assert result["candidates_generated"] == 8, name
        # 2 from round 1 + 4 from round 2 — only reachable if the round-2
        # comparison saw this call's own round-1 result.
        assert result["winning_cluster_size"] == 6, name
        assert result["n_clusters"] == 2, name
