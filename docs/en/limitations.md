# Limitations

**English** · [Русский](../ru/limitations.md)

## Supported formats

| Input | Result |
|---|---|
| Word 97, 2000, XP, 2003 (`.doc`, `nFib ≥ 0x00C1`), including files saved from Word 2007+ in compatibility mode | supported |
| Word 6/95 (`nFib < 0x00C1`), Word 2.0 and older | `UnsupportedFormatError` |
| An OLE file from another program (`.xls`, `.ppt`, `.msg`, …) | `UnsupportedFormatError` |
| A password-protected document | `EncryptedDocumentError` (decryption is not implemented) |
| `.rtf` (starts with `{\rtf`) | read by the RTF parser, see README |
| `.odt`, `.ods`, `.odp` (zip with an OpenDocument `mimetype`) | read by the ODF parsers, see README |
| `.docx`, text, any other non-OLE file | `NotOleFileError` |
| A damaged `.doc` | `CorruptDocumentError`, or a partial result with an entry in `Document.warnings` |

## What is extracted

**Content and structure:** the text of six stories, raster and vector pictures, and the structure of the main
text — headings, lists (with Word's labels), tables (with merged cells and nesting), hyperlinks, footnotes.
It is delivered in `Document.blocks` and in Markdown (`msdoc.to_markdown`).

Not extracted:

- **Appearance:** fonts, sizes, colours, alignment, indents, bold/italic/underline, table borders and shading.
- **Automatic numbering in the flat text:** list and footnote numbers do not appear in `doc.text` and `doc.stories`
  (the goal there is to match LibreOffice). List labels are available in `Paragraph.list_number` and in the Markdown, but they
  are computed approximately by Word's rules: complex multi-level and “restarting” lists may differ.
- **Embedded OLE objects** (Excel charts, equations, embedded documents) and their preview pictures.
- **Metadata:** author, dates, document properties, tags (author and date may appear in the text as fields).
- **Macros** (VBA) are neither run nor analysed.
- **Tracked changes and hidden text:** the behaviour was chosen to follow LibreOffice; for legally significant
  scenarios check the result on your files.
- **A picture's position on the page** and text wrapping are not returned; for a picture you get its kind
  (`inline`/`floating`), story, anchor position `cp` and the paragraph it sits in (the placeholder in the Markdown).
- **Order of floating objects:** text of text boxes, comments and headers/footers is delivered in `doc.extra` / `extra=True`
  with no ordering guarantees; it is not inserted into the Markdown of the main text.

**Structure accuracy** was checked against LibreOffice: about 94 % on dev and 86 % on the held-out set (see [quality.md](quality.md)).
On complex documents expect mistakes in list levels, merged cells and text-box order.

## Notes on the result

- The order of paragraphs in `headers`, `comments`, `textboxes` is not guaranteed.
- The same header/footer may appear in the list several times if it is used in several sections.
- Vector pictures (WMF/EMF/PICT) are returned as-is; rasterising is up to you. Most multimodal models do not accept
  WMF/EMF; the `examples/llm_messages.py` example skips them and leaves a note in the text.
- Tables in Markdown are emitted as HTML rather than pipe tables: otherwise merged cells and nesting cannot be conveyed.
- The text is taken as it is stored in the file: characters that Word shows as special glyphs (for example
  non-breaking space, soft hyphen) may be present as control codes. Normalise the text if needed
  (`str.translate`, regular expressions).

## Operational limitations

- **Time and memory are not limited by the library itself.** The file is read into memory whole; parsing is
  pure Python. For files from untrusted sources limit the process from outside: timeout, `ulimit -v`,
  a container. See [deployment.md](deployment.md).
- **Speed.** Pure Python is slower than native implementations; typical documents take fractions of a second,
  large files with hundreds of pictures take seconds. Use processes for batches.

## Disagreements with LibreOffice

LibreOffice was the reference during development, but it is not the only definition of the “right” result.
On deliberately damaged files (the fuzzer corpus `sw/qa/core/data/ww8/pass`) LibreOffice outputs whatever it
managed to salvage from a damaged structure, and that output is not a reference: for them the library
guarantees no crashes and no hangs, not text agreement. Details are in [quality.md](quality.md) and in
`docs/lo_disagreements.md`.
