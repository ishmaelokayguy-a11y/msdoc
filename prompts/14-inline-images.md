# Task: inline pictures (`src/msdoc/images.py`, inline part)

Read CLAUDE.md first. You own the INLINE part of `src/msdoc/images.py`
(functions with `inline` in their name) and `tests/test_images_inline.py`.
Another agent writes floating pictures in the same file in parallel: put your
code in clearly separated functions, keep `extract_images` as the dispatcher,
and keep merge conflicts small (add, don't reorganise).

Implement per the docstring in images.py: 0x01 special characters with
sprmCPicLocation (not sprmCFData, not sprmCFOle2) -> Data stream ->
PICFAndOfficeArtData -> BLIP(s). Decompress metafile BLIPs, add the
BITMAPFILEHEADER to DIBs. Reference: ww8graf2.cxx (ImportGraf1, ReadGrafFile,
WW8PicDesc), msdffimp.cxx (GetBLIPDirect).

Loop: `python3 oracle/compare.py --quiet`, look at files where
`images.ours < images.golden` for inline images; `--diff <id>` shows the golden
image table and dumps yours into `reports/dump/<id>/`. Compare bytes.
LibreOffice re-encodes most WMF as SVM, so vector images are matched by count,
not bytes — get the count and anchors right.

Done when: inline image recall is ~1.0 on files where golden has only inline
images, tests pass, `python3 oracle/compare.py --check` passes.
