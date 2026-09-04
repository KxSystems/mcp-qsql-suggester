"""Unit tests for ``mcp_qsql_shared.extract.extract_qsql``.

Every raw model completion passes through this function before it is
parsed, hashed for dedup, or clustered, so its four fallback strategies
decide what the rest of the pipeline even sees. Pure string handling —
no q, no pykx, no model. See ``tests/README.md``.
"""

from __future__ import annotations

from mcp_qsql_shared.extract import extract_qsql


# --- strategy 1: ```q fences ------------------------------------------- #

def test_q_fence_is_extracted_and_stripped():
    assert extract_qsql(
        "Here you go:\n```q\nselect avg price by sym from trade\n```\nHope that helps."
    ) == "select avg price by sym from trade"


def test_longest_q_fence_wins():
    """Chat-tuned models often narrate a wrong short attempt before the real
    answer; strategy 1 takes the longest block, not the first."""
    assert extract_qsql(
        "```q\nselect from t\n```\nBetter:\n```q\nselect avg price by sym from trade\n```"
    ) == "select avg price by sym from trade"


def test_q_fence_wins_over_other_language_fence():
    """A ```q block is preferred even when a longer non-q block precedes it,
    because strategy 1 short-circuits before strategy 2 runs."""
    out = extract_qsql(
        "```python\nprint('a much, much longer block of not-q')\n```\n"
        "```q\nselect from t\n```"
    )
    assert out == "select from t"


def test_multi_line_q_fence_keeps_internal_newlines():
    assert extract_qsql(
        "```q\nt:select from trade\nselect avg price by sym from t\n```"
    ) == "t:select from trade\nselect avg price by sym from t"


# --- strategy 2: any fence --------------------------------------------- #

def test_unlabelled_fence_is_extracted():
    assert extract_qsql("```\nselect avg price by sym from trade\n```") == \
        "select avg price by sym from trade"


def test_wrong_language_tag_still_extracted():
    """The fine-tune sometimes emits ```sql; strategy 2 takes it anyway."""
    assert extract_qsql("```sql\nselect avg price by sym from trade\n```") == \
        "select avg price by sym from trade"


# --- strategy 3: heuristic line collection ------------------------------ #

def test_unfenced_qsql_line_is_recovered():
    assert extract_qsql(
        "The answer is\nselect avg price by sym from trade"
    ) == "select avg price by sym from trade"


def test_prose_around_unfenced_code_is_dropped():
    out = extract_qsql(
        "Sure thing.\nselect avg price by sym from trade\nThat groups by sym."
    )
    assert "Sure thing." not in out
    assert "select avg price by sym from trade" in out


def test_assignment_line_is_recognised_as_code():
    assert extract_qsql("blah blah\nt:select from trade") == "t:select from trade"


def test_continuation_line_after_code_is_kept():
    """Once collection has started, a line that matches no keyword is treated
    as a continuation — this is what keeps wrapped `select` clauses intact."""
    out = extract_qsql("select avg price\n  by sym from trade")
    assert out == "select avg price\nby sym from trade"


def test_q_comment_line_is_kept():
    out = extract_qsql("/ average price\nselect avg price by sym from trade")
    assert out.startswith("/ average price")


# --- strategy 4: fallback ----------------------------------------------- #

def test_pure_prose_falls_back_to_whole_completion():
    assert extract_qsql("  I am not sure what you mean.  ") == \
        "I am not sure what you mean."


def test_empty_completion_yields_empty_string():
    assert extract_qsql("") == ""


def test_whitespace_only_completion_yields_empty_string():
    assert extract_qsql("   \n\n  ") == ""


# --- dedup-relevant behaviour ------------------------------------------- #

def test_same_code_in_different_wrappers_extracts_identically():
    """Dedup in ``ClusterState.add_round`` keys on byte-identical extracted
    text, so cosmetic differences in how the model wrapped its answer must
    collapse to one unique candidate rather than inflating cluster weight."""
    code = "select avg price by sym from trade"
    variants = [
        f"```q\n{code}\n```",
        f"```\n{code}\n```",
        f"Here:\n```q\n{code}\n```\n",
        f"```q\n{code}\n\n```",
    ]
    assert {extract_qsql(v) for v in variants} == {code}
