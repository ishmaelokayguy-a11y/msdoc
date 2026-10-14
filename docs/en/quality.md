# Quality: how it was measured and what came out

**English** · [Русский](../ru/quality.md)

The library was written “from scratch” by parallel Claude Code agents using the specifications
[MS-DOC], [MS-ODRAW], [MS-CFB] and by reading the LibreOffice sources. Its correctness was checked automatically
by comparing with LibreOffice on a corpus of documents (the “oracle”).

## Corpus

| Source | What |
|---|---|
| LibreOffice tests (`sw/qa/...`) | real and deliberately tricky documents, the fuzzer corpus (`ww8/pass`, `ww8/fail`) |
| Apache POI test data | documents of different Word versions |

502 files in total: `dev` (438; 341 of them regular) and a held-out set `holdout` (64 regular). The held-out
set was closed to the agents during development and used only for the final check.

Unsupported formats (Word 6/95, encrypted, non-OLE) are in the corpus as separate categories: the expected
result for them is the right exception.

## Reference

Every file is converted to ODT by LibreOffice (`soffice --headless --convert-to odt`), and the ODT is parsed by a script
that collects the same six stories and the pictures. The reference stores the text per story and picture
“fingerprints” (sha256 and a perceptual hash).

## File score (0…1)

```
score = 0.7 · text_similarity + 0.3 · image_F1
```

- Text similarity is computed per story (`main`, `footnotes`, `endnotes` — in order; `headers`, `comments`,
  `textboxes` — as sets of paragraphs).
- Image F1: match by sha256, then by perceptual hash; vector pictures are matched by count (LibreOffice
  re-encodes WMF into its own format).
- Files that must not open (Word 6/95, encrypted, non-OLE): 1 for the right exception.
- Fuzzer and “broken” files: 1 if there is no crash or hang.

