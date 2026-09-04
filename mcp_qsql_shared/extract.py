"""Code extraction from model completions."""

from __future__ import annotations

import re


def extract_qsql(completion: str) -> str:
    """Extract qSQL code from a model completion.

    Strategies (in order):
    1. Fenced ```q code blocks
    2. Any fenced code block
    3. Collect lines that look like q code (assignments, qSQL keywords,
       join syntax, functional forms, system commands)
    4. Full completion as fallback
    """
    # Strategy 1: ```q fenced blocks
    fenced = re.findall(r"```q\s*\n(.*?)```", completion, re.DOTALL)
    if fenced:
        return max(fenced, key=len).strip()

    # Strategy 2: any fenced blocks
    any_fenced = re.findall(r"```\w*\s*\n(.*?)```", completion, re.DOTALL)
    if any_fenced:
        return max(any_fenced, key=len).strip()

    # Strategy 3: collect all lines that look like q code (assignments, qSQL, etc.)
    # and return them as a multi-statement program.
    lines = completion.strip().split("\n")
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
            code_lines.append(stripped)
        elif code_lines:
            # Continuation line (e.g. indented part of multi-line select)
            code_lines.append(stripped)

    if code_lines:
        return "\n".join(code_lines)

    return completion.strip()
