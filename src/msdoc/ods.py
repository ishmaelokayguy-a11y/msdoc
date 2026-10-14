# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice sc/source/filter/xml/ (xmltabi.cxx,
# xmlrowi.cxx, xmlcelli.cxx) and xmloff/source/style/xmlnumfi.cxx (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""OpenDocument spreadsheet (.ods / .ots) -> Document(format="ods").

Every top-level table:table of office:spreadsheet becomes a ``Sheet`` (as xls.py fills them):
``cells`` {(row, col): displayed text}, ``merges`` (row, col, rows, cols), ``links``
{(row, col): url}, ``images`` (row, col, index into Document.images), ``hidden``.

Displayed text (what LibreOffice shows): the text:p paragraphs of a table:table-cell joined
with "\\n" -- ODF producers store the formatted display string there (ODF 1.2 part 1, 9.1.4).
When a cell has no text:p but a value (office:value-type), the value is formatted with the
cell's data style (number:*-style, converted to a format code as xmloff xmlnumfi.cxx does and
rendered by msdoc.numfmt), see ``format_value``.

Repeats (number-rows/columns-repeated) advance the position by their full count, but only
non-empty rows/cells are expanded, at most MAX_EXPAND times each, and the workbook keeps at
most MAX_CELLS cells: a sheet declaring 1e9 empty rows costs nothing, trailing empty repeats
vanish. Cell annotations (office:annotation) are not cell text (LibreOffice's text of a
cell); their paragraphs go to the "comments" story. Pictures: draw:frame/draw:image anchored
in cells or in table:shapes, one entry per distinct picture (package path); embedded objects
(charts, OLE) and their replacement previews are not reported (as odt.py).
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from . import numfmt, odf
from .errors import EncryptedDocumentError, MsDocError
from .images import blip_looks_valid
from .model import Document, Image, Sheet

MAX_SHEETS = 10000
MAX_CELLS = 2_000_000       # non-empty cells kept for one workbook (the oracle's golden stops at 1e6)
MAX_EXPAND = 4096           # expansions of one repeated non-empty cell / row
MAX_ROW = 1 << 20           # LibreOffice sheet limits (ScSheetLimits: 1048576 rows, 16384 columns)
MAX_COL = 1 << 14
MAX_IMAGES = 10000
MAX_MERGES = 200000
MAX_LINKS = 200000

_CELL, _COVERED, _ROW = odf.qn("table:table-cell"), odf.qn("table:covered-table-cell"), odf.qn("table:table-row")
_ROW_GROUPS = {odf.qn(x) for x in ("table:table-header-rows", "table:table-rows", "table:table-row-group")}
_TABLE, _SHAPES = odf.qn("table:table"), odf.qn("table:shapes")
_P, _ANNOT, _A = odf.qn("text:p"), odf.qn("office:annotation"), odf.qn("text:a")
_S, _TAB, _LB, _SPAN = odf.qn("text:s"), odf.qn("text:tab"), odf.qn("text:line-break"), odf.qn("text:span")
_RUBY, _RUBY_BASE, _SHEET_NAME = odf.qn("text:ruby"), odf.qn("text:ruby-base"), odf.qn("text:sheet-name")
_CACHED_FIELDS = {odf.qn("text:date"), odf.qn("text:title")}
_FRAME = odf.qn("draw:frame")
_DRAW = {odf.qn("draw:frame"), odf.qn("draw:g"), odf.qn("draw:a")} | odf.SHAPES

# URL schemes INetURLObject::CompareProtocolScheme knows (the common ones)
_SCHEMES = ("file", "http", "https", "ftp", "smb", "sftp", "webdav", "vnd.sun.star.", "private", "mailto",
            "data", "slot", "macro", "javascript", "ldap", "cmis", "vnd.libreoffice.")


def is_external_cache(name: str) -> bool:
    """Name of an external-reference cache table ('file:///path/x.ods'#Sheet1): LibreOffice
    keeps it as a cache, not as a sheet (sc xmltabi.cxx lcl_isExternalRefCache)."""
    if not name or name[0] != "'":
        return False
    low = name[1:].lower()
    scheme = next((s for s in _SCHEMES if low.startswith(s if s.endswith(".") else s + ":")), None)
    if scheme is None:
        return False
    k = name.find("'#", 1 + len(scheme))
    return k > 0 and k + 2 < len(name)


def is_package_url(url: str) -> bool:
    """xmloff SvXMLImport::IsPackageURL: a relative path inside the package (no scheme, not
    absolute, not going up)."""
    if not url:
        return False
    if url[0] == "/":
        return False
    if len(url) > 1 and url[0] == ".":
        if url[1] == ".":
            return False
        if url[1] == "/":
            return True
    for ch in url[1:]:
        if ch == "/":
            return True
        if ch == ":":
            return False
    return True


_BAD_NAME_CHARS = set(":\\/?*[]")


