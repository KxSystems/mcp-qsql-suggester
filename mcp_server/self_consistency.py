"""Self-consistency clustering engine.

Replaces the old corrective-retry loop: instead of asking qwen to fix its
own mistakes across sequential attempts, we draw several *independent*
candidates at a non-zero temperature (via ``vllm_client.chat(..., n=...)``),
execute each against dummy tables built from ``table_schemas`` across
multiple seeds, and cluster candidates by cross-seed execution-equivalence
(``.eval.cmp``). The winning cluster (largest total weight) is treated as
"what the model actually converged on"; the shortest-code candidate within
it is returned as the answer.

Generation happens in growing rounds (round caps e.g. ``4 -> 8 -> 16``) so
that easy cases — where a handful of candidates already agree — don't pay
for the full N. See ``ROUND_MIN_WEIGHT`` for the empirically validated
early-finalize thresholds.

Every function here does synchronous, in-process pykx calls and MUST be
invoked from within ``mcp_server.loop``'s ``_run_pykx`` serialization —
embedded q is single-threaded and this module does no locking of its own.

Terminology, mirroring the offline clustering analysis this ports:
  - "candidate": one raw model completion, in generation order.
  - "unique" (a ``UniqueCandidate``): one distinct *extracted code text* —
    multiple candidates sharing byte-identical code collapse into one
    unique entry with a higher weight, so we don't re-execute duplicates.
  - "valid": a unique candidate whose code parsed, lint-passed, and
    executed without error/timeout/degenerate-result on EVERY seed tested.
    Invalid uniques are excluded from clustering entirely (same treatment
    whether the failure was a parse error, a lint issue, a runtime error,
    or a degenerate result type) — they still count toward the round's
    raw candidate total, just contribute nothing to any cluster's weight.
  - "cluster": a maximal set of mutually execution-equivalent (across ALL
    seeds) valid uniques, found via union-find.
"""

from __future__ import annotations

import dataclasses
import itertools

from mcp_qsql_shared.extract import extract_qsql

# NOTE: `_parse_safe` / `check_c9_qlint` / `qexec` are imported lazily inside
# the methods that use them (`add_round`, `_exec_new_uniques`,
# `_results_equal_all_seeds`, `cleanup`) rather than at module scope. All
# three pull in pykx, and importing them here would make the module's pure
# decision logic — `parse_round_caps`, `UnionFind`, `winning_cluster`,
# `pick_shortest`, `meets_threshold` — unimportable without embedded q,
# which puts `tests/` out of reach of any CI runner lacking a kdb+ license.
# Deferring also keeps `mcp_server.loop`'s PYKX_THREADING=1 preamble ahead
# of the first real pykx import no matter which module is imported first.


# Only these two round-cap values are empirically validated (see the
# clustering_analysis.py phase-0 offline analysis this ports): a round of 4
# needs unanimous agreement (weight>=4) to finalize early, a round of 8
# needs weight>=6. Any round cap NOT in this dict is treated as terminal
# (always finalizes) — which is also correct for the actual last round of
# any preset, since there's nothing left to fall through to.
ROUND_MIN_WEIGHT: dict[int, int] = {4: 4, 8: 6}

# The only round-cap sequences this module will run with validated
# thresholds. A single-element preset (any N) needs no threshold at all —
# it always finalizes after that one round. Multi-round presets are
# restricted to prefixes of (4, 8, 16) since those are the only cap values
# ROUND_MIN_WEIGHT has a validated answer for.
_VALID_MULTI_ROUND_PRESETS = ((4, 8), (4, 8, 16))


