# API reference

**English** · [Русский](../ru/api.md)

Everything public is available from the top-level package:

```python
import msdoc
msdoc.extract, msdoc.to_markdown, msdoc.MarkdownResult
msdoc.Document, msdoc.Image, msdoc.STORY_KEYS
msdoc.Paragraph, msdoc.Table, msdoc.Row, msdoc.Cell, msdoc.Link       # structure blocks
msdoc.MsDocError, msdoc.NotOleFileError, msdoc.CorruptDocumentError,
msdoc.UnsupportedFormatError, msdoc.EncryptedDocumentError
msdoc.__version__
```

## `msdoc.extract`

```python
extract(source, *, images_enabled: bool = True) -> Document
```

Reads a whole Word 97–2003 file into memory, parses it and returns a `Document`.

**`source`** — any of:

| Type | Example |
|---|---|
| `str` / `os.PathLike` | `"a.doc"`, `pathlib.Path("a.doc")` |
| `bytes` / `bytearray` | file contents from a database, the network |
| file object | `open("a.doc", "rb")`, `io.BytesIO(...)`; needs a `read()` method |

**`images_enabled`** — `False` skips picture parsing (faster, less memory). Then `doc.images == []`.

**Exceptions.** Only subclasses of `MsDocError` escape (list below). This is an API guarantee: any other
exception type (`IndexError`, `struct.error`, `RecursionError`, …) is a defect. I/O errors when opening a file
by path (`FileNotFoundError`, `PermissionError`) are ordinary `OSError`s: the library does not hide them.

## `msdoc.to_markdown`

```python
to_markdown(source, *, images_enabled=True, label="Media",
            image_format="![{label} {n}](image:{n})", notes=True, extra=False) -> MarkdownResult
```

One call: `extract` + building the Markdown. Returns a named tuple
`MarkdownResult(text: str, media: list[Image])`, so `text, media = msdoc.to_markdown(src)` works.
Sources and exceptions are the same as for `extract`.

* A picture in the text is replaced by the placeholder `image_format.format(n=N, label=label)`; `N` is the index into `media`, **zero-based**.
* `images_enabled=False` — pictures are not parsed, `media == []`, and there are no placeholders in the text.
* `notes=True` — footnotes at the bottom: `[^1]: …`, endnotes: `[^e1]: …`. `extra=True` appends text boxes,
  comments and headers/footers at the end (their order is not guaranteed).
* `Document.to_markdown(**options)` gives the same result (`doc.markdown` is the same with default options).

### What ends up in the Markdown

| Element | Form |
|---|---|
| heading | `#`…`######` (`Paragraph.heading`, capped at 6) |
| list item | `- text` or `1. text` (`Paragraph.list_number` as the label when known); nesting: two spaces per level |
| table | HTML: `<table>`, `<tr>`, `<th>`/`<td>`, `colspan`, `rowspan`; paragraphs in a cell joined with `<br>`, nested tables inline |
| hyperlink | `[text](url)` |
| footnote reference | `[^N]` / `[^eN]` |
| picture | placeholder |
| a paragraph that looks like markup (`# `, `- `, `1. `, `>`) | escaped with a leading `\` |

## Document structure: `Document.blocks`

The main text with structure; built inside `extract`, no separate call needed. If the structure could not be
determined, `blocks` consists of plain `Paragraph`s without headings, lists or tables (no text is lost).

```python
Block = Paragraph | Table

@dataclass Paragraph:  text: str; heading: int = 0; list_level: int | None = None; list_ordered: bool = False;
                       list_number: str | None = None; images: list[int]; notes: list[tuple[str, int]]; links: list[Link]
