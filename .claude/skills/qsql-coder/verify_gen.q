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

NROWS0:1000;
typeGen:()!();

typeGen[`b]:{ x?01b };
typeGen[`s]:{ x?`AAPL`MSFT`GOOG };
typeGen[`f]:{ 50.0+x?100.0 };
typeGen[`j]:{ 1+x?1000j };
typeGen[`p]:{ asc `timestamp$(2024.06.01D09:00:00.000000000)+x?0D02:00:00.000000000 };
typeGen[`t]:{ asc `time$09:30:00.000+x?7200000 };
typeGen[`d]:{ asc x?2024.01.01+til 252 };
typeGen[`i]:{ 1+x?1000i };
typeGen[`e]:{ `real$50.0+x?100e };
typeGen[`h]:{ `short$1+x?100h };
typeGen[`u]:{ asc `minute$09:30+x?120 };
typeGen[`v]:{ asc `second$09:30:00+x?7200 };
typeGen[`m]:{ asc x?2024.01m+til 12 };
typeGen[`c]:{ x?.Q.a };
typeGen[`n]:{ asc x?0D01:00:00.000000000 };
typeGen[`g]:{ x?0Ng };
typeGen[`x]:{ x?0x0ff };
typeGen[`z]:{ asc x?2024.06.01T09:00:00.000 };

parseSchema0:{[s0] @[value;s0;{[e] ()!()}] };

genTable0:{[schema0;n]
    cnames:key schema0;
    ctypes:value schema0;
    d:cnames!{[n;tp] $[tp in key typeGen; typeGen[tp][n]; n?1000j] }[n;] each ctypes;
    flip d };
