"""Execution primitives for the MCP loop's runtime gate + self-consistency
clustering (``mcp_server.self_consistency``).

Builds dummy tables from ``table_schemas`` (reusing ``typeGen`` /
``parseSchema0`` / ``genTable0`` from ``mcp_server.type_gen``) and evaluates
candidate q code via ``@[value;code;{"ERR:",x}]`` to catch runtime errors
(``'rank``, ``'type``, ``'length``, timeout, etc). Also loads the relational-
compare / shape-introspection helpers (``mcp_server/eval_compare.q``) used to
decide whether two candidates' results are execution-equivalent, and whether
a result is degenerate (a bare function / unexpected atom rather than a real
query result).

Assumes pykx is already loaded and ``PYKX_THREADING=1`` is set — both done
by ``mcp_qsql_shared.qlint_common``, which must be imported before this
module.

All functions here are synchronous and touch the single embedded q session
directly — callers (``mcp_server.self_consistency`` / ``mcp_server.loop``)
are responsible for serializing access via ``loop._run_pykx``, since
embedded q is single-threaded.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path

import pykx as kx

from mcp_server.type_gen import TYPE_GEN_STMTS

log = logging.getLogger(__name__)


DEFAULT_NROWS = 20
DEFAULT_TIMEOUT_S = 3
# Cross-seed clustering needs >1 seed so that "two candidates happen to
# agree on one dummy dataset by coincidence" doesn't get counted as real
# execution-equivalence — candidates must agree on EVERY seed tested.
DEFAULT_SC_SEEDS: tuple[int, ...] = (42, 43, 44)
# These two defaults are overridable at process/session startup via
# MCP_QSQL_SC_SEEDS / MCP_QSQL_SC_NROWS (mcp_server.loop.SC_SEEDS /
# SC_NROWS) — the values here are the fallback when those env vars are
# unset or invalid.

EVAL_COMPARE_Q_PATH = (Path(__file__).parent / "eval_compare.q").resolve()

_TYPEGEN_LOADED = False
_EVALCMP_LOADED = False

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_RE = re.compile(r"^`[A-Za-z]")

# `.eval.descVal` prefixes for results that are almost certainly not the
# intended query output: a bare function (candidate wrote a projection/lambda
# but never called it) or a bare symbol atom (likely a stray column-name
# reference instead of the actual result). Detected generically — no oracle
# needed, unlike the oracle-side degenerate check in the maintainer-side
# offline analysis this was ported from (not part of this repo).
_DEGENERATE_DESC_PREFIXES = ("list type 100", "scalar -11")


def _ensure_typegen() -> None:
    """Load ``TYPE_GEN_STMTS`` into the embedded q. Idempotent."""
    global _TYPEGEN_LOADED
    if _TYPEGEN_LOADED:
        return
    for stmt in TYPE_GEN_STMTS:
        kx.q(stmt)
    _TYPEGEN_LOADED = True


def _ensure_evalcmp() -> None:
    """Load ``eval_compare.q`` (``.eval.cmp`` / ``.eval.descVal`` / ...). Idempotent."""
    global _EVALCMP_LOADED
    if _EVALCMP_LOADED:
        return
    kx.q(f"\\l {EVAL_COMPARE_Q_PATH}")
    _EVALCMP_LOADED = True


def _kx_chars_to_str(obj) -> str:
    """Best-effort extract a Python string from a pykx char vector."""
    try:
        py = obj.py()
    except Exception:
        return str(obj)
    if isinstance(py, (bytes, bytearray)):
        return py.decode(errors="replace")
    return str(py)


def _safe_delete(name: str) -> None:
    if not _NAME_RE.match(name):
        return
    try:
        kx.q(f"delete {name} from `.")
    except Exception:
        pass


def validate_table_schemas(table_schemas: dict[str, str] | None) -> str | None:
    """Return ``None`` if ``table_schemas`` looks well-formed, else a
    human-readable skip reason. Validate once per case (schemas don't vary
    across candidates or seeds within one ``generate_qsql`` call)."""
    if not table_schemas:
        return "no table_schemas"
    for name, sdef in table_schemas.items():
        if not _NAME_RE.match(name):
            return f"invalid table name: {name!r}"
        if not isinstance(sdef, str) or not _SCHEMA_RE.match(sdef):
            return f"unexpected schema string for {name!r}"
    return None


def preflight_check(table_schemas: dict[str, str] | None, nrows: int,
                     seed: int = DEFAULT_SC_SEEDS[0]) -> str | None:
    """Return a skip reason if self-consistency clustering can't run for this
    case, else ``None``. Cheap regex validation first
    (``validate_table_schemas``), then one real build+drop cycle — catches
    q-level failures (e.g. ``parseSchema0`` returning empty for a
    malformed-but-regex-matching schema) that the regex can't see. Callers
    use this once per case, before deciding whether to run the multi-round
    self-consistency path at all (see ``mcp_server.loop``).
    """
    reason = validate_table_schemas(table_schemas)
    if reason:
        return reason
    try:
        names = build_dummy_tables(table_schemas, nrows, seed)
    except Exception as e:
        return f"table build failed: {type(e).__name__}: {e}"
    drop_tables(names)
    return None


def build_dummy_tables(
    table_schemas: dict[str, str], nrows: int, seed: int
) -> list[str]:
    """Create dummy tables for one ``seed``. Caller must have already
    validated ``table_schemas`` via ``validate_table_schemas``. Returns the
    created table names (== ``table_schemas.keys()``); caller is responsible
    for dropping them (``drop_tables``) once that seed's candidates are done.

    Raises ``RuntimeError`` if ``parseSchema0`` can't parse a schema string —
    this should not happen for schemas that passed ``validate_table_schemas``,
    but a malformed-but-regex-matching schema (e.g. bad column/type count)
    can still fail at generation time.
    """
    _ensure_typegen()
    kx.q(f'system "S {int(seed)}"')
    kx.q(f"NROWS0:{int(nrows)}")
    created: list[str] = []
    for name, sdef in table_schemas.items():
        schema = kx.q("parseSchema0", kx.CharVector(sdef))
        if len(schema) == 0:
            drop_tables(created)
            raise RuntimeError(f"parseSchema0 returned empty for {name!r}: {sdef!r}")
        tbl = kx.q("genTable0", schema, int(nrows))
        kx.q("set", kx.SymbolAtom(name), tbl)
        created.append(name)
    return created


def drop_tables(names: list[str]) -> None:
    for name in names:
        _safe_delete(name)


# Pristine copies of the current seed's dummy tables, kept outside the root
# namespace: `snapshot_globals` / `cleanup_new_globals` only see root, so a
# candidate's cleanup can never sweep them, and candidate code would have to
# name this namespace to collide with it.
_PRISTINE_NS = ".mcpqsql.pristine"


def save_pristine(names: list[str]) -> None:
    """Copy each freshly built table under ``_PRISTINE_NS`` for
    ``isolated_candidate`` to restore from. Cheap: q copies on write, so this
    and every restore share one set of 20-row columns until a candidate
    amends its copy."""
    for name in names:
        if _NAME_RE.match(name):
            kx.q(f"{_PRISTINE_NS}.{name}:{name}")


def drop_pristine() -> None:
    try:
        kx.q("delete pristine from `.mcpqsql")
    except Exception:
        pass


@contextmanager
def isolated_candidate(table_names: list[str], result_var: str | None):
    """Give one candidate execution a clean, identical starting state.

    On entry, restores every table in ``table_names`` from its
    ``save_pristine`` copy — an earlier candidate may have reassigned,
    filtered or deleted one (``trade:select from trade where ...``). On exit,
    deletes every root global the body created except ``result_var``, so a
    helper one candidate defined (``f:{...}``) is not there for the next one
    to lean on. Without both, a candidate could pass, or agree with its
    neighbours, only because of what ran before it.

    Root namespace only: ``.ns.x`` globals, and pre-existing globals a
    candidate overwrites, are not restored.
    """
    for name in table_names:
        if _NAME_RE.match(name):
            kx.q(f"{name}:{_PRISTINE_NS}.{name}")
    before = snapshot_globals()
    try:
        yield
    finally:
        cleanup_new_globals(before, {result_var} if result_var else set())


def exec_capture(code: str, result_var: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """Execute ``code`` against whatever dummy tables are currently bound,
    and bind the result to global ``result_var`` on success.

    Returns ``{"ok": bool, "err": str | None}``. ``ok=False`` means the code
    raised or timed out (``err`` carries the message, ``"timeout after Ns"``
    for the timeout case); ``result_var`` is left unbound in that case.
    """
    if not _NAME_RE.match(result_var):
        raise ValueError(f"unsafe result_var name: {result_var!r}")
    kx.q(f'system "T {int(timeout_s)}"')
    try:
        res = kx.q('{[c] @[value;c;{"ERR:",x}]}', kx.CharVector(code))
    finally:
        kx.q('system "T 0"')
    # NOTE: explicit parens are load-bearing — q is right-to-left, so
    # ``count x and Y`` parses as ``count (x and Y)``, throwing 'type when x
    # is a table containing timestamp / symbol columns.
    is_err = bool(kx.q('{(10h=type x) and (4<=count x) and ("ERR:"~4#x)}', res))
    if is_err:
        err_msg = _kx_chars_to_str(kx.q("4_", res))
        if err_msg == "stop":
            return {"ok": False, "err": f"timeout after {timeout_s}s"}
        return {"ok": False, "err": err_msg}
    kx.q("set", kx.SymbolAtom(result_var), res)
    return {"ok": True, "err": None}


def describe_result(result_var: str) -> str:
    """``.eval.descVal`` shape string for the value bound at ``result_var``."""
    _ensure_evalcmp()
    if not _NAME_RE.match(result_var):
        raise ValueError(f"unsafe result_var name: {result_var!r}")
    return _kx_chars_to_str(kx.q(f".eval.descVal[{result_var}]"))


def is_degenerate_result(result_var: str) -> bool:
    """True if the value bound at ``result_var`` is a bare function or a
    bare symbol atom — almost certainly not the intended query output."""
    desc = describe_result(result_var)
    return desc.startswith(_DEGENERATE_DESC_PREFIXES)


def results_equal(var_a: str, var_b: str) -> bool:
    """``.eval.cmp`` between the values bound at ``var_a`` / ``var_b``."""
    _ensure_evalcmp()
    if not (_NAME_RE.match(var_a) and _NAME_RE.match(var_b)):
        raise ValueError(f"unsafe result var name(s): {var_a!r}, {var_b!r}")
    try:
        return bool(kx.q(f".eval.cmp[{var_a}; {var_b}]"))
    except kx.QError as e:
        # Safety net: `.eval.cmp` is meant to answer 0b, not signal, on any
        # pair of unlike shape (it once signalled 'type on a keyed table vs a
        # plain dict). A signal still means "not shown equal" -- but letting
        # it escape fails the whole generate_qsql call, and swallowing it
        # silently hides a `.eval.cmp` bug that splits clusters, so log it.
        log.warning(".eval.cmp signalled on %s vs %s: %s", var_a, var_b, e)
        return False


def snapshot_globals() -> set[str]:
    """Names bound in the q root namespace right now (``key `.``)."""
    r = kx.q("key `.")
    names = r.py() if hasattr(r, "py") else r
    return {n.decode() if isinstance(n, bytes) else str(n) for n in names}


def cleanup_new_globals(before: set[str], protect: set[str]) -> None:
    """Delete any root-namespace name that appeared since ``before`` and
    isn't in ``protect``. Candidate code routinely assigns intermediate
    helpers (``enriched:...``, ``buildAgg:{...}``) that are never referenced
    again but persist as globals — across a long-running server handling
    many self-consistency calls (each executing up to 16 candidates x 3
    seeds), this accumulates until the embedded q session becomes unstable.
    Best-effort: swallows exceptions rather than letting cleanup failure
    surface as a case failure.
    """
    try:
        after = snapshot_globals()
    except Exception:
        return
    leaked = after - before - protect
    for name in leaked:
        if not _NAME_RE.match(name):
            continue
        try:
            kx.q(f"![`.;();0b;enlist`{name}]")
        except Exception:
            pass
