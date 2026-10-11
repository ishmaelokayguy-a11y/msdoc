# msdoc

**English** · [Русский](README.ru.md)

Extract **text, structure and images** from binary Word 97–2003 files (`.doc`) in pure Python.
The result is ready-to-use **Markdown** (headings, lists, HTML tables, links, footnotes) plus the
pictures as a separate list of bytes with their MIME types. Built for feeding documents to LLMs.

No dependencies (standard library only), no LibreOffice, Word or external programs needed on the
target machine. Python ≥ 3.9.

```python
import msdoc

text, media = msdoc.to_markdown("report.doc")   # path, bytes or a file object
print(text)                                      # Markdown; pictures appear as ![Media 0](image:0)
for i, im in enumerate(media):                   # media[i] <-> ![Media i](image:i)
    print(i, im.mime, len(im.data))              # im.data: picture bytes in the original format
```

The implementation was checked against LibreOffice on a corpus of ~500 documents: on regular documents
text agreement is about 99 %, pictures (F1) about 99 %, structure (headings/lists/tables/picture
positions) about 94 % on dev and 86 % on the held-out set — see [docs/en/quality.md](docs/en/quality.md).

## Contents

- [Installation](#installation) (including offline / air-gapped)
- [Markdown and pictures](#markdown-and-pictures) · [Markdown format](#markdown-format)
- [Feeding an LLM](#feeding-an-llm)
- [Plain text and all document parts](#plain-text-and-all-document-parts)
- [Document structure (blocks)](#document-structure-blocks)
- [Errors](#errors)
- [Batch processing](#batch-processing)
- [Limitations](#limitations)
- [Documentation](#documentation) · [License](#license)

## Installation

From a prebuilt wheel (no dependencies, no internet needed):

```bash
python3 -m pip install msdoc-0.2.0-py3-none-any.whl
```

From source (just put `src/` on `PYTHONPATH`; installation is optional):

```bash
PYTHONPATH=src python3 -c "import msdoc; print(msdoc.__version__)"
```

Build a wheel and sdist: `python3 -m pip install build && python3 -m build` → `dist/`.
Offline deployment in detail: [docs/en/deployment.md](docs/en/deployment.md).

## Markdown and pictures

```python
import msdoc

text, media = msdoc.to_markdown(open("report.doc", "rb").read())
```

* `text` — a `str` with Markdown. Every picture is replaced by a placeholder `![Media N](image:N)`.
* `media` — `list[Image]`; `media[N]` is the picture behind placeholder `image:N` (numbering starts at **0**).
  `Image` has `data` (bytes), `mime`, `kind` (`"inline"`/`"floating"`), `story`, `cp`, `name`.
* The return value is a named tuple `MarkdownResult(text, media)`: both `r = msdoc.to_markdown(...)`
  (`r.text`, `r.media`) and unpacking `text, media = ...` work.

If you already have a `Document`: `doc.markdown` or `doc.to_markdown(**options)`.

Options (`msdoc.to_markdown(source, *, images_enabled=True, label="Media", image_format=..., notes=True, extra=False)`):

| Option | Default | Meaning |
|---|---|---|
| `images_enabled` | `True` | `False` skips picture parsing (faster); there are then no placeholders in the text |
| `label` | `"Media"` | label inside `![label N](image:N)` |
| `image_format` | `"![{label} {n}](image:{n})"` | placeholder template (`{n}` number, `{label}` label) |
| `notes` | `True` | footnotes and endnotes at the bottom as `[^1]: …` / `[^e1]: …` |
| `extra` | `False` | append text boxes, comments and headers/footers at the end |

Vector pictures (WMF/EMF/PICT) are returned as-is; rasterise them yourself if you need to show them.

## Markdown format

| In the document | In the Markdown |
|---|---|
| headings (“Heading N” styles, outline level) | `#` … `######` |
| bulleted / numbered lists | `- item` / `1. item` (Word's own label when it can be computed), nesting by indentation |
| tables | **HTML** `<table>` with `<th>`/`<td>`, `colspan`, `rowspan`, nested tables |
| pictures | `![Media N](image:N)` at the anchor position |
| hyperlinks | `[text](url)` |
| footnotes, endnotes | `[^1]` / `[^e1]` in the text, `[^1]: …` at the bottom |
| a paragraph that looks like markup (`# `, `- `, `1. `) | escaped with a backslash |

Tables are emitted as HTML because Markdown tables cannot express merged cells, nesting or multiple
paragraphs in a cell.

## Feeding an LLM

Two approaches (ready-made code in [`examples/llm_messages.py`](examples/llm_messages.py)):

```python
import msdoc
from llm_messages import to_openai_content          # from the examples/ directory

content = to_openai_content("report.doc")            # list of text / image_url (data: URI) parts
messages = [{"role": "user", "content": [{"type": "text", "text": "Summarise the document:"}, *content]}]
```

1. **Multimodal model:** the text is split at the placeholders and each raster picture is inserted as an
   `image_url` right where it stood, preceded by a `[Media N]` marker. Vector pictures stay in the text as
   `[Media N: image/x-wmf, not shown]`.
2. **Text-only model:** `to_text_with_captions()` replaces each picture with a caption you get from your own
   vision model (`caption()` is a stub for your call).

Check that your server accepts `image_url` and how many pictures/tokens the context allows: by default the
example sends at most 20 raster pictures of up to 5 MB each.

## Plain text and all document parts

```python
doc = msdoc.extract("report.doc")
doc.text                                   # main text without markup, one paragraph per line
for name in msdoc.STORY_KEYS:              # main, footnotes, endnotes, headers, comments, textboxes
    print(name, len(doc.stories[name]))    # each story is a list of paragraphs
```

`doc.stories` is the “flat” view that was checked against LibreOffice for text. Table cells follow one
another (one entry per cell paragraph); it carries no structure.

```python
msdoc.extract(blob)                          # bytes / bytearray (from a database, HTTP)
msdoc.extract(open("a.doc", "rb"))           # file object
msdoc.extract(pathlib.Path("a.doc"))         # path
msdoc.extract("a.doc", images_enabled=False) # no pictures: faster, less memory
```

Saving pictures to disk:

```python
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp",
       "image/tiff": "tif", "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct"}
for i, im in enumerate(doc.images):
    open(f"image_{i:02d}.{EXT.get(im.mime, 'bin')}", "wb").write(im.data)
```

## Document structure (blocks)

`doc.blocks` is the main text with structure: a list of `Paragraph` and `Table`. Markdown is rendered
from it; use it directly if you need a different output format.

```python
from msdoc import Table
for b in doc.blocks:
    if isinstance(b, Table):
        print("table with", len(b.rows), "rows")
    elif b.heading:
        print("heading", b.heading, b.text)
    elif b.list_level is not None:
        print("list item", b.list_level, b.list_number, b.text)
```

`Paragraph`: `text`, `heading` (0 = body, 1–9 = level), `list_level`, `list_ordered`, `list_number`,
`images` (indexes into `doc.images`), `notes`, `links`. Inside `text`, a picture is the character `U+FFFC`
and a footnote reference is `U+E000`. Details: [docs/en/api.md](docs/en/api.md).

## Errors

| Exception | When |
|---|---|
| `NotOleFileError` | the input is not an OLE2 container (not a `.doc`) |
| `UnsupportedFormatError` | Word 6/95/2.0 (older than Word 97) or an OLE file without a `WordDocument` stream |
| `EncryptedDocumentError` | the document is password-protected |
| `CorruptDocumentError` | the container or Word structures are damaged beyond recovery |

All of them derive from `MsDocError`, so `except msdoc.MsDocError` is usually enough. No other
exception type escapes the library. Non-fatal problems end up in `doc.warnings`.

## Batch processing

```bash
python3 examples/to_markdown.py input_dir output_dir --jobs 4 --timeout 60
python3 examples/to_markdown.py report.doc                 # -> report.md and report_media/
python3 examples/extract_folder.py input output            # text.txt + stories.json + images/
```

Every file is processed in its own process with a hard timeout, so one “bad” file cannot stop the batch.
`to_markdown.py` rewrites the placeholders into relative links to the saved files (`--keep-placeholders`
leaves them as they are).

## Limitations

In short (details in [docs/en/limitations.md](docs/en/limitations.md)):

- Only Word 97–2003 `.doc` (`nFib ≥ 0xC1`). Not `.docx`, `.rtf` or Word 6/95.
- Encrypted documents are not decrypted (`EncryptedDocumentError`).
- Of the formatting only structural elements are kept (headings, lists, tables, links, footnotes);
  fonts, colours, alignment, bold/italic are not.
- Structure agrees with LibreOffice at 94 % (dev) / 86 % (holdout): on complex documents expect mistakes in
  list levels, merged cells and text-box order.
- Embedded OLE objects (charts, equations) and their preview pictures are not extracted.
- The library does not limit time or memory itself: run untrusted files in a separate process (see the examples).

## Documentation

| File | About |
|---|---|
| [docs/en/api.md](docs/en/api.md) | public API reference |
| [docs/en/usage.md](docs/en/usage.md) | recipes: Markdown, LLM, search, JSON, pictures, multiprocessing |
| [docs/en/architecture.md](docs/en/architecture.md) | how `.doc` parsing works, modules and data flow |
| [docs/en/limitations.md](docs/en/limitations.md) | what is not supported and why |
| [docs/en/quality.md](docs/en/quality.md) | how accuracy was measured, numbers, disagreements with LibreOffice |
| [docs/en/deployment.md](docs/en/deployment.md) | offline install, security, resources, licensing |
| [docs/en/development.md](docs/en/development.md) | tests, corpus, oracle, how to contribute |

Русская документация: [docs/ru/](docs/ru/).

## License

MPL-2.0 (see `LICENSE` and `NOTICE`). Part of the parsing logic is ported from LibreOffice (also MPL-2.0);
such files carry the line “Portions of this file are derived from LibreOffice …” in their header.
Test documents and reference outputs are not part of the package.
