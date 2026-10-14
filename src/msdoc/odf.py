# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice xmloff/source/text/
# (txtparai.cxx, txtimp.cxx, txtfldi.cxx, txtlists.cxx, XMLTextListBlockContext.cxx,
# XMLTextFrameContext.cxx), xmloff/source/core/xmlmultiimagehelper.cxx and
# sw/source/core/fields/docufld.cxx (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""OpenDocument (ODF 1.2 / 1.3, OASIS) core shared by odt.py, ods.py and odp.py.

Everything here is built on ``zipfile`` and ``xml.etree`` only and treats the
package as hostile: entry count, per-entry and total uncompressed sizes,
compression ratio, XML size / depth / element count / expanded character data
are bounded; entity declarations that could amplify are refused and external
entities are never loaded (no billion laughs, no XXE); path traversal names
are never looked up. Only ``MsDocError`` subclasses escape.

HOW TO USE FROM ods/odp
-----------------------
::

    from . import odf
    from .model import Document

    def parse(pkg: odf.Package, images_enabled: bool = True) -> Document:
        content = pkg.content()                        # hardened root of content.xml
        sheet = odf.StyleSheet(content, pkg.styles())  # styles.xml may be None
        doc = Document(format="ods")
        w = odf.Walker(pkg, sheet, doc, images_enabled=images_enabled)
        body = odf.find(content, "office:body", "office:spreadsheet")   # or office:presentation
        ...
        doc.warnings.extend(pkg.warnings + w.warnings)
        return doc

Package (open_package / detect): ``read``, ``xml``, ``has``, ``names``, ``content``,
``styles``, ``meta``, ``manifest``, ``mimetype``, ``kind``, ``template``.
XML: ``parse_xml``, ``qn('text:p')``, ``local``, ``ns_of``, ``is_``, ``attr``, ``int_attr``,
``bool_attr``, ``children``, ``find``.
Styles: ``StyleSheet.get / chain / lookup / style_attr / outline_level /
paragraph_list_style / list_style / text_hidden / data_style`` (number:*-style
elements for value formatting), ``master_pages``, ``page_layouts``; ``ListStyle``,
``ListLevel``, ``ListTracker`` (numbering labels), ``format_number``.
Tables: ``iter_rows`` / ``iter_cells`` (repeats capped, header rows flagged),
``column_default_styles``, ``repeat``.
Pictures: ``image_bytes`` (package entry or office:binary-data, external links and
ObjectReplacements skipped, format sniffed), ``best_image`` (one of several draw:image
of a frame, as LibreOffice picks it), ``sniff_image_mime``, ``package_path``,
``is_external_href``.
Text: ``Walker.blocks(container, story)`` for the block content of a cell, text box,
note or header; ``Walker.paragraph(el, story)`` for one text:p/text:h;
``Walker.drawing(el, story, text_story)`` -> (blocks, picture indexes) of a draw:frame,
draw:g, draw:a or text shape (embedded objects give nothing); ``Walker.field_text``;
``flatten(blocks, w.synthetic)`` -> plain paragraph strings. Set ``w.text_type``
(``Walker.within("cell", fn, ...)``) to the XMLTextType of what is walked: it decides
which block elements LibreOffice imports there (no tables in notes/shape text, frames
only at the start of a body or in a text box). For ods, cell text is ``w.within("cell",
w.blocks, cell_el, "main")``; for odp, shape text is ``w.drawing(shape, "main", "main")``
and presentation:notes frames the same.

