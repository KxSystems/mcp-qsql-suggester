"""Tier 1 — the q files that exist twice and must NOT diverge.

``literal_pool.q`` is loaded by gate 3 (``mcp_server/``) and by the
/qsql-coder skill's Bash-verify (``.claude/skills/qsql-coder/``). The skill
must run without the server tree on disk, so it carries its own copy — but
unlike the two ``eval_compare.q`` copies, which answer different questions
and deliberately differ, both copies here answer the same one: "which rows
make this filter select something?". A drift would let a candidate clear
gate 3 on data where its filter discriminates and be verified on data where
it does not. Likewise ``.eval.isVacuous`` must mean the same thing on both
sides.

Stdlib only: a text comparison, no q.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude/skills/qsql-coder"


def test_literal_pool_copies_are_byte_identical():
    server = (ROOT / "mcp_server/literal_pool.q").read_bytes()
    skill = (SKILL / "literal_pool.q").read_bytes()
    assert server == skill, (
        "mcp_server/literal_pool.q and .claude/skills/qsql-coder/literal_pool.q "
        "differ — edit one, then copy it over the other"
    )


def _defs(path: Path, names: tuple[str, ...]) -> list[str]:
    lines = path.read_text().splitlines()
    out, keep = [], False
    for ln in lines:
        if any(ln.startswith(n + ":") for n in names):
            keep = True
        elif keep and not ln.startswith(" "):
            keep = False
        if keep:
            out.append(ln)
    return out


def test_is_vacuous_is_defined_identically_on_both_sides():
    names = (".eval.allNull", ".eval.isVacuous")
    server = _defs(ROOT / "mcp_server/eval_compare.q", names)
    skill = _defs(SKILL / "eval_describe.q", names)
    assert server and server == skill
