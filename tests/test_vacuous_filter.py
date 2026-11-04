"""Tier 2 — filters on values the default dummy pools do not contain.

The failure this guards: the type generators draw from fixed pools (dates in
2024, ten equity syms). A filter on a value outside them — a future date, an
unknown sym — matches no row on any seed, every candidate returns an empty
table, empty compares equal to empty, and the call reports a confident
unanimous pass on code nothing ever tested. Two fixes, tested separately:

- **literal rows** (``mcp_server/literal_pool.q``): constants harvested from
  the candidates' code are mixed into the dummy tables, so the filter selects
  some rows and semantically different candidates come apart;
- **vacuous results never agree** (``.eval.isVacuous``): whatever the data,
  an empty / all-null result does not join anyone's cluster, and the response
  says the winner was vacuous.

Every case is synthetic. Each "before" assertion runs with literal rows off
and shows the shape the pre-fix server produced, so the test bites.

    pytest -m "needs_q and not needs_qlint" -q tests/test_vacuous_filter.py
"""

from __future__ import annotations

import _tiers
import pytest


pytestmark = pytest.mark.needs_q

_unavailable = _tiers.pykx_reason()
if _unavailable:
    pytest.skip(f"tier 2: {_unavailable}", allow_module_level=True)


#: Two symbol columns, so a sym filter and an exchange filter share one pool.
TRADES = {"trades": "`date`time`sym`exch`price`qty!`d`p`s`s`f`j"}


def _fence(code: str) -> str:
    return f"```q\n{code}\n```"


@pytest.fixture
def scripted_chat(monkeypatch):
    """Per-round completions: round r returns ``rounds[r]`` cycled to ``n``."""
    from mcp_server import vllm_client

    def _install(rounds: list[list[str]]) -> None:
        state = {"r": 0}

        async def _chat(messages, *, temperature=0.2, top_p=0.95,
                        max_tokens=None, n=1):
            texts = rounds[min(state["r"], len(rounds) - 1)]
            state["r"] += 1
            return [_fence(texts[i % len(texts)]) for i in range(n)]

        monkeypatch.setattr(vllm_client, "chat", _chat)
    return _install


@pytest.fixture
def run(run_async, monkeypatch):
    """``run_generate_loop`` on ``TRADES`` with literal rows on or off."""
    from mcp_server import loop as qsql_loop

    def _run(*, literal_rows: int, max_rounds: int = 1, schemas=None) -> dict:
        monkeypatch.setattr(qsql_loop, "SC_LITERAL_ROWS", literal_rows)
        return run_async(qsql_loop.run_generate_loop(
            instruction="synthetic", table_schemas=schemas or TRADES,
            max_rounds=max_rounds,
        ))
    return _run


# Each filter names a value no default pool holds. `.z.d-1` is there because
# a clock-relative date is only reachable by constant-folding; `within` and
# `like` because the harvested value is a bound / a pattern, not the value.
OUT_OF_POOL_FILTERS = [
    "date=2030.01.15",
    "sym=`ZZZ",
    "date=.z.d-1",
    "date within 2030.01.10 2030.01.20",
    'sym like "ZZ*"',
    "date=2030.01.15, sym=`ZZZ, exch=`XQQ",
]


@pytest.mark.parametrize("flt", OUT_OF_POOL_FILTERS)
def test_semantically_different_candidates_split_on_literal_rows(
    scripted_chat, run, flt,
):
    """`sum price` vs `sum price*qty` under the same out-of-pool filter. They
    disagree on any data that has a matching row, so with literal rows they
    must land in different clusters on non-empty results."""
    a = f"select v:sum price by sym from trades where {flt}"
    b = f"select v:sum price*qty by sym from trades where {flt}"
    scripted_chat([[a, a, b, b]])

    after = run(literal_rows=100)
    assert after["passed"] is True
    assert after["n_clusters"] == 2
    assert after["winning_result_vacuous"] is False
    assert after["literals_harvested"] > 0

    # Before: the same two candidates on the default pools return empty
    # tables. The vacuous rule keeps them apart, and the response says so.
    before = run(literal_rows=0)
    assert before["passed"] is True
    assert before["winning_result_vacuous"] is True
    assert before["n_clusters"] == 2


