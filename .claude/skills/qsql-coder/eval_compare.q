/ Skill-side q helpers: relational compare and divergence diagnostic.
/ Concatenated (via plain `cat`) into ad-hoc q scripts by:
/   - .claude/skills/qsql-coder/SKILL.md (Bash-verify reference compare + Case C diagnostic)
/ Depends on `.eval.descVal` from eval_describe.q — load that file
/ BEFORE this one (otherwise `.eval.diag` will fail with 'undef).
/ Scalar-safe throughout: nested $[99h=type a; ...] avoids evaluating
/ `value a` on non-dict inputs (q's `and` doesn't short-circuit).
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.
/ DIVERGENCE FROM mcp_server/eval_compare.q, deliberate as of the
/ temporal-normalization change: this copy treats two temporal columns on
/ the same resolution chain as equal (see .eval.tNorm below); that copy
/ does not. The two answer different questions. Clustering asks "did
/ independent samples agree?", and two samples returning different types
/ genuinely did not agree — that disagreement is the signal that raises
/ winning_cluster_fraction past the skill's verify trigger. Bash-verify
/ asks "does qwen's result match my reference's intent?", where a
/ resolution difference is presentation. Do not "re-sync" the two by
/ copying this into mcp_server/: doing so merges the disagreeing clusters,
/ removes the trigger, and lets shortest-code pick the coarser form with
/ no note to the user.

/ ---------------------------------------------------------------------
/ Temporal resolution normalization (skill-side only -- see the note at
/ the top of mcp_server/eval_compare.q for why that copy deliberately
/ does NOT have this).
/ --
/ q's temporal point types are NOT one ordering, they are two chains that
/ share a head. `p` and `z` carry a date AND a time-of-day, so they sit
/ at the head of both; `d`/`m` carry a date and no time; `t`/`v`/`u` carry
/ a time-of-day and no date. The two branches are mutually incomparable
/ and q says so by raising: `date$09:05 and `minute$2024.06.01 are both
/ 'type. Verified against the full 7x7 cast matrix: every downcast WITHIN
/ a chain is legal, so the chain test rejects exactly the pairs q would
/ refuse and no error trapping is needed.
/ --
/ Rank is COARSENESS -- higher is coarser -- so the finer side is the one
/ cast down. That means the coarser answer wins the comparison: a `u`
/ bucket and a `p` bucket compare equal, and the caller is handed the `u`
/ one. Accepted deliberately, for two reasons. On the dummy table `p` is
/ single-day by construction (mcp_server/type_gen.q generates
/ 2024.06.01D09:00 + 0..10h), so the two really are a 1:1 relabelling of
/ the same buckets -- recognizing an isomorphism, not relaxing anything.
/ And the defect this could hide -- `5 xbar time.minute` merging rows
/ across days -- does not hide: on multi-day input the merge changes the
/ row count and the aggregate columns, which .eval.eqEps still rejects.
/ The signal lives in the aggregates, not in the key.
/ --
/ `n` (timespan) is deliberately absent: it is a duration, not a point in
/ time, so comparing it against a `p` is a category error rather than a
/ resolution difference.
/ --
/ This makes temporal columns consistent with numeric ones, which have
/ ignored the type spelling since epsilon tolerance went in (`j` volume
/ vs `f` volume already compares equal). The asymmetry that remains is
/ that numeric normalizes UP (both to float, lossless) and temporal
/ normalizes DOWN (lossy).
.eval.tChar:12 13 14 15 17 18 19h!"pmdzuvt";   / type short -> cast char
/ One dict per chain, mapping type short -> coarseness rank. Reads as
/ finest-to-coarsest p > z > t > v > u (time-of-day) and p > z > d > m (dated).
.eval.tRank:((12 15 19 18 17h)!0 1 2 3 4; (12 15 14 13h)!0 1 2 3);

/ Cast char of the coarser of two temporal types on a shared chain, or
/ " " when the pair is not normalizable (same type -- nothing to do;
/ different chains; either side non-temporal).
.eval.tTarget:{[ta;tb]
  a:abs ta; b:abs tb;
  if[a=b; :" "];
  hit:{[g;x;y] (x in key g) and y in key g}[;a;b] each .eval.tRank;
  if[not any hit; :" "];
  g:.eval.tRank[first where hit];
  :.eval.tChar $[g[a]>g[b]; a; b] };

/ (a;b) with both sides cast to their common coarser temporal type, or
/ unchanged when they are not normalizable. Casting BOTH is deliberate:
/ the coarse side's cast is a no-op, and it keeps the caller from having
/ to work out which side moved.
.eval.tNorm:{[a;b]
  c:.eval.tTarget[type a; type b];
  $[" "=c; (a;b); (c$a; c$b)] };
/ ---------------------------------------------------------------------

/ Tolerant equality. Handles column reorder, renamed columns when
/ their ORDER + values match exactly, keyed-vs-unkeyed wrap (auto-
/ unkeys the keyed side when the other side is unkeyed), and float-
/ precision noise on numeric columns.
/ Numeric tolerance uses .eval.eqEps with tol=1e-9 relative: aggregate-
/ order float drift (typical 1e-13..1e-15 on sums of thousands of
/ floats) is treated as equal; real bugs (off-by-one, raw-sum vs wavg,
/ wrong column) produce diffs >> tol*|values| so still flagged.
/ Non-numeric types (sym, time, char, bool, etc) remain byte-strict —
/ epsilon is meaningless for them.
.eval.cmp:{[a;b]
  if[a~b; :1b];
  if[(99h=type a) and 98h=type b; if[98h=type value a; :.eval.cmp[0!a; b]]];
  if[(99h=type b) and 98h=type a; if[98h=type value b; :.eval.cmp[a; 0!b]]];
  / Numeric first, THEN the type guard -- the order .eval.eqEps has always
  / used. Previously the guard came first here, so a `j` list vs an `f`
  / list returned 0b at top level while the identical pair inside a table
  / compared equal (the table path delegates straight to .eval.eqEps).
  / Same values, two answers, depending only on whether they arrived
  / wrapped in a table.
  if[((abs type a) in 5 6 7 8 9h) and (abs type b) in 5 6 7 8 9h;
    :.eval.eqEps[a; b; 1e-9]];
  if[not (type a)=type b;
    nab:.eval.tNorm[a; b];
    if[not nab~(a; b); :.eval.cmp[nab 0; nab 1]];
    :0b];
  if[98h=type a;
    :($[(asc cols a)~asc cols b; .eval.eqEps[a; (cols a) xcols b; 1e-9]; 0b])
    |.eval.eqEps[value flip a; value flip b; 1e-9]];
  if[99h=type a; if[98h=type value a; :.eval.cmp[0!a; 0!b]]];
  0b };

/ Element-wise / value-wise equality with relative epsilon tolerance
/ for numeric atoms and lists. Caller passes shape-aligned inputs
/ (.eval.cmp normalizes keyed-vs-unkeyed wrap + column reorder before
/ delegating here). Recurses into general lists (type 0h) element-wise
/ and into tables / keyed tables column-wise so the same function
/ handles all containers uniformly.
/ Threshold: |af-bf| <= tol * max(1, |af|, |bf|) — relative for values
/ >= 1, absolute floor `tol` for tiny values (treats anything < tol as
/ approximately zero, OK for our domain where meaningful aggregate
/ outputs are much larger than tol).
/ Null handling: aligned-null vectors are caught by the outer `~`
/ fast path; mixed null patterns (one side has null where the other
/ doesn't) fall through to 0b — same conservative behavior as the
/ pre-epsilon `.eval.cmp`. Not a regression vs the byte-strict baseline.
.eval.eqEps:{[a;b;tol]
  if[a~b; :1b];
  ta:type a; tb:type b;
  if[0h=ta;
    if[ta<>tb; :0b];
    if[(count a)<>count b; :0b];
    :all .eval.eqEps[;;tol] ./: flip (a; b)];
  if[((abs ta) in 5 6 7 8 9h) and (abs tb) in 5 6 7 8 9h;
    af:`float$a; bf:`float$b;
    if[(count af)<>count bf; :0b];
    denom: 1 | (abs af) | abs bf;
    :all (abs af-bf) <= tol*denom];
  if[ta<>tb;
    nab:.eval.tNorm[a; b];
    if[not nab~(a; b); :.eval.eqEps[nab 0; nab 1; tol]];
    :0b];
  if[98h=ta;
    :all .eval.eqEps[;;tol] ./: flip (value flip a; value flip b)];
  if[99h=ta; if[98h=type value a; :.eval.eqEps[0!a; 0!b; tol]]];
  0b };

/ Flatten any q value into a typed-float list of numeric atoms. Lets
/ diag compute min/max/ratio without branching per shape. Handles
/ keyed table -> unkey -> numeric cols; table -> numeric cols;
/ numeric list/atom -> cast to float; non-numeric -> empty.
.eval.flatNumeric:{[v]
  if[98h=type v; :raze {`float$x} each v[exec c from meta[v] where t in "hijef"]];
  if[99h=type v; if[98h=type value v; :.eval.flatNumeric 0!v]];
  if[(type v) in 5 6 7 8 9h; :`float$v];
  if[(neg type v) in 5 6 7 8 9h; :enlist `float$v];
  `float$() };

/ Wrap-difference inspection aid, the strict=0b/soft=1b counterpart of
/ .eval.diag. B-wrap requires an idiom_note naming which axis differs, and
/ that requirement is what produces an invented explanation when the axis is
/ not in front of the caller -- so print it.
/ --
/ `meta` is the thing that actually shows it: it lists KEY columns as well as
/ value ones, and its `t` column is where a temporal-resolution difference
/ appears (the 5-minute-bar incident printed "suj" against "spj"). Note that
/ .eval.descVal alone is NOT enough here: .eval.fmtCols formats only a keyed
/ table's value columns, so on that incident -- where the differing column
/ `bucket` was a key -- both sides printed the identical string.
/ --
/ But `meta` raises 'type on anything that is not a table, and strict=0b with
/ soft=1b is genuinely reachable for plain atoms -- measured, three ways:
/ `1.0` vs `1.0000000001` (q's own `~` tolerates about 1e-13, this epsilon
/ 1e-9, so the band between them lands here), `1` vs `1f`, and `09:05` vs a
/ `p`. So non-tables fall back to descVal.
.eval.wrapInfo:{[a;b]
  -1 "=== WRAP DIFFERENCE (values agree, presentation differs) ===";
  tbl:all (type each (a;b)) in 98 99h;
  if[tbl; -1 "  qwen meta:"; show meta a; -1 "  reference meta:"; show meta b];
  if[not tbl; -1 "  qwen     : ",.eval.descVal a; -1 "  reference: ",.eval.descVal b];
  };

/ Divergence diagnostic. Caller invokes only when ~ AND .eval.cmp
/ both return 0b (a real semantic divergence, not a shape-wrap
/ difference). -1 prints to stdout; :(::) is q's "return null"
/ early-exit. Heuristic ratio bands are tuned for the qSQL failure
/ classes observed in /qsql-coder Case C reports.
.eval.diag:{[a;b]
  -1 "=== DIVERGENCE DETECTED ===";
  if[(10h=type a) and (4<=count a) and "ERR:"~4#a; -1 "  qwen raised: ",a; :(::)];
  if[(10h=type b) and (4<=count b) and "ERR:"~4#b; -1 "  reference raised: ",b; :(::)];
  -1 "  qwen   shape: ",.eval.descVal a;
  -1 "  reference shape: ",.eval.descVal b;
  if[(type a)<>type b; -1 "  -> shape-class mismatch; ratio skipped"; :(::)];
  ua: $[99h=type a; $[98h=type value a; 0!a; a]; a];
  ub: $[99h=type b; $[98h=type value b; 0!b; b]; b];
  if[(98h=type ua) and (count ua)<>count ub;
    -1 "  -> row-count differs (qwen=",(string count ua),", reference=",(string count ub),") -- likely missing/extra `where` or wrong join direction"];
  if[98h=type ua;
    ca:cols ua; cb:cols ub;
    onlyA:ca except cb; onlyB:cb except ca;
    if[(count onlyA) or count onlyB;
      -1 "  -> column-set differs:";
      if[count onlyA; -1 "      only in qwen:   ",", " sv string onlyA];
      if[count onlyB; -1 "      only in reference: ",", " sv string onlyB]]];
  na:.eval.flatNumeric a;
  nb:.eval.flatNumeric b;
  if[0=count na; -1 "  (no numeric values in qwen; ratio skipped)"; :(::)];
  if[0=count nb; -1 "  (no numeric values in reference; ratio skipped)"; :(::)];
  -1 "  qwen   numeric range: [",(string min na),", ",(string max na),"]";
  -1 "  reference numeric range: [",(string min nb),", ",(string max nb),"]";
  hnA:any null na; hnB:any null nb;
  if[hnA<>hnB;
    -1 "  -> null propagation: qwen-has-0n=",(string hnA),", reference-has-0n=",(string hnB)," -- likely 0%0 or empty group"];
  qm:max abs na;
  om:max abs nb;
  if[(0=om) or null om; -1 "  -> reference max-abs = 0/null; ratio skipped"; :(::)];
  r:qm%om;
  -1 "  magnitude ratio (qwen_max_abs / reference_max_abs) = ",string r;
  -1 "  hints:";
  if[(r>=10) and r<=1e4;
    -1 "    - ratio in [10, 1e4] -> raw sum vs weighted avg likely (qwen may have forgotten to divide by sum of weights)"];
  if[(r>=1e-4) and r<=0.1;
    -1 "    - ratio in [1e-4, 0.1] -> inverse: reference uses raw sum, qwen uses weighted avg"];
  if[(abs r-1)<0.01;
    -1 "    - ratio ~1x but values differ -> off-by-one / wrong column / extra cast"];
  if[(0=qm) and not 0=om;
    -1 "    - qwen max-abs = 0 (reference nonzero) -> likely empty filter on qwen side"];
  };
