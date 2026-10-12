"""Tier 2 — the SKILL copy of ``eval_compare.q``, against embedded q.

``.claude/skills/qsql-coder/eval_compare.q`` is a deliberate fork of
``mcp_server/eval_compare.q``: it treats two temporal columns on the same
resolution chain as equal, and that copy does not. The two answer different
questions — clustering asks "did independent samples agree?" (two samples
returning different types did not), Bash-verify asks "does qwen's result match
my reference's intent?" (there a resolution difference is presentation). The
fork is the whole subject of this module, so the tests load the skill file
explicitly rather than relying on ``run_async``'s server-side load.

What motivated it: a real ``/qsql-coder`` run drafted 5-minute OHLCV bars as
``5 xbar time.minute`` (a ``u``, minute-of-day) while the reference used
``0D00:05 xbar time`` (a ``p``). Same grouping, same aggregates, bucket column
typed differently — and ``.eval.cmp`` bailed at its type guard before comparing
any value, so it landed in Case C ("real semantic divergence") and the skill
told the user qwen was wrong. It was not.

Needs pykx + a kdb+ licence; no qlint, no vLLM, no GPU.

    pytest -m "needs_q and not needs_qlint" -q      # this file, among others
"""

from __future__ import annotations

from pathlib import Path

import _tiers
import pytest


pytestmark = pytest.mark.needs_q

_unavailable = _tiers.pykx_reason()
if _unavailable:
    pytest.skip(f"tier 2: {_unavailable}", allow_module_level=True)


_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / ".claude/skills/qsql-coder"


@pytest.fixture(scope="module")
def kxq(run_async):
    """Swap the skill's eval_compare.q into the shared embedded q, then undo it.

    pykx gives one q process for the whole session, and both copies define
    ``.eval.cmp`` / ``.eval.eqEps`` into the same namespace — so loading the
    skill copy here would silently hand its (deliberately more tolerant)
    semantics to every later tier-2/3 test. Teardown reloads the server copy by
    path rather than through ``qexec._ensure_evalcmp``, whose idempotence guard
    would make the restore a no-op. The skill-only extras (``.eval.tNorm`` and
    friends) stay defined afterwards; nothing in the server copy references
    them.

    ``run_async`` is requested only to force conftest's session q setup to run
    first, so this fixture is the *second* writer and the restore is what the
    rest of the session sees.
    """
    import pykx as kx

    from mcp_server.qexec import EVAL_COMPARE_Q_PATH

    kx.q(f"\\l {_SKILL / 'eval_describe.q'}")
    kx.q(f"\\l {_SKILL / 'eval_compare.q'}")
    try:
        yield kx.q
    finally:
        kx.q(f"\\l {EVAL_COMPARE_Q_PATH}")


def _b(v) -> bool:
    """pykx boolean atom -> Python bool."""
    return bool(v.py())


def _c(v) -> str:
    """pykx char atom -> a one-character Python str."""
    py = v.py()
    return py.decode() if isinstance(py, (bytes, bytearray)) else str(py)


# --------------------------------------------------------------------------- #
# .eval.tTarget — which type the pair normalizes to, if any
#
# Type shorts: p=12 m=13 d=14 z=15 n=16 u=17 v=18 t=19. Two chains sharing a
# head: p/z carry a date AND a time-of-day; d/m carry a date only; t/v/u carry
# a time-of-day only. " " means "not normalizable".
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "ta,tb,want",
    [
        (12, 17, "u"),   # p vs u — the incident
        (17, 12, "u"),   # and symmetric: argument order must not matter
        (12, 14, "d"),   # p vs d — `by date` vs `by time`, the other common pair
        (14, 13, "m"),   # d vs m, both on the date chain
        (15, 19, "t"),   # z vs t — equal precision, one carries a date
        (12, 15, "z"),   # p vs z — a pure precision step (ns -> ms)
        (12, 12, " "),   # same type: nothing to normalize
        (14, 17, " "),   # d vs u — different chains
        (13, 18, " "),   # m vs v — likewise
        (16, 12, " "),   # n is a DURATION, not a point in time: category error
        (12, 7, " "),    # p vs long: not temporal at all
        (-12, -17, "u"),  # atoms carry a negative type short
    ],
)
def test_ttarget_picks_the_coarser_type_on_a_shared_chain(kxq, ta, tb, want):
    got = _c(kxq(f".eval.tTarget[{ta}h;{tb}h]"))
    assert got == want, f"tTarget[{ta}h;{tb}h] -> {got!r}, want {want!r}"


