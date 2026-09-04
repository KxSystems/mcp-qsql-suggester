"""Shared helpers for qSQL static analysis.

Used by ``mcp_qsql_shared/qlint_kx.py`` (KX qlint loader, called
by ``mcp_server.loop``). Contains q parse helpers, select/update tree
walkers, C1B / C4B detection helpers, KX qlint loader, JSONL I/O, and
CLI scaffolding.

Only five names are used from outside this file: ``_parse_safe``,
``qlint_gate``, ``resolve_qlint_gate``, ``_probe_lintitem`` and
``_ensure_qlint``. The rest — the C1B / C4B aggregation walkers, the
time-literal helpers, and the JSONL / argparse scaffolding — are **inert
in this repo**: they belong to lint checks and a batch harness that are
not part of the server's gate stack (parse -> C9 -> multi-seed exec).
The module is shared verbatim with a separate maintainer-side tree that
does call them, so they are kept here to avoid forking the file rather
than because anything here needs them. Do not read their presence as a
claim that this server runs those checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from mcp_qsql_shared.qlint_policy import (
    QLINT_ENV_VAR,
    QLINT_OFF,
    QLINT_REQUIRED,
    QlintGate,
    QlintUnavailable,
    load_qlint_mode,
    locate_qlint,
    missing_qlint_message,
)

log = logging.getLogger(__name__)

# pykx defaults to ``noupdate`` mode for any non-main thread — q globals
# can't be written from worker threads. ``.qlint.lintItem`` writes to
# ``.axqc.INPUT`` internally, so without this every call from a worker
# raised ``noupdate``, was swallowed by the surrounding try/except, and
# silently returned an empty issues list — letting bad rows slip through
# C9 in multi-agent generation. Setting ``PYKX_THREADING=1`` before
# ``import pykx`` enables the documented secondary-thread mode.
os.environ.setdefault("PYKX_THREADING", "1")

import pykx as kx  # noqa: E402


# ---------------------------------------------------------------------------
# KX qlint loader (used by qlint_kx.py / C9)
# ---------------------------------------------------------------------------

_qlint_loaded = False

#: Resolved once, by whichever of ``resolve_qlint_gate`` / ``_ensure_qlint``
#: runs first. Both are called from the single pykx worker thread (server
#: startup, then per-candidate lint calls), so no locking is needed here.
_qlint_gate: QlintGate | None = None


def _load_qlint_file(qlint_file: Path) -> None:
    """``\\l`` the file with cwd set to its directory (relative loads inside it).

    qlint.q_ is a compiled script that loads siblings out of the same
    ax-libraries ``ws/`` tree, which is why the chdir is load-bearing and why
    ``AXLIBRARIES_HOME`` does not need to be set.
    """
    qlint_dir = str(qlint_file.parent)
    prev_dir = os.getcwd()
    kx.q(f"\\cd {qlint_dir}")
    kx.q("\\l qlint.q_")
    os.chdir(prev_dir)
    kx.q(f"\\cd {prev_dir}")


def _probe_lintitem() -> None:
    r"""Prove ``.qlint.lintItem`` is actually callable. Raises if it is not.

    Load success is NOT proof of usability. ``qlint.q_`` is a ~2 KB loader stub
    that pulls in ~50 sibling ``.qlint.*.q_`` / ``.ax*.q_`` files and
    ``lib/<os>/*`` shared objects from the same AxLibraries ``ws/`` tree. When
    those siblings are missing, q reports the failed sub-loads on **stderr and
    returns normally** — ``\l qlint.q_`` does not raise — so without this probe
    the gate would report itself active while every ``check_c9_qlint`` call
    silently fell into its own except branch and returned ``pass=True``.

    That is not hypothetical: our CI materialised ``qlint.q_`` alone from a
    secret for months, and the C9 gate was dead the whole time with nothing
    saying so.
    """
    kx.q(".qlint.lintItem", kx.CharVector("a:1"), kx.q("::"))


def resolve_qlint_gate(env: dict[str, str] | None = None) -> QlintGate:
    """Decide (once) whether the C9 gate runs, loading qlint.q_ if it does.

    Honours ``MCP_QSQL_QLINT`` — see ``mcp_qsql_shared.qlint_policy``:

    - ``off``: never load, gate disabled.
    - ``required``: raise if qlint.q_ is missing or fails to load.
    - ``auto`` (default): load if present; otherwise return a disabled gate
      carrying the reason, so the caller can warn and continue.

    Idempotent; the first call wins. The returned gate is what
    ``check_c9_qlint`` and the server's startup log read.
    """
    global _qlint_gate, _qlint_loaded
    if _qlint_gate is not None:
        return _qlint_gate

    mode, mode_warning = load_qlint_mode(env)
    if mode_warning:
        log.warning("%s", mode_warning)

    if mode == QLINT_OFF:
        _qlint_gate = QlintGate(
            mode=mode, enabled=False,
            reason=f"{QLINT_ENV_VAR}={QLINT_OFF} (gate explicitly disabled)",
        )
        return _qlint_gate

    qlint_file, reason = locate_qlint(env)
    if qlint_file is None:
        if mode == QLINT_REQUIRED:
            raise QlintUnavailable(missing_qlint_message(reason, mode))
        _qlint_gate = QlintGate(mode=mode, enabled=False, reason=reason)
        return _qlint_gate

    try:
        _load_qlint_file(qlint_file)
    except Exception as e:  # a present-but-unloadable qlint.q_
        reason = f"qlint.q_ at {qlint_file} failed to load: {e}"
        if mode == QLINT_REQUIRED:
            raise QlintUnavailable(missing_qlint_message(reason, mode)) from e
        _qlint_gate = QlintGate(mode=mode, enabled=False, reason=reason)
        return _qlint_gate

    try:
        _probe_lintitem()
    except Exception as e:
        reason = (
            f"qlint.q_ at {qlint_file} loaded, but .qlint.lintItem is not "
            f"callable ({e}). qlint.q_ is only a loader stub — it needs the rest "
            f"of the AxLibraries ws/ tree beside it (the .qlint.*.q_ / .ax*.q_ "
            f"siblings AND lib/<os>/* copied into lib/). Copying qlint.q_ on its "
            f"own is not enough, and q reports the missing sub-loads on stderr "
            f"without failing the load, so this has to be probed rather than "
            f"assumed."
        )
        if mode == QLINT_REQUIRED:
            raise QlintUnavailable(missing_qlint_message(reason, mode)) from e
        _qlint_gate = QlintGate(mode=mode, enabled=False, reason=reason)
        return _qlint_gate

    _qlint_loaded = True
    _qlint_gate = QlintGate(
        mode=mode, enabled=True, qlint_dir=str(qlint_file.parent),
    )
    return _qlint_gate


def qlint_gate() -> QlintGate:
    """The resolved gate, resolving it now if nobody has yet."""
    return _qlint_gate if _qlint_gate is not None else resolve_qlint_gate()


def _ensure_qlint() -> None:
    """Load qlint.q_, raising if it cannot be used.

    Retained for callers that genuinely cannot proceed without C9 (and for
    ``MCP_QSQL_QLINT=required`` startup). Code that should tolerate a missing
    qlint — i.e. the MCP server's gate 2 — must go through
    ``resolve_qlint_gate`` / ``qlint_gate`` instead.
    """
    if _qlint_loaded:
        return
    gate = qlint_gate()
    if not gate.enabled:
        raise QlintUnavailable(
            missing_qlint_message(gate.reason or "qlint.q_ unavailable", QLINT_REQUIRED)
        )


# ---------------------------------------------------------------------------
# pykx wrapper helpers
# ---------------------------------------------------------------------------

# Vector-to-vector types in pykx that should NOT appear as the outermost
# function in a select...by column expression.
_V2V_TYPES = (kx.wrappers.Scan, kx.wrappers.EachPrior)

# q built-in names that are vector-to-vector (used as fallback string check)
_V2V_NAMES = frozenset([
    "maxs", "mins", "sums", "prds", "deltas", "ratios", "fills",
    "avgs", "mcount", "mavg", "msum", "mmax", "mmin", "mdev",
    "|\\", "&\\", "+\\", "*\\", "-':", "%':", "^\\",
])

# q aggregator function names (collapse a vector to a scalar). Resolved to
# function values via q at first use, so that ``~`` (value equality) can
# match regardless of how the function appears in a parse tree (e.g. ``first``
# parses to operator ``*:``, ``med`` parses to a ``k){...}`` lambda body).
_AGG_NAMES = (
    "sum", "count", "avg", "max", "min", "first", "last",
    "med", "dev", "var", "sdev", "svar", "prd",
    "any", "all", "distinct", "wsum", "wavg",
    "cor", "cov",
)
# Functions that always produce a vector output (regardless of arg shape).
# Includes V2V (vector → same-length vector) and S2V (scalar → vector).
# Used by ``_is_aggregating`` to short-circuit a column as non-aggregating.
_VECTOR_OUT_NAMES = (
    # V2V (length-preserving)
    "maxs", "mins", "sums", "prds", "deltas", "ratios", "fills",
    "avgs", "mcount", "mavg", "msum", "mmax", "mmin", "mdev",
    # S2V (scalar → vector)
    "til", "enlist", "where",
    # adverbs that always produce a vector (each/each-left/each-right)
    "each", "(')", "(\\:)", "(/:)",
)
_agg_fn_values: list | None = None
_vector_out_fn_values: list | None = None


def _get_agg_fn_values() -> list:
    """Return cached list of aggregator function values from q."""
    global _agg_fn_values
    if _agg_fn_values is None:
        _agg_fn_values = [kx.q(name) for name in _AGG_NAMES]
    return _agg_fn_values


def _get_vector_out_fn_values() -> list:
    """Return cached list of vector-output (V2V + S2V) function values."""
    global _vector_out_fn_values
    if _vector_out_fn_values is None:
        _vector_out_fn_values = [kx.q(name) for name in _VECTOR_OUT_NAMES]
    return _vector_out_fn_values


def _is_aggregator_fn(fn) -> bool:
    """Check via ``~`` (value equality) if fn is a known aggregator primitive.

    Matches ``first`` even when it parses as the operator ``*:``, and matches
    ``med``/``sdev``/``svar`` even when they appear as ``k){...}`` lambdas.
    """
    for agg_fn in _get_agg_fn_values():
        try:
            if kx.q("~", fn, agg_fn).py():
                return True
        except Exception:
            continue
    return False


def _is_vector_out_fn(fn) -> bool:
    """Check if fn always produces a vector output.

    Covers the existing scan/each-prior types (``+\\``, ``-':`` etc.) plus
    keyword V2V (``sums``/``prds``/``mavg``...) and S2V (``til``/``enlist``/
    ``where``) by value equality. The latter take a scalar but produce a
    vector, so for C8's purpose they're equivalent to V2V — the column does
    not collapse to a scalar even if all args are aggregating.
    """
    if isinstance(fn, _V2V_TYPES):
        return True
    for v_fn in _get_vector_out_fn_values():
        try:
            if kx.q("~", fn, v_fn).py():
                return True
        except Exception:
            continue
    return False


def _is_aggregating(node, scalar_names: frozenset[str] = frozenset()) -> bool:
    """Return True if a column expression collapses to a scalar per group.

    Recursive rule:
      * ``SymbolAtom`` (bare ref)       → False, unless the name is in
                                          ``scalar_names`` (a known scalar
                                          global like ``coupon: 0.06``)
      * ``Vector`` literal               → False
      * scalar atom literal              → True
      * list outer = aggregator          → True  (collapses regardless of args)
      * list outer = vector-out fn       → False (always produces a vector)
      * list outer = SymbolAtom (named fn ref like ``es99[ret]``) → True
      * list outer = Lambda (unknown user fn) → True (lenient: avoid FPs)
      * other list (binary op / atomic)  → True iff every arg is aggregating

    The named-fn / unknown-Lambda branches are pragmatic: parse-only analysis
    can't tell what a user lambda returns, and many finance training cases
    use scalar-returning custom helpers (``es95``, ``betaFn``, etc.). Assuming
    aggregating dodges those FPs at the cost of missing rare vector-returning
    user lambdas.

    ``scalar_names`` lets the caller flag known scalar globals defined earlier
    in the response (e.g. ``coupon: 0.06`` or ``totalVal: exec sum x from t``)
    so that a bare reference like ``coupon * sum mktVal % totalVal`` is
    correctly treated as scalar arithmetic, not a vector column expression.
    """
    if isinstance(node, kx.SymbolAtom):
        return str(node) in scalar_names
    # Generic list (function application) is a subclass of Vector — check first.
    if isinstance(node, (kx.List, kx.wrappers.List)):
        try:
            outer = node[0]
        except Exception:
            return True
        if _is_aggregator_fn(outer):
            return True
        if _is_vector_out_fn(outer):
            return False
        # Unknown function reference (named) or user lambda → assume aggregating.
        if isinstance(outer, (kx.SymbolAtom, kx.Lambda)):
            return True
        try:
            for i in range(1, len(node)):
                if not _is_aggregating(node[i], scalar_names):
                    return False
            return True
        except Exception:
            return True
    # Symbol literals in select-clauses are parsed as 1-element SymbolVectors
    # (q's enlist trick to disambiguate them from bare column refs). Treat as
    # scalar — at evaluation time they collapse to atom values.
    if isinstance(node, kx.SymbolVector) and len(node) == 1:
        return True
    if isinstance(node, kx.Vector):
        return False
    if isinstance(node, kx.Atom):
        return True
    return True


def _collect_scalar_assignments(tree) -> frozenset[str]:
    """Walk a (multi-)statement parse tree and return names assigned to scalars.

    Recognises:
      * ``name: <scalar_literal>``   — e.g. ``coupon: 0.06``, ``nav: 1e6``
      * ``name: <agg_expression>``   — RHS that ``_is_aggregating`` accepts
      * ``name: exec <agg_col> from t`` — single-column ``exec`` with an
        aggregating column expression collapses to a scalar
    """
    names: set[str] = set()
    for stmt in _unpack_statements(tree):
        if not _is_assignment(stmt):
            continue
        try:
            name = str(stmt[1])
            rhs = stmt[2]
        except Exception:
            continue
        if _rhs_is_scalar(rhs):
            names.add(name)
    return frozenset(names)


def _is_assignment(node) -> bool:
    r"""Check if a parse tree node is ``name: rhs`` (i.e. ``(:; \`name; rhs)``)."""
    try:
        if len(node) != 3:
            return False
        op = node[0]
        if not (isinstance(op, kx.Operator) and str(op) == ":"):
            return False
        return isinstance(node[1], kx.SymbolAtom)
    except Exception:
        return False


def _rhs_is_scalar(rhs) -> bool:
    """Return True if the RHS of an assignment evaluates to a scalar."""
    # An ``exec`` parse tree: ``(?; t; where; (); cols)`` (4th elem is empty
    # list, distinguishing it from ``select``'s 4th elem which is ``0b`` or
    # a Dictionary). Single-col exec with an aggregating col-expr → scalar.
    try:
        if (len(rhs) == 5
                and str(rhs[0]) == "?"
                and isinstance(rhs[3], (kx.List, kx.wrappers.List))
                and len(rhs[3]) == 0):
            cols = rhs[4]
            # single-col exec: cols is an enlisted expression (1-elem list)
            if isinstance(cols, (kx.List, kx.wrappers.List)) and len(cols) == 1:
                return _is_aggregating(cols[0])
            return False
    except Exception:
        pass
    return _is_aggregating(rhs)


def _parse_safe(code: str):
    """Try to parse q code. Returns (True, parse_tree) or (False, error_msg)."""
    try:
        tree = kx.q("{parse x}", kx.CharVector(code))
        return True, tree
    except Exception as e:
        return False, str(e)


def _is_select(tree) -> bool:
    """Return True if the parse tree represents a select statement."""
    try:
        return len(tree) == 5 and str(tree[0]) == "?"
    except Exception:
        return False


def _is_update(tree) -> bool:
    """Return True if the parse tree represents an update statement."""
    try:
        return len(tree) == 5 and str(tree[0]) == "!"
    except Exception:
        return False


def _has_by(tree) -> bool:
    """Return True if a select/update parse tree has a by clause."""
    try:
        return isinstance(tree[3], kx.Dictionary)
    except Exception:
        return False


def _is_semicolon(node) -> bool:
    """Check if a parse tree node is the multi-statement ';' separator."""
    return isinstance(node, kx.CharAtom) and node.py() == b";"


def _find_select_trees(tree) -> list:
    """Find all select/update sub-trees in a (possibly multi-statement) parse tree.

    Recurses into found select/update sub-trees as well, so that nested forms
    (e.g. ``select from (select y: bare by g from t) where y>0``) are all
    returned. C8 needs this to check inner select-by clauses for aggregation.
    """
    results = []
    if _is_select(tree) or _is_update(tree):
        results.append(tree)
        # fall through to recurse into the select's args (table source, where,
        # by, cols) — they may contain further nested select/update trees
    try:
        if _is_semicolon(tree[0]):
            for i in range(1, len(tree)):
                results.extend(_find_select_trees(tree[i]))
            return results
    except Exception:
        pass
    try:
        if str(tree[0]) == ":" and len(tree) == 3:
            results.extend(_find_select_trees(tree[2]))
            return results
    except Exception:
        pass
    try:
        for i in range(len(tree)):
            child = tree[i]
            if isinstance(child, (kx.List, kx.wrappers.List)):
                results.extend(_find_select_trees(child))
    except Exception:
        pass
    return results


def _unpack_statements(tree) -> list:
    """Unpack a multi-statement parse tree into individual statements."""
    try:
        if _is_semicolon(tree[0]):
            return [tree[i] for i in range(1, len(tree))]
    except Exception:
        pass
    return [tree]


def _last_statement(tree):
    """Extract the last statement from a (possibly multi-statement) parse tree.

    Skips trailing generic null (::) caused by trailing semicolons.
    """
    try:
        if _is_semicolon(tree[0]):
            for i in range(len(tree) - 1, 0, -1):
                try:
                    if str(tree[i]).strip() == '::':
                        continue
                except Exception:
                    pass
                return tree[i]
    except Exception:
        pass
    return tree


# ---------------------------------------------------------------------------
# C1B: division-by-comparison detection
# ---------------------------------------------------------------------------


def _is_comparison_node(node) -> bool:
    """Check if a parse tree node is a comparison expression (< > >= <=)."""
    try:
        if len(node) != 3:
            return False
        op = node[0]
        if isinstance(op, kx.Operator) and str(op) in ("<", ">"):
            return True
        if isinstance(op, (kx.List, kx.wrappers.List)):
            s = str(op)
            if "<" in s or ">" in s:
                return True
        return False
    except Exception:
        return False


def _find_div_by_comparison(node, issues: list[str]) -> None:
    """Recursively find ``%`` where the right operand is a comparison.

    Detects the bug pattern ``(a+b)%c < 0.05`` which q parses as
    ``(a+b) % (c < 0.05)`` — dividing by a boolean.
    """
    try:
        if (len(node) == 3
                and isinstance(node[0], kx.Operator)
                and str(node[0]) == "%"):
            right = node[2]
            if _is_comparison_node(right):
                cmp_op = str(right[0]).replace("\n", " ").strip()
                issues.append(
                    f"division `%` has comparison `{cmp_op}` as right operand "
                    f"— likely missing parentheses (r-to-l eval bug)"
                )
        for i in range(len(node)):
            child = node[i]
            if isinstance(child, (kx.List, kx.wrappers.List)):
                _find_div_by_comparison(child, issues)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# C4B: time-type literal mismatch helpers
# ---------------------------------------------------------------------------


def _parse_schema_types(schemas: dict[str, str]) -> dict[str, str]:
    """Build {col_name: type_char} from all table schemas.

    Columns appearing in multiple schemas with different types are excluded.
    """
    col_types: dict[str, str] = {}
    conflicts: set[str] = set()
    for sdef in schemas.values():
        parts = sdef.split("!")
        if len(parts) != 2:
            continue
        cols = [c for c in parts[0].split("`") if c]
        types = [t for t in parts[1].split("`") if t]
        if len(cols) != len(types):
            continue
        for c, t in zip(cols, types):
            if c in col_types and col_types[c] != t:
                conflicts.add(c)
            else:
                col_types[c] = t
    for c in conflicts:
        col_types.pop(c, None)
    return col_types


_C4B_TIMESPAN_PAT = r'0D\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?'
_C4B_TIME_MS_PAT  = r'(?<!D)\d{2}:\d{2}:\d{2}\.\d{3}'
_C4B_TIME_SEC_PAT = r'(?<!D)\d{2}:\d{2}:\d{2}(?!\.\d)'
_C4B_INT_MS_PAT   = r'\(\d+\s*\*\s*60\s*\*\s*1000\)'
_C4B_ANY_LIT = (f'(?:{_C4B_TIMESPAN_PAT}|{_C4B_TIME_MS_PAT}'
                f'|{_C4B_INT_MS_PAT}|{_C4B_TIME_SEC_PAT})')

_C4B_XBAR_COL_RE = re.compile(
    rf'({_C4B_ANY_LIT})\s+xbar\s+(\w+)(?![\w.])'
)
_C4B_XBAR_DIFF_RE = re.compile(
    rf'({_C4B_ANY_LIT})\s+xbar\s*\((\w+)\s*-\s*(\w+)\)'
)
_C4B_DIV_DIFF_RE = re.compile(
    rf'\((\w+)\s*-\s*(\w+)\)\s*%\s*({_C4B_ANY_LIT})'
)


def _c4b_classify(lit: str) -> str:
    """Classify a time literal: 'timespan', 'time_ms', 'time_sec', or 'int_ms'."""
    if lit.startswith("0D"):
        return "timespan"
    if lit.startswith("("):
        return "int_ms"
    if "." in lit:
        return "time_ms"
    return "time_sec"


def _c4b_is_wrong(lit_class: str, col_type: str, context: str) -> bool:
    """Return True if the literal class is wrong for the column type and context.

    context is 'xbar' or 'div'.
    """
    if col_type == "t":
        if context == "xbar":
            return lit_class in ("timespan", "time_ms", "time_sec")
        return lit_class in ("timespan", "time_sec")
    if col_type == "p":
        if context == "xbar":
            return lit_class in ("time_ms", "time_sec")
        return lit_class in ("time_ms", "time_sec", "int_ms")
    return False


# ---------------------------------------------------------------------------
# C4B literal conversion (helpers for auto-fix / customized qlint; retained
# here for self-containment even though mcp_server itself does not call these
# directly)
# ---------------------------------------------------------------------------


def _time_lit_to_ms(lit: str) -> int | None:
    """Parse any recognized time literal to total milliseconds."""
    if lit.startswith("0D"):
        parts = lit[2:].split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = float(parts[2]) if len(parts) > 2 else 0.0
        return int((h * 3600 + m * 60 + s) * 1000)
    if lit.startswith("(") and lit.endswith(")"):
        inner = lit[1:-1].replace(" ", "")
        try:
            result = 1
            for p in inner.split("*"):
                result *= int(p)
            return result
        except ValueError:
            return None
    parts = lit.split(":")
    if len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])
        return int((h * 3600 + m * 60 + s) * 1000)
    return None


def _ms_to_time(ms: int) -> str:
    """Format ms as HH:MM:SS.mmm (for type t division fix)."""
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    r = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{r:03d}"


def _ms_to_timespan(ms: int) -> str:
    """Format ms as 0DHH:MM:SS (for type p xbar fix)."""
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    return f"0D{h:02d}:{m:02d}:{s:02d}"


def _ms_to_timespan_compact(ms: int) -> str:
    """Format ms as 0DHH:MM if whole minutes, else 0DHH:MM:SS (for type p division)."""
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    if s == 0 and ms % 60000 == 0:
        return f"0D{h:02d}:{m:02d}"
    return f"0D{h:02d}:{m:02d}:{s:02d}"


def _ms_to_factored_int(ms: int) -> str:
    """Format ms as (N*60*1000) when whole minutes, else (N*1000), else raw int."""
    if ms % 60000 == 0:
        n = ms // 60000
        return f"({n}*60*1000)"
    if ms % 1000 == 0:
        n = ms // 1000
        return f"({n}*1000)"
    return str(ms)


def _get_fix_literal(ms: int, col_type: str, context: str) -> str:
    """Return the correct replacement literal for a given ms value.

    col_type is 't' or 'p'; context is 'xbar' or 'div'.
    """
    if col_type == "t":
        return _ms_to_factored_int(ms) if context == "xbar" else _ms_to_time(ms)
    return _ms_to_timespan(ms) if context == "xbar" else _ms_to_timespan_compact(ms)


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def load_examples(data_path: Path, limit: int = 0):
    """Load a JSONL file. Returns (examples, raw_lines)."""
    examples = []
    raw_lines = []
    with open(data_path) as f:
        for line in f:
            if line.strip():
                examples.append(json.loads(line))
                raw_lines.append(line.rstrip("\n"))
    if limit > 0:
        examples = examples[:limit]
        raw_lines = raw_lines[:limit]
    return examples, raw_lines


def write_results(out_path: Path, results) -> None:
    """Write all verdicts as JSONL. results is a list of (verdict_dict, raw_line)."""
    with open(out_path, "w") as f:
        for r, _raw in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_passing(out_path: Path, results) -> None:
    """Write only passing examples (original raw JSONL lines)."""
    with open(out_path, "w") as f:
        for r, raw in results:
            if r["verdict"] == "pass":
                f.write(raw + "\n")


def print_summary(n_pass: int, total: int, results, checks,
                  results_path: Path, passing_path: Path) -> None:
    """Print the trailing summary block (== separator, per-check breakdown, paths)."""
    n_fail = total - n_pass
    pct = 100 * n_pass / total if total > 0 else 0
    print()
    print("=" * 60)
    print(f"Results: {n_pass}/{total} pass ({pct:.1f}%), {n_fail} fail")
    for chk in checks:
        chk_fail = sum(
            1 for r, _ in results
            if any(c["check"].startswith(chk.upper()) and not c["pass"]
                   for c in r["checks"])
        )
        if chk_fail:
            print(f"  {chk.upper()}: {chk_fail} failures")
    print(f"\nResults:  {results_path}")
    print(f"Passing:  {passing_path} ({n_pass} examples)")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_args(parser) -> None:
    """Add the input file + verbosity flags shared by both gates."""
    parser.add_argument("data", help="Path to input JSONL file")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process first N examples only (0 = all)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print each example result")
    parser.add_argument("--fail-only", action="store_true",
                        help="In verbose mode, only print failures")
