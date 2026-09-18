# Pre-derive expected result schema — **DEVELOPER-ONLY**

> **This file is NOT loaded by the `/qsql-coder` skill in normal use, and
> it is not part of the customer flow.** It documents an optional protocol
> for measuring what the `expected_result_schema` hint is worth: derive the
> result shape from a user-supplied q reference *first*, pass it to the
> server, and compare against the same case run without it. Normal skill
> invocations never reach it — `SKILL.md`'s flow has no reference to this
> file, and Claude Code does not auto-load it. It ships with the skill
> because reproducing that comparison needs it; nothing here is required to
> use `/qsql-coder`.

## How a developer invokes this protocol

The developer's prompt to Claude Code must explicitly request it. The
skill itself does not auto-discover. A typical invocation looks like:

> "Before invoking /qsql-coder, read
> `.claude/skills/qsql-coder/pre-derive.md` and follow it —
> I've provided a priority-1 reference below."

Or, more concisely once the workflow is established:

> "Use pre-derive mode for this case."

## Preconditions (must all hold before applying this protocol)

- A **priority-1 reference** is available — user-provided q reference code
  in the **original message**. Strict reading: NOT q you (Opus)
  authored, NOT q from a "Reference idiom:" label you wrote, NOT
  priority-2 (formula literal-translated) or priority-3 (your own
  translation) substitutes.

### When the priority-1 reference is missing

The user signalled pre-derive but the original message has no
priority-1 reference (no verbatim q reference code, only an
instruction / formula / your own translation would apply). **STOP and
ask** before doing anything else — do not call the MCP yet, do not
fall back silently. Suggested phrasing:

> *"You requested pre-derive mode, but I don't see a priority-1 reference
> (verbatim q reference code) in your original message. Pre-derive
> needs the canonical q so I can run it on a dummy table and inject the
> result shape into qwen's prompt. Do you want to:
> (a) Provide the q code now, or
> (b) Proceed without pre-derive (normal skill flow, no shape hint)?"*

On the user's response:
- **(a) — q code arrives** → resume this protocol with the new reference.
- **(b) — skip** → abort this protocol; invoke the skill normally
  (call MCP without `expected_result_schema`); do **not** populate the
  formatter's `pre_derive_shape` field.

### When `q` is not on PATH

Different failure class — environmental and not user-fixable in the
moment. Abort this protocol silently, call MCP without
`expected_result_schema`, and add
`"pre-derive skipped: q not on PATH"` to the formatter's `idiom_note`
slot. Do NOT ask the user — the q binary isn't going to materialise
mid-conversation.

## Goal

Run the priority-1 reference on a dummy table to derive the output shape,
then pass the shape string to the MCP `generate_qsql` tool as the
`expected_result_schema` argument. This appends an
`Expected result schema: ...` line to qwen's user prompt — the same
hint format used during training/evaluation when canonical responses
are pre-passed for shape derivation. With the hint, the MCP path's
round-1 prompt matches that format; remaining behavioral difference
is the self-consistency clustering across rounds, which is what the
A/B experiment is designed to isolate.

## Pre-derive Bash

Build `/tmp/preshape.q` in two passes:

