"""Tier 1: the skill renderer's case-A verdict claims match its own counts.

``format_output.py`` is the single source of truth for both skills' response
format and now ships inside the `mcp-qsql-suggester` plugin, so its output is
user-facing text in two places. It had no coverage until a real
`/qsql-coder-quick` run printed this, which is self-contradictory:

    passed=true, 5/8 candidates agreed (round 2). No verification triggered
    (..., unanimous self-consistency cluster, ...)

The "unanimous" clause was a hardcoded string in ``render_case_a`` while the
counts beside it are computed, so any non-unanimous winning cluster produced a
verdict that argued with itself. Full mode cannot reach that state — SKILL.md's
case-A row requires a unanimous cluster, and a split one is itself a Bash-verify
trigger, so it lands in case B or C. Quick mode can, because it renders case A
whatever the cluster fraction.

**These tests deliberately assert invariants, not wording.** They check that a
claim of unanimity appears exactly when the counts support it, and that the
counts are stated when it does not — so the sentences can be rewritten freely
and only a *semantic* regression fails. Pinning the full verdict string would
make every copy-edit a test failure, which is how prose tests earn their bad
reputation.

Tier 1: stdlib only. The script is invoked as a subprocess, which is both the
real contract (the skills pipe a heredoc into it) and the way to run a file that
is not an importable package.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FORMATTER = (
    Path(__file__).resolve().parents[1]
    / ".claude/skills/qsql-coder/scripts/format_output.py"
)

UNANIMOUS_CLAIM = "unanimous self-consistency cluster"


def render(**overrides) -> str:
    """Render a case-A payload and return the verdict line."""
    payload = {
        "case": "A",
        "instruction_interpreted": "irrelevant to these assertions",
        "schemas": {"trade": "`time`sym`price`size!`p`s`f`j"},
        "qwen_code": "select from trade",
        "passed": True,
        "rounds_used": 1,
        "candidates_generated": 4,
        "winning_cluster_size": 4,
        "lint_gate": "kx_qlint_c9",
    }
    payload.update(overrides)
    proc = subprocess.run(
        [sys.executable, str(FORMATTER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    verdicts = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("**Verdict**")
    ]
    assert len(verdicts) == 1, f"expected one verdict line, got {verdicts!r}"
    return verdicts[0]


@pytest.mark.parametrize(
    "rounds,generated,winning,unanimous",
    [
        (1, 4, 4, True),      # early finalize, round of 4 agreeing outright
        (2, 8, 8, True),      # every candidate of a bigger round agreeing
        (2, 8, 5, False),     # the plurality that exposed the bug
        (3, 16, 10, False),   # a plurality after the full 4 -> 8 -> 16 preset
    ],
)
def test_unanimity_claim_tracks_the_counts(rounds, generated, winning, unanimous):
    """The verdict may claim unanimity only when the counts say so."""
    verdict = render(
        rounds_used=rounds,
        candidates_generated=generated,
        winning_cluster_size=winning,
    )
    # The counts themselves are what the claim has to agree with, so pin that
    # they are present at all — a verdict that dropped them could not be
    # self-contradictory, but it also could not be checked by a reader.
    assert f"{winning}/{generated} candidates agreed (round {rounds})" in verdict
    assert (UNANIMOUS_CLAIM in verdict) is unanimous, verdict


@pytest.mark.parametrize("generated,winning", [(8, 5), (16, 10)])
def test_plurality_verdict_states_how_split_it_was(generated, winning):
    """A non-unanimous cluster has to say so in its own numbers.

    Removing the false "unanimous" claim is not enough on its own: silence
    would read as "nothing to report" to someone skimming for a reason to
    double-check the draft.
    """
    verdict = render(
        candidates_generated=generated, winning_cluster_size=winning,
    )
    assert f"{winning} of {generated}" in verdict, verdict


@pytest.mark.parametrize("winning,generated", [(4, 4), (5, 8)])
@pytest.mark.parametrize("gate,disclosed", [("kx_qlint_c9", False), ("disabled", True)])
def test_lint_gate_disclosure_composes_with_either_cluster(
    winning, generated, gate, disclosed
):
    """`lint_gate: disabled` is surfaced regardless of the cluster shape.

    The two clauses are assembled independently and this is the grid where a
    refactor of one would plausibly drop the other.
    """
    verdict = render(
        candidates_generated=generated,
        winning_cluster_size=winning,
        lint_gate=gate,
    )
    assert ("qlint gate disabled" in verdict) is disclosed, verdict
