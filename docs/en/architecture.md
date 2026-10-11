# Architecture

**English** · [Русский](../ru/architecture.md)

A binary `.doc` (Word 97–2003) is an OLE2/CFB container with several streams inside. The text is stored
apart from the markup, and the markup apart from the property tables. The library walks a chain of layers;
each layer is a separate module.

```
bytes ─► cfb.py ─► fib.py ─► pieces.py ─► props.py ─► stories.py ─► images.py ─► blocks.py ─► Document
         OLE2      header     piece        character   stories,      pictures     structure    │
         container  FIB        table        properties  paragraphs                 (headings,   ▼
                                                                                   lists,     markdown.py
                                                                                   tables)    ─► str
```

## Data flow in `msdoc.extract`

1. **`cfb.CompoundFile(data)`** — parses the OLE2 container (MS-CFB): header, FAT/miniFAT, directory.
   Provides `open_stream("WordDocument")`, `open_stream("1Table")`, `open_stream("Data")`, etc.
2. **`fib.parse_fib(WordDocument)`** — the File Information Block: version (`nFib`), flags (encryption,
   `fWhichTblStm` — which of `0Table`/`1Table` to use), story lengths (`ccpText`, `ccpFtn`, `ccpHdd`, …) and
   `(fc, lcb)` pairs for all document tables. Word 6/95 and encrypted files are rejected here.
3. **`pieces.PieceTable`** — the piece table (the `Clx` structure in the table stream). It maps character
   positions (CP) to bytes in `WordDocument`: part of the text is stored “compressed” (cp1252, 1 byte per
   character), part as UTF-16LE. Provides CP ↔ FC conversion and reading text by CP range.
4. **`props.CharProps`** — character properties: FKP pages for `CHPX` and `sprm` sequences. Needed to find out
   which character a picture or special mark belongs to (for example `sprmCPicLocation`, the “special character” flag).
5. **`stories.build_stories`** — assembles the stories (main text, footnotes, endnotes, headers/footers,
   comments, text boxes): cuts the single CP stream at the boundaries from the FIB, handles special characters
   (`0x07` end of cell/row, `0x13/0x14/0x15` field marks, `0x01` picture, `0x08` drawn object), fields (code vs
   result) and tables. Returns the stories and the “anchors” for pictures.
6. **`images.extract_images`** — pictures. Two paths with a shared BLIP decoder:
   - **inline** (`0x01`): a `PICF` structure in the `Data` stream containing an OfficeArt record with a BLIP;
   - **floating** (`0x08`): the `OfficeArtContent` template (`DggInfo`, shape containers, `FBSE`); the BLIP is
     either in the `WordDocument` stream at an offset (delayed stream) or embedded in the `FBSE`.
   Metafile BLIPs (WMF/EMF/PICT) are zlib-compressed and get decompressed. A DIB becomes a BMP by adding a header.
7. **`blocks.build_blocks`** — builds `Document.blocks`, `notes` and `extra`: it lays structure over the flat
   stories. Headings come from the style (`sti`) and `sprmPOutLvl`; lists from `sprmPIlfo`/`sprmPIlvl` and the
   `PlcfLst`/`PlfLfo` tables (including computing the label “3.”, “b)”, “IV.”); tables from the nesting depth,
   `sprmPFTtp`/`sprmPFInnerTtp` and `sprmTDefTable` (cell merging); picture and footnote-reference positions are
   the characters `U+FFFC`/`U+E000` in the paragraph text; hyperlinks come from `HYPERLINK` fields. The structure is
   always checked against the flat text: on a mismatch the module falls back to plain paragraphs, so no text is lost.
8. **`markdown.to_markdown`** — a pure function over `Document`: renders blocks to Markdown, tables to HTML.
9. **`api.extract`** — assembles everything into a `Document`. Any parsing error becomes a subclass of `MsDocError`.

## Modules

| Module | Responsibility | Main sources |
|---|---|---|
| `cfb.py` | OLE2 container | [MS-CFB] |
| `fib.py` | FIB, version, encryption, table pointers | [MS-DOC] §2.5 |
| `pieces.py` | piece table, CP↔FC | [MS-DOC] §2.9 `Clx`, `PlcPcd` |
| `props.py` | CHPX/FKP, sprm | [MS-DOC] §2.6, §2.9 |
| `stories.py` | stories, paragraphs, fields, tables, special characters | [MS-DOC] |
| `images.py` | inline and floating pictures | [MS-DOC] `PICF`, [MS-ODRAW] |
| `blocks.py` | structure: headings, lists, tables, links, footnotes | [MS-DOC] `Lst`, `Lfo`, `TDefTable`, LibreOffice `ww8par*.cxx` |
| `markdown.py` | renders blocks to Markdown (HTML for tables) | — |
| `model.py` | public data model | — |
| `errors.py` | exception hierarchy | — |
| `api.py` | entry points: `extract`, `to_markdown` | — |

Some algorithms are ported from LibreOffice (`sw/source/filter/ww8/*`, `filter/source/msfilter/msdffimp.cxx`,
`sot/source/sdstor/*`); the file header says exactly from where.

## Reliability principles

Every byte of the input file is treated as untrusted:

- every read is bounds-checked against the stream;
- loops and recursion are bounded by the data size (not by values from the file); cycles in FAT chains and trees are detected;
- if a structure is damaged but the text can still be obtained, the library skips the damaged piece and records the
  reason in `Document.warnings`; fatal cases become `CorruptDocumentError`;
- no exceptions other than `MsDocError` escape.

These properties are checked by tests on synthetic data and by a run over a corpus of fuzzed documents
(see [development.md](development.md)).

## What lives where in the file (cheat sheet)

| Stream | Contents |
|---|---|
| `WordDocument` | FIB, text (pieces), FKPs with properties; delayed BLIPs of floating pictures |
| `0Table` / `1Table` | `Clx` (piece table), PLCFs of paragraph/header/footnote boundaries, OfficeArt tables, list tables |
| `Data` | `PICF` and BLIPs of inline pictures |
| `\x01CompObj`, `\x05SummaryInformation`, etc. | OLE metadata (not used) |
