#!/usr/bin/env python3
"""Format /qsql-coder output from a JSON payload to Markdown.

Reads JSON from stdin, writes Markdown to stdout. Validates required-by-case
fields and exits 2 with a clear stderr message on violation.

Single source of truth for the skill's response format. See
.claude/skills/qsql-coder/SKILL.md for the JSON schema and
invocation convention. Normally invoked by the skill flow via a heredoc
pipe; you rarely call this directly except for ad-hoc testing.

Usage:
  # Heredoc pipe (the canonical pattern — used by the skill itself):
  $ python3 .claude/skills/qsql-coder/scripts/format_output.py <<'__QSQL_FORMAT_END__'
  {
    "case": "A",
    "instruction_interpreted": "...",
    "schemas": {"trade": "`time`sym`price`size!`t`s`f`j"},
    "qwen_code": "select from trade",
    "passed": true,
    "rounds_used": 1,
    "candidates_generated": 4,
    "winning_cluster_size": 4
  }
  __QSQL_FORMAT_END__

  # Ad-hoc one-shot via echo + pipe:
  $ echo '{"case":"A","instruction_interpreted":"foo",
    "schemas":{"t":"`a`b!`s`j"},"qwen_code":"select from t",
    "passed":true,"rounds_used":1,"candidates_generated":4,
    "winning_cluster_size":4}' | \\
        python3 .claude/skills/qsql-coder/scripts/format_output.py

Per-case required fields are documented in SKILL.md "Outputs" section
(case A/B/C/D each have distinct required-field sets).
"""
from __future__ import annotations

import json
import sys
from typing import Any


B_P3_HEDGE = (
    "Verified against my own translation, which may share assumptions with "
    "qwen's output; a user spot-check is still recommended."
)
C_P3_HEDGE = (
    "Reference was my own translation; both could share assumptions, so a "
    "user spot-check is still recommended."
)

REFERENCE_PRIORITY_HEADERS = {
    1: "### SUGGESTED CODE (Provided Reference)",
    2: "### SUGGESTED CODE (generated)",
    3: "### SUGGESTED CODE (generated)",
}


def die(msg: str) -> None:
    print(f"format_output.py: {msg}", file=sys.stderr)
    sys.exit(2)