@pytest.mark.parametrize("flt", OUT_OF_POOL_FILTERS)
def test_without_either_fix_the_two_candidates_agree(run_async, flt):
    """What the pre-fix server saw: on the default pools both candidates'
    results are empty on every seed AND `.eval.cmp`-equal — exactly the
    equality clustering used to read as agreement. Pins the premise of the
    test above, so a pool change that happened to reach one of these values
    cannot make it pass vacuously."""
    from mcp_server import qexec

    a = f"select v:sum price by sym from trades where {flt}"
    b = f"select v:sum price*qty by sym from trades where {flt}"
    for seed in (42, 43, 44):
        names = qexec.build_dummy_tables(TRADES, 20, seed)
        try:
            assert qexec.exec_capture(a, "vfA", 3)["ok"]
            assert qexec.exec_capture(b, "vfB", 3)["ok"]
            assert qexec.is_vacuous_result("vfA") and qexec.is_vacuous_result("vfB")
            assert qexec.results_equal("vfA", "vfB")
        finally:
            qexec.drop_tables(names + ["vfA", "vfB"])


def test_equal_candidates_still_cluster_on_literal_rows(scripted_chat, run):
    """The fix must not split genuine agreement: filtering on `date` and on
    `time.date` selects the same rows, because every literal row derives both
    columns from one anchor timestamp."""
    a = "select v:sum qty by sym from trades where date=2030.01.15"
    b = "select v:sum qty by sym from trades where time.date=2030.01.15"
    scripted_chat([[a, b]])
    result = run(literal_rows=100)
    assert result["n_clusters"] == 1
    assert result["winning_cluster_size"] == 4
    assert result["winning_result_vacuous"] is False


def test_always_empty_result_is_never_a_confident_pass(scripted_chat, run):
    """`where 0b` is empty whatever the data — no literal rows can help, so
    the vacuous rule is the only guard. Two different texts stay apart, the
    winner is flagged, and a round that would otherwise finalize on a tie of
    vacuous singletons keeps going."""
    a = "select from trades where 0b"
    b = "select price from trades where 0b"
    scripted_chat([[a, b]])
    result = run(literal_rows=100)
    assert result["passed"] is True
    assert result["winning_result_vacuous"] is True
    assert result["n_clusters"] == 2
    assert result["winning_cluster_size"] == 2  # identical texts: one unique, weight 2
    assert result["winning_cluster_fraction"] == 0.5
    assert all(c["vacuous"] for c in result["candidates"])


def test_vacuous_unanimity_does_not_finalize_round_one(scripted_chat, run):
    """Four identical empty-result completions meet the round-of-4 weight
    threshold, which used to end the call at 4/4 = 1.0. A vacuous winner must
    not finalize early."""
    scripted_chat([["select from trades where 0b"]])
    result = run(literal_rows=100, max_rounds=2)
    assert result["winning_result_vacuous"] is True
    assert result["rounds_used"] == 2
    assert result["candidates_generated"] == 8


def test_new_literal_in_a_later_round_re_executes_earlier_candidates(
    scripted_chat, run,
):
    """Round 2's candidate names 2030.01.15, which adds literal rows the
    round-1 tables did not have. Its filter is a tautology, so on the NEW
    tables it equals round 1's unfiltered sum. Clustering is only valid if
    round 1 was re-run on those tables too: compared against its stale
    20-row result, the two would split. The parse failure keeps round 1 short
    of the round-of-4 threshold, so round 2 happens."""
    r1 = "select v:sum qty from trades"
    r2 = "select v:sum qty from trades where (date=2030.01.15) or not date=2030.01.15"
    scripted_chat([[r1, r1, r1, "select from trades where ("], [r2]])
    result = run(literal_rows=100, max_rounds=2)
    assert result["rounds_used"] == 2
    assert result["n_clusters"] == 1
    assert result["winning_cluster_size"] == 7
    assert result["winning_result_vacuous"] is False


