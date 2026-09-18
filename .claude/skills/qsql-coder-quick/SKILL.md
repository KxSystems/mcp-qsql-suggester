---
name: qsql-coder-quick
description: Quick finance qSQL draft via the fine-tuned qwen specialist (kxqsql MCP) — the MCP's own parse + KX qlint C9 + multi-seed execution gates only, with no Bash-verify against a reference and no rewrite fallback. Use whenever the task is to write or fix a single straightforward finance kdb+/q table query over market or trading data (trade, quote, order-book, execution tables) — typically one table and one filter or one aggregate — and the request has NO as-of or time-window semantics (no "as of", "prevailing", "per N minutes", "bars", "rolling", "moving", "cumulative") and supplies no formula, q snippet or worked example. If it has any of those, use qsql-coder (full mode) instead. Do NOT use for non-finance data, for generic q (IPC, .z handlers, file I/O, system commands), or for multi-step workflows.
user-invocable: true
---

# Draft qSQL via the qwen specialist (quick mode)

Quick variant of `/qsql-coder`. Delegate a single qSQL task
to the `kxqsql` MCP tool, then **directly present qwen's draft
without any Bash-verify or rewrite**. If no self-consistency cluster
survives gating (`passed=false`), tell the user there is no answer —
quick mode does not attempt a fallback rewrite. For a verified draft,
use the full `/qsql-coder` skill instead.

## Preconditions — check both before anything else

### The `kxqsql` MCP tool must exist

If `mcp__kxqsql__generate_qsql` is not among your available tools, the server
is not registered for this project. **Stop and report — do not work around
it.** Three specific non-workarounds, because each is tempting: do not attempt
the call anyway (a call to a tool that does not exist yields a harness error
that says nothing about the cause), do not substitute q you wrote yourself (a
silent substitution hides which model answered, and this skill exists to
present the specialist's draft), and do not run the formatter (no case
describes this situation). Reply in plain text:

> The `kxqsql` MCP server is not registered in this project, so I cannot reach
> the qSQL specialist. Add it to a project-scope `.mcp.json` — see the
> `mcp-qsql-suggester` README § "Point your client at your server" — and then
> **restart Claude Code**: MCP config is read once at startup, so editing
> `.mcp.json` mid-session has no effect.

Then stop. This is the one path that produces no formatter output at all.

### Only one of the two skills runs per instruction

`qsql-coder-quick` and `qsql-coder` call the same MCP tool with the same
arguments, so running both samples the model twice for one question. If
`qsql-coder` has already run on this instruction in this conversation, do
**not** run: its draft stands, and it is the more thoroughly checked of the
two. If you now think quick mode was the right call, that is not a reason to
re-run — quick mode adds nothing full mode did not already do.

## Gather parameters

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `instruction` | string | yes | — | Single-task qSQL description. Not a multi-step workflow; not non-q work (IPC, file I/O, system commands). |
| `table_schemas` | dict of name→q schema string | **yes** | — | e.g. `{"trade":"\`time\`sym\`price\`size!\`p\`s\`f\`j"}`. The MCP tool refuses calls without it — qwen was trained with schemas in context. |
| `max_rounds` | integer | no | the server's configured round count (e.g. 3 for 4→8→16) | Ceiling on self-consistency rounds this call may use. |

If `instruction` is missing or vague, ask the user.

### When the user did NOT provide a schema

**Step 0 — look for the real schema before drafting one.** An invented schema
is the quiet failure mode. The column names will match (you wrote both), so the
candidates run and agree — but on a dummy table whose types and value ranges
need not resemble the user's data, which is exactly the input a semantic defect
hides in. A wrong *name* fails loudly (`passed=false`); a wrong *type or
distribution* fails silently, and quick mode has no reference check that would
catch it. So spend one search first:

1. Anything the user already pasted in this conversation — `meta` output, a
   `([] ...)` table definition, a DDL snippet — is authoritative. Use it and
   skip the rest.
2. Otherwise search the project for a definition of each table the instruction
   names:

   ```bash
   grep -rnE '^[[:space:]]*(trade|quote)[[:space:]]*:[[:space:]]*\(\[' \
       --include='*.q' --include='*.k' . | head -20
   ```

   Substitute the actual table names. Also worth a look: any schema or loader
   `.q` file, and `meta` / `cols` output checked into the repo.
3. Only if that finds nothing, pick one of the two options below.

Whichever route produced the schema — found or drafted — it still reaches the
user in the `Schemas used` block, so a wrong guess is visible in the output
rather than buried in the call.

The MCP refuses calls without `table_schemas`; you cannot silently
infer one. Pick one of:

1. **Draft for the user**: identify every table the instruction touches
   (single-table is common; joins / lookups / enrichments often need
   2+ — signals: `join`, `lj`, `aj`, `wj`, "lookup against", "enrich
   with", "match against"), draft one entry per table in q notation,
   then present the full dict for confirmation:
   > *"I'll use: `` {"trade":"`time`sym`price`size!`p`s`f`j"} ``.
   > Confirm or correct."*
2. **Ask the user to provide**: request all the schemas in q notation
   (`{name: "\`col1\`col2!\`type1\`type2"}`) and wait.

Common q type chars: `p` (timestamp ns), `t` (time ms), `d` (date),
`s` (sym), `f` (float64), `j` (long), `i` (int), `b` (bool), `c` (char),
`g` (guid), `n` (timespan), `h` (short), `e` (real), `m` (month),
`u` (minute), `v` (second), `z` (datetime), `x` (byte).

Do NOT call the MCP until every required schema has arrived (or been
confirmed by the user after you drafted it).

## Execute

Call the MCP tool `mcp__kxqsql__generate_qsql` exactly once with the
gathered parameters plus `include_candidates=false` (quick mode reads
even less of the return than full mode — no per-candidate detail is ever
needed here either), then branch on the structured return.

**No Bash-verify. No rewrite.** That is the entire point of quick
mode. Do NOT read pre-derive.md. Do NOT consult the high-risk idiom
list. Do NOT compute a reference. Whatever the MCP returns is what you
present.

## Output

### When `passed=true` — direct present

Pipe a case-A JSON payload into the shared formatter via a
single-quoted heredoc. Single quotes around the delimiter disable
shell expansion so q code's backticks (`` `time ``, `` `sym ``, …)
survive verbatim.

The formatter is `qsql-coder`'s, not this skill's — one renderer
serves both, so the path crosses into the sibling skill's directory.
`${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}` resolves in
both of this skill's homes: the `mcp-qsql-suggester` repo (project
scope) and an installed `mcp-qsql-suggester` plugin, which bundles
both skills and so still has the sibling alongside. Do not shorten it
to a bare relative path — that assumes the caller's cwd is the repo.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}/.claude/skills/qsql-coder/scripts/format_output.py" <<'__QSQL_FORMAT_END__'
{
  "instruction_interpreted": "<one or two sentences>",
  "schemas": {"trade": "`time`sym`price`size!`p`s`f`j"},

  "case": "A",
  "qwen_code": "<q code from MCP>",
  "passed": true,
  "rounds_used": 1,
  "candidates_generated": 4,
  "winning_cluster_size": 4,
  "lint_gate": "kx_qlint_c9",

  "reference_priority": null,
  "reference_source_label": null,
  "verify_shape": null,
  "divergence_symptom": null,
  "suggested_code": null,
  "final_issues": null,
  "opus_could_write": null,
  "basis_label": null,

  "verification_unavailable": "quick_mode",

  "idiom_note": "Treat `passed=true` as 'ran without raising on the dummy table', not 'matches intended semantics'. For a draft verified against an independent reference, use `/qsql-coder`."
}
__QSQL_FORMAT_END__
```

The stdout IS the response — relay it verbatim to the user.

Free-text slots that you author: `instruction_interpreted`. The `idiom_note`
line shown above is the fixed quick-mode disclaimer — use it verbatim.

`verification_unavailable: "quick_mode"` is **not optional**, and it is the
half of the warning that cannot be dropped. Without it the formatter prints
case A's full-mode reason clause — *"No verification triggered (no high-risk
idiom, no functional form, ...)"* — which is a claim quick mode never
established: it does not consult the high-risk idiom list at all, so a `wavg`
draft would be described as having no high-risk idiom. With the field the
verdict reads *"Not verified — quick mode does not verify against a
reference"*. The `idiom_note` then says what that means for the reader; the
verdict says what actually happened.

Everything else (`###` headers, verdict skeleton, schema bullets, code fences)
is script-rendered.

### When `passed=false` — no answer

Quick mode does not write a fallback. Render **case D** through the same
formatter, with `opus_could_write=false` — nobody wrote an alternative, which
is exactly what that field says:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}/.claude/skills/qsql-coder/scripts/format_output.py" <<'__QSQL_FORMAT_END__'
{
  "instruction_interpreted": "<one or two sentences>",
  "schemas": {"trade": "`time`sym`price`size!`p`s`f`j"},

  "case": "D",
  "qwen_code": "<q_code from the first-generated candidate>",
  "passed": false,
  "rounds_used": 3,
  "candidates_generated": 16,
  "winning_cluster_size": 0,
  "final_issues": ["<verbatim from the MCP return>"],
  "opus_could_write": false,

  "verification_unavailable": "quick_mode",

  "idiom_note": null
}
__QSQL_FORMAT_END__
```

The stdout IS the response — relay it verbatim. Substitute `qwen_code`,
`rounds_used`, `candidates_generated` and `final_issues` from the MCP return;
`final_issues` must be a JSON list, copied verbatim rather than summarised.

`verification_unavailable` is what makes case D correct here. Without it the
verdict reads *"I could not confidently write an alternative — task is
outside my q knowledge or the instruction is genuinely ambiguous"*, which
blames the user's instruction for a choice this skill made: quick mode did
not try. With it, the verdict says quick mode does not verify against a
reference and points at `/qsql-coder`.

**This block used to be hand-rendered Markdown**, for exactly that reason —
case D painted the situation as "Opus couldn't write". It no longer does, so
the exception is gone and the formatter is the single source of truth for
every case in both skills. Do not reintroduce a hand-rendered variant.

## Notes

- `passed=true` from the MCP means "the q ran without raising on the
  dummy table", NOT "the q computes the intended answer". Quick mode
  skips the reference-verify step that catches semantic-intent failures
  (e.g. `wavg[size;price] % sum size` double-divides — runs cleanly,
  returns wrong value). If semantic correctness matters, use
  `/qsql-coder` (full mode).