# Per-call isolation for the captured-result q globals. Under the HTTP
# transport one process serves every client, so two `generate_qsql` calls
# can be in flight at once: `mcp_server.loop._run_pykx` serializes each
# individual pykx operation, but it releases its lock across the awaited
# `vllm_client.chat(...)` in every round, so calls interleave at round
# boundaries. The captured results deliberately outlive a round (see
# `_exec_new_uniques`), which makes their names shared state:
#
#   - `_CALL_SEQ` prefixes every bound name with a process-unique call id,
#     so two concurrent calls cannot bind the same `sc...` global. Without
#     it both calls start their unique index at 0 and each silently
#     overwrites the other's captured results, so `.eval.cmp` compares the
#     wrong pair and the winning cluster can be wrong.
#   - `_LIVE_BINDINGS` is the set of names currently bound by any live
#     `ClusterState`. `qexec.cleanup_new_globals` deletes root-namespace
#     names that appeared since ITS OWN call's snapshot, which describes
#     another in-flight call's captured results exactly; `live_bindings()`
#     is what `mcp_server.loop` adds to that call's protect set.
#
# Only the captured results need this. Dummy tables never outlive the
# locked `add_round` that builds them, and the helper globals candidate
# code leaks are deleted as soon as that candidate's `exec_capture` returns
# (`qexec.isolated_candidate`), so another call never sees them — which is
# why the snapshot-diff in `cleanup_new_globals` is kept as-is rather than
# replaced. The per-call sweep in `mcp_server.loop` is now only a backstop.
_CALL_SEQ = itertools.count()
_LIVE_BINDINGS: set[str] = set()


def live_bindings() -> set[str]:
    """Captured-result globals currently bound by any live ``ClusterState``.

    Passed by ``mcp_server.loop`` into ``qexec.cleanup_new_globals``'s
    protect set so one call's cleanup cannot delete another's in-flight
    results. Called from within ``_run_pykx``, i.e. on the single pykx
    thread, so the plain ``set`` needs no lock of its own.
    """
    return set(_LIVE_BINDINGS)


def parse_round_caps(spec: str) -> tuple[int, ...]:
    """Parse + validate a comma-separated round-cap spec (e.g. ``"4,8,16"``).

    Returns the parsed tuple. Raises ``ValueError`` if the spec doesn't
    parse as positive, strictly-increasing integers, or (for multi-round
    specs) isn't one of the validated presets above.
    """
    try:
        caps = tuple(int(p.strip()) for p in spec.split(",") if p.strip())
    except ValueError:
        raise ValueError(f"round caps must be comma-separated integers, got {spec!r}")
    if not caps or any(c <= 0 for c in caps):
        raise ValueError(f"round caps must be positive integers, got {spec!r}")
    if len(caps) == 1:
        return caps
    if list(caps) != sorted(set(caps)):
        raise ValueError(f"round caps must be strictly increasing, got {spec!r}")
    if caps not in _VALID_MULTI_ROUND_PRESETS:
        raise ValueError(
            f"unvalidated multi-round cap sequence {caps!r} — only "
            f"{_VALID_MULTI_ROUND_PRESETS} have empirically-validated "
            f"early-finalize thresholds; use a single-cap spec (e.g. "
            f"'16') for a fixed-N run instead"
        )
    return caps


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclasses.dataclass
class Candidate:
    orig_idx: int
    raw: str
    code: str
    parse_ok: bool
    parse_err: str | None
    lint_issues: list[str]
    uniq_idx: int


@dataclasses.dataclass
class UniqueCandidate:
    uniq_idx: int
    text: str
    orig_idxs: list[int]
    parse_lint_ok: bool
    valid: bool = False
    exec_err: str | None = None
    degenerate_desc: str | None = None

    @property
    def weight(self) -> int:
        return len(self.orig_idxs)


@dataclasses.dataclass
class WinningCluster:
    root: int                   # uniq_idx acting as the cluster's union-find root
    members: list[int]          # uniq_idx values (dedup groups) in this cluster
    weight: int
    n_clusters: int
    tie_occurred: bool