def sheet_name(name: str, used: set, index: int) -> str:
    """The name LibreOffice gives a sheet on load (ScDocument::AppendTabOnLoad /
    CreateValidTabName): an invalid name (empty, leading or trailing apostrophe, one of
    : \\ / ? * [ ]) becomes "Sheet<n>" (n from the sheet's position), a name already used
    (ignoring case) gets "_2", "_3", ... appended. `used` holds the casefolded names so far."""
    if not name or name[0] == "'" or name[-1] == "'" or any(ch in _BAD_NAME_CHARS for ch in name):
        n = index + 1
        while "sheet%d" % n in used:
            n += 1
        return "Sheet%d" % n
    if name.casefold() not in used:
        return name
    k = 2
    while ("%s_%d" % (name, k)).casefold() in used and k < 100000:
        k += 1
    return "%s_%d" % (name, k)


class _Walker(odf.Walker):
    """odf.Walker for the drawings of a sheet (cell text is read by _Book.para): one picture
    entry per package image (LibreOffice's export keeps one Pictures/ file per graphic), and
    the replacement picture of an OLE object linked to an outside file."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._by_href: dict = {}

    def drawing(self, el, story, text_story="textboxes", depth=0):
        if el.tag == _FRAME and depth <= 32:
            obj = el.find(odf.qn("draw:object"))
            if obj is None:
                obj = el.find(odf.qn("draw:object-ole"))
            if obj is not None:
                href = odf.attr(obj, "xlink:href")
                if href and not is_package_url(href):
                    # an OLE object linked to an outside file is a link object LibreOffice cannot
                    # load: it keeps showing (and saves) the replacement picture
                    # (xmloff ximpshap.cxx SdXMLObjectShapeContext: LinkURL for non-package URLs)
                    best = odf.best_image(odf.children(el, "draw:image"), self.pkg)
                    i = self.replacement_image(best, story) if best is not None else None
                    return [], ([i] if i is not None else [])
                return [], []
        return super().drawing(el, story, text_story, depth)

    def replacement_image(self, image_el, story):
        """A replacement picture (often ObjectReplacements/...) as a floating picture."""
        if not self.images_enabled or self.pkg is None:
            return None
        path = odf.package_path(odf.attr(image_el, "xlink:href"))
        if path is None or not self.pkg.has(path):
            return None
        if path in self._by_href:
            return self._by_href[path]
        if len(self.doc.images) >= MAX_IMAGES:
            return None
        try:
            data = self.pkg.read(path)
        except MsDocError as e:
            if isinstance(e, EncryptedDocumentError):
                raise
            self.warnings.append("picture %r skipped: %s" % (path, e))
            return None
        mime = odf.sniff_image_mime(data) if data else None
        if mime is None or (mime in ("image/png", "image/jpeg", "image/gif") and not blip_looks_valid(data, mime)):
            return None
        self.doc.images.append(Image(data=data, mime=mime, kind="floating", story=story,
                                     name=odf.attr(image_el, "draw:name")))
        self._by_href[path] = len(self.doc.images) - 1
        return self._by_href[path]

    def add_image(self, image_el, story, kind, name=None):
        href = odf.attr(image_el, "xlink:href")
        key = odf.package_path(href) if href else None
        if key is not None and key in self._by_href:
            return self._by_href[key]
        if len(self.doc.images) >= MAX_IMAGES:
            return None
        i = super().add_image(image_el, story, kind, name)
        if key is not None and i is not None:
            self._by_href[key] = i
        return i


def _rows(table_el, depth: int = 0):
    """table:table-row elements of a table, also inside header-rows / rows / row-group
    (nested groups, depth-bounded), in document order."""
    for el in table_el:
        if el.tag == _ROW:
            yield el
        elif el.tag in _ROW_GROUPS and depth < 32:
            yield from _rows(el, depth + 1)


def _count(el, name: str, hi: int) -> int:
    return odf.int_attr(el, name, 1, 1, hi)


class _Book:
    def __init__(self, pkg, styles: odf.StyleSheet, doc: Document, images: bool):
        self.styles = styles
        self.doc = doc
        self.w = _Walker(pkg, styles, doc, images_enabled=images)
        self.ncells = 0
        self.full = False
        self.cell_style_cache: dict = {}
        self.fmt_cache: dict = {}

    # ---- sheet
    def sheet(self, table_el, name: str) -> Sheet:
        disp = self.styles.lookup("table", odf.attr(table_el, "table:style-name"), "table-properties", "table:display")
        sh = Sheet(name=name, hidden=(disp == "false"))
        cols = odf.column_default_styles(table_el, MAX_COL)
        r = 0
        for row_el in _rows(table_el):
            if r >= MAX_ROW:
                break
            rrep = _count(row_el, "table:number-rows-repeated", MAX_ROW)
            row_style = odf.attr(row_el, "table:default-cell-style-name")
            cells = self.row(row_el, r, sh, cols, row_style)
            if cells:
                n = min(rrep, MAX_EXPAND, MAX_ROW - r)
                for k in range(n):
                    if not self._take(len(cells)):
                        break
                    for c, text in cells:
                        sh.cells[(r + k, c)] = text
            r += rrep
        for ch in table_el:
            if ch.tag == _SHAPES:
                for shape in ch:
                    self.drawing(shape, sh, 0, 0)
        return sh

    def _take(self, n: int) -> bool:
        if self.ncells + n > MAX_CELLS:
            if not self.full:
                self.full = True
                self.doc.warnings.append("workbook truncated after %d cells" % MAX_CELLS)
            return False
        self.ncells += n
        return True

    def row(self, row_el, r: int, sh: Sheet, cols: list, row_style) -> list:
        """[(col, text)] of the non-empty cells of one row (repeats expanded); merges, links,
        pictures and comments recorded once at the first position."""
        out = []
        c = 0
        for el in row_el:
            tag = el.tag
            if tag != _CELL and tag != _COVERED:
                continue
            if c >= MAX_COL:
                break
            crep = _count(el, "table:number-columns-repeated", MAX_COL)
            if len(el) or odf.attr(el, "office:value-type") is not None:
                if tag == _CELL:
                    style = odf.attr(el, "table:style-name") or row_style or (cols[c] if c < len(cols) else None)
                    text, url = self.cell(el, style, sh.name)
                    if text:
                        for k in range(min(crep, MAX_EXPAND, MAX_COL - c)):
                            out.append((c + k, text))
                        if url and len(sh.links) < MAX_LINKS:
                            sh.links.setdefault((r, c), url)
                    rs = odf.int_attr(el, "table:number-rows-spanned", 1, 1, MAX_ROW)
                    cs = odf.int_attr(el, "table:number-columns-spanned", 1, 1, MAX_COL)
                    if (rs > 1 or cs > 1) and len(sh.merges) < MAX_MERGES:
                        sh.merges.append((r, c, rs, cs))
                for ch in el:
                    if ch.tag in _DRAW:
                        self.drawing(ch, sh, r, c)
                    elif ch.tag == _ANNOT:
                        self.w.annotation(ch)
            elif tag == _CELL:
                rs = odf.int_attr(el, "table:number-rows-spanned", 1, 1, MAX_ROW)
                cs = odf.int_attr(el, "table:number-columns-spanned", 1, 1, MAX_COL)
                if (rs > 1 or cs > 1) and len(sh.merges) < MAX_MERGES:
                    sh.merges.append((r, c, rs, cs))
            c += crep
        return out

    def cell(self, el, style: Optional[str], sheet: str = ""):
        """(displayed text, first hyperlink URL) of a table:table-cell.

        LibreOffice re-formats every non-string value with the cell's number format on load
        (sc xmlcelli.cxx PutValueCell: the text:p of a value cell is not used), so a value
        cell shows format(office:value); see _choose for when the stored text:p is kept."""
        paras = [ch for ch in el if ch.tag == _P]
        text = None
        url = None
        if paras:
            texts = []
            links: list = []
            for p in paras:
                if not self.w._spend():
                    break
                texts.append(self.para(p, sheet, links))
            url = links[0] if links else None
            text = "\n".join(texts)
        vt = odf.attr(el, "office:value-type")
        if vt is None or vt == "string" or vt not in _VALUE_TYPES:
            if text is None and vt == "string":
                # no text:p: the string value (ScXMLTableRowCellContext maStringValue)
                text = odf.attr(el, "office:string-value") or ""
            return text or "", url
        if text and text.strip() and odf.attr(el, "table:formula") is not None:
            # sc xmlcelli.cxx: an error result (calcext:value-type="error", or for older files a
            # 0 value shown as "#..." / "Err:...") is recalculated to the same error text, and a
            # formula cell without its own style gets the number format of its result
            # (SetNeedNumberFormat): the producer's text is the best guess for both
            cvt = odf.attr(el, "calcext:value-type")
            err = en_error(text)
            if cvt == "error":
                return err or text, url
            if err and (odf.attr(el, "office:value") or "0").strip() in ("0", "0.0", "-0"):
                return err, url
            if odf.attr(el, "table:style-name") is None:
                try:
                    shown = self.format_value(el, vt, style)
                except (ValueError, OverflowError, ArithmeticError, IndexError, KeyError):
                    shown = None
                return _unstyled_formula(text, shown, vt), url
            if cvt is None and text.lstrip().startswith(("#", "Err:")):
                return text, url
        if paras and vt != "boolean" and not text.strip() \
                and (odf.attr(el, "office:value") or "").strip() in ("0", "0.0", "-0"):
            # a 0 result shown as an empty paragraph is an empty string result of a formula or
            # of a matrix formula covering the cell (xmlcelli.cxx HasSpecialCaseFormulaText:
            # mbPossibleEmptyDisplay)
            return "", url
        if text and (odf.attr(el, "office:value") or "0").strip() in ("0", "0.0", "-0") and en_error(text):
            # a 0 value displayed as an error: a formula / matrix result LibreOffice recalculates
            return en_error(text), url
        try:
            fmt = self.format_value(el, vt, style)
        except (ValueError, OverflowError, ArithmeticError, IndexError, KeyError):
            fmt = None
        return _choose(text, fmt, vt), url

    # ---- cell paragraphs (sc celltextparacontext.cxx)
    def para(self, p, sheet: str, links: list) -> str:
        """Text of one text:p of a cell as Calc's edit engine imports it: characters, text:s,
        text:tab, text:line-break, text:span, text:a (its own characters), text:ruby (the base
        only), and the fields text:sheet-name / text:date / text:title; the content of any
        other element is lost (ScXMLCellTextParaContext::createFastChildContext)."""
        out: list = []
        if p.text:
            out.append(p.text)
        for ch in p:
            tag = ch.tag
            if tag == _TAB:
                out.append("\t")
            elif tag == _LB:
                out.append("\n")
            elif tag == _RUBY:
                for rb in ch:
                    if rb.tag == _RUBY_BASE:
                        self._span(rb, sheet, links, out, base=True)
            else:
                self._inline(ch, sheet, links, out)
            if ch.tail:
                out.append(ch.tail)
        return "".join(out)

    def _span(self, el, sheet: str, links: list, out: list, base: bool = False) -> None:
        """text:span (ScXMLCellTextSpanContext): characters, text:s, the fields and text:a;
        in a ruby base (ScXMLCellRubyBaseContext) only nested spans."""
        if el.text:
            out.append(el.text)
        for ch in el:
            if base:
                if ch.tag == _SPAN:
                    self._span(ch, sheet, links, out)
            elif ch.tag != _SPAN:
                self._inline(ch, sheet, links, out)
            if ch.tail:
                out.append(ch.tail)

    def _inline(self, ch, sheet: str, links: list, out: list) -> None:
        tag = ch.tag
        if tag == _S:
            out.append(" " * odf.int_attr(ch, "text:c", 1, 1, odf.MAX_SPACES))
        elif tag == _SPAN:
            self._span(ch, sheet, links, out)
        elif tag == _A:
            # ScXMLCellFieldURLContext: the representation is the element's own characters
            rep = (ch.text or "") + "".join(c.tail or "" for c in ch)
            href = odf.attr(ch, "xlink:href") or ""
            if href and len(links) < 1000:
                links.append(href)
            out.append(rep or href)
        elif tag == _SHEET_NAME:
            out.append(sheet)
        elif tag in _CACHED_FIELDS:
            # text:date (today's date) / text:title: recomputed by Calc; the stored text is
            # the best stand-in
            out.append("".join(ch.itertext()))

    # ---- values
    def data_code(self, cell_style: Optional[str]) -> "_Fmt":
        """Format of the data style of a cell style (cached per cell style)."""
        f = self.cell_style_cache.get(cell_style)
        if f is None:
            name = self.styles.style_attr("table-cell", cell_style, "style:data-style-name")
            el = self.styles.data_style(name) if name else None
            if el is not None:
                f = _Fmt(el, self.styles)
            else:
                # no data style: a date / time / percentage / currency / boolean value gets the
                # standard format of its type in the locale of the cell's General format
                # (ScXMLImport::SetType), which follows the cell's language (text properties
                # down to the default cell style); see _choose
                f = _Fmt()
                lang = self.styles.lookup("table-cell", cell_style, "text-properties", "fo:language")
                f.foreign = bool(lang) and lang.lower() not in ("en", "zxx", "none")
            if len(self.cell_style_cache) < 100000:
                self.cell_style_cache[cell_style] = f
        return f

    def format_value(self, el, vt: str, style: Optional[str]) -> Optional["_Shown"]:
        """The value of a non-string cell formatted with its data style; None when the cell
        has no usable value. A General format on a date / time / percentage / currency /
        boolean cell is replaced by the standard format of that type (sc xmlimprt.cxx
        ScXMLImport::SetType; en-US standard formats)."""
        f = self.data_code(style)
        code = None if f.general and f.kind in (None, "number-style", "text-style") else f.code
        if vt == "boolean":
            v = _bool_value(odf.attr(el, "office:boolean-value") or "")
            if code is None or f.kind == "boolean-style":
                return _Shown(_boolean_text(f.el if f.kind == "boolean-style" else None, v), v, f, vt,
                              standard=code is None)
            return _Shown(numfmt.format_number(v, 164, code), v, f, vt)
        if vt == "date":
            v = date_serial(odf.attr(el, "office:date-value") or "")
            if v is None:
                return None
            std = code is None
            code = code or "MM/DD/YY"
        elif vt == "time":
            v = duration_days(odf.attr(el, "office:time-value") or "")
            if v is None:
                return None
            std = code is None
            code = code or "HH:MM:SS AM/PM"
        else:
            raw = odf.attr(el, "office:value")
            if raw is None:
                return None
            v = float(raw.strip())
            if v != v or v in (float("inf"), float("-inf")):
                return None
            std = code is None
            if code is None:
                if vt == "percentage":
                    code = "0%"
                elif vt == "currency":
                    cur = odf.attr(el, "office:currency") or ""
                    sym = _CURRENCY.get(cur, cur + " " if cur else "$")
                    code = '"%s"#,##0.00;-"%s"#,##0.00' % (sym, sym)
                else:
                    return _Shown(numfmt.general(v), v, f, vt, general=True)
        return _Shown(numfmt.format_number(v, 164, code), v, f, vt, general=(code == f.code and f.general),
                      standard=std)

    # ---- pictures
    def drawing(self, el, sh: Sheet, r: int, c: int) -> None:
        if el.tag not in _DRAW:
            return
        blocks, imgs = self.w.within("body", self.w.drawing, el, "main", "textboxes")
        self.w.textboxes.extend(blocks)
        for i in imgs:
            if (r, c, i) not in sh.images and len(sh.images) < MAX_IMAGES:
                sh.images.append((r, c, i))


# ---------------------------------------------------------------- value display
_VALUE_TYPES = {"float", "percentage", "currency", "date", "time", "boolean", "string"}
_CURRENCY = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
_DATE_PARTS = {"day", "month", "year", "hours", "minutes", "seconds", "day-of-week", "am-pm"}
_NUMBER_PARTS = {"number", "scientific-number", "fraction"}
# number format features msdoc.numfmt does not render: the producer's text:p is kept
_UNSUPPORTED_PARTS = {"fill-character", "embedded-text", "era", "quarter", "week-of-year"}
_UNSUPPORTED_ATTRS = ("number:transliteration-format", "number:transliteration-style", "number:calendar",
                      "number:decimal-replacement", "loext:blank-exponent-digits", "number:forced-exponent-sign",
                      "loext:forced-exponent-sign")


class _Fmt:
    """A data style: format code, kind, and what the renderer can trust about it."""
    __slots__ = ("el", "code", "kind", "has_date", "has_number", "foreign", "unsupported", "general")

    def __init__(self, el=None, styles: Optional[odf.StyleSheet] = None):
        self.el = el
        self.code = None
        self.kind = None
        self.has_date = self.has_number = self.foreign = self.unsupported = False
        self.general = True
        if el is None:
            return
        self.kind = odf.local(el)
        self.code = style_code(el, styles)
        self.general = self.code == "General"
        els = [el] + [t for t in (styles.data_style(odf.attr(m, "style:apply-style-name"))
                                  for m in el if m.tag == odf.qn("style:map")) if t is not None]
        for s in els:
            lang = odf.attr(s, "number:language")
            if lang and lang.lower() not in ("en", "zxx"):
                # the producer formatted with that locale (separators, names); numfmt is en-US only
                self.foreign = True
            for a in _UNSUPPORTED_ATTRS:
                if odf.attr(s, a) not in (None, "", "gregorian", "true"):
                    self.unsupported = True
            for ch in s:
                if odf.ns_of(ch) != odf.NS["number"]:
                    continue
                ln = odf.local(ch)
                if ln in _DATE_PARTS:
                    self.has_date = True
                    if odf.attr(ch, "number:calendar") not in (None, "", "gregorian"):
                        self.unsupported = True
                elif ln in _NUMBER_PARTS:
                    self.has_number = True
                    emb = [e for e in ch if e.tag == odf.qn("number:embedded-text")]
                    if any(odf.attr(e, "number:position") not in ("0", None) for e in emb) or len(ch) > len(emb) \
                            or any(odf.attr(ch, a) not in (None, "", "true") for a in _UNSUPPORTED_ATTRS):
                        self.unsupported = True   # number:embedded-text, decimal replacement...
                elif ln in _UNSUPPORTED_PARTS:
                    self.unsupported = True




def _bool_value(s: str) -> float:
    """office:boolean-value: true / false, or a number (non-zero = TRUE) as LibreOffice reads it."""
    s = s.strip().lower()
    if s in ("true", "false"):
        return 1.0 if s == "true" else 0.0
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return 1.0 if v == v and v != 0 else 0.0


def _boolean_text(el, v: float) -> str:
    """TRUE / FALSE (en-US) with the literal texts of a number:boolean-style around it."""
    word = "TRUE" if v else "FALSE"
    if el is None:
        return word
    out = []
    for ch in el:
        if odf.ns_of(ch) != odf.NS["number"]:
            continue
        if odf.local(ch) == "boolean":
            out.append(word)
        elif odf.local(ch) == "text":
            out.append("".join(ch.itertext()))
    return "".join(out) if word in out else word


class _Shown:
    __slots__ = ("text", "value", "fmt", "vt", "general", "standard")

    def __init__(self, text: str, value: float, fmt: _Fmt, vt: str, general: bool = False, standard: bool = False):
        self.text, self.value, self.fmt, self.vt, self.general = text, value, fmt, vt, general
        self.standard = standard


def en_error(text: str) -> Optional[str]:
    """`text` when it is one of the error values LibreOffice shows (en-US: 'Err:502', '#N/A',
    '#DIV/0!' ...), else None. Errors written in another UI language ('#WERT!') are kept as
    text by LibreOffice too."""
    t = text.strip()
    return t if _ERROR_TEXT.match(t) else None


_ERROR_TEXT = re.compile(r"^\s*(Err:\d{3}|#N/A|#DIV/0!|#VALUE!|#REF!|#NAME\?|#NUM!|#NULL!|#GETTING_DATA)\s*$")
_SEP = re.compile(r"[\s,.'   ]")
_PLAIN_NUM = re.compile(r"^\s*[-+−]?[\d\s,.'   ]*\d(?:[eE][-+]?\d+)?\s*%?\s*$")


def _parse_num(t: str):
    """(value, decimals, decimal separator) of a plain number written with '.' or ',' as decimal
    separator, or None. Returns every plausible reading."""
    s = t.strip().replace("−", "-").rstrip("%").strip()
    out = []
    for dec, grp in ((".", ",'    "), (",", ". '   ")):
        u = s
        for g in grp:
            u = u.replace(g, "")
        if dec == ",":
            if u.count(",") > 1:
                continue
            u = u.replace(",", ".")
        elif u.count(".") > 1:
            continue
        try:
            v = float(u)
        except ValueError:
            continue
        m = re.match(r"^[-+]?\d*\.(\d+)", u)
        out.append((v, len(m.group(1)) if m and "e" not in u.lower() else 0, dec))
    return out


def _close(t: float, v: float, decimals: int) -> bool:
    tol = max(abs(v) * 1e-9, 0.5 * 10 ** -decimals * 1.0000001) if decimals < 300 else abs(v) * 1e-9
    return abs(abs(t) - abs(v)) <= tol


def _en_number(text: str, value: float) -> Optional[str]:
    """`text` with en-US separators when it is a plain number (either decimal convention)
    that agrees with `value`; None otherwise."""
    if not _PLAIN_NUM.match(text) or "%" in text:
        return None
    for t, dec, sep in _parse_num(text):
        if _close(t, value, dec):
            if sep == ".":
                return text.strip().replace(",", "")
            return re.sub(r"[\s.'\u00a0\u202f\u2009]", "", text.strip()).replace(",", ".")
    return None


def _unstyled_formula(text: str, shown: Optional[_Shown], vt: str) -> str:
    """A formula cell without its own style shows its result with a format derived from the
    formula (sc ScFormulaCell::SetNeedNumberFormat): the stored text, with en-US separators
    for a plain number, or the type's standard format where the stored text is a bare serial
    number for a date / time result."""
    if shown is None:
        return text
    if vt in ("date", "time"):
        return shown.text if _PLAIN_NUM.match(text) and not _PLAIN_NUM.match(shown.text) else text
    if vt in ("float", "currency"):
        en = _en_number(text, shown.value)
        if en is not None:
            return en
    return text


def _choose(text: Optional[str], shown: Optional[_Shown], vt: str) -> str:
    """Displayed text of a value cell from the stored text:p (`text`) and our rendering of the value
    (`shown`). LibreOffice always shows its own rendering (en-US here); the stored text is kept
    where it is the same rendering and only our formatter cannot reproduce it (column-width
    rounding of General numbers, locale-specific styles, unsupported format features), and it is
    replaced where it contradicts the value (stale or empty text:p, another locale's separators
    or date order for a locale-neutral style). Documented in docs/lo_disagreements.md."""
    if shown is None:
        return text or ""
    f = shown.text
    if text is None or not text.strip():
        return f
    if text == f:
        return text
    fmt = shown.fmt
    if fmt.unsupported or (fmt.foreign and (fmt.el is not None or shown.standard)):
        return text
    if vt == "boolean":
        return f if (shown.standard or fmt.kind == "boolean-style") else text
    if vt in ("date", "time"):
        if sorted(re.findall(r"\d+", text)) == sorted(re.findall(r"\d+", f)):
            return f       # another locale's order / separators for the same date
        if _PLAIN_NUM.match(text) and not _PLAIN_NUM.match(f):
            return f       # a serial number where a date is shown
        return text
    scale = 100.0 if (vt == "percentage" or "%" in f) and "%" in text else 1.0
    if shown.general:
        # LibreOffice rounds General numbers to the column width: keep that rounding, with
        # en-US separators
        en = _en_number(text, shown.value)
        return en if en is not None else f
    if _SEP.sub("", text) == _SEP.sub("", f):
        return f
    if _PLAIN_NUM.match(text):
        reads = _parse_num(text)
        if reads and not any(_close(t / scale, shown.value, dec + (2 if scale == 100 else 0)) for t, dec, _ in reads):
            return f       # stale text
    return text


# ---------------------------------------------------------------- values
_NULL_DATE = _dt.datetime(1899, 12, 30)
_DATE_RE = re.compile(r"^\s*(-?\d{1,6})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:[.,](\d+))?)?)?")
_DUR_RE = re.compile(r"^\s*(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:[.,]\d+)?)S)?)?\s*$")


def date_serial(s: str) -> Optional[float]:
    """office:date-value (xsd:date / xsd:dateTime) -> days since 1899-12-30 (LibreOffice's null date)."""
    m = _DATE_RE.match(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= y <= 9999 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        days = (_dt.datetime(y, mo, d) - _NULL_DATE).days
    except ValueError:
        return None
    secs = 0.0
    if m.group(4) is not None:
        secs = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6) or 0)
        if m.group(7):
            secs += float("0." + m.group(7)[:9])
    return days + secs / 86400.0


