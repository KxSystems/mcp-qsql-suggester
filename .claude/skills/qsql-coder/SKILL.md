---
name: qsql-coder
description: Draft qSQL code via the fine-tuned qwen specialist (kxqsql MCP) with parse + KX qlint + runtime-exec gates against a dummy table. Use when the user says "qsql idiom", "draft qsql via qwen", "use the qsql specialist", or invokes /qsql-coder. Opt-in only — do not auto-invoke on every qSQL question.
user-invocable: true
---

# Draft qSQL via the qwen specialist

Delegate a single qSQL generation task to the `kxqsql` MCP tool
(qwen fine-tune served by local vLLM). The MCP draws several independent
candidates per call at a non-zero temperature in growing rounds (e.g.
4 -> 8 -> 16), gates each on raw q `parse` + KX qlint C9, executes valid
candidates against a dummy table (built from the supplied `table_schemas`)
across multiple seeds, and clusters candidates by cross-seed execution-
equivalence — returning the shortest-code candidate from the largest
(winning) cluster. Present the drafted q code, the verdict, and an
optional one-sentence idiom note.

## Gather parameters

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `instruction` | string | yes | — | Single-task qSQL description. Not a multi-step workflow; not non-q work (IPC, file I/O, system commands). |
| `table_schemas` | dict of name→q schema string | **yes** | — | e.g. `{"trade":"\`time\`sym\`price\`size!\`p\`s\`f\`j"}`. The MCP tool refuses calls without it — qwen was trained with schemas in context. |
| `max_rounds` | integer | no | the server's configured round count (e.g. 3 for 4→8→16) | Ceiling on self-consistency rounds this call may use. Requesting fewer is always honored; requesting more is clamped to the server's `MCP_QSQL_SC_ROUND_CAPS`. |

If `instruction` is missing or vague, ask the user.

### When the user did NOT provide a schema

The MCP tool rejects calls without `table_schemas`, so you cannot
silently call it with an empty schema. Do NOT silently infer a schema
either — `instruction` text often names columns but rarely specifies q
types unambiguously. Instead:

1. Ask the user a yes/no question:
   > *"You haven't provided a table schema. Want me to draft one based
   > on your instruction (and confirm with you before calling qwen), or
   > would you rather provide the schema yourself in q notation
   > (e.g. `{name: "\`col1\`col2!\`type1\`type2"}`)?"*
2. If the user says **"draft for me" (or yes)**: **identify every table
   the instruction references** (single-table tasks are common, but
   joins / lookups / enrichments often need 2+ tables — signals: `join`,
   `lj`, `aj`, `wj`, "lookup against", "enrich with", "match against",
   "cross-reference"). Write one schema entry per table, then present
   the full dict for confirmation. Single-table example:
   > *"I'll use: `` {"trade":"`time`sym`price`size!`p`s`f`j"} ``.
   > Confirm or correct."*
   Multi-table example (join):
   > *"I'll use: `` {"trade":"`time`sym`tradeId`price`size!`p`s`j`f`j", "quote":"`time`sym`bid`ask!`p`s`f`f"} ``.
   > Confirm or correct each entry."*
   Only call the MCP after the user confirms (or supplies corrected
   versions).
3. If the user says **"I'll provide" (or no)**: ask for **all** the
   schemas the task touches in q notation (one entry per table) and
   wait. Do NOT call the MCP until every required schema has arrived.

Common q type chars: `p` (timestamp ns), `t` (time ms), `d` (date),
`s` (sym), `f` (float64), `j` (long), `i` (int), `b` (bool), `c` (char),
`g` (guid), `n` (timespan), `h` (short), `e` (real), `m` (month),
`u` (minute), `v` (second), `z` (datetime), `x` (byte).

## Execute

Call the MCP tool `mcp__kxqsql__generate_qsql` with the gathered
parameters plus `include_candidates=false` — every field this skill reads
below (`q_code`, `passed`, `lint_gate`, `final_issues`, `rounds_used`,
`candidates_generated`, `winning_cluster_size`, `winning_cluster_fraction`,
`tie_occurred`) is at the top level of the return; the per-candidate
`candidates` list is debugging/audit detail this skill never reads, so
omitting it saves tokens on this side. Then branch on the structured
return.

The MCP's `passed=true` means the q_code **ran** without raising — it
does NOT mean the q_code computes the user's intended answer. A common
class of failure (e.g. `wavg` applied AND then divided by `sum size`
again, double-applying weighted average) parses cleanly, lints cleanly,
and executes without error — yet returns the wrong value. Semantic
intent has to be checked separately by Bash-verify against a reference
(see "Bash-verify protocol" below).