def test_no_literals_leaves_the_tables_byte_identical(run_async):
    """Code with no harvestable constant must see exactly the pre-fix
    tables, so every result that never depended on a literal is unchanged."""
    import pykx as kx
    from mcp_server import qexec

    lits = qexec.harvest_literals("select avg price by sym from trades")
    a = qexec.build_dummy_tables(TRADES, 20, 42)
    kx.q("vfT0:trades")
    qexec.drop_tables(a)
    b = qexec.build_dummy_tables(TRADES, 20, 42, literals=lits, literal_rows=100)
    try:
        assert bool(kx.q("vfT0~trades"))
    finally:
        qexec.drop_tables(b + ["vfT0"])


# --------------------------------------------------------------------------- #
# two tables: the literal rows must line the join keys up
# --------------------------------------------------------------------------- #

TRADE_QUOTE = {
    "trade": "`date`time`sym`price`size!`d`p`s`f`j",
    "quote": "`date`time`sym`bid`ask!`d`p`s`f`f",
}
_AJ = ("aj[`sym`time; select from trade where date=2030.01.15, sym=`ZZZ; "
       "select from quote where date=2030.01.15, sym=`ZZZ]")


def test_as_of_join_on_out_of_pool_values_finds_prevailing_quotes(run_async):
    """The premise of the next test. Literal rows are generated per table, so
    a join only works if both tables get rows on the same harvested sym and
    date, at interleaved times. Here a trade must find a prevailing quote:
    the joined `bid` is non-null on some rows of every seed. Without literal
    rows the selection is empty."""
    import pykx as kx
    from mcp_server import qexec

    lits = qexec.harvest_literals(f"select from {_AJ}")
    for seed in (42, 43, 44):
        names = qexec.build_dummy_tables(TRADE_QUOTE, 20, seed,
                                         literals=lits, literal_rows=100)
        try:
            assert qexec.exec_capture(f"exec bid from {_AJ}", "vfJ", 3)["ok"]
            assert int(kx.q("count vfJ").py()) > 0, seed
            assert int(kx.q("sum not null vfJ").py()) > 0, seed
        finally:
            qexec.drop_tables(names + ["vfJ"])

    names = qexec.build_dummy_tables(TRADE_QUOTE, 20, 42)
    try:
        assert qexec.exec_capture(f"exec bid from {_AJ}", "vfJ", 3)["ok"]
        assert qexec.is_vacuous_result("vfJ")
    finally:
        qexec.drop_tables(names + ["vfJ"])


def test_join_candidates_that_differ_only_after_the_join_split(scripted_chat, run):
    """`sum bid` vs `sum ask` of the joined rows differ only if the join
    actually matched quotes. Had the keys not lined up, both would be `sum`
    over nulls = 0 — a NON-null scalar, so not vacuous, and the two would
    agree for nothing. That is the residual the vacuous rule cannot see, so
    literal rows on both tables are what must prevent it."""
    a = f"select v:sum bid from {_AJ}"
    b = f"select v:sum ask from {_AJ}"
    scripted_chat([[a, a, b, b]])
    result = run(literal_rows=100, schemas=TRADE_QUOTE)
    assert result["passed"] is True
    assert result["n_clusters"] == 2
    assert result["winning_result_vacuous"] is False

    # Before: on the default pools the selection is empty, sum over nothing is
    # 0 for both, and they agree — the same false agreement, in its scalar form.
    before = run(literal_rows=0, schemas=TRADE_QUOTE)
    assert before["n_clusters"] == 1
    assert before["winning_cluster_size"] == 4


@pytest.mark.parametrize("template", [
    'system "touch {f}"',
    '.z.d+count system "touch {f}"',
    '.z.d+value "system \\"touch {f}\\""',
    '`$"x",string count system "touch {f}"',
])
def test_harvest_never_runs_unsafe_code(run_async, template, tmp_path):
    """Folding evaluates candidate sub-expressions, so it must only ever call
    the whitelisted pure primitives. The side effect is a file, not stdout,
    because embedded q's output does not reliably reach pytest's capture."""
    from mcp_server import qexec
    f = tmp_path / "pwned"
    qexec.harvest_literals(template.format(f=f))
    assert not f.exists()


@pytest.mark.parametrize("code", ["til 1000000000", "1000000000#1", ".z.d+til 1000000000"])
def test_harvest_refuses_huge_folds(run_async, code):
    """A fold whose size would exhaust memory is refused, not attempted."""
    from mcp_server import qexec
    assert qexec.count_literals(qexec.harvest_literals(code)) < 100