def duration_days(s: str) -> Optional[float]:
    """office:time-value (xsd:duration, ODF 1.2 19.385) -> days."""
    m = _DUR_RE.match(s)
    if not m or len(s) > 64:
        return None
    y, mo, d, h, mi = (int(x) if x else 0 for x in m.group(2, 3, 4, 5, 6))
    sec = float(m.group(7).replace(",", ".")) if m.group(7) else 0.0
    v = (y * 365 + mo * 30 + d) + (h * 3600 + mi * 60 + sec) / 86400.0
    return -v if m.group(1) else v


def _q(s: str) -> str:
    """A literal for a format code."""
    return '"%s"' % s.replace('"', "'")


_COND = re.compile(r"^\s*value\(\)\s*(<=|>=|<>|!=|=|<|>)\s*(-?[0-9.]+(?:[eE][+-]?\d+)?)\s*$")


def style_code(el, styles: odf.StyleSheet, depth: int = 0) -> str:
    """Format code of a number:*-style element (xmloff xmlnumfi.cxx SvXMLNumFormatContext builds the
    same: style:map conditions become leading conditional sections, the style itself the last)."""
    body = _section(el)
    if depth > 4:
        return body
    secs = []
    for m in el:
        if m.tag != odf.qn("style:map") or len(secs) >= 3:
            continue
        cm = _COND.match(odf.attr(m, "style:condition") or "")
        target = styles.data_style(odf.attr(m, "style:apply-style-name"))
        if not cm or target is None or target is el:
            continue
        op = "<>" if cm.group(1) == "!=" else cm.group(1)
        secs.append("[%s%s]%s" % (op, cm.group(2), _section(target)))
    if not secs:
        return body
    return ";".join(secs + [body])


