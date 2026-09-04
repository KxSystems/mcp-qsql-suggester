"""Tier 3 — the claims that need a genuinely working KX qlint.

Everything here asserts something about **qlint's own output**, so none of it is
meaningful without a linter. That is why it is a tier rather than a footnote on
tier 2: `qlint.q_` is a separately-licensed artifact from an EULA-gated KX
download, so "has embedded q" and "has a working linter" are different rungs of
the dependency ladder, reached by different people.

Needs everything tier 2 needs, plus a **full AxLibraries ws/ tree**. The bare
`qlint.q_` is a ~2 KB loader stub; see
``mcp_qsql_shared.qlint_common._probe_lintitem`` for why having the file is not
the same as having a linter.

    pytest -m needs_qlint -q       # this file

The ``_require_working_qlint`` fixture below skips the whole module's worth of
tests when the gate is not live, so a bare ``pytest`` stays correct on a box
without qlint. The ``needs_qlint`` marker is what lets you *select* them; the
fixture is what lets you *ignore* them. Both are wanted.

Shared fixtures come from ``conftest.py``. See ``tests/README.md`` for the
four-tier split.
"""

from __future__ import annotations

import _tiers
import pytest


pytestmark = [pytest.mark.needs_q, pytest.mark.needs_qlint]


# Same availability gate as tier 2, sharing the same probe — see the note there.
_unavailable = _tiers.pykx_reason()
if _unavailable:
    pytest.skip(f"tier 3: {_unavailable}", allow_module_level=True)


@pytest.fixture(autouse=True)
def _require_working_qlint(request, run_async):
    """Stop every test in this module unless the C9 gate actually came up.

    Depends on ``run_async`` so the gate is resolved through the server's own
    startup path (``ensure_qlint_loaded`` on the pykx worker) before being read,
    rather than being resolved incidentally from whichever thread asks first.

    Skip or fail, on the same rule as ``_tiers.check_selected_tiers``: a bare
    ``pytest`` on a box with no linter skips, but ``-m needs_qlint`` asked for
    these tests specifically and has nothing else to run, so a silent skip
    would answer the wrong question. That check has already rejected an unset
    or wrong ``QLINT_DIR`` before collection; what reaches here is the case
    only embedded q can see — a ``qlint.q_`` that loads but leaves
    ``.qlint.lintItem`` undefined (the loader stub without its ``lib/``).
    """
    from mcp_qsql_shared.qlint_common import qlint_gate

    gate = qlint_gate()
    if gate.enabled:
        return
    message = f"tier 3 needs a working qlint: {gate.reason}"
    if _tiers.tier3_targeted(request.config):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


# --------------------------------------------------------------------------- #
# the gate reports itself honestly
# --------------------------------------------------------------------------- #

def test_response_reports_the_lint_gate_as_active(fake_chat, generate):
    """Where qlint IS available, every response must advertise that C9 really
    ran — callers key off this to decide how much the gate stack is worth.

    The counterpart (`lint_gate: "disabled"`) is tier 2, since faking the
    disabled state needs no linter.
    """
    fake_chat(["```q\nselect avg price by sym from trade\n```"])
    result = generate(instruction="Average price per sym.")

    assert result["lint_gate"] == "kx_qlint_c9"


# --------------------------------------------------------------------------- #
# lint-as-diagnostics: qlint runs even when parse fails
#
# Every error-level qlint rule we could actually trigger ALSO fails q's parse,
# so short-circuiting the lint gate on a parse failure threw away the better
# message in exactly the cases where it mattered. q reports a 120-global
# function as the single word `globals`; qlint names the rule. The candidate is
# still rejected either way — this is about what the caller is told.
# --------------------------------------------------------------------------- #

def test_parse_failure_still_gets_the_qlint_message(fake_chat, generate):
    """`globals` (q) vs `TOO_MANY_GLOBALS: Too many globals in a function`
    (qlint) — both must reach the caller, parse error first."""
    body = ";".join(f"g{i}::{i}" for i in range(120))
    fake_chat(["```q\n{" + body + "}[]\n```"])
    result = generate(instruction="Too many globals.")

    assert result["passed"] is False
    assert any("parse error" in i.lower() for i in result["final_issues"])
    assert any("TOO_MANY_GLOBALS" in i for i in result["final_issues"])
    # Order matters: the parse error says it is unusable, the lint issue says why.
    assert "parse error" in result["final_issues"][0].lower()


def test_qlint_message_explains_a_bad_escape(fake_chat, generate):
    """q echoes the offending token and stops; qlint lists the valid escapes.
    The second is the one a caller's model can act on."""
    fake_chat(['```q\nselect from trade where sym like "a\\q"\n```'])
    result = generate(instruction="Bad escape sequence.")

    assert result["passed"] is False
    joined = " | ".join(result["final_issues"])
    assert "INVALID_ESCAPE" in joined
    assert "Valid escape sequences" in joined


def test_plain_parse_error_is_not_padded_with_cannot_parse(fake_chat, generate):
    """Linting unparseable code must yield a *better* message, not a second
    content-free one: qlint's own CANNOT_PARSE restates q's parse error, so it
    is dropped when the caller already has one."""
    fake_chat(["```q\nselect from trade where (\n```"])
    result = generate(instruction="Unbalanced paren.")

    assert result["passed"] is False
    assert any("parse error" in i.lower() for i in result["final_issues"])
    assert not any("CANNOT_PARSE" in i for i in result["final_issues"])


def test_lint_still_cannot_rescue_an_unparseable_candidate(fake_chat, generate):
    """The enrichment is diagnostics-only. A candidate that fails parse must
    stay out of every cluster no matter how clean the lint came back."""
    fake_chat(["```q\nselect from trade where (\n```"])
    result = generate(instruction="Unbalanced paren.")

    assert result["passed"] is False
    assert result["winning_cluster_size"] == 0
    assert result["n_clusters"] == 0


# --------------------------------------------------------------------------- #
# C9 as a rejector, not just a translator
# --------------------------------------------------------------------------- #

def test_qlint_rejects_what_parse_and_execution_cannot(fake_chat, generate):
    """`{x::1} peach til 3` is the one demonstration that C9 pulls its weight
    as a *filter* rather than only as a message translator.

    It parses. It also *executes* cleanly against the dummy table, because the
    embedded q here is single-threaded and the global assignment is harmless in
    that setting — so gates 1 and 3 both wave it through and all four identical
    candidates agree unanimously. Under a real `-s N` process it is a genuine
    defect. Measured with `MCP_QSQL_QLINT=off` this exact candidate comes back
    `passed=True` with a 4/4 cluster; C9 is the only reason it does not.

    If this test ever starts failing because the candidate now dies at
    execution, that is worth understanding rather than deleting: it would mean
    the dummy-table environment gained thread semantics it did not have before.
    """
    fake_chat(["```q\n{x::1} peach til 3\n```"])
    result = generate(instruction="Assign a global inside peach.")

    assert result["passed"] is False
    assert result["winning_cluster_size"] == 0
    assert any("GLOBAL_PEACH" in i for i in result["final_issues"])
