"""Thin loader for the canonical q-side type-generator file.

The actual typeGen / parseSchema0 / genTable0 definitions live in
``mcp_server/type_gen.q`` — also `cat`-loaded by
``.claude/skills/qsql-coder/pre-derive.md`` (which must share gate-3
dummy bytes so the shape hint matches what qwen sees at runtime).
SKILL.md's Bash-verify uses a separate, verify-tuned dummy
(``.claude/skills/qsql-coder/verify_gen.q``).

``TYPE_GEN_STMTS`` is preserved as the loader interface so
``mcp_server.qexec._ensure_typegen`` can iterate it unchanged: one
``\\l <absolute-path>`` directive per ``.q`` file. ``literal_pool.q``
(literal-aware dummy rows, ``genTableLit``) comes second because it falls
back to ``type_gen.q``'s ``typeGen`` / ``genTable0``.
"""

from __future__ import annotations

from pathlib import Path


TYPE_GEN_Q_PATH = (Path(__file__).parent / "type_gen.q").resolve()

LITERAL_POOL_Q_PATH = (Path(__file__).parent / "literal_pool.q").resolve()

TYPE_GEN_STMTS: list[str] = [
    f"\\l {TYPE_GEN_Q_PATH}",
    f"\\l {LITERAL_POOL_Q_PATH}",
]