# xmloff xmlnumfi.cxx aDefaultDateFormats: (dow, day, month, year, hours, minutes, seconds, from
# system) -> the en-US format code of that built-in format (i18npool en_US locale data).
# "any" = any non-absent value. "ts"/"tl" = textual short/long month.
_DEFAULT_DATES = (
    (("", "any", "any", "any", "", "", "", True), "M/D/YY"),                         # DATE_SYSTEM_SHORT
    (("any", "any", "any", "any", "", "", "", True), "DDDD, MMMM D, YYYY"),          # DATE_SYSTEM_LONG
    (("", "", "l", "s", "", "", "", False), "MM/YY"),                                # DATE_SYS_MMYY
    (("", "l", "ts", "", "", "", "", False), "MMM DD"),                              # DATE_SYS_DDMMM
    (("", "l", "l", "l", "", "", "", False), "MM/DD/YYYY"),                          # DATE_SYS_DDMMYYYY
    (("", "l", "l", "s", "", "", "", False), "MM/DD/YY"),                            # DATE_SYS_DDMMYY
    (("", "s", "ts", "s", "", "", "", False), "MMM D, YY"),                          # DATE_SYS_DMMMYY
    (("", "s", "ts", "l", "", "", "", False), "MMM D, YYYY"),                        # DATE_SYS_DMMMYYYY
    (("", "s", "tl", "l", "", "", "", False), "MMMM D, YYYY"),                       # DATE_SYS_DMMMMYYYY
    (("s", "s", "ts", "s", "", "", "", False), "DDD, MMM D, YY"),                    # DATE_SYS_NNDMMMYY
    (("s", "s", "tl", "l", "", "", "", False), "DDD, MMMM D, YYYY"),                 # DATE_SYS_NNDMMMMYYYY
    (("l", "s", "tl", "l", "", "", "", False), "DDDD, MMMM D, YYYY"),                # DATE_SYS_NNNNDMMMMYYYY
    (("", "any", "any", "l", "any", "any", "", False), "MM/DD/YYYY HH:MM"),          # DATETIME_SYS_DDMMYYYY_HHMM
    (("", "any", "any", "any", "any", "any", "", True), "MM/DD/YY HH:MM AM/PM"),     # DATETIME_SYSTEM_SHORT_HHMM
    (("", "any", "any", "any", "any", "any", "any", False), "MM/DD/YYYY HH:MM:SS"),  # DATETIME_SYS_DDMMYYYY_HHMMSS
)
_DATE_SLOTS = ("day-of-week", "day", "month", "year", "hours", "minutes", "seconds")


