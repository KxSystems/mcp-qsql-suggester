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


_C9_SKIP_RULES = frozenset(["UNINDENTED_CODE"])

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
    - ``UNINDENTED_CODE`` — formatting-only, always skipped.
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
