# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Public data model. This is the CONTRACT between the library and the oracle.

Do not rename fields or story keys without updating oracle/compare.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Union

# Story keys. Each story is a list of paragraphs (str, no trailing newline).
#   main       - main document text; table cells contribute one paragraph per
#                cell paragraph, in reading order (row by row, cell by cell)
#   footnotes  - footnote text, one paragraph per footnote paragraph
#   endnotes   - endnote text
#   headers    - all header/footer text of all sections (order not scored)
#   comments   - annotation (comment) text (order not scored)
#   textboxes  - text of text boxes in main and header stories (order not scored)
STORY_KEYS = ("main", "footnotes", "endnotes", "headers", "comments", "textboxes")

Story = Literal["main", "footnotes", "endnotes", "headers", "comments", "textboxes"]


@dataclass
class Image:
    data: bytes
    """Image bytes in their native format (PNG/JPEG/GIF/TIFF/BMP/WMF/EMF/PICT),
    already decompressed (metafile BLIPs are zlib-compressed in the file)."""
    mime: str
    """e.g. image/png, image/jpeg, image/x-wmf, image/x-emf, image/x-pict, image/bmp."""
    kind: Literal["inline", "floating"]
    """inline = character picture (sprmCPicLocation / 0x01); floating = OfficeArt shape (0x08)."""
    story: Story = "main"
    cp: Optional[int] = None
    """Character position of the anchor character, if known."""
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Structured view of the document ("blocks"), the source of the Markdown output.
#
# `Document.stories` stays the flat, LibreOffice-checked text (one str per
# paragraph). `Document.blocks` is a second, additive view of the main story
# with structure: headings, list items, tables, and image/note anchors.
# It is built by `msdoc.blocks.build_blocks`; if that fails or a feature is not
# implemented, every paragraph simply becomes a plain `Paragraph` (so the text
# is never lost and `to_markdown` always works).
#
# Inline markers inside Paragraph.text (private use / object replacement chars,
# they never occur in real extracted text):
IMAGE_MARK = "\ufffc"   # one per picture; Paragraph.images[k] = index into Document.images
NOTE_MARK = "\ue000"    # one per footnote/endnote reference; Paragraph.notes[k] = ("footnote"|"endnote", n)
# `n` counts from 1 in document order (the index of the note in Document.notes[kind]).


@dataclass
class Link:
    start: int
    end: int
    """Character range [start, end) of Paragraph.text that is the link text."""
    url: str


@dataclass
class Paragraph:
    text: str
    heading: int = 0
    """0 = body text, 1..9 = heading level (Word "Heading N" / outline level)."""
    list_level: Optional[int] = None
    """None = not a list item; 0 = top level, 1 = nested, ..."""
    list_ordered: bool = False
    list_number: Optional[str] = None
    """Word's rendered label if known ("3.", "b)", "IV."); None for bullets/unknown."""
    images: list[int] = field(default_factory=list)
    notes: list[tuple[str, int]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


@dataclass
class Cell:
    blocks: list["Block"] = field(default_factory=list)
    colspan: int = 1
    rowspan: int = 1
    """HTML semantics: a merged cell is stored once with colspan/rowspan; the cells it covers are omitted."""
    header: bool = False


@dataclass
class Row:
    cells: list[Cell] = field(default_factory=list)
    header: bool = False
    """True for a row marked "repeat as header row" (sprmTTableHeader)."""


@dataclass
class Table:
    rows: list[Row] = field(default_factory=list)


Block = Union[Paragraph, Table]


@dataclass
class Document:
    stories: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in STORY_KEYS})
    images: list[Image] = field(default_factory=list)
    nfib: Optional[int] = None
    warnings: list[str] = field(default_factory=list)
    """Non-fatal problems (skipped structures, recovered corruption)."""
    blocks: list[Block] = field(default_factory=list)
    """Main story with structure (see the block model above). Empty only if blocks were disabled."""
    notes: dict[str, list[list[Paragraph]]] = field(default_factory=lambda: {"footnotes": [], "endnotes": []})
    """Footnotes / endnotes: one list of paragraphs per note, in document order."""
    extra: dict[str, list[Block]] = field(default_factory=lambda: {"textboxes": [], "comments": [], "headers": []})
    """Text boxes, comments and header/footer text as blocks (no reading-order guarantee)."""

    def to_markdown(self, **options) -> str:
        """Markdown for this document, see msdoc.markdown.to_markdown for the options."""
        from .markdown import to_markdown
        return to_markdown(self, **options)

    @property
    def markdown(self) -> str:
        return self.to_markdown()

    @property
    def text(self) -> str:
        """Main story as plain text."""
        return "\n".join(self.stories.get("main", []))