def _auto_order_date(el) -> Optional[str]:
    """number:automatic-order="true" date style made of exactly the elements of one of
    LibreOffice's default date formats: that format, with the order and separators of the
    locale (xmlnumfi.cxx SvXMLNumFormatContext::CreateAndInsert, #109651#), here en-US.
    None when it is not such a style."""
    if not odf.bool_attr(el, "number:automatic-order"):
        return None
    slots = dict.fromkeys(_DATE_SLOTS, "")
    for ch in el:
        if odf.ns_of(ch) != odf.NS["number"]:
            continue
        ln = odf.local(ch)
        if ln in ("text", "am-pm"):
            continue
        if ln not in slots:
            return None    # m_bDateNoDefault
        long_ = odf.attr(ch, "number:style") == "long"
        v = "l" if long_ else "s"
        if ln == "month" and odf.bool_attr(ch, "number:textual"):
            v = "t" + v
        slots[ln] = v
    system = odf.attr(el, "number:format-source") == "language"
    have = tuple(slots[k] for k in _DATE_SLOTS)
    for pattern, code in _DEFAULT_DATES:
        if pattern[7] != system:
            continue
        if all(p == h or (p == "any" and h) for p, h in zip(pattern[:7], have)):
            return code
    return None


def _section(el) -> str:
    kind = odf.local(el)
    if kind == "date-style":
        auto = _auto_order_date(el)
        if auto:
            return auto
    out = []
    for ch in el:
        ln = odf.local(ch)
        if odf.ns_of(ch) != odf.NS["number"] and not (ln == "text" and odf.ns_of(ch) == odf.NS["loext"]):
            continue
        if ln == "text":
            t = "".join(ch.itertext())
            if kind == "percentage-style" and "%" in t:
                parts = t.split("%")
                out.append("%".join(_q(p) for p in parts))
            else:
                out.append(_q(t))
        elif ln == "currency-symbol":
            out.append(_q("".join(ch.itertext())))
        elif ln == "number":
            out.append(_number_code(ch))
        elif ln == "scientific-number":
            n = _number_code(ch, grouping=False)
            e = odf.int_attr(ch, "number:min-exponent-digits", 2, 1, 9)
            out.append(n + "E+" + "0" * e)
        elif ln == "fraction":
            ints = odf.int_attr(ch, "number:min-integer-digits", -1, -1, 15)
            num = odf.int_attr(ch, "number:min-numerator-digits", 1, 1, 9)
            den_val = odf.int_attr(ch, "number:denominator-value", 0, 0, 1 << 20)
            den = odf.int_attr(ch, "number:min-denominator-digits", 1, 1, 9)
            # no number:min-integer-digits: no integer part ("?/100"); xmlnumfi.cxx AddNumber
            s = "" if ints == -1 else ("#" if ints == 0 else "0" * ints) + " "
            s += "?" * num + "/" + (str(den_val) if den_val else "?" * den)
            out.append(s)
        elif ln == "text-content":
            out.append("@")
        elif kind in ("date-style", "time-style"):
            out.append(_date_code(ch, ln, el))
        elif ln == "boolean":
            out.append('"TRUE"')
    return "".join(out) or "General"