def test_every_in_chain_downcast_q_would_accept_is_actually_legal(kxq):
    """The chain model's load-bearing assumption, asserted rather than trusted.

    ``.eval.tNorm`` casts without any error handling, which is only safe
    because every downcast *within* a chain is legal in q — the chain test
    rejects exactly the pairs q would refuse with 'type. Adding a type to the
    wrong chain would turn "returns unmatched" into "throws", so this guards
    the table rather than the code around it.
    """
    illegal = kxq(
        "raze {[g] k:key g; raze {[g;x;y]"
        "   if[g[x]>=g[y]; :()];"
        "   ok:@[{[c;v] c$v; 1b}[.eval.tChar y;]; "
        "       (12 13 14 15 17 18 19h!(2024.06.01D09:05:00.0;2024.06m;2024.06.01;"
        "        `datetime$2024.06.01D09:05:00.0;09:05;09:05:00;09:05:00.000))x; "
        "       {[e] 0b}];"
        "   $[ok; (); enlist (x;y)] }[g;;] ./: k cross k } each .eval.tRank"
    ).py()
    assert not illegal, f"in-chain downcasts q rejects: {illegal}"


# --------------------------------------------------------------------------- #
# .eval.cmp — the behaviour the skill's Case B/C classification rests on
# --------------------------------------------------------------------------- #

def test_the_incident_is_now_a_wrap_difference_not_a_divergence(kxq):
    """5-min bars as `u` vs as `p`: same buckets, same aggregates, both typed.

    ``strict`` must stay 0b — the skill needs that to classify B-**wrap** and
    demand an idiom note explaining the type axis. ``soft`` must be 1b, which
    is what stops it being Case C.
    """
    kxq(
        "t::([]time:2024.06.01D09:00:00+1000000000*til 40; sym:40#`A`B;"
        " price:40#1 2 3f; size:40#10 20j)"
    )
    agg = "open:first price,high:max price,low:min price,close:last price,volume:sum size"
    qwen = kxq(f"select {agg} by sym,bucket:5 xbar time.minute from t")
    ref = kxq(f"select {agg} by sym,bucket:0D00:05 xbar time from t")
    assert not _b(kxq("{[a;b] a~b}", qwen, ref)), "strict must stay 0b (B-wrap, not B-clean)"
    assert _b(kxq(".eval.cmp", qwen, ref)), "soft must be 1b — this is not a divergence"


def test_a_multi_day_merge_is_still_rejected(kxq):
    """The defect the relaxation could have hidden, and does not.

    ``5 xbar time.minute`` on multi-day input merges rows from different days
    into one bucket. That changes the row count and the aggregates, so the
    rejection comes from those columns rather than from the key — which is why
    normalizing the key away is safe. The dummy table is single-day by
    construction (``mcp_server/type_gen.q``), so this input is synthetic on
    purpose: the unit under test is the comparison, not the dummy.
    """
    kxq(
        "t2::([]time:(2024.06.01D09:00:00;2024.06.02D09:00:00;2024.06.01D09:01:00);"
        " sym:3#`A; price:1 2 3f; size:3#10j)"
    )
    qwen = kxq("select volume:sum size by bucket:5 xbar time.minute from t2")
    ref = kxq("select volume:sum size by bucket:0D00:05 xbar time from t2")
    assert not _b(kxq(".eval.cmp", qwen, ref))