``Walker`` keeps what it meets outside the current block list in
``w.notes`` (footnotes/endnotes, one block list per note), ``w.comments``
(office:annotation), ``w.textboxes`` (text of frames/shapes met inside
paragraphs) and appends pictures to ``doc.images``. Set ``w.part = "styles"``
before walking content that lives in styles.xml (header/footer, master pages)
so that automatic style names resolve in the right file.
"""
from __future__ import annotations

import base64
import binascii
import functools
import io
import re
import struct
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from typing import Iterator, Optional
from xml.parsers import expat

from .errors import CorruptDocumentError, EncryptedDocumentError, MsDocError
from .images import blip_looks_valid, wmf_looks_valid
from .model import IMAGE_MARK, NOTE_MARK, Cell, Document, Image, Link, Paragraph, Row, Table

# ---------------------------------------------------------------- limits
MAX_ENTRIES = 65536                 # zip entries in a package
MAX_ENTRY_SIZE = 256 << 20          # uncompressed bytes of one entry
MAX_XML_SIZE = 192 << 20            # uncompressed bytes of one XML stream
MAX_TOTAL_SIZE = 1 << 30            # uncompressed bytes read from one package
MAX_RATIO = 200                     # compression ratio allowed for entries above RATIO_MIN_SIZE
RATIO_MIN_SIZE = 8 << 20
MAX_XML_DEPTH = 160                 # deeper elements are flattened into their ancestor
MAX_XML_ELEMENTS = 8_000_000
MAX_REPEAT = 1024                   # one number-columns/rows-repeated expansion (Writer tables)
MAX_SPACES = 1 << 16                # text:s text:c
MAX_WORK = 2_000_000                # paragraphs + cells produced by one Walker (repeat expansion budget)
MAX_LIST_LEVEL = 10

# ---------------------------------------------------------------- namespaces
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "number": "urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "chart": "urn:oasis:names:tc:opendocument:xmlns:chart:1.0",
    "dr3d": "urn:oasis:names:tc:opendocument:xmlns:dr3d:1.0",
    "form": "urn:oasis:names:tc:opendocument:xmlns:form:1.0",
    "script": "urn:oasis:names:tc:opendocument:xmlns:script:1.0",
    "anim": "urn:oasis:names:tc:opendocument:xmlns:animation:1.0",
    "smil": "urn:oasis:names:tc:opendocument:xmlns:smil-compatible:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
    "of": "urn:oasis:names:tc:opendocument:xmlns:of:1.2",
    "loext": "urn:org:documentfoundation:names:experimental:office:xmlns:loext:1.0",
    "calcext": "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0",
    "field": "urn:openoffice:names:experimental:ooo-ms-interop:xmlns:field:1.0",
    "officeooo": "http://openoffice.org/2009/office",
    "tableooo": "http://openoffice.org/2009/table",
    "drawooo": "http://openoffice.org/2010/draw",
    "ooo": "http://openoffice.org/2004/office",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "xml": "http://www.w3.org/XML/1998/namespace",
}


@functools.lru_cache(maxsize=None)
def qn(name: str) -> str:
    """'text:p' -> '{urn:oasis:...:text:1.0}p' (Clark notation used by ElementTree)."""
    prefix, _, local_name = name.partition(":")
    return "{%s}%s" % (NS[prefix], local_name)


def local(el_or_tag) -> str:
    """Local name of an element (or a Clark tag): '{ns}p' -> 'p'."""
    tag = el_or_tag if isinstance(el_or_tag, str) else el_or_tag.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def ns_of(el_or_tag) -> str:
    """Namespace URI of an element (or a Clark tag), '' if none."""
    tag = el_or_tag if isinstance(el_or_tag, str) else el_or_tag.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return ""
    return tag[1:tag.find("}")]


def is_(el, name: str) -> bool:
    """True when `el` is the element 'prefix:local'."""
    return el is not None and el.tag == qn(name)


def attr(el, name: str, default=None):
    """Attribute 'prefix:local' of `el` (default when absent or el is None)."""
    if el is None:
        return default
    return el.get(qn(name), default)


def int_attr(el, name: str, default: int, lo: int = -(1 << 31), hi: int = (1 << 31) - 1) -> int:
    """Integer attribute clamped to [lo, hi]; `default` when absent or not a number."""
    v = attr(el, name)
    if v is None:
        return default
    try:
        n = int(v.strip())
    except (ValueError, AttributeError):
        return default
    return max(lo, min(hi, n))


def bool_attr(el, name: str, default: bool = False) -> bool:
    """xsd:boolean attribute ('true'/'1' -> True)."""
    v = attr(el, name)
    if v is None:
        return default
    return v.strip() in ("true", "1")


def children(el, name: Optional[str] = None) -> list:
    """Child elements, optionally only those named 'prefix:local'."""
    if el is None:
        return []
    if name is None:
        return list(el)
    tag = qn(name)
    return [c for c in el if c.tag == tag]


def find(el, *path: str):
    """Follow child names ('office:body', 'office:text', ...); None if any step is missing."""
    for name in path:
        if el is None:
            return None
        el = el.find(qn(name))
    return el


# ---------------------------------------------------------------- hardened XML
class _DtdRefused(Exception):
    pass


class _Builder:
    """expat handlers building an ElementTree; bounds depth and element count.

    Elements deeper than MAX_XML_DEPTH are not created: their character data
    is appended to the nearest kept ancestor, so text survives but walkers
    never recurse deeper than the limit."""

    def __init__(self):
        self.tb = ET.TreeBuilder()
        self.depth = 0
        self.count = 0
        self.flattened = 0
        self.names: dict = {}
        self.chars = 0
        self.max_chars = MAX_XML_SIZE

    def _name(self, n: str) -> str:
        r = self.names.get(n)
        if r is None:
            r = ("{" + n) if "}" in n else n
            if len(self.names) < 100000:
                self.names[n] = r
        return r

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.count > MAX_XML_ELEMENTS:
            raise CorruptDocumentError("XML has more than %d elements" % MAX_XML_ELEMENTS)
        if self.depth > MAX_XML_DEPTH:
            self.flattened += 1
            return
        nm = self._name
        self.tb.start(nm(tag), {nm(k): v for k, v in attrs.items()} if attrs else {})

    def end(self, tag):
        self.depth -= 1
        if self.depth >= MAX_XML_DEPTH:
            return
        self.tb.end(self._name(tag))

    def data(self, data):
        self.chars += len(data)
        if self.chars > self.max_chars:
            raise CorruptDocumentError("XML character data expands beyond %d characters" % self.max_chars)
        self.tb.data(data)


def parse_xml(data: bytes, what: str = "XML", warnings: Optional[list] = None):
    """Parse untrusted XML into an ElementTree element (Clark-notation tags).

    Entity declarations that could amplify (internal entities referencing
    entities, parameter entities, unparsed entities) raise CorruptDocumentError;
    external entities and the external DTD subset are never loaded (a
    reference to an external entity expands to nothing); the total character
    data is capped at max(4 MiB, 4 x input). Elements deeper than MAX_XML_DEPTH are flattened (a warning
    is appended to `warnings`). Any parse error -> CorruptDocumentError."""
    if len(data) > MAX_XML_SIZE:
        raise CorruptDocumentError("%s larger than %d bytes" % (what, MAX_XML_SIZE))
    target = _Builder()

    def entity_decl(name, is_param, value, base, system_id, public_id, notation):
        # External entities are never fetched (a reference expands to nothing, see below).
        # Internal entities that reference other entities (the billion-laughs shape) and
        # parameter entities are refused; plain ones are bounded by Builder.max_chars.
        if is_param or (value is not None and ("&" in value or "%" in value)):
            raise _DtdRefused()

    def refuse(*_a):
        raise _DtdRefused()
    target.max_chars = max(1 << 22, 4 * len(data))
    try:
        xp = expat.ParserCreate(None, "}")
        xp.buffer_text = True
        try:
            xp.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        except Exception:  # pragma: no cover - very old expat
            pass
        xp.EntityDeclHandler = entity_decl
        xp.UnparsedEntityDeclHandler = refuse
        xp.ExternalEntityRefHandler = lambda *_a: 1   # not loaded: expands to nothing
        xp.StartElementHandler = target.start
        xp.EndElementHandler = target.end
        xp.CharacterDataHandler = target.data
        xp.Parse(bytes(data), True)
        root = target.tb.close()
    except _DtdRefused:
        raise CorruptDocumentError("%s declares DTD entities (refused)" % what) from None
    except MsDocError:
        raise
    except RecursionError:
        raise CorruptDocumentError("%s nested too deeply" % what) from None
    except Exception as e:
        raise CorruptDocumentError("%s is not well-formed: %s" % (what, str(e)[:200])) from None
    if root is None:
        raise CorruptDocumentError("%s is empty" % what)
    if target.flattened and warnings is not None:
        warnings.append("%s: %d elements nested deeper than %d were flattened" % (what, target.flattened, MAX_XML_DEPTH))
    return root


# ---------------------------------------------------------------- package
ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.text-template": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.spreadsheet-template": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.presentation-template": "odp",
}
ODF_PREFIX = "application/vnd.oasis.opendocument."
ZIP_SIGNATURE = b"PK\x03\x04"


def _clean_mimetype(raw: bytes) -> str:
    try:
        return raw.decode("ascii", "replace").strip().strip("\0").strip()
    except Exception:  # pragma: no cover
        return ""


def _first_entry_mimetype(data: bytes) -> Optional[str]:
    """Media type stored in a 'mimetype' first local header (stored, not compressed), else None.
    Used to recognise an ODF package whose central directory is damaged."""
    if len(data) < 30 or data[:4] != ZIP_SIGNATURE:
        return None
    method, csize = struct.unpack_from("<H", data, 8)[0], struct.unpack_from("<I", data, 18)[0]
    nlen, elen = struct.unpack_from("<HH", data, 26)
    if data[30:30 + nlen] != b"mimetype" or method != 0:
        return None
    start = 30 + nlen + elen
    if csize > 1024:
        return None
    return _clean_mimetype(data[start:start + csize]) or None


def _safe_name(name: str) -> bool:
    """False for names that could escape the package root (absolute, '..', drive letters, backslashes)."""
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        return False
    if len(name) > 1 and name[1] == ":":
        return False
    return ".." not in name.split("/")


class Package:
    """An opened ODF package (zip). Never raises anything but MsDocError.

    Attributes: ``mimetype`` (str), ``kind`` ('odt' | 'ods' | 'odp' | None for
    another ODF type), ``template`` (bool), ``manifest`` ({full-path: media-type}),
    ``warnings`` (list[str]). Use ``read(name)``, ``xml(name)``, ``has(name)``,
    ``content()``, ``styles()``, ``meta()``."""

    def __init__(self, data: bytes):
        self.warnings: list = []
        self._read_total = 0
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except MsDocError:
            raise
        except Exception as e:
            raise CorruptDocumentError("damaged zip package: %s" % (str(e)[:200] or type(e).__name__)) from None
        if len(infos) > MAX_ENTRIES:
            raise CorruptDocumentError("zip package has more than %d entries" % MAX_ENTRIES)
        self._infos: dict = {}
        bad = 0
        for info in infos:
            if not _safe_name(info.filename):
                bad += 1
                continue
            self._infos.setdefault(info.filename, info)
        if bad:
            self.warnings.append("%d zip entries with unsafe names ignored" % bad)
        mt = ""
        if "mimetype" in self._infos:
            try:
                mt = _clean_mimetype(self.read("mimetype", limit=1024))
            except MsDocError:
                mt = ""
            if infos and infos[0].filename != "mimetype":
                self.warnings.append("'mimetype' is not the first zip entry")
        self.manifest: dict = {}
        self.encrypted = False
        self.encrypted_entries: set = set()
        self._parse_manifest()
        if not mt.startswith(ODF_PREFIX):
            mt = self.manifest.get("/", mt)
        self.mimetype = mt
        self.kind = ODF_MIMETYPES.get(mt)
        self.template = mt.endswith("-template")

    # ---- entries
    def names(self) -> list:
        """Safe entry names in zip order."""
        return list(self._infos)

    def has(self, name: str) -> bool:
        """True when the package holds the (safe) entry `name`."""
        return name in self._infos

    def read(self, name: str, limit: int = MAX_ENTRY_SIZE) -> bytes:
        """Uncompressed bytes of entry `name` (at most `limit`). Raises CorruptDocumentError
        when the entry is missing, too large, has a bomb-like ratio or is damaged, and
        EncryptedDocumentError for a zip-encrypted entry."""
        info = self._infos.get(name)
        if info is None:
            raise CorruptDocumentError("package entry %r missing" % name)
        size = info.file_size
        if size > limit:
            raise CorruptDocumentError("package entry %r too large (%d bytes)" % (name, size))
        if size > RATIO_MIN_SIZE and size > MAX_RATIO * max(1, info.compress_size):
            raise CorruptDocumentError("package entry %r has compression ratio above %d" % (name, MAX_RATIO))
        if self._read_total + size > MAX_TOTAL_SIZE:
            raise CorruptDocumentError("package uncompressed size above %d bytes" % MAX_TOTAL_SIZE)
        if info.flag_bits & 1:
            raise EncryptedDocumentError("zip entry %r is encrypted" % name)
        try:
            with self.zip.open(info) as f:
                out = f.read(limit + 1)
        except MsDocError:
            raise
        except Exception as e:
            raise CorruptDocumentError("cannot read package entry %r: %s" % (name, str(e)[:200] or type(e).__name__)) from None
        if len(out) > limit:
            raise CorruptDocumentError("package entry %r too large" % name)
        self._read_total += len(out)
        return out

    def xml(self, name: str):
        """Hardened parse of XML entry `name`; None when the entry does not exist."""
        if name not in self._infos:
            return None
        return parse_xml(self.read(name, MAX_XML_SIZE), name, self.warnings)

    def content(self):
        """Root of content.xml (CorruptDocumentError when missing; EncryptedDocumentError when encrypted)."""
        self.check_not_encrypted()
        root = self.xml("content.xml")
        if root is None:
            raise CorruptDocumentError("content.xml missing")
        return root

    def styles(self):
        """Root of styles.xml or None."""
        return self.xml("styles.xml")

    def meta(self):
        """Root of meta.xml or None (a damaged meta.xml only adds a warning)."""
        try:
            return self.xml("meta.xml")
        except EncryptedDocumentError:
            raise
        except MsDocError as e:
            self.warnings.append("meta.xml ignored: %s" % e)
            return None

    # ---- manifest / encryption
    def _parse_manifest(self) -> None:
        if "META-INF/manifest.xml" not in self._infos:
            self.warnings.append("META-INF/manifest.xml missing")
            return
        try:
            root = self.xml("META-INF/manifest.xml")
        except EncryptedDocumentError:
            self.encrypted = True
            return
        except MsDocError as e:
            self.warnings.append("META-INF/manifest.xml ignored: %s" % e)
            return
        for el in root.iter():
            if el.tag == qn("manifest:file-entry"):
                path = attr(el, "manifest:full-path")
                if path is not None:
                    self.manifest.setdefault(path, attr(el, "manifest:media-type", ""))
                    if el.find(qn("manifest:encryption-data")) is not None and len(self.encrypted_entries) < MAX_ENTRIES:
                        self.encrypted_entries.add(path)
        # The document is password-protected when its content streams are encrypted (ODF 1.2
        # 3.4); LibreOffice 7.x "wholesome" encryption stores everything in 'encrypted-package'.
        # Encrypted entries elsewhere (a password-protected Basic library) do not matter.
        self.encrypted = bool(self.encrypted_entries & {"content.xml", "styles.xml", "encrypted-package"})

    def check_not_encrypted(self) -> None:
        """Raise EncryptedDocumentError for a password/key-encrypted package (manifest:encryption-data
        on content.xml / styles.xml, or LibreOffice 7.x 'wholesome' encryption: a placeholder
        content.xml next to an encrypted 'encrypted-package' entry)."""
        if self.encrypted:
            raise EncryptedDocumentError("password-protected OpenDocument package")


def detect(data: bytes) -> Optional[str]:
    """ODF media type of a zip, or None when `data` is not an ODF package.

    Cheap and never raises. The 'mimetype' entry wins (also when it is not the
    first entry, as LibreOffice accepts), then the manifest's '/' entry; a
    package whose central directory is damaged is still recognised from a
    stored 'mimetype' first entry (so that it fails as CorruptDocumentError)."""
    if bytes(data[:4]) != ZIP_SIGNATURE:
        return None
    first = _first_entry_mimetype(bytes(data[:2048]))
    try:
        pkg = Package(bytes(data))
        mt = pkg.mimetype
    except MsDocError:
        mt = first or ""
    except Exception:  # pragma: no cover - detect() must never raise
        mt = first or ""
    return mt if mt.startswith(ODF_PREFIX) else None


def open_package(data: bytes) -> Package:
    """Open and validate an ODF package; EncryptedDocumentError when encrypted."""
    pkg = Package(bytes(data))
    pkg.check_not_encrypted()
    return pkg


# ---------------------------------------------------------------- styles
_GROUPS = ("paragraph-properties", "text-properties", "table-properties", "table-column-properties",
           "table-row-properties", "table-cell-properties", "graphic-properties", "section-properties",
           "drawing-page-properties", "list-level-properties", "chart-properties", "ruby-properties")


class Style:
    """One style:style / style:default-style: family, name, parent name, the element, and
    the property elements by group ('text-properties', 'paragraph-properties', ...)."""
    __slots__ = ("family", "name", "parent", "el", "props")

    def __init__(self, el):
        self.el = el
        self.family = attr(el, "style:family", "")
        self.name = attr(el, "style:name", "")
        self.parent = attr(el, "style:parent-style-name")
        self.props = {}
        for ch in el:
            ln = local(ch)
            if ln in _GROUPS and ns_of(ch) == NS["style"]:
                self.props.setdefault(ln, ch)

    def get(self, name: str, default=None):
        """Attribute of the style element itself ('style:list-style-name', ...)."""
        return attr(self.el, name, default)

    def prop(self, group: str, name: str, default=None):
        """Attribute `name` of the `group` property element of this style only."""
        p = self.props.get(group)
        return default if p is None else attr(p, name, default)


class ListLevel:
    """One level (1..10) of a text:list-style / text:outline-style."""
    __slots__ = ("kind", "num_format", "prefix", "suffix", "display_levels", "start", "letter_sync",
                 "bullet", "list_format")

    def __init__(self, el=None):
        self.kind = "number"          # 'number' | 'bullet' | 'image' | 'outline'
        self.num_format = ""
        self.prefix = ""
        self.suffix = ""
        self.display_levels = 1
        self.start = 1
        self.letter_sync = False
        self.bullet = ""
        self.list_format = None      # loext:num-list-format ("%1%.%2%.")
        if el is None:
            return
        ln = local(el)
        self.kind = {"list-level-style-number": "number", "list-level-style-bullet": "bullet",
                     "list-level-style-image": "image", "outline-level-style": "number"}.get(ln, "number")
        self.num_format = attr(el, "style:num-format", "") if self.kind == "number" else ""
        self.prefix = attr(el, "style:num-prefix", "") or ""
        self.suffix = attr(el, "style:num-suffix", "") or ""
        self.display_levels = int_attr(el, "text:display-levels", 1, 1, MAX_LIST_LEVEL)
        self.start = int_attr(el, "text:start-value", 1, 0, 1 << 30)
        self.letter_sync = bool_attr(el, "style:num-letter-sync")
        self.bullet = attr(el, "text:bullet-char", "") or ""
        self.list_format = attr(el, "loext:num-list-format")

    @property
    def ordered(self) -> bool:
        return self.kind == "number"


class ListStyle:
    """text:list-style or text:outline-style: ``levels[1..10]`` of ListLevel (missing levels
    are numbered decimal levels with no format, the ODF default)."""

    def __init__(self, el=None, name: str = ""):
        self.name = name
        self.levels: dict = {}
        if el is not None:
            for ch in el:
                lvl = int_attr(ch, "text:level", 0, 0, 1000)
                if 1 <= lvl <= MAX_LIST_LEVEL and lvl not in self.levels:
                    self.levels[lvl] = ListLevel(ch)

    def level(self, n: int) -> ListLevel:
        """ListLevel for 1-based level n (clamped to 1..10)."""
        n = max(1, min(MAX_LIST_LEVEL, n))
        lv = self.levels.get(n)
        if lv is None:
            lv = ListLevel()
        return lv


class StyleSheet:
    """Styles of a package: automatic styles of content.xml (part 'content') and of
    styles.xml (part 'styles'), common styles (office:styles), default styles, list
    styles, the outline style, master pages and number/date styles.

    Lookups take the part the referencing element lives in, because both
    files may define automatic styles with the same name (P1, T1, ...)."""

    MAX_CHAIN = 64

    def __init__(self, content=None, styles=None):
        self.auto = {"content": {}, "styles": {}}       # (family, name) -> Style
        self.common: dict = {}                          # (family, name) -> Style
        self.defaults: dict = {}                        # family -> Style
        self.lists = {"content": {}, "styles": {}}      # name -> ListStyle (automatic)
        self.common_lists: dict = {}
        self.data_styles = {"content": {}, "styles": {}}
        self.outline: Optional[ListStyle] = None
        self.master_pages: list = []
        self.page_layouts: dict = {}
        if styles is not None:
            self._load(find(styles, "office:styles"), "common")
            self._load(find(styles, "office:automatic-styles"), "styles")
            mp = find(styles, "office:master-styles")
            if mp is not None:
                self.master_pages = children(mp, "style:master-page")
        if content is not None:
            # office:document-content may carry office:styles too (flat-like packages)
            self._load(find(content, "office:styles"), "common")
            self._load(find(content, "office:automatic-styles"), "content")

    def _load(self, root, where: str) -> None:
        if root is None:
            return
        for el in root:
            tag = el.tag
            if tag == qn("style:style"):
                s = Style(el)
                d = self.common if where == "common" else self.auto[where]
                d.setdefault((s.family, s.name), s)
            elif tag == qn("style:default-style"):
                s = Style(el)
                self.defaults.setdefault(s.family, s)
            elif tag == qn("text:list-style"):
                name = attr(el, "style:name", "")
                d = self.common_lists if where == "common" else self.lists[where]
                d.setdefault(name, ListStyle(el, name))
            elif tag == qn("text:outline-style"):
                if self.outline is None:
                    self.outline = ListStyle(el, attr(el, "style:name", ""))
            elif tag == qn("style:page-layout"):
                self.page_layouts.setdefault(attr(el, "style:name", ""), el)
            elif ns_of(el) == NS["number"] and attr(el, "style:name"):
                d = self.data_styles["styles" if where == "common" else where]
                d.setdefault(attr(el, "style:name"), el)

    def get(self, family: str, name: Optional[str], part: str = "content") -> Optional[Style]:
        """Style named `name` of `family`: automatic style of `part` first, then common."""
        if not name:
            return None
        s = self.auto.get(part, {}).get((family, name))
        if s is None:
            s = self.common.get((family, name))
        return s

    def chain(self, family: str, name: Optional[str], part: str = "content") -> list:
        """[style, parent, grandparent, ...] (cycle-safe, at most MAX_CHAIN), then the
        default style of the family when there is one."""
        out = []
        seen = set()
        s = self.get(family, name, part)
        while s is not None and len(out) < self.MAX_CHAIN and id(s) not in seen:
            seen.add(id(s))
            out.append(s)
            s = self.common.get((family, s.parent)) if s.parent else None
        d = self.defaults.get(family)
        if d is not None:
            out.append(d)
        return out

    def lookup(self, family: str, name: Optional[str], group: str, prop: str, part: str = "content", default=None):
        """Inherited value of property `prop` ('fo:font-weight') of property group
        `group` ('text-properties') along the parent chain of style `name`."""
        for s in self.chain(family, name, part):
            v = s.prop(group, prop)
            if v is not None:
                return v
        return default

    def style_attr(self, family: str, name: Optional[str], a: str, part: str = "content", default=None):
        """Inherited attribute of style:style itself ('style:list-style-name', ...)."""
        for s in self.chain(family, name, part):
            v = s.get(a)
            if v is not None:
                return v
        return default

    def outline_level(self, name: Optional[str], part: str = "content") -> int:
        """style:default-outline-level of a paragraph style (inherited), 0 when none."""
        v = self.style_attr("paragraph", name, "style:default-outline-level", part)
        try:
            return max(0, min(MAX_LIST_LEVEL, int(v))) if v not in (None, "") else 0
        except ValueError:
            return 0

    def paragraph_list_style(self, name: Optional[str], part: str = "content") -> Optional[str]:
        """style:list-style-name of a paragraph style chain: None when not set, '' when
        explicitly set to no list."""
        return self.style_attr("paragraph", name, "style:list-style-name", part)

    def list_style(self, name: Optional[str], part: str = "content") -> Optional[ListStyle]:
        """text:list-style named `name` (automatic of `part` first, then common)."""
        if not name:
            return None
        ls = self.lists.get(part, {}).get(name)
        if ls is None:
            ls = self.common_lists.get(name)
        if ls is None and part != "content":
            ls = self.lists["content"].get(name)
        return ls

    def text_hidden(self, family: str, name: Optional[str], part: str = "content") -> bool:
        """True when the style's text-properties say text:display="none" (hidden text)."""
        return self.lookup(family, name, "text-properties", "text:display", part) in ("none", "true")

    def data_style(self, name: Optional[str], part: str = "content"):
        """number:*-style element (number/date/time/currency/percentage/boolean/text) named `name`."""
        if not name:
            return None
        el = self.data_styles.get(part, {}).get(name)
        if el is None:
            el = self.data_styles["styles"].get(name) or self.data_styles["content"].get(name)
        return el


