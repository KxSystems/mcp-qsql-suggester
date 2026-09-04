# Contributing

Thanks for looking. Two things worth knowing up front: we are **not accepting
pull requests at the moment**, and the reason is a constraint on who can verify a
change to this project at all — see § Pull requests and § Running the tests
below. Issues are welcome, and under that policy they are the channel that
actually works.

## Pull requests

We are not accepting pull requests at the moment. That is not a comment on the
quality of outside work: tiers 2 to 4 of the test suite need a kdb+ licence,
pykx, and KX's EULA-gated `qlint.q_`, so neither public CI nor a contributor
without those can verify a change to the parts of this project most likely to
break. Until that changes, a well-described issue is genuinely more useful to us
than a patch, and we would rather say so than leave pull requests open
indefinitely.

A pull request opened here is closed automatically, with a pointer back to this
file. That is the policy being applied consistently rather than a judgement on
what is in it — an unattended pull request left open for months is the outcome
this is meant to avoid.

None of this restricts what you do with the code: it is Apache-2.0, so fork it
and change it for your own use freely.

## Filing an issue

Issues are welcome. The more of this you can include, the faster it goes:

- What you ran — the launch command, the transport (`stdio` or
  `streamable-http`), and any `MCP_QSQL_*` / `VLLM_*` environment variables you
  set. The server logs its resolved configuration on startup; that line is the
  single most useful thing to paste.
- The `generate_qsql` arguments, especially `table_schemas`.
- The full response, not just the code — `passed`, the cluster sizes and
  `final_issues` are usually where the answer is.
- Versions: q (`q --version`), pykx, vLLM, and whether `qlint.q_` is loaded
  (responses say `lint_gate: "disabled"` when it is not).

### Things that are already answered

- **Requests for the training data or the fine-tuning pipeline.** Neither is
  published and there are no plans to publish them. See
  [README.md § "What is and is not in this release"](README.md#what-is-and-is-not-in-this-release).
- **Security reports.** Do not open a public issue; see
  [SECURITY.md](SECURITY.md), which also lists what is *not* a vulnerability
  here — chiefly that the server executes model-generated q without a sandbox,
  by design.
- **"The generated q was wrong."** Sometimes it will be. `passed=true` means the
  code ran without raising and independent samples agreed; it is not a claim
  about semantics. A report is still useful if you can show the instruction, the
  schemas, and what the right answer was — that is data about a failure mode. A
  report that is only "it got this one wrong" is not actionable.

## Running the tests

The suite is in four tiers, one per rung of the dependency ladder, and `pytest`
skips the tiers whose dependencies are absent — so a bare `pytest -q` is the
correct command at every rung. [`tests/README.md`](tests/README.md) has the
detail.

```bash
pytest -q                                   # everything your machine supports
pytest -q -m "not needs_q"                  # tier 1 only
pytest -q -m "needs_q and not needs_qlint"  # tier 2 only
pytest -q -m needs_qlint                    # tier 3 only
```

**The constraint worth knowing about**: only tier 1 (pure logic, 87 tests) runs
without licensed software. Tier 2 needs kdb+/q and pykx with a licence, tier 3
additionally needs KX's `qlint.q_` from the EULA-gated AxLibraries download, and
tier 4 needs a GPU serving the fine-tuned model. Public CI therefore runs tier 1
only.

The practical consequence: **a change to the gates, the q sources or the
clustering cannot be fully verified by CI, or by a contributor without a kdb+
licence.** That is not a comment on anyone's work; it is the reason for the
pull-request policy above. A change that neither CI nor its author can run is a
change merged on trust, in exactly the area of this project where trust is the
wrong instrument.

Changes confined to tier-1 territory — `extract_qsql`, the self-consistency
decision logic, argument validation, documentation — are fully verifiable by
anyone. If you are reporting a bug in one of those, a failing test case in the
issue is the most useful thing you can attach.

## House style

Mostly for anyone working in a fork. Match the surrounding code rather than any
external style guide: the same comment density, naming, and idiom as the file you
are editing. Two conventions that are load-bearing rather than aesthetic, both
documented where they matter:

- `mcp_qsql_shared.qlint_common` must be imported **before** `pykx`.
- The pykx imports in `mcp_server/self_consistency.py` are lazy on purpose.
  Hoisting them to module scope would pull pykx into tier 1 and take the whole
  `tests/` directory out of CI's reach.

If you are unsure whether something is a convention or an accident, ask in the
issue before changing it.