class ClusterState:
    """Accumulates candidates + their pairwise execution-equivalence across
    rounds. Rounds only pay execution cost for NEWLY added unique texts —
    every seed's captured result is bound to a persistent q global for the
    lifetime of the case, so later rounds compare against it without
    re-executing it. Those globals are named per instance (``call_id``) and
    registered in ``_LIVE_BINDINGS`` so concurrent calls stay isolated — see
    the note at module scope.
    """

    def __init__(
        self,
        table_schemas: dict[str, str],
        schema_names: set[str],
        seeds: tuple[int, ...],
        nrows: int,
        timeout_s: int,
    ) -> None:
        self.table_schemas = table_schemas
        self.schema_names = schema_names
        self.seeds = seeds
        self.nrows = nrows
        self.timeout_s = timeout_s
        self.candidates: list[Candidate] = []
        self.uniques: list[UniqueCandidate] = []
        self._text_to_uniq: dict[str, int] = {}
        self._equal: dict[tuple[int, int], bool] = {}
        self._bound_vars: list[str] = []  # for cleanup
        # Process-unique, so concurrent calls can't collide on `sc...`
        # names — see the `_CALL_SEQ` note at module scope.
        self.call_id = next(_CALL_SEQ)

    # -- adding candidates -------------------------------------------------

    def add_round(self, raw_completions: list[str]) -> None:
        """Extract + parse + lint + dedupe a batch of raw completions, then
        execute (multi-seed) + cluster any genuinely-new unique texts."""
        from mcp_qsql_shared.qlint_common import _parse_safe
        from mcp_qsql_shared.qlint_kx import check_c9_qlint

        new_uniq_idxs: list[int] = []
        for raw in raw_completions:
            code = extract_qsql(raw)
            orig_idx = len(self.candidates)
            uniq_idx = self._text_to_uniq.get(code)
            if uniq_idx is None:
                parse_ok, parse_err = _parse_safe(code)
                # Lint runs even when parse fails. It cannot rescue the
                # candidate — `parse_lint_ok` below still ANDs in parse_ok — but
                # q's parse errors are famously terse (a 120-global function
                # reports the single word `globals`; a bad escape reports the
                # offending token and nothing else), whereas qlint names the
                # rule and often the fix ("Valid escape sequences are: ..."):
                # exactly the difference between a message a caller's model can
                # act on and one it cannot. Every error-level qlint rule we
                # could actually trigger also fails parse, so short-circuiting
                # here threw away the better message in precisely the cases
                # where it mattered most.
                lint = check_c9_qlint(code, self.schema_names, parse_failed=not parse_ok)
                lint_pass = bool(lint["pass"])
                lint_issues = list(lint["issues"])
                uniq_idx = len(self.uniques)
                self._text_to_uniq[code] = uniq_idx
                uniq = UniqueCandidate(
                    uniq_idx=uniq_idx,
                    text=code,
                    orig_idxs=[orig_idx],
                    parse_lint_ok=parse_ok and lint_pass,
                )
                if not parse_ok:
                    uniq.exec_err = "; ".join(
                        [f"q parse error: {parse_err}"] + lint_issues
                    )
                elif not lint_pass:
                    uniq.exec_err = "; ".join(lint_issues)
                self.uniques.append(uniq)
                new_uniq_idxs.append(uniq_idx)
                self.candidates.append(Candidate(
                    orig_idx=orig_idx, raw=raw, code=code,
                    parse_ok=parse_ok, parse_err=None if parse_ok else str(parse_err),
                    lint_issues=lint_issues, uniq_idx=uniq_idx,
                ))
            else:
                self.uniques[uniq_idx].orig_idxs.append(orig_idx)
                first = self.candidates[self.uniques[uniq_idx].orig_idxs[0]]
                self.candidates.append(Candidate(
                    orig_idx=orig_idx, raw=raw, code=code,
                    parse_ok=first.parse_ok, parse_err=first.parse_err,
                    lint_issues=first.lint_issues, uniq_idx=uniq_idx,
                ))

        execable = [u for u in new_uniq_idxs if self.uniques[u].parse_lint_ok]
        if execable:
            self._exec_new_uniques(execable)
        self._cluster_new_uniques(new_uniq_idxs)

    def _exec_new_uniques(self, uniq_idxs: list[int]) -> None:
        from mcp_server import qexec

        remaining = set(uniq_idxs)
        for seed in self.seeds:
            if not remaining:
                break
            try:
                table_names = qexec.build_dummy_tables(self.table_schemas, self.nrows, seed)
            except Exception as e:
                # `run_generate_loop` already preflight-checks table_schemas
                # against the first seed before self-consistency mode is
                # even entered, so this should be rare — but if a later
                # seed still fails for schema-dependent reasons, treat it
                # as a wash for THIS seed only (mirrors the offline
                # analysis's per-seed failure handling) rather than
                # crashing the whole in-flight round and losing every
                # candidate already generated.
                for u in remaining:
                    self.uniques[u].exec_err = f"dummy table build failed (seed={seed}): {e}"
                continue
            try:
                qexec.save_pristine(table_names)
                for u in list(remaining):
                    uniq = self.uniques[u]
                    var = f"sc{self.call_id}_{u}_s{seed}"
                    with qexec.isolated_candidate(table_names, var):
                        r = qexec.exec_capture(uniq.text, var, self.timeout_s)
                    if not r["ok"]:
                        uniq.exec_err = r["err"]
                        remaining.discard(u)
                        continue
                    self._bound_vars.append(var)
                    _LIVE_BINDINGS.add(var)
                    if qexec.is_degenerate_result(var):
                        uniq.degenerate_desc = qexec.describe_result(var)
                        remaining.discard(u)
            finally:
                qexec.drop_tables(table_names)
                qexec.drop_pristine()
        # Anything still in `remaining` after all seeds succeeded on every seed.
        for u in uniq_idxs:
            uniq = self.uniques[u]
            if uniq.exec_err is None and uniq.degenerate_desc is None:
                uniq.valid = True

    def _cluster_new_uniques(self, new_uniq_idxs: list[int]) -> None:
        new_valid = [u for u in new_uniq_idxs if self.uniques[u].valid]
        if not new_valid:
            return
        prior_valid = [
            u.uniq_idx for u in self.uniques
            if u.valid and u.uniq_idx not in new_valid
        ]
        for i in new_valid:
            for j in prior_valid + [x for x in new_valid if x < i]:
                lo, hi = (j, i) if j < i else (i, j)
                self._equal[(lo, hi)] = self._results_equal_all_seeds(lo, hi)

    def _results_equal_all_seeds(self, u_a: int, u_b: int) -> bool:
        from mcp_server import qexec

        for seed in self.seeds:
            va = f"sc{self.call_id}_{u_a}_s{seed}"
            vb = f"sc{self.call_id}_{u_b}_s{seed}"
            if not qexec.results_equal(va, vb):
                return False
        return True

    # -- reading state -------------------------------------------------

    @property
    def n_generated(self) -> int:
        return len(self.candidates)

    def _all_clusters(self) -> dict[int, list[int]]:
        """root uniq_idx -> member uniq_idx list, for every VALID unique
        candidate. Two uniques share a cluster iff they execute equal on
        every seed tested (directly or transitively) — this is the
        post-execution grouping, distinct from `UniqueCandidate.uniq_idx`
        (the pre-execution dedup-by-text group id)."""
        valid_idxs = [u.uniq_idx for u in self.uniques if u.valid]
        uf = UnionFind(len(self.uniques))
        for (i, j), eq in self._equal.items():
            if eq:
                uf.union(i, j)
        clusters: dict[int, list[int]] = {}
        for u in valid_idxs:
            clusters.setdefault(uf.find(u), []).append(u)
        return clusters

    def cluster_roots(self) -> dict[int, int]:
        """uniq_idx -> cluster root uniq_idx, for every valid unique
        candidate (absent if invalid — never joined any cluster)."""
        return {u: root for root, members in self._all_clusters().items() for u in members}

    def winning_cluster(self) -> WinningCluster:
        clusters = self._all_clusters()
        if not clusters:
            return WinningCluster(root=-1, members=[], weight=0, n_clusters=0, tie_occurred=False)

        weights = {root: sum(self.uniques[u].weight for u in members)
                   for root, members in clusters.items()}
        min_orig = {root: min(self.uniques[u].orig_idxs[0] for u in members)
                    for root, members in clusters.items()}
        max_weight = max(weights.values())
        tied_roots = [r for r, w in weights.items() if w == max_weight]
        tie_occurred = len(tied_roots) > 1
        winning_root = min(tied_roots, key=lambda r: min_orig[r])
        return WinningCluster(
            root=winning_root,
            members=clusters[winning_root],
            weight=max_weight,
            n_clusters=len(clusters),
            tie_occurred=tie_occurred,
        )

    def pick_shortest(self, members: list[int]) -> UniqueCandidate:
        """Shortest-code candidate within `members`, ties -> earliest generated."""
        return min(
            (self.uniques[u] for u in members),
            key=lambda u: (len(u.text), u.orig_idxs[0]),
        )

    def meets_threshold(self, cap: int, weight: int, is_last_round: bool) -> bool:
        if is_last_round:
            return True
        min_weight = ROUND_MIN_WEIGHT.get(cap)
        if min_weight is None:
            return True
        return weight >= min_weight

    def cleanup(self) -> None:
        from mcp_server import qexec

        qexec.drop_tables(self._bound_vars)
        _LIVE_BINDINGS.difference_update(self._bound_vars)
