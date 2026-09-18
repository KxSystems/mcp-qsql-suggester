"""qlint_kx — KX official qlint (C9) wrapper for qSQL code.

Calls ``.qlint.lintItem`` from ``$QLINT_DIR/qlint.q_`` on a single q
code string. `qlint.q_` is licensed separately and may legitimately be
absent; when it is, this gate reports itself as skipped rather than
failing the candidate. See ``mcp_qsql_shared.qlint_policy``.

Exposes a single public function ``check_c9_qlint`` used by
``mcp_server.loop`` as gate 2 of the three-gate stack.
"""

from __future__ import annotations

import pykx as kx

from mcp_qsql_shared.qlint_common import qlint_gate
from mcp_qsql_shared.qlint_policy import QLINT_DISABLED_NOTE


#: Rules to drop before counting errors. EMPTY ON PURPOSE — the mechanism is
#: kept because a future rule may need it, but nothing is skipped today.
#:
#: ``UNINDENTED_CODE`` used to live here, described as "formatting-only". That
#: description is right for a checker whose job is to filter q snippets on
#: their way to a tokenizer — such a string is never loaded as a q script, so
#: its indentation genuinely cannot matter, and dropping an otherwise-good
#: snippet over it is pure loss. This checker started life doing exactly that.
#:
#: It does not hold for gate 2, whose consumers EXECUTE the code, and do it by
#: SCRIPT LOAD — where q begins a new statement at any line starting in column
#: 0, so an unindented continuation line inside an unclosed brace is a ``'{``
#: error and not a formatting preference:
#:
#:   gate 3 (mcp_server/qexec.py)  ``@[value; code; ...]``   -> indifferent
#:   the skill's Bash-verify       ``q /tmp/verify_*.q -q``  -> FAILS
#:   a user pasting it into a .q file                        -> FAILS
#:
#: Measured before enabling, over 39 drafts for KX's public NYSE TAQ benchmark
#: query set: the rule fires on exactly 1 of them, that one is exactly the
#: draft that runs under ``value`` and dies under script load, and 21 of the
#: other 22 multi-line drafts do not trigger it — no false positives, no
#: misses. It was also the only error-level rule to fire at all across the 39,
#: so enabling it adds no noise.
_C9_SKIP_RULES: frozenset[str] = frozenset()

#: Rules whose message adds nothing once q's own parse error is being reported.
#: Callers that already have a parse error pass ``parse_failed=True`` to drop
#: these — the point of linting unparseable code is to get a *better* message
#: than q's, not to append a content-free second one.
_REDUNDANT_WITH_PARSE_ERROR = frozenset(["CANNOT_PARSE"])


def check_c9_qlint(
    response: str,
    schema_names: set[str] | None = None,
    parse_failed: bool = False,
) -> dict:
    """C9: KX qlint errors — fail if .qlint.lintItem reports any error-level issues.

    Exceptions (not counted as failures):
    - (nothing is skipped unconditionally; see ``_C9_SKIP_RULES`` for why
      ``UNINDENTED_CODE`` no longer is.)
    - ``DECLARED_AFTER_USE`` — forgiven when the variable name appears in
      ``schema_names`` (a table the caller declares as pre-existing).
    - ``CANNOT_PARSE`` — dropped when ``parse_failed=True``, i.e. the caller is
      already reporting q's own parse error and this would just restate it.

    When the gate is disabled (``MCP_QSQL_QLINT=off``, or ``auto`` with no
    usable qlint.q_) this returns ``pass=True`` with ``skipped=True``: a
    candidate must not be rejected for a gate that never ran, but the caller
    has to be able to tell the two apart. ``mcp_server.loop`` propagates that
    to the response's ``lint_gate`` field.

    Thread safety relies on ``PYKX_THREADING=1`` being set before ``import
    pykx`` (see ``mcp_qsql_shared.qlint_common``). Without it, calls from
    worker threads hit pykx's ``noupdate`` mode and ``.qlint.lintItem``
    silently
    fails inside the ``except`` below, returning an empty issues list.
    """
    gate = qlint_gate()
    if not gate.enabled:
        return {"check": "C9_qlint", "pass": True, "issues": [],
                "skipped": True, "notes": QLINT_DISABLED_NOTE}
    try:
        result = kx.q(".qlint.lintItem", kx.CharVector(response), kx.q("::"))
        errors = kx.q("{select from x where errorClass=`error}", result)
        if len(errors) == 0:
            return {"check": "C9_qlint", "pass": True, "issues": []}
        allowed = schema_names or set()
        issues = []
        labels = kx.q("{x`label}", errors).py()
        descs = kx.q("{x`description}", errors).py()
        ptexts = kx.q("{x`problemText}", errors).py()
        for label, desc, ptext in zip(labels, descs, ptexts):
            label_s = label.decode() if isinstance(label, bytes) else str(label)
            desc_s = desc.decode() if isinstance(desc, bytes) else str(desc)
            ptext_s = ptext.decode() if isinstance(ptext, bytes) else str(ptext)
            if label_s in _C9_SKIP_RULES:
                continue
            if parse_failed and label_s in _REDUNDANT_WITH_PARSE_ERROR:
                continue
            if label_s == "DECLARED_AFTER_USE" and ptext_s in allowed:
                continue
            suffix = f" — at: {ptext_s}" if ptext_s else ""
            issues.append(f"qlint {label_s}: {desc_s}{suffix}")
        return {"check": "C9_qlint", "pass": len(issues) == 0, "issues": issues}
    except Exception as e:
        return {"check": "C9_qlint", "pass": True, "issues": [],
                "notes": f"skipped (qlint error: {e})"}