@pytest.mark.parametrize(
    "a,b,want,why",
    [
        (
            "([]b:2024.06.01 2024.06.02)",
            "([]b:09:05 09:10)",
            False,
            "d vs u are on different chains — no common representation",
        ),
        (
            "([]b:2024.06.01 2024.06.02)",
            "([]b:2024.06.01D09:05:00 2024.06.02D09:05:00)",
            True,
            "p->d drops the time: the accepted 'coarser wins' trade",
        ),
        (
            "([]b:2024.06.01 2024.06.02)",
            "([]b:2024.06.01D09:05:00 2024.06.09D09:05:00)",
            False,
            "different dates survive the downcast",
        ),
        (
            "([]b:09:05 09:10)",
            "([]b:2024.06.01D09:05:00 2024.06.15D09:15:00)",
            False,
            "p->u keeps the minute, and these minutes differ",
        ),
        (
            "09:05 09:10",
            "2024.06.01D09:05:00 2024.06.01D09:10:00",
            True,
            "normalization applies to bare lists, not only to table columns",
        ),
        (
            "([]b:2#0Np)",
            "([]b:2#0Nu)",
            True,
            "nulls survive the downcast as nulls",
        ),
        (
            "([]b:0Np,2024.06.01D09:05:00)",
            "([]b:09:05 09:05)",
            False,
            "a null on one side only stays a mismatch",
        ),
    ],
)
def test_temporal_pairs(kxq, a, b, want, why):
    assert _b(kxq(f".eval.cmp[{a}; {b}]")) is want, why


def test_by_date_vs_by_time_is_rejected_on_row_count(kxq):
    """The realistic version of the `d` vs `p` relaxation, and why it is safe.

    Two queries that really do group differently produce different row counts,
    and ``.eval.eqEps`` bails on a count mismatch before any value comparison.
    """
    src = "([]time:2024.06.01D09:00:00 2024.06.01D10:00:00)"
    assert not _b(
        kxq(
            f".eval.cmp[select n:count i by d:time.date from {src};"
            f"          select n:count i by d:time from {src}]"
        )
    )


# --------------------------------------------------------------------------- #
# The numeric guard-order fix that shipped alongside it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "a,b,want",
    [
        ("1 2 3", "1 2 3f", True),                # was 0b at top level, 1b in a table
        ("([]v:1 2 3)", "([]v:1 2 3f)", True),    # the table path, unchanged
        ("1 2 3", "1 2 4f", False),               # a real difference is still a difference
    ],
)
def test_numeric_type_spelling_is_ignored_at_every_level(kxq, a, b, want):
    """``.eval.cmp`` used to guard on type equality *before* its numeric branch.

    ``.eval.eqEps`` has always done the opposite, so the same ``j`` vs ``f``
    pair compared equal inside a table and unequal as a bare list — one answer
    depending only on whether the values arrived wrapped. The temporal work
    rewrote that guard, so the orders now agree.
    """
    assert _b(kxq(f".eval.cmp[{a}; {b}]")) is want


@pytest.mark.parametrize(
    "a,b,want",
    [
        ("([]a:1 2;b:`x`y)", "([]a:1 2;b:`x`y)", True),
        ("([]a:1 2;b:`x`y)", "([]b:`x`y;a:1 2)", True),      # column reorder
        ("([]a:1 2)", "([]z:1 2)", True),                     # positional rename
        ("([a:1 2]b:3 4)", "([]a:1 2;b:3 4)", True),          # keyed vs unkeyed wrap
        ("([]a:`x`y)", "([]a:`x`z)", False),
        ("([]a:1 2f)", "([]a:1.0000000000001 2f)", True),     # float drift
        ("([]a:1 2f)", "([]a:10 20f)", False),                # a real bug
    ],
)
def test_the_tolerances_that_were_already_there_still_behave(kxq, a, b, want):
    """Guard the four pre-existing axes against the guard-order rewrite."""
    assert _b(kxq(f".eval.cmp[{a}; {b}]")) is want


# --------------------------------------------------------------------------- #
# .eval.wrapInfo — the B-wrap inspection aid
# --------------------------------------------------------------------------- #

def test_wrapinfo_shows_the_temporal_axis_on_a_KEY_column(kxq):
    """The reason it prints `meta` and not `.eval.descVal`.

    ``.eval.fmtCols`` formats only a keyed table's *value* columns, so on the
    incident -- where the differing column ``bucket`` is a key -- descVal
    printed the identical string for both sides and named no axis at all.
    ``meta`` lists keys too, and its ``t`` column is where the difference is.
    """
    kxq(
        "t3::([]time:2024.06.01D09:00:00+1000000000*til 40; sym:40#`A`B;"
        " price:40#1 2 3f; size:40#10 20j)"
    )
    qwen = kxq("select v:sum size by sym,bucket:5 xbar time.minute from t3")
    ref = kxq("select v:sum size by sym,bucket:0D00:05 xbar time from t3")

    assert _c(kxq(".eval.descVal", qwen)) == _c(kxq(".eval.descVal", ref)), (
        "if descVal ever starts distinguishing these, wrapInfo could be simplified"
    )
    types = [_c(kxq("{[r] exec first t from meta[r] where c=`bucket}", r)) for r in (qwen, ref)]
    assert types == ["u", "p"], types


