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

# Why verification did not happen, when the answer is not "nothing triggered it".
# Cases A and D both used to state a *reason* for the absence of a reference
# check -- case A a list ("no high-risk idiom, ..."), case D "outside my q
# knowledge or the instruction is genuinely ambiguous" -- and both reasons are
# false whenever the check was impossible rather than unnecessary. Quick mode
# renders case A with no reference check at all, and full mode degrades to the
# same shape when `q` is missing, so in both the old wording asserted something
# untrue. The caller names the real reason instead; absent field keeps the
# original wording, which is correct for a full-mode run that simply had no
# trigger.
VERIFICATION_UNAVAILABLE = {
    "quick_mode": "quick mode does not verify against a reference",
    "no_q": "`q` is not on PATH, so Bash-verify could not run",
}

# Case D's whole sentence, per reason, rather than one skeleton with the reason
# slotted in: why no alternative was written and what the reader should do
# differ together, not independently. Quick mode wrote none because rewriting
# is not what it does; a missing q means one could not be verified. Composing
# those from shared fragments produced "this skill only presents q it has
# verified" under quick mode, which had just presented unverified q in case A.
UNAVAILABLE_CASE_D = {
    "quick_mode": (
        "I did not write an alternative — quick mode presents the "
        "specialist's draft or nothing, and never rewrites. For an "
        "independently verified alternative, use `/qsql-coder` (full mode)."
    ),
    "no_q": (
        "I did not write an alternative — `q` is not on PATH, so I could not "
        "Bash-verify one, and this skill does not present q it has not "
        "verified. Make `q` reachable and re-run for a verified alternative."
    ),
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


def _verification_unavailable(payload: dict) -> str | None:
    """The validated reason key for why verification could not run, or None.

    Returns the key rather than the prose because the two renderers need
    different halves of it: case A only states the reason, case D also has to
    name a remedy. Validated here rather than at the call sites so a typo in
    the enum is a hard error instead of silently falling through to the
    wording that made the false claim in the first place.
    """
    reason = payload.get("verification_unavailable")
    if reason is None:
        return None
    if reason not in VERIFICATION_UNAVAILABLE:
        die(
            "'verification_unavailable' must be one of "
            f"{sorted(VERIFICATION_UNAVAILABLE)} (got {reason!r})"
        )
    return reason


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
    unavailable = _verification_unavailable(payload)
    out = ["### TOOL CODE (suggested)", ""]
    out += code_fence("q", qwen_code)
    # The "unanimous cluster" clause used to be unconditional, which
    # contradicted the candidate counts printed one clause earlier whenever the
    # winning cluster was a bare plurality ("4/8 candidates agreed ...
    # unanimous self-consistency cluster"). Both modes can reach a plurality
    # here: quick mode renders case A whatever the fraction because it never
    # verifies, and full mode's cluster trigger is a threshold
    # (`winning_cluster_fraction < 0.6` in SKILL.md), not unanimity, so a 7/8
    # cluster with no other trigger is a legitimate case A.
    if win == n:
        reasons = (
            "no high-risk idiom, no functional form, unanimous "
            "self-consistency cluster, no user-supplied reference"
        )
        tail = ""
    else:
        reasons = (
            "no high-risk idiom, no functional form, a winning cluster above "
            "the skill's agreement threshold, no user-supplied reference"
        )
        # Numbers only, no verdict on what they warrant. Which fractions
        # trigger a check is policy that lives in SKILL.md and is tunable; a
        # renderer that asserted "this is a Bash-verify trigger" would go
        # stale the moment that threshold moved, which is exactly what
        # happened when it moved from 1.0 to 0.6.
        tail = (
            f" The winning cluster was a plurality rather than unanimous "
            f"({win} of {n})."
        )
    if unavailable:
        # No reason list: the draft was not held up against a reference, so
        # none of "no high-risk idiom, no functional form, ..." was ever
        # established. Claiming them here is exactly the bug this replaces.
        out.append(
            f"**Verdict**: passed=true, {sc}. Not verified — "
            f"{VERIFICATION_UNAVAILABLE[unavailable]}.{tail}"
        )
    else:
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
    unavailable = _verification_unavailable(payload)
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
        # Two different reasons for the same empty result, and they call for
        # different next steps from the reader: nothing to write vs. nothing
        # to verify a write against. Conflating them told a user with no q on
        # PATH that their instruction was ambiguous.
        if unavailable:
            why = UNAVAILABLE_CASE_D[unavailable]
        else:
            why = (
                "I could not confidently write an alternative — task is "
                "outside my q knowledge or the instruction is genuinely "
                "ambiguous. Please refine the instruction or schema."
            )
        out.append(
            f"**Verdict**: no self-consistency cluster survived gating "
            f"(passed=false, {sc}, final_issues=`{issues_repr}`). {why}"
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
    # B and C *are* the verified cases -- they carry a reference priority, a
    # source label and a verify shape. Saying verification was unavailable
    # there is a contradiction, not a nuance, so refuse it rather than render
    # a self-arguing verdict.
    if payload.get("verification_unavailable") is not None and case not in ("A", "D"):
        die(
            f"case={case}: 'verification_unavailable' applies to case A or D "
            f"only (B and C are the Bash-verified cases)"
        )
    out_lines = render_preamble(payload) + RENDERERS[case](payload)
    sys.stdout.write("\n".join(out_lines).rstrip() + "\n")


if __name__ == "__main__":
    main()