def _number_code(ch, grouping: Optional[bool] = None) -> str:
    """number:number -> '#,##0.00' (SvNumberFormatter::GenerateFormat as AddNumber calls it)."""
    dec_attr = odf.attr(ch, "number:decimal-places")
    ints = odf.int_attr(ch, "number:min-integer-digits", 0, 0, 30)
    if grouping is None:
        grouping = odf.bool_attr(ch, "number:grouping")
    if dec_attr is None and odf.local(ch) == "number":
        return "General"
    dec = odf.int_attr(ch, "number:decimal-places", 0, 0, 30)
    mind = odf.int_attr(ch, "number:min-decimal-places", dec, 0, 30)
    if odf.attr(ch, "number:decimal-replacement") is not None:
        mind = dec = 0
    if grouping:
        s = "#,##" + "0" * max(1, ints) if ints <= 4 else "0" * ints
        if ints == 0:
            s = "#,###"
    else:
        s = "0" * ints if ints else "#"
    for e in ch:
        if e.tag == odf.qn("number:embedded-text") and odf.attr(e, "number:position") in ("0", None):
            # number:embedded-text at position 0: a literal right after the integer digits
            s += _q("".join(e.itertext()))
    if dec:
        s += "." + "0" * min(mind, dec) + "#" * (dec - min(mind, dec))
    factor = odf.attr(ch, "number:display-factor")
    if factor:
        try:
            f = float(factor)
            while f >= 1000 and len(s) < 64:
                s += ","
                f /= 1000
        except ValueError:
            pass
    return s