**Ratchet.** `baseline.json` stores the best score ever reached for each file. Any change that lowers the score of even one
file is rejected (including by the agents' Stop hook). That is why quality never went backwards during development.

## Results (version 0.2.0; text and pictures unchanged since 0.1.0)

| Set | Files | Mean score | Text | Pictures (F1) |
|---|---|---|---|---|
| dev, all categories | 438 | 0.9759 | 0.9777 | 0.9870 |
| dev, regular documents without fuzzer files | 308 | — | **0.9926** | — |
| holdout (hidden from agents) | 64 | 0.9555 | 0.9364 | 0.9951 |
| holdout, regular documents without fuzzer files | 55 | — | **0.9964** | — |

## Structure (Markdown)

Since 0.2.0 the structure of the main text is scored separately (`struct`, 0…1): the reference is built from the same ODT
(headings and their levels, list items and list type, tables — number of rows, columns and merged cells, picture positions,
number of links and footnote references). The score is not part of the overall score and has its own ratchet (`data/struct_baseline.json`).

| Set | Files | Structure |
|---|---|---|
| dev | 334 | 0.939 |
| holdout | 62 | 0.855 |

Before structure was implemented (all paragraphs plain) the score was 0.47 on dev. The score checks types and sizes, not the exact text of
numbering labels or the order of text boxes, so it is more optimistic than a strict character-by-character comparison.

Conclusion: on regular documents text agreement with LibreOffice is about 99 %, and on the held-out set no worse
than on dev — there is no overfitting to the corpus. Most of the remaining losses are fuzzer files, where the LibreOffice
reference is the result of emergency recovery.

## Why fuzzer files are counted separately

The `sw/qa/core/data/ww8/pass` directory in LibreOffice contains files found by a fuzzer: they are deliberately
corrupted to check that LibreOffice **does not crash**. The text LibreOffice pulls out of them is a by-product of
recovery and sometimes looks like garbage (`1111…`) or is cut off. Trying to match it would mean fitting to
someone else's errors. For such files the library's guarantee is no crashes and no hangs. In the oracle report they
are shown on a separate line.

The cases examined (from the held-out set) showed three types:

1. LibreOffice “collapses” a table or cuts the document short, while the library reads it fully.
2. The structure is so broken that the correct result is empty or an exception.
3. Real document text that LibreOffice skips for the same reasons.

## What was not checked

- Comparison with Word itself: the reference is LibreOffice, not Word. Structure (especially list numbering) may be shown differently by Word and LibreOffice.
  In disputed places LibreOffice may be wrong.
- A large corpus of real Word documents (for example Govdocs1): the corpus consists mostly of LibreOffice and POI test
  files, so on “live” documents from your organisation the result needs to be checked separately (see
  [development.md](development.md), section “Add your own documents”).

## How to repeat the measurement

```bash
python3 oracle/compare.py --quiet                       # dev
python3 oracle/compare.py --split holdout --quiet       # held-out set
python3 oracle/compare.py --diff <id>                   # difference from LibreOffice for one file (including structure tokens)
```

The text score uses `rapidfuzz` if it is installed and `difflib` otherwise; the values differ slightly, so
`--update-baseline` must be run in the same environment where the check is later run.

Data in `data/` (corpus and references) are required; they are not part of the package or the repository.

## Excel (.xls) and PowerPoint (.ppt)

The same oracle idea as for `.doc`, in `oracle/office_*.py`: `office_fetch.py` builds a corpus from the LibreOffice
`qa` files and the Apache POI test data (15 % held out), `office_golden.py` converts every file with LibreOffice to
`.ods`/`.odp` and parses that (displayed cell text, merged ranges, slide texts and notes, picture fingerprints),
`office_compare.py` scores msdoc against it:

```bash
python3 oracle/office_golden.py --ext xls --split dev        # resumable, needs soffice
python3 oracle/office_compare.py --ext xls                   # dev split, reports/office-xls.md
python3 oracle/office_compare.py --ext ppt --diff <id>       # slide/sheet diff for one file
python3 oracle/office_compare.py --ext xls --check           # ratchet against data/office/baseline-xls.json
python3 oracle/office_compare.py --ext ppt --split holdout   # final evaluation
```

Score = 0.7 × text similarity + 0.3 × picture F1 (xls text = per sheet the name plus one tab-separated line per row;
ppt text = per slide the paragraphs, then the notes). Files that must be rejected (encrypted, pre-97, not OLE) score 1
when the matching `MsDocError` is raised; a crash, hang or foreign exception scores 0.

| version 0.4.0 | files | mean | no crashes |
|---|---|---|---|
| `.xls` dev | 641 | 0.974 | yes |
| `.xls` holdout | 115 | 0.981 | yes |
| `.ppt` dev | 190 | 0.957 | yes |
| `.ppt` holdout | 26 | 0.969 | yes |

Known differences that are LibreOffice behaviour, not defects: formulas that LibreOffice recalculates (it shows its own
result or `Err:5xx` where Excel stored a value), pivot-cache sheets, fields that show the current date, a default empty
slide for presentations without slides, and a few files where LibreOffice repairs a damaged structure differently.

## Rich Text Format (.rtf)

Corpus: the `.rtf` files of LibreOffice's `sw/qa`, Apache POI and Apache Tika (`oracle/office_fetch.py --ext rtf`),
605 dev files (532 normal, 34 fuzz, 23 that LibreOffice rejects, 16 that are not RTF) and a held-out split.
Reference: LibreOffice's RTF -> ODT conversion, scored with the same `oracle/compare.py --ext rtf`.

| dev split | value |
|---|---|
| mean score (all kinds) | 0.986 |
| normal files: score / text / picture F1 / structure | 0.992 / 0.993 / 0.991 / 0.980 |
| crashes, timeouts | 0 |
| holdout, all kinds / normal files | 0.976 / 0.976 (103 / 86 files) |

Known gaps: LibreOffice stores a PNG fallback next to every WMF/EMF picture (we return the vector picture
only); fields LibreOffice recomputes (DATE, page numbers in the body of multi-page documents) keep their
cached RTF result; OLE objects outside tables are not reported (LibreOffice embeds them).
