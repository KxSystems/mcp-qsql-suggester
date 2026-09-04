"""Tier availability probes, shared by ``conftest.py`` and the tier-2/3 modules.

One set of probes, two deliberately different reactions to the same answer:

* a **bare** ``pytest`` run must stay correct on a box with no pykx and no
  linter, so an unavailable tier **skips** — that is ``tests/README.md``'s
  promise that the same command is right at every rung of the ladder;
* a run that **named** the tier (``-m needs_q``, ``-m needs_qlint``) asked for
  exactly the tests that cannot run, so it is a **usage error**, not a skip.

The failure that motivated the split: a bare ``pytest`` printed ``77 passed,
2 skipped`` and ``pytest -m needs_qlint`` printed ``2 skipped, 77 deselected``
(77 being tier 1's size at the time — these two are a record of that run, not
a current expectation, so they are deliberately not updated with the count)
on a box where ``QLINT_DIR`` was set correctly — because ``pytest`` resolved
through ``PATH`` to an interpreter with no pykx. Both lines are
indistinguishable from the documented self-skip, and a module-level skip counts
as **one** skip, not one per test, so the ``8`` and ``6`` from
``tests/README.md`` never appeared to be missing. Naming a tier now says so.

``addopts = ["-ra"]`` in ``pyproject.toml`` is the other half of the fix: the
skip path keeps a reason, and ``-ra`` is what prints it under ``-q``. The skips
themselves stay in the tier modules, so the summary line names the file whose
tests went missing rather than naming this one.

Kept free of pykx at module scope for the same reason ``conftest.py`` is: this
file is imported on every run, tier-1-only ones included.
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

from mcp_qsql_shared.qlint_policy import (  # pykx-free by design
    QLINT_DIR_ENV_VAR,
    QLINT_ENV_VAR,
    QLINT_OFF,
    load_qlint_mode,
    locate_qlint,
)

#: The marker set a test of each tier carries, i.e. what ``-m`` has to match.
TIER2_MARKS = frozenset({"needs_q"})
TIER3_MARKS = frozenset({"needs_q", "needs_qlint"})

_BOOL_OPS = frozenset({"and", "or", "not"})
_WORD = re.compile(r"[A-Za-z_]\w*")


# --------------------------------------------------------------------------- #
# what this run asked for
# --------------------------------------------------------------------------- #

def _markexpr_selects(markexpr: str, marks: frozenset[str]) -> bool:
    """Would ``-m <markexpr>`` select a test carrying exactly ``marks``?

    Evaluated rather than substring-matched, because the two spellings differ
    only by a ``not``: ``-m "needs_q and not needs_qlint"`` names tier 2 while
    mentioning tier 3's marker. ``-m`` accepts marker names combined with
    ``and`` / ``or`` / ``not`` / parentheses, so rewriting every name to
    ``True``/``False`` and evaluating is exact for that grammar. Anything it
    cannot evaluate answers False — falling back to the skip behaviour, never
    to a spurious error.
    """

    def _as_bool(match: re.Match[str]) -> str:
        word = match.group()
        if word in _BOOL_OPS:
            return word
        return "True" if word in marks else "False"

    try:
        return bool(eval(_WORD.sub(_as_bool, markexpr), {"__builtins__": {}}, {}))
    except Exception:  # pragma: no cover — an expression pytest itself rejects
        return False


def selected_explicitly(config, marks: frozenset[str]) -> bool:
    """True when this run's ``-m`` expression targets the tier owning ``marks``."""
    markexpr = getattr(config.option, "markexpr", "") or ""
    return bool(markexpr) and _markexpr_selects(markexpr, marks)


def tier3_targeted(config) -> bool:
    """True for a run that asked for tier 3 **and not** tier 2.

    The distinction decides whether a missing linter is fatal. ``-m
    needs_qlint`` has nothing else to run, so it is; a broader ``-m needs_q``
    still has tier 2 to run, and tier 3 skipping out of it is the documented
    behaviour of an optional, separately-licensed dependency.
    """
    return (selected_explicitly(config, TIER3_MARKS)
            and not selected_explicitly(config, TIER2_MARKS))


# --------------------------------------------------------------------------- #
# the probes
# --------------------------------------------------------------------------- #

