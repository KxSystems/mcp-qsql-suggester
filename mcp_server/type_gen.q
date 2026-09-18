/ Canonical type-generators for the qSQL-specialist dummy-table pipeline.
/ Single source of truth for the SERVER-SIDE dummy, consumed by:
/   - mcp_server/qexec.py (via type_gen.py loader, gate 3)
/   - .claude/skills/qsql-coder/pre-derive.md (expected-shape derivation;
/     must share these dummy bytes with gate 3 for the shape hint to be
/     accurate against what qwen sees at runtime)
/ The column-type vocabulary matches what the fine-tuned model saw at
/ training / evaluation time. Edit here; both consumers pick up.
/ See also: .claude/skills/qsql-coder/verify_gen.q — same API but with
/ verify-tuned density (1000 rows / 3 syms / 2h span) for the Bash-verify
/ reference compare in SKILL.md. Decoupled from this file so verify dummy
/ can be retuned without changing gate-3 contract.
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.
/ -
/ THE GENERATED VALUES MUST DISCRIMINATE, NOT LOOK REALISTIC. Revised
/ because the earlier ranges were narrow enough to make whole classes of
/ filter vacuous, and a vacuous filter defeats all three gates at once:
/ the candidate parses, lints, executes without raising, and every sample
/ agrees — because every sample returned an empty table. Agreement on
/ nothing is the one failure mode self-consistency cannot see, since
/ agreement is its only evidence.
/ -
/ Measured over the 39 distinct in-scope tasks of KX's public NYSE TAQ
/ benchmark query set: under the old ranges 10 of 39 reference implementations
/ returned an EMPTY result, and `where size>500000` / `size>700000` /
/ `2000i<bsize+asize` matched zero rows at nrows=20, 1000 AND 50000 — no
/ row count rescues a range that cannot reach the threshold. One of those
/ 10 (upstream idx 83) shipped as passed=true with 12 of 16 candidates
/ "agreeing", and raised 'type the moment it met populated data.
/ -
/ So the numeric generators are LOG-UNIFORM across decades rather than
/ uniform inside one band: a threshold at 10, 10^3 or 10^5 then splits the
/ rows roughly evenly instead of taking all or none. Same principle on the
/ other axis for the temporal and char types — cover the type's domain
/ (a full day, both letter cases) rather than one plausible-looking slice.
/ -
/ Residual risk, stated because it is real: at NROWS0=20 a 10^5-scale
/ threshold populates only ~5% of rows, so one seed can still come up
/ empty (p ~ 0.36). Gate 3 draws three seeds and excludes a candidate that
/ is invalid on ANY of them, which puts all-three-empty at ~5%. Raising
/ MCP_QSQL_SC_NROWS shrinks it further; narrow WINDOW filters (a
/ 10-second span inside a day) stay out of reach at 20 rows whatever the
/ range does, and that is a density limit, not a range one.

NROWS0:20;
typeGen:()!();

typeGen[`b]:{ x?01b };
typeGen[`s]:{ x?`AAPL`MSFT`GOOG`JPM`GS`BAC`IBM`TSLA`AMZN`META };
typeGen[`f]:{ 10 xexp 6*x?1.0 };
typeGen[`j]:{ `long$ 10 xexp 9*x?1.0 };
typeGen[`p]:{ asc `timestamp$(2024.06.01D00:00:00.000000000)+x?1D };
typeGen[`t]:{ asc `time$x?24:00:00.000 };
typeGen[`d]:{ asc x?2024.01.01+til 252 };
/ `int$ is load-bearing, not tidiness: `1+x?1000i` returned a LONG vector,
/ because 1 is a long atom and long+int promotes, so a column declared `i`
/ arrived as `j` and the declared schema was a lie.
typeGen[`i]:{ `int$ 10 xexp 4.5*x?1.0 };
typeGen[`e]:{ `real$ 10 xexp 6*x?1.0 };
/ From 0h, not 1h: a status/correction column is overwhelmingly 0 in real
/ data, so `not corr=0h` was always-true and never filtered anything.
typeGen[`h]:{ `short$x?100h };
typeGen[`u]:{ asc `minute$x?24:00 };
typeGen[`v]:{ asc `second$x?24:00:00 };
typeGen[`m]:{ asc x?2024.01m+til 12 };
/ Upper case and digits as well as lower: exchange, condition and
/ indicator codes are uppercase letters or digit characters, and `.Q.a`
/ alone made every one of those comparisons match nothing.
typeGen[`c]:{ x?.Q.A,.Q.a,.Q.n };
typeGen[`n]:{ asc `timespan$x?1D };
typeGen[`g]:{ x?0Ng };
typeGen[`x]:{ x?0x0ff };
typeGen[`z]:{ asc `datetime$(2024.06.01T00:00:00.000)+x?1.0 };

/ Deliberately NOT changed while doing the above, so the change stays one
/ idea: `s` still draws from ten non-null symbols. Admitting a null would
/ make `null`/`not null` filters discriminate too, but it also adds a null
/ group to every `by`-grouped result, which is a behavioural change on a
/ different axis and belongs to its own decision.

parseSchema0:{[s0] @[value;s0;{[e] ()!()}] };

genTable0:{[schema0;n]
    cnames:key schema0;
    ctypes:value schema0;
    d:cnames!{[n;tp] $[tp in key typeGen; typeGen[tp][n]; n?1000j] }[n;] each ctypes;
    flip d };