### When `passed=true` — decide whether to Bash-verify

| Signal in the q_code or return | Action |
|---|---|
| Uses a **high-risk named idiom**: `wavg`, `wsum`, `mavg`, `msum`, `mmax`/`mmin`/`mcount`/`mdev`/`mmedian`, `fby`, `xexp`, `prds`, `sums`/`sum` mixed with `wavg`-family | **Bash-verify** (these idioms condense multiple ops into one call; easy for qwen to over-apply or combine with redundant arithmetic) |
| Functional form `![...]` or `?[...]` with nested parse-tree aggregates | **Bash-verify** (parse-tree composition is fragile) |
| `winning_cluster_fraction < 1.0` (not every candidate agreed) or `tie_occurred=true` | **Bash-verify** — qwen was uncertain / split across answers, verify against an independent reference |
| **User provided a q snippet / canonical form / formula** in the prompt | **Bash-verify** — even when no other trigger fires. The priority-1 reference is free (no need to draft), and catching divergence is high-value. A reference you don't use is a reference wasted. |
| None of the above | **Trust the gate.** No verification needed. Present q_code + verdict directly. |

### When `passed=false` — Opus rewrites, then Bash-verifies

No self-consistency cluster survived gating — every candidate across every
round either failed parse/lint/exec or produced a degenerate result;
`final_issues` explains why the first-generated candidate specifically
failed. Do not ship qwen's broken draft as the answer.

1. **Write your own independent q version** from the instruction +
   schema. If the instruction contains an explicit formula, literal-
   translate it (priority 2 in the reference priority system); otherwise use
   your own qSQL knowledge. Do **not** patch qwen's draft — the value of this
   step is that it is an independent reference, and a fix derived from the
   draft inherits the draft's framing.

   One exception to that independence, and only one: if `final_issues` names a
   **qlint rule** (e.g. `GLOBAL_PEACH`, `INVALID_ESCAPE`), that is a q-level
   constraint rather than a mistake specific to qwen — avoid the same
   construct in your own version. Reading a rule name is not the same as
   patching the draft.
2. **Bash-verify your version** against a dummy table matching the
   schema. Check it executes without error and the output shape looks
   reasonable (correct columns, correct row count given the grouping).
3. Present per outcome:
   - **Your version Bash-verifies cleanly** → present yours as the
     primary answer, with qwen's failed draft + `final_issues` shown as
     a secondary "what qwen tried" note.
   - **Your version also fails / is inconclusive** → present both as
     "uncertain", surface the actual q errors verbatim from both
     attempts, and ask the user to refine the instruction or schema.
   - **Task is outside your q knowledge / genuinely ambiguous** → admit
     it. Do not fabricate. Show qwen's failed draft + errors and
     recommend the user provide a worked example or refine.

## Probing q during write

When writing q yourself (rewrite path on `passed=false`, or drafting
the reference via priority 2/3), you may probe q via
`echo '<expr>' | q -q` to confirm an idiom before committing.

**You ALREADY know** (do NOT probe): standard `select` / `update` /
`exec` / `delete` with `where` / `by`, sorts (`asc` / `desc` / `xasc` /
`xdesc`), type casts (`` `long$ ``, `` `float$ ``, `` `symbol$ ``,
`` `timestamp$ ``), common aggs (`count` / `distinct` / `sum` / `avg` /
`first` / `last` / `min` / `max` / `prev` / `next`), `enlist` / `raze` /
`ungroup`, basic `lj` / 3-arg `` aj[`sym`time; t; q] ``.

**You MAY probe these** (established failure classes):

