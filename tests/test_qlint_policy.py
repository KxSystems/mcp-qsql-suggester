"""Tier 1 — the qlint gate policy (``MCP_QSQL_QLINT``).

Pure logic: no pykx, no kdb+ licence, no ``qlint.q_``. That is the whole
reason ``mcp_qsql_shared.qlint_policy`` exists as its own module — the gate
has to be decided before anything touches embedded q, and the decision is
worth testing on a machine that has none of it.

The loader that consumes these decisions (``qlint_common.resolve_qlint_gate``)
does need pykx, so its three branches are exercised in
``tests/test_pipeline_gates.py`` (tier 2) instead.
"""

from __future__ import annotations

from mcp_qsql_shared.qlint_policy import (
    QLINT_AUTO,
    QLINT_DIR_ENV_VAR,
    QLINT_ENV_VAR,
    QLINT_OFF,
    QLINT_REQUIRED,
    QlintGate,
    load_qlint_mode,
    locate_qlint,
    missing_qlint_message,
)


# --------------------------------------------------------------------------- #
# mode parsing
# --------------------------------------------------------------------------- #

def test_unset_defaults_to_auto():
    mode, warning = load_qlint_mode({})
    assert mode == QLINT_AUTO
    assert warning is None


def test_each_mode_round_trips():
    for value in (QLINT_AUTO, QLINT_REQUIRED, QLINT_OFF):
        mode, warning = load_qlint_mode({QLINT_ENV_VAR: value})
        assert (mode, warning) == (value, None)


def test_mode_is_case_and_whitespace_insensitive():
    mode, warning = load_qlint_mode({QLINT_ENV_VAR: "  ReQuIrEd "})
    assert (mode, warning) == (QLINT_REQUIRED, None)


def test_empty_string_is_treated_as_unset():
    """A shell that exports the var without a value must not trip the warning."""
    mode, warning = load_qlint_mode({QLINT_ENV_VAR: "   "})
    assert (mode, warning) == (QLINT_AUTO, None)


def test_invalid_mode_falls_back_to_auto_with_a_warning():
    """Same convention as the other MCP_QSQL_* loaders: warn, don't fail
    startup. A typo must not take a working deployment down."""
    mode, warning = load_qlint_mode({QLINT_ENV_VAR: "yes"})
    assert mode == QLINT_AUTO
    assert warning is not None
    assert "yes" in warning
    # The warning has to name the alternatives, or it isn't actionable.
    for valid in (QLINT_AUTO, QLINT_REQUIRED, QLINT_OFF):
        assert valid in warning


# --------------------------------------------------------------------------- #
# locating qlint.q_
# --------------------------------------------------------------------------- #

def test_locate_reports_unset_dir():
    path, reason = locate_qlint({})
    assert path is None
    assert QLINT_DIR_ENV_VAR in reason


def test_locate_reports_missing_file(tmp_path):
    path, reason = locate_qlint({QLINT_DIR_ENV_VAR: str(tmp_path)})
    assert path is None
    assert "qlint.q_" in reason
    assert str(tmp_path) in reason      # the reason must name the path it tried


def test_locate_finds_the_file(tmp_path):
    (tmp_path / "qlint.q_").write_bytes(b"not really qlint, but present")
    path, reason = locate_qlint({QLINT_DIR_ENV_VAR: str(tmp_path)})
    assert reason is None
    assert path == tmp_path / "qlint.q_"


def test_locate_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "qlint.q_").write_bytes(b"x")
    path, reason = locate_qlint({QLINT_DIR_ENV_VAR: "~"})
    assert reason is None
    assert path == tmp_path / "qlint.q_"


# --------------------------------------------------------------------------- #
# what the operator is told
# --------------------------------------------------------------------------- #

def test_required_message_is_actionable():
    """The fatal message must carry both ways out: install it, or opt out."""
    msg = missing_qlint_message("QLINT_DIR is not set", QLINT_REQUIRED)
    assert "QLINT_DIR is not set" in msg
    assert "code.kx.com" in msg                  # where to get it
    assert "ax-libraries" in msg
    assert f"{QLINT_ENV_VAR}={QLINT_OFF}" in msg  # how to proceed without it


def test_auto_message_says_what_was_lost_and_how_to_harden():
    msg = missing_qlint_message("qlint.q_ not found at QLINT_DIR=/nope", QLINT_AUTO)
    assert "DISABLED" in msg
    assert "lint_gate" in msg                          # the response marker
    assert f"{QLINT_ENV_VAR}={QLINT_REQUIRED}" in msg   # how to fail-fast instead


# --------------------------------------------------------------------------- #
# the resolved gate
# --------------------------------------------------------------------------- #

def test_gate_status_strings():
    """``status`` is what lands in every tool response, so pin the literals."""
    assert QlintGate(mode=QLINT_AUTO, enabled=True).status == "kx_qlint_c9"
    assert QlintGate(mode=QLINT_AUTO, enabled=False, reason="x").status == "disabled"
    assert QlintGate(mode=QLINT_OFF, enabled=False, reason="x").status == "disabled"
