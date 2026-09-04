/ Skill-side q helpers: introspection over a single q value.
/ Concatenated (via plain `cat`) into ad-hoc q scripts by:
/   - .claude/skills/qsql-coder/SKILL.md       (Bash-verify; .eval.diag calls .eval.descVal)
/   - .claude/skills/qsql-coder/pre-derive.md  (shape hint going to MCP)
/ Pure introspection — single-value in, shape-string out. No relational
/ logic; for two-value compare/diagnostic see eval_compare.q.
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.

.eval.fmtCols:{[x] c:cols x; t:exec t from meta x; ", " sv {(string x),"(",y,")"} ./: flip (c;t)};

/ Nested $[99h=type r; $[98h=type value r; ...]] is deliberate. Flat
/ `(99h=type r) and 98h=type value r` evaluates `value r` even when r
/ is not 99h (q's `and` doesn't short-circuit), raising 'type on
/ scalars / lists. Same pattern is used in eval_compare.q.
.eval.descVal:{[r] $[
  98h=type r; "table: ",.eval.fmtCols r;
  99h=type r; $[98h=type value r; "keyed table. keys: ",(", " sv string keys r),". cols: ",.eval.fmtCols value r; "dict (",(string count r)," entries)"];
  0>type r; "scalar ",string type r;
  "list type ",string type r]};
