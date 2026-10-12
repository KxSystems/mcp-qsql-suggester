"""Code extraction from model completions."""

from __future__ import annotations

import re
import textwrap


def extract_qsql(completion: str) -> str:
    """Extract qSQL code from a model completion.

    Strategies (in order):
    1. Fenced ```q code blocks
    2. Any fenced code block
    3. Collect lines that look like q code (assignments, qSQL keywords,
       join syntax, functional forms, system commands)
    4. Full completion as fallback
    """
    # Fenced blocks are dedented before .strip(): .strip() alone drops only the
    # first line's indent, and a uniformly indented block then becomes one
    # statement under script load (see Strategy 3 below).
    # Strategy 1: ```q fenced blocks
    fenced = re.findall(r"```q\s*\n(.*?)```", completion, re.DOTALL)
    if fenced:
        return textwrap.dedent(max(fenced, key=len)).strip()

    # Strategy 2: any fenced blocks
    any_fenced = re.findall(r"```\w*\s*\n(.*?)```", completion, re.DOTALL)
    if any_fenced:
        return textwrap.dedent(max(any_fenced, key=len)).strip()

    # Strategy 3: collect all lines that look like q code (assignments, qSQL, etc.)
    # and return them as a multi-statement program. Lines keep their own
    # indentation (only the block's common indent is removed): q starts a new
    # statement at any line in column 0, so a flattened continuation line inside
    # an open bracket is a different program under script load, and the C9 gate
    # rejects it (UNINDENTED_CODE). The model answers unfenced, so this is the
    # path almost every candidate takes.
    # not .strip()-ed first: that would drop only the FIRST line's indent
    lines = completion.split("\n")
    qsql_keywords = (
        "select", "exec", "update", "delete", "aj[", "wj[", "lj", "ij",
        "?[", "![", "count ", "meta ",
    )
    code_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        is_qsql = any(stripped.startswith(kw) for kw in qsql_keywords)
        is_join = bool(re.match(r"\w+\s+(lj|ij|uj)\s+", stripped))
        is_assignment = bool(re.match(r"\w[\w.]*\s*:", stripped))
        is_q_comment = stripped.startswith("/")
        is_functional = stripped.startswith("@[") or stripped.startswith(".[")
        is_system_cmd = stripped.startswith("\\")
        if is_qsql or is_join or is_assignment or is_q_comment or is_functional or is_system_cmd:
            code_lines.append(line.rstrip())
        elif code_lines:
            # Continuation line (e.g. indented part of multi-line select)
            code_lines.append(line.rstrip())

    if code_lines:
        return textwrap.dedent("\n".join(code_lines))

    return completion.strip()