def _date_code(ch, ln: str, style) -> str:
    long_ = odf.attr(ch, "number:style") == "long"
    if ln == "day":
        return "DD" if long_ else "D"
    if ln == "month":
        if odf.bool_attr(ch, "number:textual"):
            return "MMMM" if long_ else "MMM"
        return "MM" if long_ else "M"
    if ln == "year":
        return "YYYY" if long_ else "YY"
    if ln == "day-of-week":
        return "DDDD" if long_ else "DDD"
    if ln == "hours":
        h = "HH" if long_ else "H"
        if odf.attr(style, "number:truncate-on-overflow") == "false":
            h = "[" + h + "]"
        return h
    if ln == "minutes":
        return "MM" if long_ else "M"
    if ln == "seconds":
        n = odf.int_attr(ch, "number:decimal-places", 0, 0, 9)
        return ("SS" if long_ else "S") + ("." + "0" * n if n else "")
    if ln == "am-pm":
        return "AM/PM"
    return ""


def parse(pkg: odf.Package, images_enabled: bool = True) -> Document:
    """Parse an opened OpenDocument spreadsheet package."""
    content = pkg.content()
    styles = odf.StyleSheet(content, pkg.styles())
    doc = Document(format="ods")
    book = _Book(pkg, styles, doc, images_enabled)
    body = odf.find(content, "office:body", "office:spreadsheet")
    used: set = set()
    if body is not None:
        for el in body:
            if el.tag != _TABLE:
                continue
            if is_external_cache(odf.attr(el, "table:name", "") or ""):
                continue
            if len(doc.sheets) >= MAX_SHEETS:
                doc.warnings.append("more than %d sheets: the rest is ignored" % MAX_SHEETS)
                break
            name = sheet_name(odf.attr(el, "table:name", "") or "", used, len(doc.sheets))
            used.add(name.casefold())
            doc.sheets.append(book.sheet(el, name))
    w = book.w
    from .xls import _build_blocks, _fill_flat
    _fill_flat(doc)
    _build_blocks(doc)
    doc.extra["comments"] = list(w.comments)
    doc.extra["textboxes"] = list(w.textboxes)
    doc.stories["comments"] = odf.flatten(w.comments)
    doc.stories["textboxes"] = odf.flatten(w.textboxes, w.synthetic)
    doc.warnings.extend(pkg.warnings + w.warnings)
    return doc