@pytest.mark.parametrize(
    "a,b",
    [
        ("1.0", "1.0000000001"),   # q's `~` tolerates ~1e-13; this epsilon is 1e-9
        ("1", "1f"),               # the numeric guard-order fix reaches atoms too
        ("09:05", "2024.06.01D09:05:00.0"),  # and so does temporal normalization
    ],
)
def test_wrapinfo_survives_the_non_table_results_that_reach_b_wrap(kxq, a, b):
    """`meta` raises 'type on an atom, so the fallback is not hypothetical.

    Each pair here is measured to give strict=0b with soft=1b, i.e. it lands in
    the branch that calls wrapInfo. The assertion is simply that the call does
    not raise -- if it did, the Bash-verify script would die after computing a
    perfectly good verdict.
    """
    assert not _b(kxq(f"{a} ~ {b}")), "pair must be strict=0b to reach this branch"
    assert _b(kxq(f".eval.cmp[{a}; {b}]")), "pair must be soft=1b to reach this branch"
    kxq(f".eval.wrapInfo[{a}; {b}]")


def test_wrapinfo_handles_a_mixed_table_and_atom_pair(kxq):
    """One side a table, the other not: still must not raise."""
    kxq(".eval.wrapInfo[([]v:1 2); 3f]")


# --------------------------------------------------------------------------- #
# dict shapes: keyed table vs dict vs dict of a table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "a,b,want",
    [
        # keyed table vs a plain dict, both ways and against a dict of a table
        ("([s:`a`b] x:1 2f)", "(enlist`s)!enlist`a", False),
        ("(enlist`s)!enlist`a", "([s:`a`b] x:1 2f)", False),
        ("([s:`a`b] x:1 2f)", "`a`b!([]x:1 2f)", False),
        ("`a`b!([]x:1 2f)", "([s:`a`b] x:1 2f)", False),
        # dict of a table (`exec ... by sym`): float drift tolerated, a real
        # difference is not, and it never equals a dict of a plain list
        ("`a`b!([]x:1 2f)", "`a`b!([]x:1 2f+1e-11)", True),
        ("`a`b!([]x:1 2f)", "`a`b!([]x:1 3f)", False),
        ("`a`b!([]x:1 2f)", "`a`b!1 2f", False),
        ("`a`b!1 2f", "`a`b!([]x:1 2f)", False),
        # plain numeric dict (`exec avg price by sym`): same rule
        ("`a`b!1 2f", "`a`b!1 2f+1e-11", True),
        ("`a`b!1 2f", "`a`c!1 2f", False),
        ("`a`b!1 2f", "`a`b!1 3f", False),
    ],
)
def test_cmp_answers_dict_shapes_without_signalling(kxq, a, b, want):
    """Testing only ``98h=type value`` took a dict of a table for a keyed table
    and signalled 'type on ``0!``; the same cases as the server copy's test."""
    assert _b(kxq(f".eval.cmp[{a}; {b}]")) is want


def test_dict_of_table_is_described_as_a_dict(kxq):
    assert kxq(".eval.descVal[`a`b!([]x:1 2)]").py() == b"dict (2 entries)"


@pytest.mark.parametrize(
    "a,b",
    [("`a`b!1 2f", "`a`b!1 2f+1e-11"), ("`a`b!([]x:1 2f)", "`a`b!([]x:1 2f+1e-11)")],
)
def test_wrapinfo_and_diag_survive_plain_dicts(kxq, a, b):
    """``meta`` raises 'type on a plain dict, which wrapInfo used to call on
    any 99h; diag's unkey and flatNumeric took the same wrong test."""
    kxq(f".eval.wrapInfo[{a}; {b}]")
    kxq(f".eval.diag[{a}; `a`b!1 9f]")
