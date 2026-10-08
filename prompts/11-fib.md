# Task: File Information Block (`src/msdoc/fib.py`)

Read CLAUDE.md first. You own `src/msdoc/fib.py` and `tests/test_fib.py`.
`cfb.py` is being written in parallel by another agent: until it lands, use
`olefile` **in tests only** to get the WordDocument stream bytes.

Implement `parse_fib(word_document: bytes) -> Fib` per the docstring in
`fib.py` and [MS-DOC] 2.5 (Fib, FibBase, FibRgW97, FibRgLw97, FibRgFcLcb97 …
FibRgFcLcb2007, FibRgCswNew). Reference: `WW8Fib` in
`sw/source/filter/ww8/ww8scan.cxx`.

Requirements
- Fill every field of the `Fib` dataclass. Expose ALL fc/lcb pairs of
  FibRgFcLcb97/2000/2002/2003/2007 in `fib.fclcb` keyed by the spec name
  without prefix (`"Clx"`, `"PlcfBteChpx"`, `"PlcfBtePapx"`, `"PlcffndRef"`,
  `"PlcffndTxt"`, `"PlcfandTxt"`, `"PlcfHdd"`, `"PlcfendTxt"`, `"PlcftxbxTxt"`,
  `"PlcfHdrtxbxTxt"`, `"PlcSpaMom"`, `"PlcSpaHdr"`, `"DggInfo"`, `"PlcfFldMom"`, ...).
- Errors: wIdent != 0xA5EC or nFib < 0xC1 -> `UnsupportedFormatError`;
  fEncrypted -> `EncryptedDocumentError`. Check against the corpus manifest:
  every `legacy`/`encrypted` file must raise the right error.
- Tolerate short FIBs (cbRgFcLcb smaller than nFib implies): missing pairs = (0,0).
- Sanity-check (fc, lcb) pairs against the table stream size later — expose a
  helper `fib.pair(name, table_len) -> (fc, lcb) | None` that returns None for
  out-of-range pairs.

Done when: tests pass for every dev corpus file (normal files parse, legacy/
encrypted raise the right errors) and `python3 oracle/compare.py --check` passes.