@dataclass Link:       start: int; end: int; url: str      # range [start, end) in Paragraph.text
@dataclass Table:      rows: list[Row]
@dataclass Row:        cells: list[Cell]; header: bool
@dataclass Cell:       blocks: list[Block]; colspan: int = 1; rowspan: int = 1; header: bool = False
```

* `heading`: `0` — body paragraph, `1`…`9` — heading level.
* `list_level`: `None` — not a list, `0` — top level, `1` — nested, and so on; `list_ordered` — numbered;
  `list_number` — the label as Word draws it (`"3."`, `"b)"`, `"IV."`, `"3.5.1"`), `None` for bullets or when it cannot be computed.
* In `Paragraph.text` a picture is the character `U+FFFC` (`model.IMAGE_MARK`), a footnote reference is `U+E000` (`model.NOTE_MARK`);
  `images[k]` is the index into `Document.images` for the k-th `U+FFFC`; `notes[k] = ("footnote" | "endnote", N)` is for the k-th `U+E000`
  (`N` starts at 1, the number of the note in `Document.notes`).
* A merged cell is stored once with `colspan`/`rowspan`; the cells it covers are not in `cells` (HTML semantics).

Extra `Document` fields:

| Field | Type | Description |
|---|---|---|
| `blocks` | `list[Paragraph \| Table]` | main text with structure |
| `notes` | `dict[str, list[list[Paragraph]]]` | `"footnotes"` / `"endnotes"`: a list of notes, each a list of paragraphs |
| `extra` | `dict[str, list[Block]]` | `"textboxes"`, `"comments"`, `"headers"` as blocks; order not guaranteed |

`Document.stories` is unchanged and remains the “flat” view; `blocks` and `stories` are built from the same data.

## `msdoc.Document`

A `@dataclass`. Fields:

| Field | Type | Description |
|---|---|---|
| `stories` | `dict[str, list[str]]` | six keys (`msdoc.STORY_KEYS`); each value is a list of paragraphs (`str`) without the trailing newline; an empty story is `[]` |
| `images` | `list[Image]` | all pictures found |
| `nfib` | `int \| None` | `nFib` from the header; `0x00C1` (193), `0x00D9` (217), `0x0101` (257), … for Word 97 and later |
| `warnings` | `list[str]` | non-fatal problems: skipped damaged structures, recovered errors |
| `blocks`, `notes`, `extra` | see above | document structure |

Property **`text`** → `str`: `"\n".join(stories["main"])`. Property **`markdown`** → `str`: `to_markdown()` with default options.

### Stories (`stories`)

| Key | Contents |
|---|---|
| `main` | the main text. Table cells give one paragraph per cell paragraph, in reading order (row by row, cell by cell). The table structure is not conveyed |
| `footnotes` | footnote text, one entry per footnote paragraph |
| `endnotes` | endnote text |
| `headers` | header/footer text of all sections; order is **not** guaranteed |
| `comments` | comment (annotation) text; order is not guaranteed |
| `textboxes` | text of text boxes and text frames (of the main text and of headers/footers); order is not guaranteed |

Treat `headers`, `comments` and `textboxes` as sets of unique paragraphs: they are compared with LibreOffice
without regard to order.

## `msdoc.Image`

A `@dataclass`. Fields:

| Field | Type | Description |
|---|---|---|
| `data` | `bytes` | the picture in its original format, already decompressed (metafiles are zlib-compressed in the file) |
| `mime` | `str` | `image/png`, `image/jpeg`, `image/gif`, `image/bmp`, `image/tiff`, `image/x-wmf`, `image/x-emf`, `image/x-pict` |
| `kind` | `"inline" \| "floating"` | `inline` — a picture within a line of text (character `0x01`); `floating` — an OfficeArt shape (character `0x08`) |
| `story` | `str` | the story where the anchor was found (`"main"` by default) |
| `cp` | `int \| None` | position of the anchor character in the text, if known |
| `name` | `str \| None` | shape name, if set |

The same picture may occur several times in a document; the library does not de-duplicate them.

## Exceptions

```
MsDocError
├── NotOleFileError          the input is not an OLE2 container
├── CorruptDocumentError     the container or Word structures are damaged
├── UnsupportedFormatError   Word 6/95 (nFib < 0x00C1), Word 2.0, or an OLE file that is not Word
└── EncryptedDocumentError   the document is password-protected
```

Typical causes:

- `NotOleFileError` — a `.docx` (it is a ZIP), `.rtf`, a text file, a truncated `.doc` without the signature
  `D0CF11E0A1B11AE1`.
- `UnsupportedFormatError` — very old Word files (pre-97), and `.xls`/`.ppt` that have no `WordDocument` stream.
- `EncryptedDocumentError` — the encryption flag is set in the header; decryption is not supported.
- `CorruptDocumentError` — a broken container, missing mandatory streams, an unreadable piece table.

The exception message (`str(e)`) contains the reason in English; it is meant for logs.

## Thread safety and performance

- By design `extract` uses no global mutable state and calls are independent of each other (no dedicated
  multithreading test was run).
- The work is pure Python (CPU-bound): threads do not speed it up; use processes (`multiprocessing`,
  `concurrent.futures.ProcessPoolExecutor`) for parallelism.
- The file is read into memory whole; peak usage depends on the document size and the number of pictures.
