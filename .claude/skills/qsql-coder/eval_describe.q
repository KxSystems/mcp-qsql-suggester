/ Skill-side q helpers: introspection over a single q value.
/ Concatenated (via plain `cat`) into ad-hoc q scripts by:
/   - .claude/skills/qsql-coder/SKILL.md       (Bash-verify; .eval.diag calls .eval.descVal)
/   - .claude/skills/qsql-coder/pre-derive.md  (shape hint going to MCP)
/ Pure introspection — single-value in, shape-string out. No relational
/ logic; for two-value compare/diagnostic see eval_compare.q.
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.

.eval.fmtCols:{[x] c:cols x; t:exec t from meta x; ", " sv {(string x),"(",y,")"} ./: flip (c;t)};

/ A keyed table is a 99h whose key AND value are both tables. Testing the
/ value alone also matches a dict OF a table (`a`b!([]x:1 2), the shape
/ `exec ... by sym` returns), and then `keys` / `0!` on it signal 'rank /
/ 'type. Safe on any input: the $ keeps `key x` / `value x` off non-dicts,
/ which a flat `(99h=type x) and ...` would not (q's `and` doesn't
/ short-circuit). eval_compare.q uses it too.
.eval.isKeyed:{[x] $[99h=type x; (98h=type key x) and 98h=type value x; 0b]};

.eval.descVal:{[r] $[
  98h=type r; "table: ",.eval.fmtCols r;
  99h=type r; $[.eval.isKeyed r; "keyed table. keys: ",(", " sv string keys r),". cols: ",.eval.fmtCols value r; "dict (",(string count r)," entries)"];
  0>type r; "scalar ",string type r;
  "list type ",string type r]};
