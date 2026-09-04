/ Skill-side q helpers: relational compare and divergence diagnostic.
/ Concatenated (via plain `cat`) into ad-hoc q scripts by:
/   - .claude/skills/qsql-coder/SKILL.md (Bash-verify reference compare + Case C diagnostic)
/ Depends on `.eval.descVal` from eval_describe.q — load that file
/ BEFORE this one (otherwise `.eval.diag` will fail with 'undef).
/ Scalar-safe throughout: nested $[99h=type a; ...] avoids evaluating
/ `value a` on non-dict inputs (q's `and` doesn't short-circuit).
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.

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
  if[not (type a)=type b; :0b];
  if[(abs type a) in 5 6 7 8 9h; :.eval.eqEps[a; b; 1e-9]];
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
  if[ta<>tb; :0b];
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
