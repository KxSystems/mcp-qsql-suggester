"""Policy for the KX qlint (C9) gate — whether it is required, optional, or off.

Deliberately **pykx-free**. The gate has to be decided before anything touches
embedded q, the decision is worth unit-testing without a kdb+ licence, and the
loader that consumes it (``mcp_qsql_shared.qlint_common``) imports pykx at
module scope. Keep this module free of pykx and of any q call.

`qlint.q_` is licensed separately from this repo and cannot be redistributed
with it (see the repo's ``.gitignore``), so a fresh install often will not have
it. `MCP_QSQL_QLINT` decides what happens then:

    auto (default)  use qlint if present; otherwise disable the gate, warn
                    loudly at startup, and mark every response.
    required        fail fast if qlint is unavailable. Set this on a shared
                    deployment where you want a hard guarantee that the gate
                    is really running.
    off             skip qlint even when it is present — useful for measuring
                    what C9 actually contributes over parse + execution.

Two spellings are three-valued rather than binary (unlike ``MCP_RUNTIME_EXEC``)
because "use it if you have it" is a genuinely different state from both "must
have it" and "don't use it".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

QLINT_AUTO = "auto"
QLINT_REQUIRED = "required"
QLINT_OFF = "off"

QLINT_MODES = (QLINT_AUTO, QLINT_REQUIRED, QLINT_OFF)

QLINT_ENV_VAR = "MCP_QSQL_QLINT"
QLINT_DIR_ENV_VAR = "QLINT_DIR"

#: Where to actually get the file. Shown in both the fatal error (required
#: mode) and the startup warning (auto mode), so it lives in one place.
QLINT_INSTALL_HINT = (
    "qlint.q_ is part of KX's AxLibraries collection, which ships inside the\n"
    "KX Developer download: https://code.kx.com/developer/ (EULA-gated, so it\n"
    "cannot be fetched unattended). KX Analyst is the larger product and ships\n"
    "the same AxLibraries -- Developer is the smaller download and is enough.\n"
    "KX's own step-by-step reference (written on the Analyst pages, but the\n"
    "steps are identical): https://code.kx.com/analyst/libraries/#download\n"
    "  1. unzip the KX Developer archive\n"
    "  2. unzip the ax-libraries.zip contained inside it\n"
    "  3. copy ax-libraries/ws/lib/<your-os>/* into ax-libraries/ws/lib/\n"
    "     (the shared objects qlint loads at runtime)\n"
    "  4. export QLINT_DIR=<...>/ax-libraries/ws\n"
    "KX's instructions also have you set AXLIBRARIES_HOME. This server does\n"
    "not need it -- the loader chdirs into QLINT_DIR before `\\l qlint.q_`, so\n"
    "qlint's relative loads resolve either way -- but setting it is harmless\n"
    "and other AxLibraries tools do expect it."
)

#: What a caller loses when the gate is off, in one line. Used in responses.
QLINT_DISABLED_NOTE = (
    "KX qlint C9 gate disabled — candidates were filtered by q parse and "
    "execution only"
)


class QlintUnavailable(RuntimeError):
    """qlint.q_ cannot be used and ``MCP_QSQL_QLINT=required`` demands it.

    A dedicated type so the server can print this as a configuration error
    (message only, exit 1) while every other startup failure keeps its
    traceback.
    """


@dataclass(frozen=True)
class QlintGate:
    """The resolved decision. ``reason`` is set iff ``enabled`` is False."""

    mode: str
    enabled: bool
    qlint_dir: str | None = None
    reason: str | None = None

    @property
    def status(self) -> str:
        """The value reported to callers as ``lint_gate``."""
        return "kx_qlint_c9" if self.enabled else "disabled"


def load_qlint_mode(env: dict[str, str] | None = None) -> tuple[str, str | None]:
    """Read ``MCP_QSQL_QLINT``. Returns ``(mode, warning_or_None)``.

    An unrecognised value falls back to ``auto`` with a warning rather than
    failing startup — same convention as the other ``MCP_QSQL_*`` loaders in
    ``mcp_server.loop``.
    """
    environ = os.environ if env is None else env
    raw = environ.get(QLINT_ENV_VAR, "").strip().lower()
    if not raw:
        return QLINT_AUTO, None
    if raw in QLINT_MODES:
        return raw, None
    return QLINT_AUTO, (
        f"{QLINT_ENV_VAR}={raw!r} is not one of {'/'.join(QLINT_MODES)}; "
        f"falling back to {QLINT_AUTO!r}"
    )


def locate_qlint(env: dict[str, str] | None = None) -> tuple[Path | None, str | None]:
    """Find ``$QLINT_DIR/qlint.q_``. Returns ``(path, reason_unavailable)``.

    Exactly one of the two is None. Pure filesystem work — no q, no pykx.
    """
    environ = os.environ if env is None else env
    raw = environ.get(QLINT_DIR_ENV_VAR, "").strip()
    if not raw:
        return None, f"{QLINT_DIR_ENV_VAR} is not set"
    qlint_dir = Path(os.path.expanduser(raw))
    qlint_file = qlint_dir / "qlint.q_"
    if not qlint_file.exists():
        return None, f"qlint.q_ not found at {QLINT_DIR_ENV_VAR}={qlint_dir}"
    return qlint_file, None


def missing_qlint_message(reason: str, mode: str) -> str:
    """The message shown when qlint is unavailable, tailored to the mode."""
    if mode == QLINT_REQUIRED:
        return (
            f"{reason}, and {QLINT_ENV_VAR}={QLINT_REQUIRED} demands it.\n\n"
            f"{QLINT_INSTALL_HINT}\n\n"
            f"To run without the lint gate instead (q parse + execution gates "
            f"only), set {QLINT_ENV_VAR}={QLINT_AUTO} or {QLINT_ENV_VAR}={QLINT_OFF}."
        )
    return (
        f"{reason}.\n\n"
        f"The KX qlint C9 gate is DISABLED for this process. Candidates are\n"
        f"filtered by q parse and execution only, and every response will\n"
        f'carry "lint_gate": "disabled".\n\n'
        f"{QLINT_INSTALL_HINT}\n\n"
        f"To make a missing qlint.q_ a startup failure instead, set "
        f"{QLINT_ENV_VAR}={QLINT_REQUIRED}."
    )


def banner(text: str, width: int = 74) -> str:
    """Wrap a multi-line message in rules so it survives a busy startup log."""
    rule = "=" * width
    return f"{rule}\n{text}\n{rule}"
