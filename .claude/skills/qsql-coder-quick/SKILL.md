---
name: qsql-coder-quick
description: Quick qSQL draft via the fine-tuned qwen specialist (kxqsql MCP) — direct adoption with no Bash-verify and no rewrite fallback. Use when the user says "qsql quick", "draft qsql quick", "use qsql specialist quick", or invokes /qsql-coder-quick. Opt-in only — do not auto-invoke on every qSQL question.
user-invocable: true
---

# Draft qSQL via the qwen specialist (quick mode)

Quick variant of `/qsql-coder`. Delegate a single qSQL task
to the `kxqsql` MCP tool, then **directly present qwen's draft
without any Bash-verify or rewrite**. If no self-consistency cluster
survives gating (`passed=false`), tell the user there is no answer —
quick mode does not attempt a fallback rewrite. For a verified draft,
use the full `/qsql-coder` skill instead.

## Gather parameters

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `instruction` | string | yes | — | Single-task qSQL description. Not a multi-step workflow; not non-q work (IPC, file I/O, system commands). |
| `table_schemas` | dict of name→q schema string | **yes** | — | e.g. `{"trade":"\`time\`sym\`price\`size!\`p\`s\`f\`j"}`. The MCP tool refuses calls without it — qwen was trained with schemas in context. |
| `max_rounds` | integer | no | the server's configured round count (e.g. 3 for 4→8→16) | Ceiling on self-consistency rounds this call may use. |

If `instruction` is missing or vague, ask the user.

### When the user did NOT provide a schema

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

  "idiom_note": "Quick mode — no Bash-verify against a reference. Treat `passed=true` as 'ran without raising', not 'matches intended semantics'. For a verified draft, use `/qsql-coder`."
}
__QSQL_FORMAT_END__
```

The stdout IS the response — relay it verbatim to the user.

Free-text slots that you author: `instruction_interpreted`. The
`idiom_note` line shown above is the fixed quick-mode disclaimer —
use it verbatim so users always see the "no reference verification"
warning. Everything else (`###` headers, verdict skeleton, schema
bullets, code fences) is script-rendered.

### When `passed=false` — no answer

Quick mode does not write a fallback. Render this fixed Markdown by
hand (the formatter's case D wrong-paints the situation as "Opus
couldn't write", which is misleading in quick mode):

```markdown
### TOOL CODE (failed)

\`\`\`q
<qwen's q_code from the first-generated candidate>
\`\`\`

**Verdict**: no self-consistency cluster survived gating (`passed=false`,
`rounds_used=R`, `candidates_generated=N`, `winning_cluster_size=0`,
`final_issues=[...]`). Quick mode does not attempt
a fallback rewrite — for a verified draft, use
`/qsql-coder` (full mode).
```

Substitute q_code, rounds_used, candidates_generated, and final_issues
from the MCP return. Do not add any other commentary.

## Notes

- `passed=true` from the MCP means "the q ran without raising on the
  dummy table", NOT "the q computes the intended answer". Quick mode
  skips the reference-verify step that catches semantic-intent failures
  (e.g. `wavg[size;price] % sum size` double-divides — runs cleanly,
  returns wrong value). If semantic correctness matters, use
  `/qsql-coder` (full mode).
