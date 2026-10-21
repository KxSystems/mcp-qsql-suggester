# tests/

Four tiers, one per rung of the dependency ladder. Each rung is a separately
obtained thing — a kdb+ licence, a KX download, a GPU — so which tier you can
run is decided by what you have, not by what you choose.

| Tier | Adds | Count | Where it lives | Runs in CI? |
|---|---|---|---|---|
| **1** Pure logic | pytest, nothing else | 125 | `test_extract.py`, `test_self_consistency.py`, `test_qlint_policy.py`, `test_format_output.py`, `test_vllm_client.py` | ✅ `unit` |
| **2** Embedded q | pykx + a kdb+ licence | 74 | `test_pipeline_gates.py`, `test_eval_compare_skill.py` (`needs_q`) | ⚠️ only with a kdb+ licence secret |
| **3** Real qlint | a working `qlint.q_` | 6 | `test_pipeline_gates_qlint.py` (`needs_qlint`) | ❌ nobody's — see below |
| **4** Live model | a vLLM serving the model | 3 + 1 | `mcp_server/smoke_test.py`, `mcp_server/protocol_test.py` | ❌ by hand only |

## Running them

Install the dependencies you have access to, then run `pytest -q`. Tiers 2 and
3 skip themselves when their dependencies are absent, so **the same bare
command is correct at every rung** — no flag juggling.

```bash
# Rung 1 — nothing but pytest                        -> 98 passed, 3 skipped
pip install pytest
pytest -q

# Rung 2 — + pykx and a kdb+ licence                 -> 145 passed, 6 skipped
pip install -r requirements-server.txt
pytest -q

# Rung 3 — + a working qlint.q_                      -> 151 passed
export QLINT_DIR=<...>/ax-libraries/ws
pytest -q

# Rung 4 — + a vLLM serving the model. Not pytest; run by hand.
QLINT_DIR=$QLINT_DIR PYTHONPATH=. python -m mcp_server.smoke_test      # 3/3
QLINT_DIR=$QLINT_DIR PYTHONPATH=. python -m mcp_server.protocol_test
```

**A skipped tier always prints its reason** (`addopts = ["-ra"]` in
`pyproject.toml`), because the counts alone are ambiguous and misleadingly
small: a module-level skip counts **once**, not once per test, so rung 1 above
reports `3 skipped` for 53 absent tests, and nothing in `98 passed, 3 skipped`
distinguishes "no kdb+ licence on this box" from "`pytest` came off `PATH` and
that interpreter has no pykx". The reason line names the interpreter:

```
SKIPPED [1] tests/test_pipeline_gates.py:44: tier 2: pykx is not installed in /usr/bin/python3
```

If you see that on a box where pykx *is* installed, the run picked the wrong
interpreter. Prefer `python -m pytest -q` from the env that has pykx — a bare
`pytest` resolves through `PATH` and may not be that env's.

To run one tier in isolation — which is what the CI jobs do, so that their
scope cannot drift with whatever happens to be installed on a runner:

```bash
pytest -q -m "not needs_q"                  # tier 1        -> 98 passed
pytest -q -m "needs_q and not needs_qlint"  # tier 2        -> 74 passed
pytest -q -m needs_qlint                    # tier 3        -> 6 passed
```

**Naming a tier that cannot run here is an error, not a skip.** Those three
commands ask for a specific tier and nothing else, so answering `3 skipped, 98
deselected` — indistinguishable from the tolerated absence above — would answer
the wrong question. `tests/_tiers.py` probes the tier from
`conftest.pytest_configure` and fails the run before collection instead:

```
$ pytest -q -m needs_qlint            # QLINT_DIR unset
ERROR: -m 'needs_qlint' selects tier 3 only, but it cannot run here:
    QLINT_DIR is not set
```

Same for a `-m needs_q*` run on an interpreter without pykx, or with pykx
unlicensed. Both reactions come off one set of probes in `tests/_tiers.py`;
which reaction you get depends only on whether the run named the tier.

A broader `-m needs_q` is deliberately *not* fatal when only the linter is
missing: it still has tier 2 to run, and tier 3 dropping out of it is the
documented behaviour of an optional, separately-licensed dependency. Only a
tier-3-targeted run (tier 3 selected, tier 2 not) treats a missing linter as
fatal.

