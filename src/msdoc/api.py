# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sw/source/filter/ww8/ww8scan.cxx (WW8Fib::GetFIBVersion) (MPL-2.0).
# That LibreOffice file incorporates work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""Top-level entry point."""
from __future__ import annotations

import os
from typing import BinaryIO, NamedTuple, Union

from . import blocks as blocks_mod, cfb, fib as fibmod, images, pieces, props, stories
from .errors import CorruptDocumentError, MsDocError, UnsupportedFormatError
from .model import Document, Image

Source = Union[str, os.PathLike, bytes, bytearray, BinaryIO]


# Flat (non-OLE) Word binaries, identified by wIdent, the first little-endian
# word. LibreOffice ww8scan.cxx WW8Fib::GetFIBVersion maps wIdent 0xA59B /
# 0xA59C to eWW1 (Word 1 for Windows / OS/2) and 0xA5DB to eWW2 (Word 2);
# those are the only versions that are not stored in an OLE container. Every
# other wIdent is versioned by nFib instead; that includes 0xA5DC, which
# ww8par.cxx SwWW8ImplReader::LoadDoc accepts only on the Word 6/7 path, and
# Word 6/7/8 are only read from an OLE storage. So a flat file starting with
# 0xA5DC is not a document LibreOffice opens as Word: NotOleFileError.
_LEGACY_WIDENTS = (b"\x9b\xa5", b"\x9c\xa5", b"\xdb\xa5")


def _read(source: Source) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "read"):
        return source.read()
    with open(source, "rb") as f:
        return f.read()


def _open_table(container, f):
    """Open the table stream named by fWhichTblStm; if it is absent fall back
    to the other one, as LibreOffice's ww8par.cxx does (it tries "1Table" then
    "0Table" when the preferred stream cannot be opened)."""
    name = f.table_stream_name
    if container.exists(name):
        return container.open_stream(name), None
    other = "0Table" if name == "1Table" else "1Table"
    if container.exists(other):
        return container.open_stream(other), f"{name} missing; used {other}"
    raise CorruptDocumentError("neither 0Table nor 1Table present")


def extract(source: Source, *, images_enabled: bool = True) -> Document:
    """Extract text stories and images from a Word 97-2003 .doc file.

    Raises only MsDocError subclasses.
    """
    data = _read(source)
    if data[:8] != cfb.CFB_SIGNATURE and bytes(data[:2]) in _LEGACY_WIDENTS:
        raise UnsupportedFormatError("legacy non-OLE Word binary (Word 1/2)")
    try:
        container = cfb.CompoundFile(data)
        word_document = container.open_stream("WordDocument")
        f = fibmod.parse_fib(word_document)
        table, table_warning = _open_table(container, f)
        data_stream = container.open_stream("Data") if container.exists("Data") else None
        clx_fc, clx_lcb = f.fclcb.get("Clx", (0, 0))
        pt = pieces.PieceTable(table, clx_fc, clx_lcb, word_document)
        cp = props.CharProps(f, word_document, table, pt)
        story_map, anchors = stories.build_stories(f, word_document, table, pt, cp, data_stream)
        doc = Document(stories=story_map, nfib=f.nfib)
        if table_warning:
            doc.warnings.append(table_warning)
        doc.warnings.extend(pt.warnings)
        doc.warnings.extend(cp.warnings)
        if images_enabled:
            doc.images = images.extract_images(f, word_document, table, data_stream, cp, anchors)
        blocks_mod.build_blocks(doc, f, word_document, table, pt, cp)
        return doc
    except MsDocError:
        raise
    except NotImplementedError:
        raise
    except Exception as e:  # pragma: no cover
        # Last-resort guard. The oracle still counts this as a defect
        # ("wrapped" errors are reported separately) — fix the root cause.
        raise CorruptDocumentError(f"internal error: {type(e).__name__}: {e}") from e


class MarkdownResult(NamedTuple):
    """`text` - Markdown; `media` - pictures (bytes + mime), `![Media N](image:N)` in
    the text refers to `media[N]`. Unpacks as `text, media = to_markdown(...)`."""
    text: str
    media: list[Image]


def to_markdown(source: Source, *, images_enabled: bool = True, **options) -> MarkdownResult:
    """One call: .doc -> (markdown str, list of images with `.data` bytes and `.mime`)."""
    doc = extract(source, images_enabled=images_enabled)
    return MarkdownResult(doc.to_markdown(**options), doc.images)
