/ Verify-side dummy-table generator for the /qsql-coder Bash-verify protocol.
/ Sibling of mcp_server/type_gen.q (gate-3 dummy), tuned for grouped-query
/ rigor: more rows, narrower sym universe, narrower timestamp span so that
/ filter-by-sym + bucket-by-N-min queries get multiple rows per (sym, bucket)
/ cell — enough density for wavg / avg / wsum divergences to surface.
/ Consumed only by .claude/skills/qsql-coder/SKILL.md (Bash-verify pass-1).
/ WARNING: defines the SAME top-level symbols as mcp_server/type_gen.q
/ (NROWS0, typeGen, parseSchema0, genTable0). Do NOT load both into one q
/ session — the second load silently wins.
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.
/ -
/ VALUE RANGES ARE KEPT IN STEP WITH mcp_server/type_gen.q; THE SPAN IS NOT.
/ The two files must agree on value ranges or a candidate can clear gate 3
/ on data where its filter discriminates and then be Bash-verified against
/ a reference on data where the same filter matches nothing — empty vs
/ empty compares equal, so the verify reports a match it never tested. So
/ the numeric generators here are the same log-uniform decades as gate 3's
/ (see that file's header for the measurement that forced it), and `c`
/ likewise covers upper case and digits.
/ -
/ What deliberately does NOT track gate 3 is the timestamp span, because
/ narrowing it is this file's whole purpose: 1000 rows over 2h with 3 syms
/ puts ~28 rows in each (sym, 10-min bucket) cell, where a full day would
/ leave ~2 and no wavg / wsum divergence could surface. Gate 3 spans a
/ full day; this file must not.
/ -
/ The `n` and `z` generators were changed anyway, and that is an
/ anchor change rather than a span change: `p`, `t`, `u` and `v` here were
/ already anchored inside trading hours, while `n` sat at 00:00-01:00 and
/ `z` was uniform across ~24 years. Both are now 09:30 + 2h like their
/ siblings, which is what makes a whole-session filter
/ (`time within 0D08:30 0D16:30`) return rows instead of nothing.
/ Residual, and accepted: a narrow LATE-day window (`0D16:20 0D16:30`)
/ still falls outside the 2h span and compares empty vs empty. Widening to
/ reach it is exactly the density trade this file exists to refuse.

NROWS0:1000;
typeGen:()!();

typeGen[`b]:{ x?01b };
typeGen[`s]:{ x?`AAPL`MSFT`GOOG };
typeGen[`f]:{ 10 xexp 6*x?1.0 };
typeGen[`j]:{ `long$ 10 xexp 9*x?1.0 };
typeGen[`p]:{ asc `timestamp$(2024.06.01D09:00:00.000000000)+x?0D02:00:00.000000000 };
typeGen[`t]:{ asc `time$09:30:00.000+x?7200000 };
typeGen[`d]:{ asc x?2024.01.01+til 252 };
typeGen[`i]:{ `int$ 10 xexp 4.5*x?1.0 };
typeGen[`e]:{ `real$ 10 xexp 6*x?1.0 };
typeGen[`h]:{ `short$x?100h };
typeGen[`u]:{ asc `minute$09:30+x?120 };
typeGen[`v]:{ asc `second$09:30:00+x?7200 };
typeGen[`m]:{ asc x?2024.01m+til 12 };
typeGen[`c]:{ x?.Q.A,.Q.a,.Q.n };
typeGen[`n]:{ asc `timespan$(0D09:30:00.000000000)+x?0D02:00:00.000000000 };
typeGen[`g]:{ x?0Ng };
typeGen[`x]:{ x?0x0ff };
typeGen[`z]:{ asc `datetime$(2024.06.01T09:00:00.000)+x?2%24 };

parseSchema0:{[s0] @[value;s0;{[e] ()!()}] };

genTable0:{[schema0;n]
    cnames:key schema0;
    ctypes:value schema0;
    d:cnames!{[n;tp] $[tp in key typeGen; typeGen[tp][n]; n?1000j] }[n;] each ctypes;
    flip d };
