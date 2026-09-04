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

NROWS0:20;
typeGen:()!();

typeGen[`b]:{ x?01b };
typeGen[`s]:{ x?`AAPL`MSFT`GOOG`JPM`GS`BAC`IBM`TSLA`AMZN`META };
typeGen[`f]:{ 50.0+x?100.0 };
typeGen[`j]:{ 1+x?1000j };
typeGen[`p]:{ asc `timestamp$(2024.06.01D09:00:00.000000000)+x?0D10:00:00.000000000 };
typeGen[`t]:{ asc `time$09:30:00.000+x?23400000 };
typeGen[`d]:{ asc x?2024.01.01+til 252 };
typeGen[`i]:{ 1+x?1000i };
typeGen[`e]:{ `real$50.0+x?100e };
typeGen[`h]:{ `short$1+x?100h };
typeGen[`u]:{ asc `minute$09:30+x?390 };
typeGen[`v]:{ asc `second$09:30:00+x?23400 };
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
