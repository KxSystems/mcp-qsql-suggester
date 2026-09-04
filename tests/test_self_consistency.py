"""Unit tests for the pure decision logic in ``mcp_server.self_consistency``.

Covers round-cap preset validation, union-find, cluster formation, the
winning-cluster choice and its tie-break, shortest-code selection, and the
``ROUND_MIN_WEIGHT`` early-finalize thresholds.

``ClusterState``'s pykx-backed methods (``add_round``, ``_exec_new_uniques``,
``_results_equal_all_seeds``, ``cleanup``) are never called. The helpers
below build state directly, which is what keeps these tests runnable
without a kdb+ license. See ``tests/README.md``.
"""

from __future__ import annotations

import pytest

from mcp_server.self_consistency import (
    ROUND_MIN_WEIGHT,
    ClusterState,
    UniqueCandidate,
    UnionFind,
    parse_round_caps,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _state(seeds: tuple[int, ...] = (42, 43, 44)) -> ClusterState:
    """A ClusterState with no tables — safe because these tests only touch
    methods that read ``.uniques`` / ``._equal``."""
    return ClusterState(
        table_schemas={}, schema_names=set(), seeds=seeds, nrows=20, timeout_s=3,
    )


def _add(
    st: ClusterState,
    text: str,
    *,
    weight: int = 1,
    valid: bool = True,
    first_orig: int | None = None,
) -> int:
    """Append a unique candidate. ``weight`` is expressed by giving it that
    many ``orig_idxs`` (a unique's weight is how many raw completions
    collapsed onto its text); ``first_orig`` pins generation order, which is
    what both tie-breaks key on."""
    uniq_idx = len(st.uniques)
    start = len(st.candidates) if first_orig is None else first_orig
    orig_idxs = list(range(start, start + weight))
    st.uniques.append(UniqueCandidate(
        uniq_idx=uniq_idx, text=text, orig_idxs=orig_idxs,
        parse_lint_ok=valid, valid=valid,
    ))
    st.candidates.extend([None] * weight)  # only len() is ever read here
    return uniq_idx


def _equate(st: ClusterState, a: int, b: int, equal: bool = True) -> None:
    lo, hi = (a, b) if a < b else (b, a)
    st._equal[(lo, hi)] = equal


# --------------------------------------------------------------------------- #
# parse_round_caps
# --------------------------------------------------------------------------- #

def test_default_preset_parses():
    assert parse_round_caps("4,8,16") == (4, 8, 16)


def test_two_round_preset_parses():
    assert parse_round_caps("4,8") == (4, 8)


@pytest.mark.parametrize("spec,expected", [("16", (16,)), ("1", (1,)), ("64", (64,))])
def test_any_single_cap_is_allowed(spec, expected):
    """A single-element preset needs no early-finalize threshold — it always
    finalizes after its one round — so any N is legal."""
    assert parse_round_caps(spec) == expected


def test_whitespace_is_tolerated():
    assert parse_round_caps(" 4 , 8 , 16 ") == (4, 8, 16)


def test_trailing_comma_is_tolerated():
    assert parse_round_caps("4,8,") == (4, 8)


@pytest.mark.parametrize("spec", ["4,16", "8,16", "2,4,8", "4,8,16,32"])
def test_unvalidated_multi_round_preset_is_rejected(spec):
    """Multi-round specs are restricted to prefixes of (4, 8, 16) because
    those are the only caps ROUND_MIN_WEIGHT has a validated threshold for."""
    with pytest.raises(ValueError, match="unvalidated multi-round"):
        parse_round_caps(spec)


@pytest.mark.parametrize("spec", ["8,4", "16,8,4"])
def test_decreasing_caps_are_rejected(spec):
    with pytest.raises(ValueError, match="strictly increasing"):
        parse_round_caps(spec)


def test_repeated_caps_are_rejected():
    with pytest.raises(ValueError, match="strictly increasing"):
        parse_round_caps("4,4,8")


@pytest.mark.parametrize("spec", ["", "   ", ",", ",,"])
def test_empty_spec_is_rejected(spec):
    with pytest.raises(ValueError, match="positive integers"):
        parse_round_caps(spec)


@pytest.mark.parametrize("spec", ["0", "-4", "4,0"])
def test_non_positive_caps_are_rejected(spec):
    with pytest.raises(ValueError, match="positive integers"):
        parse_round_caps(spec)


@pytest.mark.parametrize("spec", ["four", "4,x", "4.5", "4;8"])
def test_non_integer_spec_is_rejected(spec):
    with pytest.raises(ValueError, match="comma-separated integers"):
        parse_round_caps(spec)


# --------------------------------------------------------------------------- #
# UnionFind
# --------------------------------------------------------------------------- #

def test_union_find_starts_fully_disjoint():
    uf = UnionFind(4)
    assert len({uf.find(i) for i in range(4)}) == 4


def test_union_find_merges_transitively():
    uf = UnionFind(4)
    uf.union(0, 1)
    uf.union(1, 2)
    assert uf.find(0) == uf.find(2)
    assert uf.find(3) != uf.find(0)


def test_union_find_is_idempotent():
    uf = UnionFind(3)
    uf.union(0, 1)
    root = uf.find(0)
    uf.union(0, 1)
    uf.union(1, 0)
    assert uf.find(0) == uf.find(1) == root


# --------------------------------------------------------------------------- #
# cluster formation
# --------------------------------------------------------------------------- #

def test_no_valid_candidates_yields_empty_winner():
    """Every candidate failed parse/lint/exec — the caller must see weight 0
    and no clusters, which is what drives ``passed=False``."""
    st = _state()
    _add(st, "select from t", valid=False)
    _add(st, "select from u", valid=False)
    w = st.winning_cluster()
    assert (w.root, w.members, w.weight, w.n_clusters, w.tie_occurred) == \
        (-1, [], 0, 0, False)


def test_invalid_candidates_are_excluded_from_clustering():
    """An invalid unique contributes nothing to any cluster's weight even if
    its text is identical in spirit to a valid one."""
    st = _state()
    a = _add(st, "select avg price by sym from t", weight=2)
    _add(st, "select avg price by sym from t2", weight=5, valid=False)
    w = st.winning_cluster()
    assert w.members == [a]
    assert w.weight == 2


def test_equivalent_uniques_merge_into_one_cluster():
    st = _state()
    a = _add(st, "select avg price by sym from t")
    b = _add(st, "select avg price by sym from t ")
    _equate(st, a, b)
    w = st.winning_cluster()
    assert w.n_clusters == 1
    assert sorted(w.members) == [a, b]
    assert w.weight == 2


def test_inequivalent_uniques_stay_separate():
    st = _state()
    a = _add(st, "select avg price by sym from t")
    b = _add(st, "select sum price by sym from t")
    _equate(st, a, b, equal=False)
    w = st.winning_cluster()
    assert w.n_clusters == 2
    assert w.weight == 1


def test_equivalence_is_transitive_across_a_cluster():
    """a==b and b==c puts c with a without a direct a==c comparison — the
    union-find is what saves those pairwise executions."""
    st = _state()
    a = _add(st, "aaa")
    b = _add(st, "bbb")
    c = _add(st, "ccc")
    _equate(st, a, b)
    _equate(st, b, c)
    w = st.winning_cluster()
    assert w.n_clusters == 1
    assert sorted(w.members) == [a, b, c]


def test_cluster_roots_maps_only_valid_uniques():
    st = _state()
    a = _add(st, "aaa")
    b = _add(st, "bbb")
    bad = _add(st, "ccc", valid=False)
    _equate(st, a, b)
    roots = st.cluster_roots()
    assert set(roots) == {a, b}
    assert roots[a] == roots[b]
    assert bad not in roots


# --------------------------------------------------------------------------- #
# winning cluster
# --------------------------------------------------------------------------- #

def test_majority_cluster_wins_over_minority():
    """The 3-1 split that ``smoke_test.scenario_clustering_picks_majority``
    exercises end-to-end, here without a model or embedded q."""
    st = _state()
    majority = [_add(st, f"maj{i}") for i in range(3)]
    _add(st, "minority")
    _equate(st, majority[0], majority[1])
    _equate(st, majority[1], majority[2])
    w = st.winning_cluster()
    assert w.weight == 3
    assert w.n_clusters == 2
    assert not w.tie_occurred
    assert sorted(w.members) == sorted(majority)


def test_weight_counts_duplicate_completions_not_unique_texts():
    """One unique text drawn 3 times outweighs two distinct texts drawn once
    each — dedup must not throw away the model's repetition signal."""
    st = _state()
    heavy = _add(st, "select avg price by sym from t", weight=3)
    light_a = _add(st, "select sum price by sym from t", weight=1)
    light_b = _add(st, "select max price by sym from t", weight=1)
    _equate(st, light_a, light_b)
    w = st.winning_cluster()
    assert w.members == [heavy]
    assert w.weight == 3


def test_tie_is_flagged_and_broken_by_earliest_generation():
    st = _state()
    first = _add(st, "generated first", first_orig=0)
    second = _add(st, "generated second", first_orig=1)
    _equate(st, first, second, equal=False)
    w = st.winning_cluster()
    assert w.tie_occurred
    assert w.weight == 1
    assert w.members == [first]


def test_tie_break_ignores_uniq_idx_order():
    """The tie-break keys on first generation index, not on the order the
    uniques happened to be created in."""
    st = _state()
    later = _add(st, "later", first_orig=7)
    earlier = _add(st, "earlier", first_orig=2)
    _equate(st, later, earlier, equal=False)
    w = st.winning_cluster()
    assert w.tie_occurred
    assert w.members == [earlier]


def test_no_tie_flag_when_one_cluster_leads():
    st = _state()
    a = _add(st, "aaa", weight=2)
    _add(st, "bbb", weight=1)
    w = st.winning_cluster()
    assert not w.tie_occurred
    assert w.members == [a]


def test_single_cluster_reports_one_cluster():
    st = _state()
    _add(st, "only", weight=4)
    w = st.winning_cluster()
    assert (w.n_clusters, w.weight, w.tie_occurred) == (1, 4, False)


# --------------------------------------------------------------------------- #
# pick_shortest
# --------------------------------------------------------------------------- #

def test_pick_shortest_returns_shortest_code():
    st = _state()
    long_ = _add(st, "select avg price by sym from trade where date=.z.d")
    short = _add(st, "select avg price by sym from trade")
    assert st.pick_shortest([long_, short]).uniq_idx == short


def test_pick_shortest_breaks_length_ties_by_generation_order():
    st = _state()
    later = _add(st, "select b from t", first_orig=5)
    earlier = _add(st, "select a from t", first_orig=1)
    assert len(st.uniques[later].text) == len(st.uniques[earlier].text)
    assert st.pick_shortest([later, earlier]).uniq_idx == earlier


def test_pick_shortest_on_single_member():
    st = _state()
    only = _add(st, "select from t")
    assert st.pick_shortest([only]).uniq_idx == only


# --------------------------------------------------------------------------- #
# early-finalize thresholds
# --------------------------------------------------------------------------- #

def test_round_min_weight_thresholds_are_the_validated_pair():
    """These two numbers come from the offline clustering analysis; changing
    them changes how often the server pays for another generation round."""
    assert ROUND_MIN_WEIGHT == {4: 4, 8: 6}


def test_round_of_4_needs_unanimous_agreement():
    st = _state()
    assert not st.meets_threshold(4, weight=3, is_last_round=False)
    assert st.meets_threshold(4, weight=4, is_last_round=False)


def test_round_of_8_needs_six():
    st = _state()
    assert not st.meets_threshold(8, weight=5, is_last_round=False)
    assert st.meets_threshold(8, weight=6, is_last_round=False)


def test_last_round_always_finalizes():
    """The 3-1 split in ``test_majority_cluster_wins_over_minority`` can't hit
    the round-1 threshold, but with max_rounds=1 there is nothing to fall
    through to, so it must finalize on the majority rather than bail out."""
    st = _state()
    assert st.meets_threshold(4, weight=1, is_last_round=True)
    assert st.meets_threshold(8, weight=0, is_last_round=True)


def test_cap_without_a_validated_threshold_is_terminal():
    """A cap not in ROUND_MIN_WEIGHT (e.g. the final 16, or a custom single
    cap) always finalizes rather than looping forever."""
    st = _state()
    assert st.meets_threshold(16, weight=1, is_last_round=False)
    assert st.meets_threshold(64, weight=1, is_last_round=False)


# --------------------------------------------------------------------------- #
# weight property
# --------------------------------------------------------------------------- #

def test_unique_candidate_weight_is_completion_count():
    u = UniqueCandidate(
        uniq_idx=0, text="select from t", orig_idxs=[0, 3, 7], parse_lint_ok=True,
    )
    assert u.weight == 3


def test_n_generated_counts_raw_completions():
    st = _state()
    _add(st, "aaa", weight=3)
    _add(st, "bbb", weight=1)
    assert st.n_generated == 4
