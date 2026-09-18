"""Tier 1: the skill renderer's case-A verdict claims match its own counts.

``format_output.py`` is the single source of truth for both skills' response
format and now ships inside the `mcp-qsql-suggester` plugin, so its output is
user-facing text in two places. It had no coverage until a real
`/qsql-coder-quick` run printed this, which is self-contradictory:

    passed=true, 5/8 candidates agreed (round 2). No verification triggered
    (..., unanimous self-consistency cluster, ...)

The "unanimous" clause was a hardcoded string in ``render_case_a`` while the
counts beside it are computed, so any non-unanimous winning cluster produced a
verdict that argued with itself. Quick mode reaches that state because it
renders case A whatever the cluster fraction; full mode reaches it too, now
that its cluster trigger is a threshold (`winning_cluster_fraction < 0.6`)
rather than unanimity -- so a 7/8 cluster with no other trigger is a
legitimate case A.

The same family of defect turned up a second time, in the *reason* the verdict
gives for the absence of a check rather than in the counts beside it. Case A
listed "no high-risk idiom, no functional form, ..." and case D said "outside my
q knowledge or the instruction is genuinely ambiguous" -- both true only when
verification was *unnecessary*, and both false when it was *impossible*: quick
mode never verifies, and full mode cannot when `q` is missing from PATH. So a
`wavg` draft rendered by quick mode claimed "no high-risk idiom" about code that
contains one. ``verification_unavailable`` names the real reason and the tests
below pin that the two wordings never trade places.

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


# The three claims a verdict must only make when it has earned them. Named
# rather than inlined because each is asserted in both directions.
REASON_LIST_CLAIM = "No verification triggered"
TRIGGER_CLAIM = "Bash-verify trigger"
AMBIGUITY_CLAIM = "outside my q knowledge"

CASE_D_BASE = {
    "case": "D",
    "instruction_interpreted": "irrelevant to these assertions",
    "schemas": {"trade": "`time`sym`price`size!`p`s`f`j"},
    "qwen_code": "select wavg[size;price] % sum size from trade",
    "passed": False,
    "rounds_used": 3,
    "candidates_generated": 16,
    "winning_cluster_size": 0,
    "final_issues": ["parse error: ]"],
    "opus_could_write": False,
}


def run(payload: dict) -> subprocess.CompletedProcess:
    """Invoke the formatter without asserting success, for the reject cases."""
    return subprocess.run(
        [sys.executable, str(FORMATTER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def verdict_of(payload: dict) -> str:
    proc = run(payload)
    assert proc.returncode == 0, proc.stderr
    verdicts = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("**Verdict**")
    ]
    assert len(verdicts) == 1, f"expected one verdict line, got {verdicts!r}"
    return verdicts[0]


def render_d(**overrides) -> str:
    payload = dict(CASE_D_BASE)
    payload.update(overrides)
    return verdict_of(payload)


@pytest.mark.parametrize("reason", ["quick_mode", "no_q"])
def test_unavailable_replaces_the_reason_list_entirely(reason):
    """A verdict that did not verify may not list reasons it never checked.

    The counts stay -- they are established by the MCP return, not by any
    reference check -- but every clause of the "nothing triggered it" list is a
    claim about the draft that only Bash-verify's decision table establishes.
    """
    verdict = render(verification_unavailable=reason)
    assert REASON_LIST_CLAIM not in verdict, verdict
    assert UNANIMOUS_CLAIM not in verdict, verdict
    assert "Not verified" in verdict, verdict
    assert "4/4 candidates agreed (round 1)" in verdict, verdict


@pytest.mark.parametrize(
    "reason,expected",
    [("quick_mode", "quick mode"), ("no_q", "PATH")],
)
def test_unavailable_names_which_reason_it_was(reason, expected):
    """The two reasons ask different things of the reader, so they must differ.

    `quick_mode` is the user's own choice and the fix is to use full mode;
    `no_q` is an environment gap and the fix is to install q. A single shared
    "not verified" sentence would leave both readers without a next step.
    """
    assert expected in render(verification_unavailable=reason)


def test_plurality_states_its_numbers_without_judging_them():
    """The renderer reports the split; it does not rule on what it warrants.

    Which fractions warrant a check is skill policy and tunable -- it moved
    from "anything short of unanimous" to `< 0.6` -- so a renderer asserting
    "this is a Bash-verify trigger" was wrong for every 7/8 cluster the moment
    the threshold moved. The numbers are what stays true.
    """
    split = dict(candidates_generated=8, winning_cluster_size=5, rounds_used=2)
    for verdict in (render(**split), render(verification_unavailable="no_q", **split)):
        assert "5 of 8" in verdict, verdict
        assert TRIGGER_CLAIM not in verdict, verdict


def test_plurality_reason_list_does_not_claim_unanimity():
    """Case A now admits a plurality, so its reason list must not say otherwise.

    Under the old `< 1.0` trigger a full-mode case A was unanimous by
    construction and the clause could be asserted freely. Under `< 0.6` a 7/8
    cluster is a legitimate case A, so the clause has to describe a threshold
    rather than unanimity -- while still accounting for the cluster, since a
    reason list that silently omits it reads as an oversight.
    """
    verdict = render(candidates_generated=8, winning_cluster_size=7, rounds_used=2)
    assert REASON_LIST_CLAIM in verdict, verdict
    assert UNANIMOUS_CLAIM not in verdict, verdict
    assert "threshold" in verdict, verdict


def test_case_d_remedy_matches_the_reason():
    """The two reasons need different remedies, so they must not share one.

    Telling a quick-mode user to install q, or a user with no q to switch to
    full mode (which needs the q they do not have), sends half the readers to
    a remedy that cannot work.
    """
    quick = render_d(verification_unavailable="quick_mode")
    no_q = render_d(verification_unavailable="no_q")
    assert "/qsql-coder" in quick and "PATH" not in quick, quick
    assert "PATH" in no_q and "/qsql-coder" not in no_q, no_q


def test_case_d_ambiguity_claim_only_when_verification_was_possible():
    """Case D's empty-handed verdict has two causes, not one.

    `opus_could_write=false` is forced whenever q is missing -- the skill only
    presents q it has verified -- so without this split every no-q run told the
    user their instruction was ambiguous.
    """
    assert AMBIGUITY_CLAIM in render_d()
    degraded = render_d(verification_unavailable="no_q")
    assert AMBIGUITY_CLAIM not in degraded, degraded
    assert "PATH" in degraded, degraded


@pytest.mark.parametrize("case", ["B", "C"])
def test_unavailable_is_rejected_on_the_verified_cases(case):
    """B and C carry a reference priority and a verify shape by definition."""
    payload = {
        "case": case,
        "instruction_interpreted": "x",
        "schemas": {"trade": "`a`b!`s`j"},
        "qwen_code": "select from trade",
        "passed": True,
        "rounds_used": 1,
        "candidates_generated": 4,
        "winning_cluster_size": 4,
        "verification_unavailable": "no_q",
    }
    proc = run(payload)
    assert proc.returncode == 2, proc.stdout
    assert "verification_unavailable" in proc.stderr


def test_unknown_reason_is_a_hard_error():
    """A typo must not fall through to the wording it was meant to replace."""
    payload = {
        "case": "A",
        "instruction_interpreted": "x",
        "schemas": {"trade": "`a`b!`s`j"},
        "qwen_code": "select from trade",
        "passed": True,
        "rounds_used": 1,
        "candidates_generated": 4,
        "winning_cluster_size": 4,
        "verification_unavailable": "quick",  # near-miss for "quick_mode"
    }
    proc = run(payload)
    assert proc.returncode == 2, proc.stdout
    assert "must be one of" in proc.stderr