# ---------------------------------------------------------------- numbering
def _roman(n: int) -> str:
    vals = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
            (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def format_number(n: int, fmt: str, letter_sync: bool = False) -> str:
    """A list/page number in an ODF style:num-format: '1', 'a', 'A', 'i', 'I'
    (letters: a..z, aa, ab, ... or with style:num-letter-sync a..z, aa, bb, ...);
    '' -> ''; any other format falls back to decimal."""
    if fmt == "":
        return ""
    if fmt in ("a", "A"):
        if n <= 0:
            return ""
        if letter_sync:
            s = chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        else:
            s = ""
            m = n
            while m > 0 and len(s) < 64:
                m -= 1
                s = chr(ord("a") + m % 26) + s
                m //= 26
        return s.upper() if fmt == "A" else s
    if fmt in ("i", "I"):
        if n <= 0 or n >= 4000:
            return str(n)
        s = _roman(n)
        return s.upper() if fmt == "I" else s
    return str(n)


class ListTracker:
    """List numbering state (ODF 1.2 19.880 text:list, 19.168 continue-numbering,
    19.167 continue-list; LibreOffice XMLTextListsHelper).

    ``open(list_el, style_name, parent)`` returns the list id of a text:list
    (nested lists share the id of their parent); ``count(list_id, level,
    style, start)`` advances the counter of a numbered paragraph and returns
    its label ('3.', 'b)', '1.2.'), or None for bullets/unnumbered levels."""

    def __init__(self, mso: bool = False):
        self.counters: dict = {}
        self.by_xmlid: dict = {}
        self.last_of_style: dict = {}
        self.last = None
        self.last_style = None
        self.mso = mso
        """Documents written by Microsoft Office: continue-numbering joins the last list of the
        same style even across other lists (XMLTextListBlockContext, IsMSO)."""
        self._next = 0

    def new_id(self) -> int:
        self._next += 1
        self.counters[self._next] = [None] * (MAX_LIST_LEVEL + 1)
        return self._next

    def open(self, el, style_name: Optional[str], parent: Optional[int] = None) -> int:
        """List id of a text:list (XMLTextListBlockContext): a nested list shares its parent's
        id; a root list is identified by its xml:id; text:continue-list joins the named list;
        text:continue-numbering="true" joins the list processed just before when it has the
        same list style; otherwise a new list starts."""
        if parent is not None:
            return parent
        style = style_name or ""
        xid = attr(el, "xml:id")
        lid = self.by_xmlid.get(xid) if xid else None
        if lid is None:
            cont = attr(el, "text:continue-list")
            cn = attr(el, "text:continue-numbering")
            if cont and cont in self.by_xmlid:
                lid = self.by_xmlid[cont]
            elif cn is not None and cn.strip() == "true":
                if self.last is not None and self.last_style == style:
                    lid = self.last
                elif self.mso:
                    lid = self.last_of_style.get(style)
            if lid is None:
                lid = self.new_id()
            if xid and len(self.by_xmlid) < 100000:
                self.by_xmlid[xid] = lid
        self.last = lid
        self.last_style = style
        if len(self.last_of_style) < 100000:
            self.last_of_style[style] = lid
        return lid

    def count(self, lid: int, level: int, style: Optional[ListStyle], start: Optional[int] = None) -> Optional[str]:
        """Advance level `level` (1-based) of list `lid`; deeper levels restart."""
        c = self.counters.get(lid)
        if c is None:
            c = self.counters[lid] = [None] * (MAX_LIST_LEVEL + 1)
        level = max(1, min(MAX_LIST_LEVEL, level))
        lv = style.level(level) if style is not None else ListLevel()
        if start is not None:
            c[level] = start
        elif c[level] is None:
            c[level] = lv.start
        else:
            c[level] += 1
        for k in range(level + 1, MAX_LIST_LEVEL + 1):
            c[k] = None
        if style is None or not lv.ordered:
            return None
        return self.label(c, level, style)

    @staticmethod
    def label(c: list, level: int, style: ListStyle) -> Optional[str]:
        lv = style.level(level)

        def val(k):
            v = c[k]
            return style.level(k).start if v is None else v

        if lv.list_format:
            def sub(m):
                k = int(m.group(1))
                if not 1 <= k <= level:
                    return ""
                lk = style.level(k)
                return format_number(val(k), lk.num_format, lk.letter_sync)
            s = re.sub(r"%(\d+)%", sub, lv.list_format)
            return s or None
        d = max(1, min(lv.display_levels, level))
        parts = []
        for k in range(level - d + 1, level + 1):
            lk = style.level(k)
            if k < level and lk.num_format == "":
                continue
            parts.append(format_number(val(k), lk.num_format, lk.letter_sync))
        s = lv.prefix + ".".join(parts) + lv.suffix
        return s or None


# ---------------------------------------------------------------- images
_QUALITY = {"image/bmp": 10, "image/gif": 20, "image/jpeg": 30, "image/png": 40, "image/x-vclgraphic": 990,
            "image/x-svm": 1000, "image/x-wmf": 1010, "image/x-emf": 1020, "image/x-eps": 1025,
            "application/pdf": 1030, "image/svg+xml": 1040}
_EXT_MIME = {"bmp": "image/bmp", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg",
             "png": "image/png", "svm": "image/x-svm", "wmf": "image/x-wmf", "emf": "image/x-emf",
             "eps": "image/x-eps", "pdf": "application/pdf", "svg": "image/svg+xml", "tif": "image/tiff",
             "tiff": "image/tiff"}


def image_quality(image_el, pkg: Optional["Package"] = None) -> int:
    """Rank of a draw:image among the alternatives of one draw:frame (xmloff
    xmlmultiimagehelper.cxx getQualityIndex): the declared draw:mime-type (or
    loext:mime-type), else the href extension, else the sniffed content."""
    mt = attr(image_el, "draw:mime-type") or attr(image_el, "loext:mime-type")
    if not mt:
        href = attr(image_el, "xlink:href")
        if href:
            tail = href.rsplit("/", 1)[-1]
            mt = _EXT_MIME.get(tail.rsplit(".", 1)[-1].lower(), "") if "." in tail else ""
        elif pkg is not None:
            got = image_bytes(pkg, image_el)
            mt = got[1] if got else ""
    return _QUALITY.get(mt or "", 0)


def best_image(images: list, pkg: Optional["Package"] = None):
    """The draw:image LibreOffice keeps out of several in one frame (MultiImageImportHelper::
    solveMultipleImages): the first one of the highest quality; the last one when none ranks."""
    if not images:
        return None
    if len(images) == 1:
        return images[0]
    best, bq = None, 0
    for im in images:
        q = image_quality(im, pkg)
        if q > bq:
            best, bq = im, q
    return best if best is not None else images[-1]


def is_external_href(href: Optional[str]) -> bool:
    """True for an href that does not point into the package (URL scheme, '../', absolute path)."""
    if not href:
        return True
    h = href.strip()
    if h.startswith(("/", "../", "\\")) or h.startswith("#"):
        return True
    scheme = h.split("/", 1)[0]
    return ":" in scheme


def package_path(href: Optional[str]) -> Optional[str]:
    """Package entry name of an internal href ('./Pictures/a%20b.png' -> 'Pictures/a b.png'), else None."""
    if is_external_href(href):
        return None
    h = href.strip()
    while h.startswith("./"):
        h = h[2:]
    h = h.rstrip("/")
    return h or None


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Media type from the leading bytes (PNG, JPEG, GIF, BMP, TIFF, EMF, WMF, SVG, SVM, PDF, WebP), or None."""
    b = data[:64]
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:2] == b"BM":
        return "image/bmp"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if len(data) >= 44 and b[:4] == b"\x01\x00\x00\x00" and b[40:44] == b" EMF":
        return "image/x-emf"
    if wmf_looks_valid(data):
        return "image/x-wmf"
    if b[:6] == b"VCLMTF":
        return "image/x-svm"
    if b[:5] == b"%PDF-":
        return "application/pdf"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<?xml", b"<svg", b"<!doctype svg", b"<!--")) and b"<svg" in data[:65536].lower():
        return "image/svg+xml"
    return None


def image_bytes(pkg: Package, image_el, warnings: Optional[list] = None):
    """(bytes, mime) of a draw:image (package entry or office:binary-data), or None for an
    external link, a LibreOffice replacement graphic (ObjectReplacements/), a missing,
    empty or unrecognised picture."""
    href = attr(image_el, "xlink:href")
    data = None
    if href:
        path = package_path(href)
        if path is None:
            return None
        if path.startswith("ObjectReplacements/") or path.split("/")[0] == "TablePreview":
            return None
        cands = [path]
        unq = urllib.parse.unquote(path)
        if unq != path:
            cands.append(unq)
        for c in cands:
            if pkg.has(c):
                try:
                    data = pkg.read(c)
                except EncryptedDocumentError:
                    raise
                except MsDocError as e:
                    if warnings is not None:
                        warnings.append("picture %r skipped: %s" % (c, e))
                    return None
                break
        if data is None:
            return None
    else:
        bd = image_el.find(qn("office:binary-data"))
        if bd is None or not bd.text:
            return None
        txt = bd.text
        if len(txt) > MAX_ENTRY_SIZE * 4 // 3 + 1024:
            return None
        txt = re.sub(r"[^A-Za-z0-9+/]", "", txt)
        txt += "=" * (-len(txt) % 4)
        try:
            data = base64.b64decode(txt)
        except (binascii.Error, ValueError):
            if warnings is not None:
                warnings.append("bad base64 picture skipped")
            return None
    if not data:
        return None
    mime = sniff_image_mime(data)
    if mime is None:
        return None
    if mime in ("image/png", "image/jpeg", "image/gif") and not blip_looks_valid(data, mime):
        return None
    return data, mime


# ---------------------------------------------------------------- tables
def repeat(el, name: str, cap: int = MAX_REPEAT) -> int:
    """number-*-repeated style count of `el`, clamped to [1, cap]."""
    return int_attr(el, name, 1, 1, cap)


def iter_rows(table_el, cap: int = MAX_REPEAT, max_rows: int = 1 << 20) -> Iterator:
    """(row element, header: bool, repeat count) for every table:table-row of a table, also
    inside table:table-header-rows / table-rows / table-row-group (nested groups),
    in document order. Repeat counts are capped at `cap`; at most `max_rows` rows."""
    n = 0
    stack = [(iter(list(table_el)), False, 0)]
    while stack:
        it, header, depth = stack[-1]
        el = next(it, None)
        if el is None:
            stack.pop()
            continue
        tag = el.tag
        if tag == qn("table:table-row"):
            r = repeat(el, "table:number-rows-repeated", cap)
            yield el, header, r
            n += r
            if n >= max_rows:
                return
        elif tag in (qn("table:table-header-rows"), qn("table:table-rows"), qn("table:table-row-group")) and depth < 32:
            stack.append((iter(list(el)), header or tag == qn("table:table-header-rows"), depth + 1))


def iter_cells(row_el, cap: int = MAX_REPEAT) -> Iterator:
    """(cell element, covered: bool, repeat count) for each table:table-cell /
    table:covered-table-cell of a row; repeat counts capped at `cap`."""
    for el in row_el:
        if el.tag == qn("table:table-cell"):
            yield el, False, repeat(el, "table:number-columns-repeated", cap)
        elif el.tag == qn("table:covered-table-cell"):
            yield el, True, repeat(el, "table:number-columns-repeated", cap)


def column_default_styles(table_el, max_cols: int = 1 << 14) -> list:
    """table:default-cell-style-name of every column (table:table-column, also inside
    table:table-columns / table-header-columns / table-column-group), repeats expanded,
    at most `max_cols` entries; None where a column has no default cell style."""
    out: list = []

    def walk(el, depth):
        for ch in el:
            if len(out) >= max_cols:
                return
            if ch.tag == qn("table:table-column"):
                n = repeat(ch, "table:number-columns-repeated", max_cols - len(out))
                out.extend([attr(ch, "table:default-cell-style-name")] * n)
            elif ch.tag in (qn("table:table-columns"), qn("table:table-header-columns"),
                            qn("table:table-column-group")) and depth < 32:
                walk(ch, depth + 1)
    walk(table_el, 0)
    return out


# ---------------------------------------------------------------- text
_WS = re.compile(r"[ \t\r\n]+")

# inline elements without visible text (LibreOffice does not render their content)
_INLINE_SKIP = {qn(x) for x in (
    "text:note-citation", "text:number", "text:bookmark", "text:bookmark-start", "text:bookmark-end",
    "text:reference-mark", "text:reference-mark-start", "text:reference-mark-end",
    "text:soft-page-break", "text:change", "text:change-start", "text:change-end",
    "office:annotation-end", "text:toc-mark", "text:toc-mark-start", "text:toc-mark-end",
    "text:alphabetical-index-mark", "text:alphabetical-index-mark-start", "text:alphabetical-index-mark-end",
    "text:user-index-mark", "text:user-index-mark-start", "text:user-index-mark-end",
    "text:hidden-paragraph", "text:variable-decls", "text:sequence-decls", "text:user-field-decls",
    "office:event-listeners", "text:tracked-changes", "field:fieldmark", "field:fieldmark-start",
    "field:fieldmark-separator", "field:fieldmark-end")}

# text fields LibreOffice imports (xmloff txtfldi.cxx CreateTextFieldImportContext)
FIELDS = {qn("text:" + x) for x in (
    "author-initials", "author-name", "bibliography-mark", "bookmark-ref", "chapter", "character-count",
    "conditional-text", "creation-date", "creation-time", "creator", "database-display", "database-name",
    "database-next", "database-row-number", "database-row-select", "date", "dde-connection", "description",
    "drop-down", "editing-cycles", "editing-duration", "execute-macro", "expression", "file-name",
    "hidden-text", "image-count", "initial-creator", "keywords", "measure", "modification-date",
    "modification-time", "note-ref", "object-count", "page-continuation", "page-continuation-string",
    "page-count", "page-name", "page-number", "page-variable-get", "page-variable-set", "paragraph-count",
    "placeholder", "printed-by", "print-date", "print-time", "reference-ref", "script", "sender-city",
    "sender-company", "sender-country", "sender-email", "sender-fax", "sender-firstname", "sender-initials",
    "sender-lastname", "sender-phone-private", "sender-phone-work", "sender-position", "sender-postal-code",
    "sender-state-or-province", "sender-street", "sender-title", "sequence", "sequence-ref", "sheet-name",
    "style-ref", "subject", "table-count", "table-formula", "template-name", "text-input", "time", "title",
    "user-defined", "user-field-get", "user-field-input", "variable-get", "variable-input", "variable-set",
    "word-count")} | {qn("loext:" + x) for x in ("page-count-range", "page-name", "sender-initials", "style-ref")} \
    | {qn("presentation:" + x) for x in ("date-time", "footer", "header")}

# inline containers whose content is ordinary paragraph text
_PASS_THROUGH = {qn(x) for x in ("text:ruby", "text:ruby-base", "text:ruby-text", "text:meta", "text:meta-field",
                                 "loext:content-control")}

# block-level elements that hold no document text
_BLOCK_SKIP = {qn(x) for x in (
    "text:tracked-changes", "text:sequence-decls", "text:variable-decls", "text:user-field-decls",
    "text:dde-connection-decls", "office:forms", "text:table-of-content-source", "text:alphabetical-index-source",
    "text:illustration-index-source", "text:user-index-source", "text:bibliography-source",
    "text:object-index-source", "text:table-index-source", "text:index-title-template",
    "table:table-columns", "table:table-column", "table:table-header-columns", "table:table-column-group",
    "table:calculation-settings", "table:content-validations", "table:label-ranges", "table:named-expressions",
    "table:database-ranges", "table:data-pilot-tables", "table:consolidation", "table:dde-links",
    "text:soft-page-break", "text:change", "text:change-start", "text:change-end", "office:event-listeners",
    "text:alphabetical-index-auto-mark-file", "text:section-source", "office:dde-source",
    "text:bookmark", "text:bookmark-start", "text:bookmark-end", "office:annotation-end")}

# block-level elements whose children are walked as blocks (XMLTextType::Section)
_INDEXES = {qn(x) for x in (
    "text:table-of-content", "text:illustration-index", "text:table-index", "text:object-index",
    "text:user-index", "text:alphabetical-index", "text:bibliography")}
_BLOCK_CONTAINERS = {qn(x) for x in ("text:section", "text:index-body", "text:index-title")}
# block elements that end the start of the body (m_bBodyContentStarted): page-anchored frames
# are only imported before them
_CONTENT = {qn(x) for x in (
    "text:p", "text:h", "loext:p", "text:numbered-paragraph", "text:list", "table:table", "loext:table",
    "text:section", "text:index-title", "text:change", "text:change-start", "text:change-end")} | _INDEXES
# text types (XMLTextType) in which tables are imported
_TABLE_TYPES = {"body", "textbox", "section", "header", "changed", "cell"}

# drawing shapes whose own text:p children are shown (ODF 10.3 / 10.6 shapes)
SHAPES = {qn("draw:" + x) for x in (
    "custom-shape", "rect", "ellipse", "polygon", "polyline", "path", "line", "connector", "circle",
    "regular-polygon", "caption", "measure")}
_P, _H, _LIST, _NP = qn("text:p"), qn("text:h"), qn("text:list"), qn("text:numbered-paragraph")
_SPAN, _A, _S, _TAB, _LB = qn("text:span"), qn("text:a"), qn("text:s"), qn("text:tab"), qn("text:line-break")
_NOTE, _ANNOT, _FRAME, _G, _DA = qn("text:note"), qn("office:annotation"), qn("draw:frame"), qn("draw:g"), qn("draw:a")
_TABLE = qn("table:table")
_LOP, _LOTABLE = qn("loext:p"), qn("loext:table")
_PAGE_NUMBER, _CHAPTER = qn("text:page-number"), qn("text:chapter")


class _Para:
    """Paragraph being collected: text parts, picture / note anchors, links, and the
    xmloff white-space state (a space after a space is dropped, ODF 6.1.2)."""
    __slots__ = ("parts", "n", "images", "notes", "links", "ign")

    def __init__(self):
        self.parts: list = []
        self.n = 0
        self.images: list = []
        self.notes: list = []
        self.links: list = []      # (start, end, url) hints of text:a, in element start order
        self.ign = True

    def chars(self, s: str) -> None:
        if not s:
            return
        s = _WS.sub(" ", s)
        if self.ign and s[0] == " ":
            s = s[1:]
        if not s:
            return
        self.ign = s[-1] == " "
        self.parts.append(s)
        self.n += len(s)

    def raw(self, s: str) -> None:
        if s:
            self.parts.append(s)
            self.n += len(s)
            self.ign = False

    def para(self) -> Paragraph:
        return Paragraph("".join(self.parts), images=self.images, notes=self.notes, links=self.resolve_links())

    def resolve_links(self) -> list:
        """Links from the text:a hints. LibreOffice stores a hyperlink as a character attribute:
        hints are applied in the order their elements start, so a link nested in another one
        wins inside its range, and the export writes one text:a per run of equal links. An
        empty text:a survives only in an empty paragraph (the last one of them)."""
        hints = [h for h in self.links if h is not None]
        if not hints:
            return []
        if not self.n:
            # zero-length attributes at one position replace each other: the last one stays
            return [Link(0, 0, hints[-1][2])]
        paint = [None] * self.n
        for start, end, url in hints:
            for i in range(max(0, start), min(end, self.n)):
                paint[i] = url
        out = []
        i = 0
        while i < self.n:
            url = paint[i]
            j = i + 1
            while j < self.n and paint[j] == url:
                j += 1
            if url is not None:
                out.append(Link(i, j, url))
            i = j
        return out


def flatten(blocks, skip=None, out=None, depth: int = 0) -> list:
    """Plain paragraph strings of blocks (table cells row by row, nested tables
    recursively); markers removed; paragraphs whose id() is in `skip`
    (Walker.synthetic) omitted."""
    out = [] if out is None else out
    for b in blocks:
        if isinstance(b, Table):
            if depth > 64:
                continue
            for r in b.rows:
                for c in r.cells:
                    flatten(c.blocks, skip, out, depth + 1)
        else:
            if skip and id(b) in skip:
                continue
            out.append(b.text.replace(IMAGE_MARK, "").replace(NOTE_MARK, ""))
    return out


class Walker:
    """Turns ODF text content into msdoc blocks (Paragraph / Table).

    ``blocks(container, story)`` walks the block-level children of any element
    (office:text, table:table-cell, draw:text-box, text:note-body, header...),
    ``paragraph(el, story)`` one text:p / text:h, ``table(el, story)`` one
    table:table, ``drawing(el, story, text_story)`` a frame or shape.

    Side outputs: ``notes`` {'footnotes'|'endnotes': [blocks per note]},
    ``comments`` (blocks of office:annotation), ``textboxes`` (blocks of frames
    and shapes met inside paragraphs or at block level), ``synthetic`` (ids
    of picture-only paragraphs made for frames that sit between paragraphs:
    they belong to blocks but not to text stories), ``warnings``. Pictures are
    appended to ``doc.images`` (Image.story = story of the anchor)."""

    def __init__(self, pkg: Optional[Package], styles: StyleSheet, doc: Document, images_enabled: bool = True):
        self.pkg = pkg
        self.styles = styles
        self.doc = doc
        self.images_enabled = images_enabled
        self.part = "content"
        self.notes = {"footnotes": [], "endnotes": []}
        self.comments: list = []
        self.textboxes: list = []
        self.synthetic: set = set()
        self.warnings: list = []
        self.lists = ListTracker()
        self.work = 0
        self._work_warned = False
        self._img_cache: dict = {}
        self.textbox_story = "textboxes"
        self.in_frame = 0
        self.text_type = "body"
        """XMLTextType of the content being walked: body | section | header | cell | textbox |
        footnote | shape | changed. Decides which block elements are imported (see block)."""
        self.body_start = True
        self._page_field_seen = False

    # ---- budget
    def _spend(self, n: int = 1) -> bool:
        """Count produced paragraphs/cells; False once MAX_WORK is exceeded."""
        self.work += n
        if self.work > MAX_WORK:
            if not self._work_warned:
                self._work_warned = True
                self.warnings.append("content truncated after %d paragraphs/cells" % MAX_WORK)
            return False
        return True

    # ---- block level
    def blocks(self, container, story: str, out: Optional[list] = None, lst=None) -> list:
        """Blocks of the block-level children of `container`. `lst` is the list context
        (list id, level, style name) when walking inside a text:list."""
        out = [] if out is None else out
        for el in container:
            self.block(el, story, out, lst)
        return out

    def block(self, el, story: str, out: list, lst=None) -> None:
        """One block-level element, accepted as XMLTextImportHelper::CreateTextChildContext does
        for the current `text_type`: tables not in notes / shape text, frames and shapes only
        at the start of the body (before its first paragraph, i.e. page-anchored), in a text
        box or in a deleted region."""
        tag = el.tag
        if not isinstance(tag, str):
            return
        tt = self.text_type
        start = self.body_start
        if tt == "body" and tag in _CONTENT:
            self.body_start = False
        if tag in _BLOCK_SKIP:
            return
        if tag == _P or tag == _H or tag == _LOP:
            if self._spend():
                out.append(self.paragraph(el, story, lst))
            return
        if tag == _LIST:
            self.list(el, story, out, lst)
            return
        if tag == _NP:
            self.numbered_paragraph(el, story, out)
            return
        if tag == _TABLE or tag == _LOTABLE:
            if tt in _TABLE_TYPES:
                out.append(self.within("cell", self.table, el, story))
            return
        if tag == _FRAME or tag == _G or tag == _DA or tag in SHAPES:
            # frames anchored to the page / to a text frame
            if not (tt in ("textbox", "changed") or (tt == "body" and start)):
                return
            blocks, imgs = self.drawing(el, story, self.textbox_story)
            self.textboxes.extend(blocks)
            if imgs:
                p = Paragraph(IMAGE_MARK * len(imgs), images=imgs)
                self.synthetic.add(id(p))
                out.append(p)
            return
        if tag in _INDEXES:
            if tt != "shape":
                self.within("section", self.blocks, el, story, out, lst)
            return
        if tag in _BLOCK_CONTAINERS:
            # text:section, text:index-body / text:index-title (XMLTextType::Section)
            self.within("section", self.blocks, el, story, out, lst)
        # anything else is not imported by LibreOffice

    def within(self, text_type: str, fn, *args):
        """Call fn(*args) with `text_type` set (restored afterwards)."""
        old = self.text_type
        self.text_type = text_type
        try:
            return fn(*args)
        finally:
            self.text_type = old

    def list(self, el, story: str, out: list, lst=None) -> None:
        """text:list: items get list_level = nesting depth (0-based), the first paragraph
        of each item is counted and labelled from the list style."""
        style = attr(el, "text:style-name")
        if lst is None:
            lid = self.lists.open(el, style, None)
            level = 1
            if not style:
                style = None
        else:
            plid, plevel, pstyle = lst
            lid = self.lists.open(el, style or pstyle, plid)
            level = plevel + 1
            style = style or pstyle
        for item in el:
            if item.tag == qn("text:list-item"):
                start = attr(item, "text:start-value")
                try:
                    start = max(0, min(1 << 30, int(start))) if start is not None else None
                except ValueError:
                    start = None
                override = attr(item, "text:style-override")
                self._item(item, story, out, (lid, level, override or style), True, start)
            elif item.tag == qn("text:list-header"):
                self._item(item, story, out, (lid, level, style), False, None)

    def _item(self, item, story, out, ctx, counted, start) -> None:
        lid, level, style = ctx
        first = counted
        for ch in item:
            tag = ch.tag
            if tag == _P or tag == _H:
                if not self._spend():
                    continue
                p = self.paragraph(ch, story, ctx)
                ls = self._list_style(style, ch)
                if tag == _H and not p.heading:
                    p.heading = 1
                if first:
                    label = self.lists.count(lid, level, ls, start)
                    p.list_number = label
                    first = False
                out.append(p)
            elif tag == _LIST:
                self.list(ch, story, out, ctx)
                first = False
            else:
                self.block(ch, story, out, ctx)

    def _list_style(self, style_name: Optional[str], p_el=None) -> Optional[ListStyle]:
        ls = self.styles.list_style(style_name, self.part)
        if ls is None and p_el is not None:
            name = self.styles.paragraph_list_style(attr(p_el, "text:style-name"), self.part)
            ls = self.styles.list_style(name, self.part)
        return ls

    def numbered_paragraph(self, el, story: str, out: list) -> None:
        """text:numbered-paragraph (ODF 1.2 5.3.6): one list item outside text:list."""
        level = int_attr(el, "text:level", 1, 1, MAX_LIST_LEVEL)
        style = attr(el, "text:style-name")
        key = attr(el, "text:list-id") or ""
        lid = self.lists.by_xmlid.get("\0np:" + key)
        if lid is None:
            lid = self.lists.new_id()
            self.lists.by_xmlid["\0np:" + key] = lid
        start = attr(el, "text:start-value")
        try:
            start = int(start) if start is not None else None
        except ValueError:
            start = None
        for ch in el:
            if ch.tag in (_P, _H) and self._spend():
                p = self.paragraph(ch, story, (lid, level, style))
                p.list_number = self.lists.count(lid, level, self._list_style(style, ch), start)
                out.append(p)
                break

    # ---- paragraphs
    def paragraph(self, el, story: str, lst=None) -> Paragraph:
        """One text:p / text:h -> Paragraph (heading level from text:outline-level of text:h;
        list_level / list_ordered from the list context `lst`)."""
        ps = _Para()
        self.inline(el, ps, story)
        p = ps.para()
        if el.tag == _H:
            # txtparai.cxx XMLParaContext: text:h is level 1 unless text:outline-level > 0;
            # a text:p is never a heading (txtimp.cxx resets the style's outline level to 0)
            lvl = int_attr(el, "text:outline-level", 1, 1, 127)
            p.heading = max(1, min(9, lvl))
        if lst is not None and not p.heading:
            lid, level, style = lst
            ls = self._list_style(style, el)
            p.list_level = level - 1
            p.list_ordered = bool(ls is not None and ls.level(level).ordered)
        return p

    def inline(self, el, ps: _Para, story: str) -> None:
        """Collect the text of `el`'s content into `ps` (ODF 6.1 white space rules)."""
        if el.text:
            ps.chars(el.text)
        for ch in el:
            tag = ch.tag
            if not isinstance(tag, str):
                pass
            elif tag == _SPAN:
                self.inline(ch, ps, story)
            elif tag == _S:
                ps.raw(" " * int_attr(ch, "text:c", 1, 1, MAX_SPACES))
            elif tag == _TAB:
                ps.raw("\t")
            elif tag == _LB:
                ps.raw("\n")
            elif tag == _A:
                start = ps.n
                href = attr(ch, "xlink:href")
                k = len(ps.links)
                if href and k < 10000:
                    ps.links.append(None)   # hint order = element start order
                self.inline(ch, ps, story)
                if href and k < 10000:
                    ps.links[k] = (start, ps.n, href)
            elif tag == _NOTE:
                self.note(ch, ps)
                ps.ign = False
            elif tag == _ANNOT:
                self.annotation(ch)
                ps.ign = False
            elif tag == _FRAME or tag == _G or tag == _DA or tag in SHAPES:
                blocks, imgs = self.drawing(ch, story, self.textbox_story)
                self.textboxes.extend(blocks)
                for i in imgs:
                    ps.raw(IMAGE_MARK)
                    ps.images.append(i)
                if tag != _DA:
                    ps.ign = False
            elif tag in _INLINE_SKIP:
                pass
            elif tag in _PASS_THROUGH:
                # text:ruby (base and ruby text), text:meta, text:meta-field, loext:content-control
                self.inline(ch, ps, story)
            elif tag in FIELDS:
                ps.raw(self.field_text(ch))
                ps.ign = False
            else:
                # unknown content (also a text:p nested in a paragraph) is not imported, but
                # "behind fields, shapes and any unknown content blanks aren't ignored"
                ps.ign = False
            if ch.tail:
                ps.chars(ch.tail)

    def field_text(self, el) -> str:
        """Text LibreOffice shows for a field element: its cached presentation (the character
        data directly inside the element, verbatim), except for fields Writer re-expands from
        layout state it does not have before the document is laid out: text:page-number
        (SwPageNumberField: page 0 plus text:page-adjust; '' for next/previous page) and
        text:chapter (SwChapterField: empty until the layout sets it)."""
        tag = el.tag
        if tag == _PAGE_NUMBER:
            if self.in_frame or (self.text_type in ("body", "section", "cell") and not self._page_field_seen):
                # the first page and text frames are laid out before the document is saved:
                # the first page-number field of the body and those in frames keep a value
                if not self.in_frame:
                    self._page_field_seen = True
                return (el.text or "") + "".join(c.tail or "" for c in el)
            sel = attr(el, "text:select-page", "current")
            n = int_attr(el, "text:page-adjust", 0, -(1 << 15), 1 << 15)
            if sel != "current" or n < 0 or n > 0:
                return ""
            return "0" if attr(el, "style:num-format", "1") == "1" else ""
        if tag == _CHAPTER:
            return ""
        return (el.text or "") + "".join(c.tail or "" for c in el)

    def note(self, el, ps: _Para) -> None:
        """text:note: body to notes[footnotes|endnotes], NOTE_MARK anchor in the paragraph.
        Inside a text frame or shape the note is dropped (txtparai.cxx: no footnotes in frames)."""
        if self.in_frame:
            return
        cls = attr(el, "text:note-class", "footnote")
        kind = "endnotes" if cls == "endnote" else "footnotes"
        body = el.find(qn("text:note-body"))
        blocks = self.within("footnote", self.blocks, body, kind) if body is not None else []
        self.notes[kind].append(blocks)
        ps.raw(NOTE_MARK)
        ps.notes.append(("endnote" if kind == "endnotes" else "footnote", len(self.notes[kind])))

    def annotation(self, el) -> None:
        """office:annotation: its text:p / text:h / text:list go to `comments`
        (dc:creator, dc:date and meta:date-string are not text)."""
        for ch in el:
            if ch.tag in (_P, _H, _LIST, _TABLE):
                self.within("shape", self.block, ch, "comments", self.comments)

    # ---- tables
    def table(self, el, story: str) -> Table:
        """table:table -> Table. Header rows (table:table-header-rows) get Row.header;
        number-columns/rows-spanned -> colspan/rowspan; covered cells are omitted;
        repeated rows/cells are walked once per repetition (capped)."""
        t = Table()
        for row_el, header, nrep in iter_rows(el):
            for _ in range(nrep):
                if not self._spend():
                    break
                row = Row(header=header)
                for cell_el, covered, ncol in iter_cells(row_el):
                    if covered:
                        continue
                    for _ in range(ncol):
                        if not self._spend():
                            break
                        c = Cell(colspan=int_attr(cell_el, "table:number-columns-spanned", 1, 1, 1 << 14),
                                 rowspan=int_attr(cell_el, "table:number-rows-spanned", 1, 1, 1 << 20))
                        self.blocks(cell_el, story, c.blocks)
                        row.cells.append(c)
                t.rows.append(row)
        return t

    # ---- drawings
    def drawing(self, el, story: str, text_story: str = "textboxes", depth: int = 0):
        """(blocks, picture indexes) of a draw:frame / draw:g / draw:a / text shape.

        A frame holding draw:object or draw:object-ole (embedded chart, formula,
        OLE) gives nothing: neither the object nor its replacement picture.
        draw:image children: the best alternative only (xmloff
        MultiImageImportHelper). draw:text-box and shape text are walked into
        `text_story` blocks. Pictures are 'inline' for text:anchor-type="as-char",
        else 'floating'."""
        tag = el.tag
        blocks: list = []
        imgs: list = []
        if depth > 32:
            return blocks, imgs
        if tag == _G or tag == _DA:
            for ch in el:
                if ch.tag == _FRAME or ch.tag == _G or ch.tag == _DA or ch.tag in SHAPES:
                    b, i = self.drawing(ch, story, text_story, depth + 1)
                    blocks.extend(b)
                    imgs.extend(i)
            return blocks, imgs
        if tag == _FRAME:
            if el.find(qn("draw:object")) is not None or el.find(qn("draw:object-ole")) is not None:
                return blocks, imgs
            kind = "inline" if attr(el, "text:anchor-type") == "as-char" else "floating"
            best = best_image(children(el, "draw:image"), self.pkg)
            if best is not None:
                i = self.add_image(best, story, kind, attr(el, "draw:name"))
                if i is not None:
                    imgs.append(i)
            for ch in el:
                if ch.tag == qn("draw:text-box"):
                    self.in_frame += 1
                    try:
                        self.within("textbox", self.blocks, ch, text_story, blocks)
                    finally:
                        self.in_frame -= 1
            return blocks, imgs
        if tag in SHAPES:
            self.in_frame += 1
            try:
                for ch in el:
                    if ch.tag in (_P, _H, _LIST):
                        self.within("shape", self.block, ch, text_story, blocks)
            finally:
                self.in_frame -= 1
        return blocks, imgs

    def add_image(self, image_el, story: str, kind: str, name: Optional[str] = None) -> Optional[int]:
        """Append the picture of a draw:image to doc.images; its index, or None."""
        if not self.images_enabled or self.pkg is None:
            return None
        if len(self.doc.images) >= 100000:
            return None
        got = image_bytes(self.pkg, image_el, self.warnings)
        if got is None:
            return None
        data, mime = got
        self.doc.images.append(Image(data=data, mime=mime, kind=kind, story=story, name=name))
        return len(self.doc.images) - 1
