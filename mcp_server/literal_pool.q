/ Literal-aware dummy rows: make a candidate's filters discriminate.
/ Loaded AFTER a type-generator file (mcp_server/type_gen.q for gate 3,
/ .claude/skills/qsql-coder/verify_gen.q for Bash-verify), because
/ genTableLit falls back to that file's typeGen / genTable0. Byte-identical
/ copy at .claude/skills/qsql-coder/literal_pool.q — unlike eval_compare.q
/ the two copies answer the SAME question, so keep them identical
/ (tests/test_literal_pool.py enforces it).
/ NOTE: do NOT add a bare `/` line — q treats it as the start of a
/ block comment that runs until a bare `\` line.
/ -
/ THE PROBLEM. The type generators draw from fixed pools (dates in 2024,
/ ten equity syms). A filter on a value outside them — a 2030 date,
/ `BTCUSD, an exchange code like `XQQ — matches no row on any seed, so every
/ candidate returns an empty table and every candidate "agrees". Wider
/ fixed pools cannot fix that: whatever range is chosen, a task can name
/ a value outside it.
/ -
/ THE FIX. A value a filter compares against is, by construction, written
/ in some candidate's code. So: harvest the constants from each
/ candidate's parse tree (.lit.harvest), turn the union into per-type
/ pools (.lit.pools), and append rows drawn from those pools to the default
/ table (genTableLit). The default rows stay, so `=` still excludes rows.
/ -
/ Design points, each load-bearing:
/ - Parse tree, not regex: symbols in a tree are enlisted when they are
/   literals and bare when they are names, so `sym=`BTCUSD harvests
/   `BTCUSD and never `sym.
/ - Constant folding: `.z.d-1`, `2030.01.14+til 3`, "D"$"2030.01.15" and
/   `$"BTCUSD" are evaluated, but ONLY when every function in the
/   sub-tree is on .lit.ok and every name is a .z clock — candidate code
/   is model output, and nothing that can touch the filesystem, a handle
/   or `value` a string is ever called. til / # / _ refuse counts above
/   10000 so a fold cannot exhaust memory.
/ - One anchor per literal row: the row's d / p / z / m columns all derive
/   from one timestamp, so `date=X` and `time.date=X` select the SAME rows
/   and candidates that differ only in which column they filter on still
/   agree.
/ - Neighbours: every harvested date also contributes the day before and
/   after, so `within`, `>` and `<` have rows on both sides of the bound.
/ - Strings become symbol candidates (`like "BTC*"` -> `BTC), wildcards
/   stripped. Column and table names are excluded from the symbol pool.
/ - The table is re-sorted on its temporal columns, rows kept intact, so
/   literal rows interleave by time with the default ones (`aj`, `first`,
/   `last` see an ordered table, as they do without literal rows).
/ - No literals harvested -> genTableLit returns genTable0's table
/   unchanged, byte for byte.
/ -
/ HONEST LIMIT. This is best-effort coverage, not a guarantee: a value that
/ is computed from the data, held in a variable defined outside the code,
/ or compared through a join that the dummy keys do not line up, stays out
/ of reach. Agreement on an empty result is caught separately and is not
/ counted as agreement (.eval.isVacuous in mcp_server/eval_compare.q).
/ Density is also only partly addressed: literal rows make a single-date
/ filter non-empty, but at a few rows per (sym, 5-minute bucket) avg and
/ wavg can still coincide.

.lit.zs:`.z.d`.z.D`.z.p`.z.P`.z.z`.z.Z`.z.t`.z.T`.z.n`.z.N;
.lit.ok:(+;-;*;%;$;,;enlist;til;neg;abs;string;#;_;first;last;min;max);
.lit.big:(til;#;_);
.lit.MAXN:10000;
.lit.MAXV:64;
/ Time-of-day window for a literal row's anchor timestamp: a whole day for
/ gate 3. A generator file loaded first may set its own (verify_gen.q uses
/ its 2h trading-hours span, so literal rows keep its bucket density).
.lit.T0:@[get;`.lit.T0;0D00:00];
.lit.SPAN:@[get;`.lit.SPAN;1D];

/ (ok;value) for one parse-tree node; ok=0b means "not a foldable constant".
.lit.fold:{[n]
  t:type n;
  if[-11h=t; :$[n in .lit.zs; (1b;value n); (0b;::)]];
  if[t within -19 -1h; :(1b;n)];
  / An 11h node is a quoted symbol: ,`a is the atom `a, `a`b the vector.
  if[11h=t; :(1b;$[1=count n; first n; n])];
  if[t within 1 19h; :(1b;n)];
  if[(0h<>t) or 0=count n; :(0b;::)];
  f:first n;
  if[not any f~/:.lit.ok; :(0b;::)];
  a:.z.s each 1_n;
  if[not all a[;0]; :(0b;::)];
  args:a[;1];
  if[any .lit.MAXN<count each args; :(0b;::)];
  if[any f~/:.lit.big; if[any {(type[x] in -5 -6 -7h) and .lit.MAXN<abs x} each args; :(0b;::)]];
  @[{(1b;x . y)}[f]; args; {(0b;::)}] };

/ A lambda in a parse tree is an opaque 100h value, so a literal inside a
/ helper ({select from t where date=2030.01.15} each ...) would be missed:
/ re-parse its body from its source text instead.
/ Built-ins such as til are k lambdas ("k){...}"): skip anything whose
/ source is not a q lambda.
.lit.body:{[f] s:last value f; if[not (10h=type s) and "{"=first s; :(::)]; s:1_-1_s; if["["=first s; s:(1+s?"]")_s]; @[parse;s;{(::)}]};
.lit.walk:{[n]
  if[100h=type n; :.z.s .lit.body n];
  r:.lit.fold n;
  (enlist $[r 0; r 1; ()]),$[(0h=type n) and 0<count n; raze .z.s each n; ()] };
.lit.atoms:{[x] $[0>type x; enlist x; 10h=type x; enlist x; 0h=type x; raze .z.s each x; x]};

/ General list of the constants in one code string (atoms, plus whole
/ strings). Code that does not parse harvests nothing.
.lit.harvest:{[code] tree:@[parse;code;{(::)}]; if[(::)~tree; :()]; .lit.atoms .lit.walk tree};

.lit.cleanStr:{[s] s:s except "*?[]^"; $[(count[s] within 2 40) and (all s in .Q.an,"-./") and (any s in .Q.a,.Q.A) and not first[s] in ".";`$s;`]};

/ Per-type pools from harvested values. excl: column and table names.
/ Returns `d`p`s!(anchor dates; exact timestamps; symbols); empty lists
/ mean "nothing harvested for this type".
/ .lit.pick casts the values of the wanted types to ONE typed vector, and
/ to a typed empty when there are none: q keeps the type of whatever it
/ was handed (a harvest of one boolean is ,1b), and joining a typed empty
/ to that gives a boolean list that `except` on symbols rejects with 'type.
.lit.pick:{[v;ty;want;cast] $[count i:where ty in want; cast$v i; cast$()]};
.lit.pools:{[vals;excl]
  vals:(),vals;
  ty:type each vals;
  ms:.lit.pick[vals;ty;-13h;`month];
  ds:.lit.pick[vals;ty;-14 -12 -15h;`date],raze {(`date$x)+0 14} each ms;
  ds:.lit.MAXV sublist distinct raze ds+/:-1 0 1;
  tt:.lit.MAXV sublist distinct .lit.pick[vals;ty;-12 -15h;`timestamp];
  sy:.lit.pick[vals;ty;-11h;`symbol];
  sy:sy,`symbol$.lit.cleanStr each vals where ty=10h;
  sy:.lit.MAXV sublist distinct sy except excl,`;
  `d`p`s!(ds;tt;sy) };

/ n literal rows for one schema.
.lit.gen:{[schema0;n;pool]
  cn:key schema0; ct:value schema0;
  D:pool`d; T:pool`p; S:pool`s;
  anc:$[0<count D; (`timestamp$n?D)+.lit.T0+n?.lit.SPAN; 0<count T; n?T; ::];
  if[(0<count T) and 0<count D; anc:?[n?01b; (n?T)+(neg 0D01)+n?0D02; anc]];
  col:{[n;anc;S;tp] $[
    (tp in `d`p`z`m) and not (::)~anc; $[tp=`d; `date$anc; tp=`p; anc; tp=`z; `datetime$anc; `month$anc];
    (tp=`s) and 0<count S; n?S;
    tp in key typeGen; typeGen[tp][n];
    n?1000j]}[n;anc;S] each ct;
  flip cn!col };

/ genTable0's n default rows, plus nLit rows drawn from pool.
genTableLit:{[schema0;n;nLit;pool]
  t0:genTable0[schema0;n];
  if[(nLit<=0) or 0=sum count each value pool; :t0];
  t:t0,.lit.gen[schema0;nLit;pool];
  tc:(key schema0) where (value schema0) in `d`m`p`z`n`t`u`v;
  $[0<count tc; tc xasc t; t] };

/ Bash-verify entry point: pools from the code in script files (the
/ /qsql-coder skill writes the draft and the reference to one file each)
/ for a dict of table name -> schema string.
.lit.fromFiles:{[paths;schemas]
  excl:(key schemas),raze {key parseSchema0 x} each value schemas;
  .lit.pools[raze {.lit.harvest "\n" sv read0 hsym `$x} each paths; excl] };
