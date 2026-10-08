# Task: OLE2 / Compound File Binary reader (`src/msdoc/cfb.py`)

Read CLAUDE.md first. You own `src/msdoc/cfb.py` and `tests/test_cfb.py`.

Implement `CompoundFile` per the docstring in `cfb.py` and [MS-CFB]
(https://learn.microsoft.com/openspecs/windows_protocols/ms-cfb/). LibreOffice's
`sot/source/sdstor/` is your reference for edge cases.

Requirements
- Header validation, major version 3 (512-byte sectors) and 4 (4096-byte sectors).
- DIFAT (header 109 entries + chained DIFAT sectors), FAT, mini-FAT (64-byte
  mini sectors inside the root entry's stream), cutoff 4096.
- Directory: walk the red-black tree from the root's child, build '/'-joined
  paths for storages and streams, case-insensitive lookup.
- Untrusted input: cycle detection on every chain, sector ids out of range,
  stream size larger than the chain allows (truncate + warning-free tolerance,
  like LibreOffice), directory loops, version-3 files with garbage in the high
  32 bits of the stream size. Raise `CorruptDocumentError`, never hang.
- Performance: a 50 MB file must open in well under a second (no per-sector
  Python-level copying of the whole file; build chains as lists of ints and
  join slices once).

Tests
- Cross-check against `olefile` (tests only) for every file in `$DATA/corpus/dev`:
  same stream list and identical bytes for WordDocument, 0Table/1Table, Data.
- Synthetic corrupted headers / cyclic FAT / out-of-range sectors.

Done when: tests pass; `python3 oracle/compare.py --check` passes. The oracle
score will not move much yet — that is expected for this module.
