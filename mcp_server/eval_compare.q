/ mcp_server's own copy of the relational-compare + shape-introspection q
/ helpers used by the self-consistency clustering engine
/ (mcp_server/self_consistency.py) to decide whether two candidates'
/ execution results are equivalent, and whether a result is degenerate
/ (a bare function / unexpected atom rather than a real query result).
/ Deliberately a standalone copy, NOT loaded from
/ .claude/skills/qsql-coder/eval_compare.q / eval_describe.q — those serve
/ the /qsql-coder skill's Bash-verify workflow (a separate, ad-hoc q
/ session). mcp_server is meant to be deployable on its own (see
/ CLAUDE.md's Prerequisites / self-hosting section), so it should not
/ depend on `.claude/skills/` being present on disk. Keep the two copies
/ in sync by hand if the comparison semantics change — see
/ .claude/skills/qsql-coder/eval_compare.q for the canonical rationale
/ behind each rule (epsilon tolerance, keyed/unkeyed normalization, etc).
/ Only the subset actually needed for clustering is kept here (no
/ `.eval.diag` / `.eval.flatNumeric` — those are Bash-verify-only
/ diagnostics, not used to decide cluster membership).
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.
/ THE SKILL COPY HAS DIVERGED ON PURPOSE. It normalizes temporal
/ resolution: two columns on the same chain (p/z/t/v/u, or p/z/d/m) are
/ compared after casting the finer down to the coarser, so a `u`
/ minute-of-day bucket and a `p` timestamp bucket count as equal. That
/ belongs there and NOT here. Clustering asks "did independent samples
/ agree?" — two candidates returning different types genuinely did not,
/ and keeping them in separate clusters is what holds
/ winning_cluster_fraction below the skill's Bash-verify trigger so the
/ difference gets looked at. Bash-verify asks the different question
/ "does qwen's result match my reference's intent?", where the same
/ difference is presentation. Porting .eval.tNorm into this file would
/ merge those clusters, remove the trigger, and let the shortest-code
/ pick return the coarser form with nothing said to the user.

.eval.fmtCols:{[x] c:cols x; t:exec t from meta x; ", " sv {(string x),"(",y,")"} ./: flip (c;t)};

/ A keyed table is a 99h whose key AND value are both tables. Testing the
/ value alone also matches a dict OF a table (`a`b!([]x:1 2), the shape
/ `exec ... by sym` returns), and then `keys` / `0!` on it signal 'rank /
/ 'type. Safe on any input: the $ keeps `key x` / `value x` off non-dicts,
/ which a flat `(99h=type x) and ...` would not (q's `and` doesn't
/ short-circuit).
.eval.isKeyed:{[x] $[99h=type x; (98h=type key x) and 98h=type value x; 0b]};

.eval.descVal:{[r] $[
  98h=type r; "table: ",.eval.fmtCols r;
  99h=type r; $[.eval.isKeyed r; "keyed table. keys: ",(", " sv string keys r),". cols: ",.eval.fmtCols value r; "dict (",(string count r)," entries)"];
  0>type r; "scalar ",string type r;
  "list type ",string type r]};

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
  if[.eval.isKeyed[a] and 98h=type b; :.eval.cmp[0!a; b]];
  if[.eval.isKeyed[b] and 98h=type a; :.eval.cmp[a; 0!b]];
  if[not (type a)=type b; :0b];
  if[(abs type a) in 5 6 7 8 9h; :.eval.eqEps[a; b; 1e-9]];
  if[98h=type a;
    :($[(asc cols a)~asc cols b; .eval.eqEps[a; (cols a) xcols b; 1e-9]; 0b])
    |.eval.eqEps[value flip a; value flip b; 1e-9]];
  / 99h: keyed tables unkey; a keyed table never equals a plain dict; a
  / plain dict (`exec avg price by sym`, `exec ... by sym` -> dict of a
  / table) compares key against key and value against value, so float
  / drift in its values is tolerated the same as in a table's columns.
  if[99h=type a;
    if[.eval.isKeyed[a] and .eval.isKeyed b; :.eval.cmp[0!a; 0!b]];
    if[.eval.isKeyed[a] or .eval.isKeyed b; :0b];
    :.eval.cmp[key a; key b] and .eval.cmp[value a; value b]];
  0b };

/ Element-wise / value-wise equality with relative epsilon tolerance
/ for numeric atoms and lists. Caller passes shape-aligned inputs
/ (.eval.cmp normalizes keyed-vs-unkeyed wrap + column reorder before
/ delegating here). Recurses into general lists (type 0h) element-wise
/ and into tables / keyed tables column-wise so the same function
/ handles all containers uniformly.
/ Threshold: |af-bf| <= tol * max(1, |af|, |bf|) — relative for values
/ >= 1, absolute floor `tol` for tiny values.
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
  if[99h=ta;
    if[.eval.isKeyed[a] and .eval.isKeyed b; :.eval.eqEps[0!a; 0!b; tol]];
    if[.eval.isKeyed[a] or .eval.isKeyed b; :0b];
    :.eval.eqEps[key a; key b; tol] and .eval.eqEps[value a; value b; tol]];
  0b };
