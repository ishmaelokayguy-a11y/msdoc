# msdoc — agent instructions

Pure-Python extractor of **text and images** from Microsoft Word 97–2003 binary
files (`.doc`, nFib ≥ 0x00C1). Correctness is measured against LibreOffice by
the oracle in `oracle/`. Your job is to raise the oracle score without
regressing any file.

## Scope
- In: Word 97/2000/XP/2003/2007-saved-as-.doc. Main text, footnotes, endnotes,
  headers/footers, comments, text boxes; inline and floating pictures.
- Out (raise the matching error, do not try to parse): Word 6/95/2
  (`UnsupportedFormatError`), password-protected (`EncryptedDocumentError`),
  non-OLE input (`NotOleFileError`). OLE objects (embedded charts, equations)
  and their preview images are not reported.
- Output contract: `src/msdoc/model.py`. Do not change field names or story keys.

## Hard rules
1. **Stdlib only**, Python ≥ 3.9 (`struct`, `zlib`, `codecs`, ...). No
   dependencies in `src/`. `olefile`/`pytest` may be used in `tests/` only.
2. **Only `MsDocError` subclasses may escape `msdoc.extract`.** Everything read
   from the file is untrusted: bound every loop and every recursion by the
   data size, check every offset/length before slicing, detect cycles. A hang
   or crash on any file scores 0 and is a bug. Prefer recovering (skip the
   broken structure, append to `Document.warnings`) over failing the document.
3. **Never** special-case a file (by id, hash, name, size, or content
   fingerprint) and never tune behaviour to one test file. Fix the general
   rule the file exposes, and cite the spec section or LibreOffice code.
4. **Do not edit** `oracle/`, `.claude/settings.json`, `data/` or the baseline.
   If you believe the oracle or LibreOffice is wrong for a file, record it in
   `docs/lo_disagreements.md` (file id, spec section, what LO does, what the
   spec says) and move on. The integrator decides.
5. **Do not read the holdout split** (`data/corpus/holdout/`) or golden files
   for holdout ids. It is the final exam.
6. Stay inside the module(s) your prompt assigns you. If you need an interface
   change in another module, make the smallest change and explain it in the
   commit message.

## Licensing (MPL-2.0)
This project is licensed under MPL-2.0, the same licence as LibreOffice, so
**you may read and port LibreOffice code**. Reference sources are checked out at
`$DATA/sources/libreoffice-core/` (see "Paths"):
- `sw/source/filter/ww8/` — ww8scan.cxx (FIB, piece table, FKPs, PLCFs, sprm
  sizes), ww8par*.cxx (text, fields, tables, notes, headers), ww8graf*.cxx (pictures)
- `filter/source/msfilter/msdffimp.cxx`, `include/filter/msfilter/` — OfficeArt/Escher, BLIPs
- `sot/source/sdstor/` — OLE2 container

Rules when porting:
- Every file in `src/` starts with the MPL-2.0 header (copy from any existing file).
- If a file contains logic translated from a LibreOffice file, add below the
  header: `# Portions of this file are derived from LibreOffice <path> (MPL-2.0).`
  If that LibreOffice file carries the Apache-2.0 "incorporates work covered by"
  notice, copy that notice too.
- Do **not** copy code from GPL/LGPL projects (antiword, wvWare, catdoc,
  AbiWord). Apache POI (Apache-2.0) may be consulted but not copied.

## Specifications (primary source of truth)
- [MS-DOC] https://learn.microsoft.com/openspecs/office_file_formats/ms-doc/
- [MS-ODRAW] https://learn.microsoft.com/openspecs/office_file_formats/ms-odraw/
- [MS-CFB] https://learn.microsoft.com/openspecs/windows_protocols/ms-cfb/
Use WebFetch on these pages. When spec and LibreOffice disagree, follow
LibreOffice's behaviour if it reflects what real Word files contain, and leave a
comment citing both.

## Paths
- Shared data dir `$DATA` = `<main checkout>/data` (or `$MSDOC_DATA`); find it with
  `python3 -c "import sys; sys.path.insert(0,'oracle'); import common; print(common.data_dir())"`.
- `$DATA/corpus/dev/<id>.doc` test documents, `$DATA/corpus/manifest.json` (kind, source path)
- `$DATA/golden/<id>.json` LibreOffice reference output, `$DATA/golden/<id>/NN.ext` reference images
- `reports/latest.md` — your last oracle run (worst files first)

## Commands
```bash
python3 -m pytest -q                                   # unit tests (fast)
python3 oracle/compare.py --ids <id> <id>              # score a few files
python3 oracle/compare.py --diff <id>                  # story diff + image table for one file
python3 oracle/compare.py --kind normal --quiet        # all normal files
python3 oracle/compare.py --check                      # full dev run + ratchet (the Stop hook runs this)
```
A Stop hook runs the ratchet check when you try to finish. If it reports
regressions, fix them; do not try to bypass the hook.

## Module map
| module | responsibility | prompt |
|---|---|---|
| `cfb.py` | OLE2 container | prompts/10-cfb.md |
| `fib.py` | FIB, version/encryption detection | prompts/11-fib.md |
| `pieces.py`, `props.py` | piece table, CP↔FC, CHPX/sprms | prompts/12-text.md |
| `stories.py` | stories, paragraphs, fields, tables, special chars | prompts/13-stories.md |
| `images.py` | inline pictures (Data stream) | prompts/14-inline-images.md |
| `images.py` | floating pictures (OfficeArt) | prompts/15-floating-images.md |
| `xls.py`, `numfmt.py` | Excel 97-2003 (BIFF8) workbooks and number formats -> `Document.sheets` + blocks | oracle: `oracle/office_compare.py --ext xls` |
| `ppt.py` | PowerPoint 97-2003 (persist directory, slides, notes, comments, pictures) -> `Document.slides` + blocks | oracle: `oracle/office_compare.py --ext ppt` |
| `rtf.py` | Rich Text Format (tokenizer, group state, codepages, tables, lists, fields, pictures) -> `Document` with `format="rtf"`, detected in `api.py` before the OLE check | oracle: `oracle/compare.py --ext rtf` (corpus `data/rtf/`, `oracle/office_fetch.py --ext rtf`) |
| `blocks.py`, `markdown.py` | structure (headings, lists, HTML tables, image/note anchors) -> Markdown | prompts/40-blocks.md |
| all | robustness on broken/fuzzed input | prompts/20-robustness.md |

## Working style
- Write unit tests in `tests/` for each structure you parse (synthetic bytes
  for edge cases; real corpus files for integration).
- Commit early on your branch with clear messages; never push to or merge into `main`.
- Before finishing: `python3 -m pytest -q` passes and `python3 oracle/compare.py --check` passes.
  End with a short summary: what improved (mean score before/after), what is
  still failing and why.