- **Functional `?[t;w;b;c]` / `![t;w;b;c]`**: `b` and `c` are **dicts**,
  not lists. `` (enlist `name)!enlist (op;...) ``, not
  `` enlist (op;`a;`b) ``. The parse-tree expression `(op;a;b)` is data,
  not a function call.
- **`n xbar v` arg order + type-pairing**: `0D01` for `p` (ns),
  `00:01:00` for `t` (ms), integer minutes for `u`. Literal `60` on a
  `p` column buckets by 60 nanoseconds.
- **`wavg` / `wsum` arg order**: `weight wavg value`. `wavg[size;price]`
  already computes `sum[price*size] % sum size` — do **not** divide
  again.
- **`fby` shape**: `(agg; col) fby key` — different from `by`.
- **Adverbs in functional / prior context**: `':`, `\:`, `/:`,
  `differ`, each-prior.
- **Time-literal × column-type beyond `xbar`**: `0D...` (timespan) vs
  `t`-type (ms) compares/casts; `p` arithmetic with `n`.

**Budget**: 2–3 probes per invocation. More → escalate to the user;
the instruction or schema is probably ambiguous. Probing confirms
**idiom correctness**, not **result correctness** — post-write
Bash-verify against the reference is still required.

## Bash-verify protocol

The protocol compares **runtime results**, not code text. The q
expression we compare qwen's draft against is the **reference**. Pick
one by priority order (confidence decreases 1 → 3):

1. **User-provided q code** in the prompt — anything labelled
   "reference", "canonical", "expected", "conventional", "equivalent
   to ...", or shown as a worked-example q snippet. Use verbatim;
   confidence: **high**.

   **Strict reading of "user-provided":** the q code must appear in
   the **user's original message** — not in the `instruction`
   ARGUMENT you (Opus) pass to this skill. If you translated the
   user's natural-language phrase ("price-weighted volume") into q
   (`price wavg volume`) as part of your pre-call cleanup and
   embedded it in the skill ARGUMENTS or in a "Reference idiom:"
   label inside your own invocation, that is **priority 3** (your
   own translation), not priority 1. Self-injected q code is yours,
   regardless of how you labelled it on the way in.
2. **A formula explicit in the instruction** (e.g. *"VWAP =
   sum(price*size)/sum(size)"*). **Literal-translate** the formula to
   q — do NOT rewrite into a more idiomatic form (e.g. do NOT collapse
   it back into `wavg`); literal translation breaks the "both share the
   same misapplication" failure mode. Confidence: **high**.
3. **Your own independent translation** of the instruction in q.
   Confidence: **medium** — qwen and you may share idiomatic
   assumptions; hedge in the verdict (*"verified against my own
   translation, which may share assumptions with qwen's output"*).

Throughout this skill, bare *reference* means "whichever priority is
in play" (generic); qualifiers like *user-provided* / *priority-1* /
*priority-3* narrow to a specific one.

Compare results (not code) against the same dummy data. **Write the q
to a temp file (`/tmp/verify_*.q`) and run
`q /tmp/verify_*.q -q </dev/null`** — do NOT pipe via
`echo '...' | q -q` or `q -q <<EOF` heredoc. Reason: q code is full of
backtick-prefixed symbols (`` `sym ``, `` `time ``, etc.); on many
shells the backticks inside `echo` / heredoc trigger command
substitution and get stripped before reaching q, producing
`'( error or silently malformed code. The temp-file approach
sidesteps shell quoting entirely. The trailing **`</dev/null`** is
load-bearing: after running the file, q tries to enter the REPL and
read from stdin; without `</dev/null` (and without a trailing
`exit 0;` in the script), q hangs waiting for input until the Bash
tool's 2-min timeout fires. Don't use `q -q < /tmp/verify_*.q`
(stdin-redirect, no file arg) either — that mode parses each line as a
top-level expression and breaks multi-line function definitions.

Build `/tmp/verify.q` in two passes: pass 1 concatenates three skill-
local `.q` files — `verify_gen.q` (gives `NROWS0`, `typeGen`,
`parseSchema0`, `genTable0`), `eval_describe.q` (`.eval.fmtCols` +
`.eval.descVal`), `eval_compare.q` (`.eval.cmp` + `.eval.flatNumeric`
+ `.eval.diag`). Pass 2 appends the call-specific verification via
single-quoted heredoc so the backticks in q syms survive shell
quoting.

Resolve those three files through `$SKILL_ROOT` as defined below —
never through a bare relative path, and never through
`git rev-parse` alone. This skill has two homes: the
`mcp-qsql-suggester` repo, where it is a project-scope skill under
`.claude/skills/`, and an installed `mcp-qsql-suggester` plugin, where
the harness sets `CLAUDE_PLUGIN_ROOT` to the plugin's own root. The
`${CLAUDE_PLUGIN_ROOT:-...}` fallback resolves correctly in both, and
the plugin layout keeps `.claude/skills/` so the suffix is the same
either way. A bare relative path assumes the caller's cwd is the repo,
and `git rev-parse` on its own silently resolves to *the user's* repo
when this runs as a plugin in someone else's project — the `cat` then
fails with an error that says nothing about paths. Each Bash call gets
a fresh shell, so every block below defines the root it needs rather
than relying on an earlier one.

```bash
SKILL_ROOT="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}/.claude/skills/qsql-coder"
{
  cat "$SKILL_ROOT/verify_gen.q"
  cat "$SKILL_ROOT/eval_describe.q"
  cat "$SKILL_ROOT/eval_compare.q"
} > /tmp/verify.q
cat >> /tmp/verify.q <<'PYEOF'
system "S 42";
/ build dummy table(s) — one genTable0 line per entry in table_schemas
trade: genTable0[parseSchema0["`time`sym`price`size!`t`s`f`j"]; NROWS0];
qwen_result: <qwen q_code>;
reference_result: <reference q code from priority 1/2/3>;
strict: qwen_result ~ reference_result;
soft:   .eval.cmp[qwen_result; reference_result];
show `strict`soft!(strict; soft);        / 11b = clean match; 01b = shape-wrap only; 00b = real divergence
if[(not strict) and not soft; .eval.diag[qwen_result; reference_result]];
exit 0;
PYEOF
q /tmp/verify.q -q </dev/null
```

The two-pass split is deliberate: pass 1 needs `$SKILL_ROOT`
interpolation (unquoted) for the three `cat` arguments; pass 2 needs
backtick protection (single-quoted heredoc) for q syms in the per-call
code. Building both into one quoted heredoc would block `$SKILL_ROOT`
interpolation; unquoted would let backticks trigger command
substitution. The `{ cat; cat; cat; } > file` pattern sidesteps both.
After pass 2, `/tmp/verify.q` is a single q script that q parses
end-to-end; semantically identical to using `\l` plus inline code.

Note: load order matters — `.eval.diag` (in `eval_compare.q`) calls
`.eval.descVal` (in `eval_describe.q`), so `eval_describe.q` must
precede `eval_compare.q` in the `cat` sequence above.

Verify dummy is tuned for grouped-query rigor (1000 rows, 3 syms
``` `AAPL`MSFT`GOOG ```, 2h timestamp span — each (sym, 5-min bucket)
cell carries ~14 rows on average; enough density for `wavg` / `avg` /
`wsum` divergences to surface). Gate-3 dummy (`mcp_server/type_gen.q`,
20 rows / 10 syms / 10h) is deliberately different — its purpose is
fast runtime safety on the server, not semantic-divergence detection
on the skill side. The two are intentionally decoupled.

### Layered compare: `strict` vs `soft`

The template emits a 2-bit verdict `` `strict`soft!(...) `` because the
two compares answer different questions:

- **`strict` = `qwen_result ~ reference_result`** — q's exact deep-equality.
  Strict on column order, keyed-vs-unkeyed wrap, type spelling.
- **`soft` = `.eval.cmp[qwen_result; reference_result]`** — tolerant
  equality from `eval_compare.q`. Treats column reorder, the
  keyed/unkeyed wrap, and renamed-but-positionally-identical columns
  as equivalent. Implements the relation: same values, possibly
  different presentation.

Three meaningful outcomes (the fourth, `strict=1b, soft=0b`, is
impossible — strict implies soft):

| `strict` | `soft` | meaning | classify as |
|---|---|---|---|
| `1b` | `1b` | clean match, identical bytes | **Case B** (no idiom note) |
| `0b` | `1b` | values agree but wrap differs (col reorder / keyed wrap / col rename) | **Case B + idiom note** explaining the wrap difference |
| `0b` | `0b` | real semantic divergence | **Case C** — `.eval.diag` fires automatically |

`.eval.diag` only fires on the third outcome (both `0b`). It prints
both shapes, numeric ranges, magnitude ratio, and heuristic hints
pointing at the likely failure class (raw sum vs weighted avg, off-
by-one, null propagation, empty filter, column rename, etc). Use the
diag output to write `divergence_symptom` in the case-C JSON payload
— don't hand-inspect both results manually.

When `strict=0b, soft=1b`, the values match but the presentation
differs. This is **not** a Case C, but it is worth a one-line
`idiom_note` because the user may have expected a specific wrap (e.g.
keyed table when they ran `select ... by sym`). Inspect both results
manually to identify which axis differs (col order, key/unkey, col
name), then write the idiom note. Diag does NOT run in this branch;
its job is divergence diagnosis, not wrap diagnosis.

**Divergence = same input, different output.** Both qwen and the
reference must run on **identical dummy input** in the same .q file. If they
produce different output on that shared input, that's divergence
(Case C). If qwen's code might fail on **different inputs** the user
did not ask about (multi-date when task is single-date; empty groups;
type variants) — that's **fragility**, not divergence. Do NOT
construct adversarial dummy data designed to expose fragility; the
dummy should mimic the data the user actually has. If you don't know
the shape, default to the sample dummy above without adversarial
tweaks. Fragility observations belong in `**Idiom note**:` (with
evidence the user's data may exhibit that shape — don't speculate),
not in verdict downgrade.

## Outputs

The skill's response is **rendered by a Python formatter, not hand-written**.
You decide which case fired and fill the free-text slots; the script handles
all headers, verdict skeletons, hedge clauses, and bullet formatting. Single
source of truth lives in `.claude/skills/qsql-coder/scripts/format_output.py`.

### Decide which case fired

Two preamble blocks always render first: `### Instruction interpreted as`
(your one or two clean sentences — light-cleanup the user's instruction
preserving intent; pass the cleaned version as the MCP `instruction`
argument too, since qwen was trained on clean instructions) and `### Schemas
used` (the `table_schemas` you passed to the MCP call, shown once at the
top).

Then pick the case from the MCP return + your verify outcome:

| case | when |
|---|---|
| **A** | `passed=true`; no Bash-verify triggered (no high-risk idiom, no functional form, unanimous self-consistency cluster, no user reference) |
| **B** | `passed=true`; Bash-verified; **`soft=1b`** (qwen's values match the reference). Two sub-cases:<br>• **B-clean** — `strict=1b`: identical bytes; `idiom_note` optional.<br>• **B-wrap** — `strict=0b, soft=1b`: values agree but col-order / keyed-wrap / col-name differ; `idiom_note` **required** to explain the wrap. |
| **C** | `passed=true`; Bash-verified; **`strict=0b` AND `soft=0b`** (real divergence). `.eval.diag` ran automatically — read its output to write `divergence_symptom`. |
| **D** | `passed=false`; no self-consistency cluster survived gating |

For B and C, pick `reference_priority`:

- **1** — user-provided q code in the **original message** (strict reading: not q you injected into ARGUMENTS or relabelled as "reference"). Confidence high.
- **2** — formula explicit in the instruction (e.g. *"VWAP = sum(price*size)/sum(size)"*), **literal-translated** to q. Confidence high.
- **3** — your own independent translation. Confidence medium; formatter auto-appends a hedge clause to the verdict.

**Case C pre-flight**: confirm qwen and the reference ran on **identical input**
in the verification `.q` file. If divergence emerged only after you changed
the dummy between runs, that's fragility on a different input shape, not
divergence on shared input — reclassify as case B (clean or B-wrap depending
on whether `soft=1b` on a shared-input rerun) and do NOT downgrade qwen's
verdict.

**Case D sub-case**: set `opus_could_write` true if you wrote and
Bash-verified an independent q version; false if the task is outside your
q knowledge or genuinely ambiguous (formatter then omits the SUGGESTED
CODE header and asks the user to refine).

### Render via formatter

Pipe the JSON payload directly into the formatter via a single-quoted
heredoc. Single quotes around the delimiter disable shell expansion, so
q code's backticks (`` `time ``, `` `sym ``, …) survive verbatim — same
protection the Bash-verify protocol relies on. The delimiter is
`__QSQL_FORMAT_END__` (more unique than `JSONEOF`) to avoid the unlikely
case that q code contains a line equal to the sentinel.

No temp file: heredoc goes straight to the script's stdin, so multiple
parallel invocations (e.g. spawning 5 agents to render 5 cases at once)
don't race on a shared path.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}/.claude/skills/qsql-coder/scripts/format_output.py" <<'__QSQL_FORMAT_END__'
{
  "instruction_interpreted": "<one or two sentences>",
  "schemas": {"trade": "`time`sym`price`size!`p`s`f`j"},

  "case": "A",                    // A | B | C | D
  "qwen_code": "<q code from MCP>",
  "passed": true,                 // true for A/B/C; false for D
  "rounds_used": 1,
  "candidates_generated": 4,
  "winning_cluster_size": 4,
  "lint_gate": "kx_qlint_c9",     // copy the MCP's field verbatim; when it is
                                  // "disabled" the verdict says so, because the
                                  // server had no usable qlint.q_ and filtered
                                  // on parse + execution only

  "reference_priority": null,        // null for A/D; 1|2|3 for B/C
  "reference_source_label": null,    // free text e.g. "the user-provided reference"
  "verify_shape": null,           // e.g. "20 rows x 3 cols"

  "divergence_symptom": null,     // one sentence — case C only
  "suggested_code": null,         // q code — case C, and case D when opus_could_write

  "final_issues": null,           // list[str] — case D only
  "opus_could_write": null,       // bool — case D only
  "basis_label": null,            // free text e.g. "the instruction plus schema" — case D1 only

  "idiom_note": null              // optional one sentence; null = omit line
}
__QSQL_FORMAT_END__
```

The stdout IS the response — relay it verbatim to the user.

Free-text slots that you author: `instruction_interpreted`,
`reference_source_label`, `divergence_symptom`, `basis_label`, `idiom_note`.
Everything else (`###` headers, verdict skeleton, priority-3 hedge,
schema bullets, code fences) is script-rendered.

`reference_source_label` must read grammatically after "Bash-verified against"
(case B) and "diverged from" (case C). Include the article: e.g. *"the
user-provided reference"*, *"the literal-translated formula from the
instruction"*, *"my own independent translation"*.

### Required-by-case fields

| case | required (in addition to base) |
|---|---|
| A | `qwen_code`, `passed=true`, `rounds_used`, `candidates_generated`, `winning_cluster_size`, `lint_gate` |
| B-clean (`strict=1b`) | + `reference_priority` ∈ {1,2,3}, `reference_source_label`, `verify_shape` |
| B-wrap (`strict=0b, soft=1b`) | + `reference_priority`, `reference_source_label`, `verify_shape`, **`idiom_note` (required)** — describe the wrap axis (col order / keyed wrap / col name) |
| C (both `0b`) | + `reference_priority`, `reference_source_label`, `verify_shape`, `divergence_symptom`, `suggested_code` |
| D | + `final_issues` (list), `opus_could_write` (bool); if `opus_could_write=true`: + `suggested_code`, `basis_label` |

### Locked rule: `suggested_code` must be verbatim verified code

`suggested_code` must be byte-for-byte the q you ran in the verify
`.q` file. No rewrites — even cosmetic ones can cross q eval-order
boundaries. Want a different shape? Re-verify it first.

### Locked rule: do not hand-render

If the formatter exits non-zero (validation error, invalid JSON), surface
the stderr message to the user and ask which field to correct. **Do NOT
fall back to writing the Markdown yourself** — the whole point is that
the script is single source of truth. Free-form fallback re-opens the
drift bug we just fixed.

### Convention summary

| Header | Source | Meaning |
|---|---|---|
| `TOOL CODE (suggested)` | qwen via MCP | Use this; no verification needed |
| `TOOL CODE (suggested, verified)` | qwen via MCP | Use this; Bash-verified |
| `TOOL CODE (not suggested)` | qwen via MCP | Don't use this; shown for context |
| `TOOL CODE (failed)` | qwen via MCP (no cluster survived gating) | Don't use; shown for transparency |
| `SUGGESTED CODE (Provided Reference)` | user (priority 1, q code from user's original message echoed back) | Use this — your provided reference matches expected result; qwen diverged from it |
| `SUGGESTED CODE (generated)` | you (priority 2 literal-formula / priority 3 own translation) | Use this — wrote and verified because qwen diverged |
| `SUGGESTED CODE (without MCP Tool)` | you | Use this — qwen failed entirely |

## Notes

- The runtime-exec gate's `passed=true` means "the q ran without
  raising", NOT "the q computes the intended answer". Semantic intent
  failures (e.g. wrong aggregate idiom, value off by an order of
  magnitude) escape every static and runtime gate; only Bash-verify
  against an independent reference catches them.
- **Developer-only A/B-test mode — "pre-derive" protocol** (not for
  customer use): there is a separate protocol in `pre-derive.md`,
  the file sitting next to this `SKILL.md`, that injects an
  `Expected result schema:` hint into qwen's prompt, aligning the MCP
  path with the prompt format used during model training/evaluation.
  It is **not** auto-loaded and **not** part of the customer flow.
  Apply it only when the developer explicitly requests pre-derive
  mode (e.g. "use pre-derive mode" / "read pre-derive.md and follow
  it"); then Read that file and follow its preconditions + Bash
  template.
- **Scope rule: robustness ≠ correctness.** This skill checks whether
  qwen's code computes the user's stated intent on their data — not
  whether it survives hypothetical inputs. If you observe a robustness
  concern, mention it under `**Idiom note**:` only with evidence the
  user's data exhibits that shape; otherwise omit. Speculating about
  unseen inputs is a known source of false Case C verdicts.
