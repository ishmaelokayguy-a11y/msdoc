# Task: piece table and character properties (`pieces.py`, `props.py`)

Read CLAUDE.md first. You own `src/msdoc/pieces.py`, `src/msdoc/props.py`
and their tests. `cfb.py`/`fib.py` are written in parallel: until they land, use
`olefile` in tests and read fcClx/lcbClx directly (FibRgFcLcb97 offsets) in a
test helper — code against the `Fib` dataclass interface in `fib.py`.

Implement per the docstrings and [MS-DOC] 2.4.1 Retrieving Text, 2.9.38 Clx,
2.9.172 Pcd, 2.9.73 FcCompressed, 2.9.62 ChpxFkp, 2.9.? Prm, sprm operand sizes.
Reference: ww8scan.cxx (WW8ScannerBase, WW8PLCFpcd, WW8PLCFx_PCD, WW8Fkp,
WW8PLCFx_Fc_FKP, wwSprmParser).

Requirements
- `PieceTable.text(cp_start, cp_end)`; compressed pieces use the MS-DOC 2.9.73
  byte->Unicode table (cp1252 with overrides), uncompressed UTF-16LE.
- `PieceTable.cp_to_fc(cp)`.
- `CharProps.runs()` / `sprms_at(cp)`: CHPX FKP runs mapped to CP ranges
  through the piece table, with piece-level Prm sprms applied on top.
- Correct operand size for every sprm (spra 0..7, sprmTDefTable,
  sprmPChgTabs, sprmTDefTable10 special cases) so unknown sprms are skipped
  safely.

Tests: synthetic piece tables (mixed compressed/uncompressed pieces, pieces out
of FC order); for corpus files check that `text(0, ccpText)` contains the
golden main-story paragraphs in order (loose check — stories.py will clean up
special characters).

Done when: tests pass and `python3 oracle/compare.py --check` passes.