def pykx_reason() -> str | None:
    """Why embedded q (tiers 2 and 3) is unavailable, or None if it is fine.

    ``find_spec`` first rather than ``pytest.importorskip`` because pykx must
    not be imported directly here: ``mcp_qsql_shared.qlint_common`` has to be
    imported first to set ``PYKX_THREADING=1`` (see the preamble in
    ``mcp_server/loop.py``). Importing ``mcp_server.loop`` does that in the
    right order; only then is it safe to touch pykx.

    The interpreter path is in the message on purpose: "pykx is not installed"
    is misleading when it *is* installed, in another env, and a bare ``pytest``
    picked this one off ``PATH``.
    """
    if importlib.util.find_spec("pykx") is None:
        return f"pykx is not installed in {sys.executable}"

    from mcp_server import loop  # noqa: F401  (import order: sets PYKX_THREADING)

    import pykx as kx

    if not kx.licensed:
        return (f"pykx {kx.__version__} in {sys.executable} is unlicensed — "
                f"embedded q is unavailable")
    return None


def qlint_reason() -> str | None:
    """Why the C9 gate cannot come up, from a filesystem probe alone.

    Deliberately static — ``mcp_qsql_shared.qlint_policy`` is pykx-free — so it
    answers the case that actually happens (``QLINT_DIR`` unset, or pointing
    somewhere without ``qlint.q_``) with no q session and no licence. The case
    it cannot see is a ``qlint.q_`` that loads yet leaves ``.qlint.lintItem``
    undefined: the ~2 KB loader stub without its ``lib/``. That needs embedded
    q, so tier 3's ``_require_working_qlint`` fixture stays the backstop.
    """
    mode, _ = load_qlint_mode()
    if mode == QLINT_OFF:
        return f"{QLINT_ENV_VAR}={QLINT_OFF} — the C9 gate is explicitly disabled"
    _, reason = locate_qlint()
    return reason


# --------------------------------------------------------------------------- #
# reacting to the probes
# --------------------------------------------------------------------------- #

_SKIP_IS_DELIBERATE = (
    "A bare `pytest -q` skips this tier instead of failing — that is "
    "deliberate; see tests/README.md."
)


def check_selected_tiers(config) -> None:
    """Turn "you asked for a tier this box cannot run" into a usage error.

    Called from ``conftest.pytest_configure``, i.e. before collection, so the
    message arrives on its own with exit status 4 rather than as a traceback
    inside a collection error.
    """
    wants_embedded_q = (selected_explicitly(config, TIER2_MARKS)
                        or selected_explicitly(config, TIER3_MARKS))
    if not wants_embedded_q:
        return

    markexpr = config.option.markexpr
    reason = pykx_reason()
    if reason:
        raise pytest.UsageError(
            f"-m {markexpr!r} selects tests that need embedded q, but it is "
            f"unavailable here:\n"
            f"    {reason}\n\n"
            f"Tiers 2 and 3 need pykx + a kdb+ licence. If pykx is installed in "
            f"another env, this run picked the wrong interpreter — a bare "
            f"`pytest` resolves through PATH, so prefer `python -m pytest` from "
            f"the env that has it:\n"
            f"    python -m pytest -q -m {markexpr!r}\n"
            f"Tier 1 needs neither pykx nor a licence:\n"
            f"    pytest -q -m 'not needs_q'\n\n"
            f"{_SKIP_IS_DELIBERATE}"
        )

    if not tier3_targeted(config):
        return

    reason = qlint_reason()
    if reason:
        raise pytest.UsageError(
            f"-m {markexpr!r} selects tier 3 only, but it cannot run here:\n"
            f"    {reason}\n\n"
            f"Every tier-3 test asserts something about qlint's own output, so "
            f"there is nothing left to run without a working linter. Point "
            f"{QLINT_DIR_ENV_VAR} at a full AxLibraries `ws/` tree — not just "
            f"the `qlint.q_` loader stub — and leave {QLINT_ENV_VAR} unset or "
            f"`auto`; `mcp_qsql_shared/qlint_policy.py`'s QLINT_INSTALL_HINT has "
            f"the download steps. To run the tiers this box does support:\n"
            f"    pytest -q -m 'needs_q and not needs_qlint'\n"
            f"    pytest -q -m 'not needs_q'\n\n"
            f"{_SKIP_IS_DELIBERATE}"
        )