- **Pass 1** concatenates two files: (a) `mcp_server/type_gen.q` —
  gives `NROWS0`, `typeGen`, `parseSchema0`, `genTable0`,
  byte-identical to what the MCP runtime-exec gate (gate 3) loaded
  (`mcp_server/qexec.py` loads the same file via `_ensure_typegen`,
  so the dummy here and at gate 3 share the same bytes, which is
  critical because the shape hint pre-derive emits gets matched
  against gate-3's runtime result); (b)
  `.claude/skills/qsql-coder/eval_describe.q` — gives
  `.eval.fmtCols` + `.eval.descVal`. (Post-call Bash-verify in
  SKILL.md uses a **separate**, verify-tuned dummy at
  `.claude/skills/qsql-coder/verify_gen.q`; alignment is not
  required there because shape derivation is value-independent for
  standard q queries — both dummies share the same schema → meta
  mapping.)
- **Pass 2** appends `.eval.descSchema` (pre-derive-only helper),
  per-call `genTable0[...]` lines (one per `table_schemas` entry)
  and the priority-1 reference on `.eval.tmpRes:`. Uses single-quoted
  heredoc (`<<'PYEOF'`) so backtick-prefixed q syms like
  `` `time `` survive shell quoting verbatim.

`system "S 42"` + `NROWS0:20` (the latter inherited from pass-1
`type_gen.q`) match `mcp_server/qexec.py`'s `DEFAULT_NROWS` and the
first entry of `DEFAULT_SC_SEEDS` (`(42, 43, 44)` — self-consistency
clustering executes each candidate across all three seeds; 42 is the
one pre-derive shares). Splitting into two passes is deliberate: pass 1 needs
`$ROOT` interpolation (unquoted); pass 2 needs backtick protection
(single-quoted). Building both into one quoted heredoc would block
`$ROOT`; unquoted would let backticks trigger command substitution.

`$ROOT` is the **repo/plugin** root, not the skill directory — pass 1
reaches outside the skill into `mcp_server/type_gen.q`, so it cannot
use `SKILL.md`'s `$SKILL_ROOT`. Same two-homes reasoning as there
(`SKILL.md` § "Bash-verify protocol"): `CLAUDE_PLUGIN_ROOT` when this runs as an
installed plugin, `git rev-parse` when it runs project-scope inside
the repo. Both layouts keep `mcp_server/` and `.claude/skills/` at the
root, so one variable covers both trees.

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}"
{
  cat "$ROOT/mcp_server/type_gen.q"
  cat "$ROOT/.claude/skills/qsql-coder/eval_describe.q"
} > /tmp/preshape.q
cat >> /tmp/preshape.q <<'PYEOF'
/ q-schema-style shape (e.g. `time`sym`price!`n`s`f) for plain tables;
/ falls back to descVal format for keyed tables / scalars / lists.
/ Used purely for the formatter's dev-only display — does NOT enter qwen's prompt.
.eval.descSchema:{[r] $[
  98h=type r;
    "`",("`" sv string cols r),"!`",("`" sv string exec t from meta r);
  .eval.descVal[r]]};
system "S 42";
/ one genTable0 line per entry in table_schemas — fill in per call
trade: genTable0[parseSchema0["`time`sym`tradeId`price`size!`t`s`j`f`j"]; NROWS0];
alloc: genTable0[parseSchema0["`tradeId`allocAcct`allocQty!`j`s`j"]; NROWS0];
/ priority-1 reference from the user's original message — verbatim
.eval.tmpRes: trade ij `tradeId xkey alloc;
/ emit two lines: line 1 = descVal (goes to MCP), line 2 = descSchema (goes to formatter)
-1 .eval.descVal[.eval.tmpRes];
-1 .eval.descSchema[.eval.tmpRes];
exit 0;
PYEOF
# timeout -k: a spinning q ignores SIGTERM and -T does not bound a script file —
# SKILL.md § "Bash-verify protocol" has the measurements. GNU coreutils; drop it if absent.
mapfile -t LINES < <(timeout -k 5 60 q /tmp/preshape.q -q </dev/null 2>&1)
SHAPE="${LINES[0]}"           # descVal — for MCP's expected_result_schema (matches eval format)
DISPLAY_SHAPE="${LINES[1]}"   # descSchema — for formatter's pre_derive_shape (dev-friendly)
```

Both `.eval.descVal` and `.eval.descSchema` describe column names +
type chars only (driven by `meta`), so the shape strings are
data-independent — the seed/nrows alignment is paranoia for future
drift, not strict necessity. **Why two**: `descVal` (defined in
`.claude/skills/qsql-coder/eval_describe.q`, also concat-loaded by
SKILL.md's Bash-verify) mirrors the format the model was trained
against, so qwen sees a consistent hint string in MCP pre-derive
mode. `descSchema` uses the same q-schema notation as the input
`table_schemas` entries (`` `col!`type ``), purely for the
formatter's dev-only audit-trail display — never enters qwen's
prompt, so it doesn't perturb the A/B test. `descSchema` is defined
inline below (pre-derive-only helper, not shared).

## On pre-derive failure

If `q` exits non-zero, OR `$SHAPE` contains a q error sentinel
(e.g. starts with `'`), OR `$SHAPE` is empty: **do not** pass
`expected_result_schema` to MCP — call as if the flag were off, and
add `"pre-derive shape failed: <SHAPE>"` to the formatter's `idiom_note`
slot in the final output. The MCP path remains a valid fallback.
(Same goes if `$DISPLAY_SHAPE` is malformed — omit the formatter's
`pre_derive_shape` field rather than displaying garbage.)

## Wiring into the MCP call

When pre-derive succeeds, include the captured `$SHAPE` in the MCP
call's `expected_result_schema` argument:

```python
mcp__kxqsql__generate_qsql(
    instruction=...,
    table_schemas=...,
    expected_result_schema=SHAPE,
)
```

### Audit-trail wiring into `format_output.py`

When you subsequently call `scripts/format_output.py` (resolved as in
`SKILL.md` § "Outputs", via `${CLAUDE_PLUGIN_ROOT:-...}`) to
render the final response, pass `$DISPLAY_SHAPE` (the descSchema
output, q-schema notation) as the JSON payload's `pre_derive_shape`
field — NOT `$SHAPE` (descVal format, which is what MCP/qwen got):

```json
{
  "case": "...",
  "instruction_interpreted": "...",
  "schemas": {...},
  "pre_derive_shape": "<DISPLAY_SHAPE — descSchema output, e.g. `time`sym`price!`n`s`f>",
  ...
}
```

The formatter renders `**Pre-derive shape (dev-only)**: \`<shape>\``
in the preamble. Using the q-schema format keeps it visually
consistent with the `Schemas used` section above it. If pre-derive
aborted (user picked (b), q not on PATH, q failed) — **do not** set
`pre_derive_shape`; omit the field or pass `null`.

Reminder: `$SHAPE` (descVal format) is what gets injected into qwen's
prompt as `expected_result_schema`; `$DISPLAY_SHAPE` (descSchema
format) is purely for dev observability and never reaches qwen.

## After MCP returns

The **second** execution of the reference (for value-equality compare)
happens later in the SKILL.md "Bash-verify protocol" section. Both
runs use seed 42, but Bash-verify loads `verify_gen.q` (NROWS=1000,
3 syms, 2h span) instead of `type_gen.q` (NROWS=20, 10 syms, 10h) —
the verify-side dummy is tuned for grouped-query rigor (each
sym × 5-min bucket carries enough rows for `wavg` / `avg` /  `wsum`
divergences to surface). The decoupling is intentional: pre-derive
needs gate-3-aligned dummy bytes (shape hint must match runtime);
Bash-verify needs grouped-query density (semantic divergence must
be detectable). Pre-derive (here) captures shape; Bash-verify
(there) captures value.
