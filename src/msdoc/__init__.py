# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""msdoc — pure-Python text and image extractor for Word 97–2003 (.doc)."""
from .api import MarkdownResult, extract, to_markdown
from .errors import (
    CorruptDocumentError,
    EncryptedDocumentError,
    MsDocError,
    NotOleFileError,
    UnsupportedFormatError,
)
from .model import STORY_KEYS, Cell, Document, Image, Link, Paragraph, Row, Table

__all__ = [
    "extract",
    "to_markdown",
    "MarkdownResult",
    "Document",
    "Image",
    "Paragraph",
    "Table",
    "Row",
    "Cell",
    "Link",
    "STORY_KEYS",
    "MsDocError",
    "NotOleFileError",
    "CorruptDocumentError",
    "UnsupportedFormatError",
    "EncryptedDocumentError",
]
__version__ = "0.2.0"