def require(payload: dict, key: str, case: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        die(f"case={case}: missing required field '{key}'")
    return payload[key]


def code_fence(label: str, code: str) -> list[str]:
    return [f"```{label}", code.rstrip("\n"), "```", ""]


def render_preamble(payload: dict) -> list[str]:
    case = payload.get("case", "?")
    instr = require(payload, "instruction_interpreted", case)
    schemas = payload.get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        die("missing or empty 'schemas' (must be dict of name -> q schema string)")
    out = [
        "### Instruction interpreted as",
        "",
        instr.strip(),
        "",
        "### Schemas used",
        "",
    ]
    for name, schema in schemas.items():
        out.append(f"- `{name}`: `` {schema} ``")
    out.append("")
    shape = payload.get("pre_derive_shape")
    if isinstance(shape, str) and shape.strip():
        out.append(f"**Pre-derive shape (dev-only)**: `` {shape.strip()} ``")
        out.append("")
    return out


def render_idiom_note(payload: dict) -> list[str]:
    note = payload.get("idiom_note")
    if note is None or (isinstance(note, str) and not note.strip()):
        return []
    return [f"**Idiom note**: {note.strip()}", ""]


def _sc_summary(payload: dict, case: str) -> str:
    """Self-consistency summary phrase, e.g. '4/4 candidates agreed (round 1)'.

    Replaces the old single 'attempts_used' number now that the MCP
    generates several independent candidates per call and clusters them
    (see mcp_server/self_consistency.py) instead of retrying one candidate
    with corrective feedback.
    """
    rounds = require(payload, "rounds_used", case)
    n = require(payload, "candidates_generated", case)
    win = payload.get("winning_cluster_size", 0)
    summary = f"{win}/{n} candidates agreed (round {rounds})"
    # A deployment without qlint.q_ (licensed separately) filters candidates on
    # parse + execution only. The MCP marks that in `lint_gate`; surface it here
    # so the human reading the verdict knows the gate stack was one short,
    # rather than having to inspect the raw MCP response. Absent field (older
    # server, or a caller that did not forward it) reads as "no claim made".
    if payload.get("lint_gate") == "disabled":
        summary += ", KX qlint gate disabled server-side"
    return summary


def _reference_fields(payload: dict, case: str) -> tuple[int, str, str]:
    p = payload.get("reference_priority")
    if p not in (1, 2, 3):
        die(f"case={case}: 'reference_priority' must be 1, 2, or 3 (got {p!r})")
    label = require(payload, "reference_source_label", case)
    shape = require(payload, "verify_shape", case)
    return p, label, shape


def render_case_a(payload: dict) -> list[str]:
    qwen_code = require(payload, "qwen_code", "A")
    if payload.get("passed") is not True:
        die("case=A: 'passed' must be true")
    sc = _sc_summary(payload, "A")
    n = require(payload, "candidates_generated", "A")
    win = payload.get("winning_cluster_size", 0)
    out = ["### TOOL CODE (suggested)", ""]
    out += code_fence("q", qwen_code)
    # The "unanimous cluster" clause used to be unconditional, which
    # contradicted the candidate counts printed one clause earlier whenever the
    # winning cluster was a bare plurality ("4/8 candidates agreed ...
    # unanimous self-consistency cluster"). Full mode cannot reach that state —
    # SKILL.md's case-A row requires a unanimous cluster, and a split one is
    # itself a Bash-verify trigger, so it lands in case B or C instead. Quick
    # mode can: it renders case A whatever the cluster fraction, because it
    # never verifies at all.
    if win == n:
        reasons = (
            "no high-risk idiom, no functional form, unanimous "
            "self-consistency cluster, no user-supplied reference"
        )
        tail = ""
    else:
        reasons = (
            "no high-risk idiom, no functional form, no user-supplied "
            "reference"
        )
        tail = (
            f" The winning cluster was a plurality rather than unanimous "
            f"({win} of {n}) — on its own that is a Bash-verify trigger in "
            f"full mode."
        )
    out.append(
        f"**Verdict**: passed=true, {sc}. No verification "
        f"triggered ({reasons}).{tail}"
    )
    out.append("")
    out += render_idiom_note(payload)
    return out


def render_case_b(payload: dict) -> list[str]:
    qwen_code = require(payload, "qwen_code", "B")
    if payload.get("passed") is not True:
        die("case=B: 'passed' must be true")
    sc = _sc_summary(payload, "B")
    p, label, shape = _reference_fields(payload, "B")
    out = ["### TOOL CODE (suggested, verified)", ""]
    out += code_fence("q", qwen_code)
    verdict = (
        f"**Verdict**: passed=true, {sc}. Bash-verified "
        f"against {label}; result match on `{shape}`."
    )
    if p == 3:
        verdict += f" {B_P3_HEDGE}"
    out.append(verdict)
    out.append("")
    out += render_idiom_note(payload)
    return out


def render_case_c(payload: dict) -> list[str]:
    qwen_code = require(payload, "qwen_code", "C")
    suggested = require(payload, "suggested_code", "C")
    if payload.get("passed") is not True:
        die("case=C: 'passed' must be true")
    sc = _sc_summary(payload, "C")
    p, label, shape = _reference_fields(payload, "C")
    symptom = require(payload, "divergence_symptom", "C")
    out = ["### TOOL CODE (not suggested)", ""]
    out += code_fence("q", qwen_code)
    out.append(REFERENCE_PRIORITY_HEADERS[p])
    out.append("")
    out += code_fence("q", suggested)
    symptom_clean = symptom.strip().rstrip(".")
    verdict = (
        f"**Verdict**: qwen's draft passed all MCP gates (passed=true, "
        f"{sc}) but diverged from {label} on Bash-verify "
        f"(result shape `{shape}`) — {symptom_clean}. The SUGGESTED CODE "
        f"above matches the reference."
    )
    if p == 3:
        verdict += f" {C_P3_HEDGE}"
    out.append(verdict)
    out.append("")
    out += render_idiom_note(payload)
    return out


def render_case_d(payload: dict) -> list[str]:
    qwen_code = require(payload, "qwen_code", "D")
    if payload.get("passed") is not False:
        die("case=D: 'passed' must be false")
    sc = _sc_summary(payload, "D")
    final_issues = payload.get("final_issues")
    if not isinstance(final_issues, list):
        die("case=D: 'final_issues' must be a list")
    could_write = payload.get("opus_could_write")
    if not isinstance(could_write, bool):
        die("case=D: 'opus_could_write' must be a boolean")
    issues_repr = json.dumps(final_issues, ensure_ascii=False)
    out: list[str] = []
    if could_write:
        suggested = require(payload, "suggested_code", "D")
        basis = require(payload, "basis_label", "D")
        out += ["### SUGGESTED CODE (without MCP Tool)", ""]
        out += code_fence("q", suggested)
        out += ["### TOOL CODE (failed)", ""]
        out += code_fence("q", qwen_code)
        out.append(
            f"**Verdict**: no self-consistency cluster survived gating "
            f"(passed=false, {sc}, final_issues=`{issues_repr}`). I wrote "
            f"and Bash-verified the SUGGESTED CODE above from {basis}."
        )
    else:
        out += ["### TOOL CODE (failed)", ""]
        out += code_fence("q", qwen_code)
        out.append(
            f"**Verdict**: no self-consistency cluster survived gating "
            f"(passed=false, {sc}, final_issues=`{issues_repr}`). I could "
            f"not confidently write an alternative — task is outside my q "
            f"knowledge or the instruction is genuinely ambiguous. Please "
            f"refine the instruction or schema."
        )
    out.append("")
    out += render_idiom_note(payload)
    return out


RENDERERS = {
    "A": render_case_a,
    "B": render_case_b,
    "C": render_case_c,
    "D": render_case_d,
}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        die(f"invalid JSON on stdin: {e}")
    if not isinstance(payload, dict):
        die("payload must be a JSON object")
    case = payload.get("case")
    if case not in RENDERERS:
        die(f"'case' must be one of A, B, C, D (got {case!r})")
    out_lines = render_preamble(payload) + RENDERERS[case](payload)
    sys.stdout.write("\n".join(out_lines).rstrip() + "\n")


if __name__ == "__main__":
    main()