Tier 3 therefore carries **three** gates, and each buys something the others do
not: the marker is how you *select* it, the pre-collection probe is how a run
that named it *fails loudly*, and the module-wide autouse fixture is how a bare
`pytest` *tolerates* its absence. The fixture is also the only one of the three
that can see a `qlint.q_` which loads yet defines no `.qlint.lintItem` (the
loader stub without its `lib/`) — the pre-collection probe is a filesystem
check, deliberately pykx-free, so it cannot. Under a tier-3-targeted run that
fixture fails rather than skips, on the same rule.

## What each tier is for

**Tier 1** is the one an outside contributor can always run, which is why it
is the one public CI protects. It covers the clustering engine's decision
logic, fenced-code extraction, and the qlint gate policy — everything that can
be decided without touching embedded q.

**Tier 2** runs the real pipeline with the model stubbed: `vllm_client.chat`
returns fixed completions, so nothing depends on what qwen samples on the day,
but every candidate is genuinely parsed, executed against dummy tables across
all seeds, and clustered. No GPU, no vLLM, no qlint. This is the configuration
most outside contributors with a kdb+ licence will have, and it is the one tier
a CI job can cover given a licence secret — see "Tiers 2 and 3 in CI" below.

**Tier 3** is the subset whose claims are *about qlint's own output* — that a
response reports `lint_gate` truthfully, and that a parse failure carries
qlint's much more useful message alongside q's terse one (without being padded
by qlint's own `CANNOT_PARSE`, which just restates it). These are meaningless
without a linter, hence their own tier rather than a footnote on tier 2.

It runs in **no** CI, for reasons worth reading before anyone tries to fix
that: see "Tiers 2 and 3 in CI" below.

**Tier 4** is operational rather than regression testing — "is vLLM up, is the
served checkpoint still producing usable qSQL". Those scenarios sample the
model at a non-zero temperature, so they can go red on an unlucky draw rather
than on a code regression, which is why they are neither pytest nor CI.

## What tier 1 covers

`test_extract.py` — `mcp_qsql_shared.extract.extract_qsql`, the fence
stripping applied to every raw model completion before it is parsed,
hashed for dedup, or clustered.

`test_self_consistency.py` — the decision logic of the clustering
engine: round-cap preset validation, union-find, cluster formation,
winning-cluster selection and its tie-break, shortest-code pick, and the
`ROUND_MIN_WEIGHT` early-finalize thresholds.

`test_qlint_policy.py` — the `MCP_QSQL_QLINT` gate policy: mode parsing and
its invalid-value fallback, locating `qlint.q_`, and that the operator-facing
messages stay actionable (where to get qlint, how to opt out, how to harden).
`mcp_qsql_shared.qlint_policy` is kept pykx-free precisely so this can be
tier 1 — the gate has to be decided before anything touches embedded q.

`test_format_output.py` — the verdicts assembled by
`.claude/skills/qsql-coder/scripts/format_output.py`, the renderer both skills
share and the plugin ships. Every test here guards one property: **the verdict
may not claim something the run did not establish.** Added after a real
`/qsql-coder-quick` run printed `5/8 candidates agreed ... unanimous
self-consistency cluster` — the clause was a constant beside computed counts —
and grown as the same family of defect turned up in the *reasons* a verdict
gives. So it covers: a unanimity claim appearing exactly when the counts
support it; a plurality stating how split it was, without ruling on whether
that warrants a check (the threshold is tunable skill policy, and moved from
`1.0` to `0.6`); `verification_unavailable` replacing case A's reason list
rather than sitting beside it, since quick mode never consults the high-risk
idiom list and a missing `q` makes the check impossible rather than
unnecessary; case D naming the right remedy for each of those two reasons; the
field being refused on cases B and C, which are the verified cases by
definition; and the `lint_gate: disabled` disclosure composing with either
cluster shape. The assertions deliberately target those invariants rather than
the verdicts' exact wording, so copy-edits stay free and only a semantic
regression fails. It runs the script as a subprocess, which is
both the skills' real contract (a heredoc pipe) and the way to exercise a file
that is not an importable package.

`test_eval_compare_skill.py` — **tier 2** — the skill copy of
`eval_compare.q`, which is a deliberate fork of `mcp_server`'s: it treats two
temporal columns on the same resolution chain as equal (a `u` minute-of-day
bucket vs the `p` timestamp bucket covering the same minute), and the server
copy does not. The fork exists because the two ask different questions —
clustering asks "did independent samples agree?", where returning a different
type *is* disagreement and is the signal that raises the Bash-verify trigger;
Bash-verify asks "does this match my reference's intent?", where it is
presentation. Prompted by a real run that told a user their correct
`5 xbar time.minute` draft was wrong. The module loads the skill file into the
shared embedded q and **restores the server copy on teardown**, since both
define `.eval.cmp` into one namespace and pykx gives one q process per session.
It also covers `.eval.wrapInfo` (why B-wrap prints `meta` rather than
`.eval.descVal` — the latter formats only a keyed table's value columns, so it
said nothing about a differing *key*) and the numeric guard-order fix that
shipped with it.

These tests reach `ClusterState`'s pure methods by populating `.uniques`
and `._equal` directly, skipping `add_round` (which parses, lints, and
executes through pykx). See `_state()` in `test_self_consistency.py`.

## What tiers 2 and 3 cover

Two files for tier 2 (`test_eval_compare_skill.py` is the second, described
above under what it forks and why), one for tier 3, sharing their fixtures via
`conftest.py` — which is where `run_async`, `fake_chat`, `generate` and
`lint_gate_disabled` live. Everything pykx-dependent is imported *inside* those
fixtures, so `conftest.py` (loaded on every run, including tier-1-only ones)
does not drag pykx into tier 1.

**Tier 2** — `test_pipeline_gates.py` (22 tests) — the gates end to end with the model stubbed: a
candidate that parses and lints clean but raises at execution (`price+sym`,
float + symbol) must leave no cluster; an unparseable candidate must be
rejected at the first gate; a 3-1 split must finalize on the majority when the
round threshold is unreachable but no round remains; byte-identical
completions must dedupe to one execution while keeping their combined weight;
cosmetically different but execution-equivalent code must land in one cluster;
with the lint gate disabled the pipeline must still work while the execution
gate still bites, and `check_c9_qlint` must report `skipped` rather than a bare
`pass=True` that reads as "linted clean"; and two overlapping calls in one
process must each cluster against their own results (the HTTP transport's real
shape — `_run_pykx` releases its lock across the awaited `chat`, so the calls
interleave at round boundaries). Dict-shaped results must not escape as a q
signal either: a dict of a table (`exec ... by sym`) must be described as a
dict, not a keyed table, and `.eval.cmp` must answer keyed table vs dict vs
dict of a table with a boolean — tolerating float drift inside a dict as it does
inside a table — rather than signal into `results_equal`'s safety net.

**Tier 3** — `test_pipeline_gates_qlint.py` (6 tests) — the claims that need a real linter: that a response
reports `lint_gate: "kx_qlint_c9"` when C9 genuinely ran, that a parse failure
carries qlint's much more useful message alongside q's terse one, that
`INVALID_ESCAPE` really reaches the caller with its list of valid escapes,
that qlint's own `CANNOT_PARSE` is *not* appended (it restates q), that none of
this enrichment lets an unparseable candidate into a cluster, and — the one
that justifies the gate as a *filter* — that `{x::1} peach til 3` is rejected
even though it parses and executes cleanly.

Not covered at all: the startup probe that catches a *present-but-unusable*
qlint (`qlint_common._probe_lintitem`). Exercising its failure branch would
mean loading a deliberately broken qlint into the shared q session, which would
poison every later test in the process. A run with `QLINT_DIR` unset does not
reach it either — `resolve_qlint_gate` returns a disabled gate at
`locate_qlint`, well before the probe. Why the probe exists anyway is in
"Tiers 2 and 3 in CI" below.

Everything runs in a single event loop (see the `run_async` fixture).
`mcp_server.loop` holds a module-level `asyncio.Lock` that binds to the
first event loop that awaits it, so a per-test `asyncio.run()` would
fail every test after the first with "bound to a different event loop".

Both embedded-q modules gate themselves on the same probe,
`_tiers.pykx_reason()`, which is shared with `conftest.pytest_configure` so
that the skip and the usage error cannot disagree about why a tier is
unavailable. It uses `importlib.util.find_spec("pykx")` rather than
`pytest.importorskip("pykx")` on purpose: `mcp_qsql_shared.qlint_common`
must be imported before pykx to set `PYKX_THREADING=1` (see the preamble
in `mcp_server/loop.py`), and `importorskip` would import pykx directly,
out of order.

`tests/_tiers.py` is not a test module (nothing matches `test_*`); it is
imported by name, which works because pytest puts `tests/` on `sys.path` for a
directory with no `__init__.py`.

## Tiers 2 and 3 in CI

Tier 2 is CI-able if you can supply a kdb+ licence. Tier 3 is not, and the
reason is worth writing down, because the obvious approach fails *silently*.

**The licence can be shipped as a secret**; base64 it and decode it at job
runtime (see the pykx caveat below). `.gitignore` blocks `*.lic` and `*.q_`
so neither artefact can be committed by accident.

**`qlint.q_` cannot usefully be shipped as a secret.** It is a ~2 KB loader
stub: it pulls in ~50 sibling `.qlint.*.q_` / `.ax*.q_` files plus
`lib/<os>/*` shared objects from the same AxLibraries `ws/` tree — 4.7 MB
without the shared libs, 124 MB with them, against a CI secret-size limit
measured in kilobytes. Copying the stub alone gets you a **silent** failure,
not a loud one: q reports the missing sub-loads on stderr and returns
normally, so the load appears to succeed while `.qlint.lintItem` is never
defined. `mcp_qsql_shared.qlint_common._probe_lintitem` exists to turn that
into an honest "gate disabled" with a reason; before it existed, this repo's
own CI ran for months believing it had a working linter while every
candidate passed lint by default.

So a CI job should leave `QLINT_DIR` unset and select tier 2 explicitly with
`-m "needs_q and not needs_qlint"`. Selecting rather than relying on the
runtime skip means the job's scope cannot drift if a runner ever does acquire
AxLibraries — which is the same class of silent scope change the probe above
was added to catch. That configuration is also exactly what an outside
contributor has, which makes it the right thing to guard.

Selecting explicitly buys one more thing, for the same reason: because the job
*names* tier 2, a licence secret that fails to decode now fails the job with a
message instead of reporting a green `0 passed, N skipped`. That is the same
silent-success failure mode as the dead C9 gate above, on the other rung.

**Do not expect pykx to consume a base64 licence env var by itself.**
PyKX only reads `KDB_LICENSE_B64` inside `_license_install`
(`pykx/config.py`), which opens with:

```python
if not hasattr(sys, 'ps1'):  # Exit if running in a non-interactive session
    return False
```

`sys.ps1` exists only in an interactive REPL, so in any non-interactive
session the function returns before it ever looks at the variable and pykx
quietly falls back to unlicensed mode — `import pykx` still succeeds, and
the failure surfaces later as "Unlicensed user". Decode the licence to
`$QLIC/kc.lic` yourself instead: that is the path q resolves natively (it
tries `$QLIC/kx.lic`, then `$QLIC/kc.lic`) and it works headless. This
applies to a headless *server* deployment just as much as to CI.

## Why the lazy imports in `self_consistency.py`

`_parse_safe`, `check_c9_qlint`, and `qexec` are imported inside the
methods that need them rather than at module scope. All three pull in
pykx; hoisting them would make this module — and therefore every test
below — unimportable without a kdb+ license. Keep them deferred.

## Not covered here

`mcp_server.loop`'s env-var config loaders (`_load_round_caps`,
`_load_seeds`, `_load_nrows`) and their documented "invalid value falls
back to the default with a warning" behaviour. `loop` imports `qexec` at
module scope for its pykx worker, so it cannot be imported without a
license. Decoupling that is a larger change than the one made for
`self_consistency`, and has not been done.
