# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice xmloff/source/text/
# (XMLTextMasterPageContext.cxx, XMLTextHeaderFooterContext.cxx, txtimp.cxx),
# sw/source/filter/xml/ (xmltbli.cxx, xmlimp.cxx) and
# sw/source/core/table/swnewtable.cxx (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""OpenDocument text (.odt / .ott) -> Document(format="odt").

Same shape as .doc / .rtf: ``stories`` (main, footnotes, endnotes, headers,
comments, textboxes), ``blocks`` (main story with headings, lists, tables,
links, note and picture anchors), ``notes``, ``extra`` and ``images``.
The text is what LibreOffice shows: tracked deletions are dropped, fields
show their cached result, white space follows ODF 6.1.2, embedded objects
(and their replacement pictures) are not reported.
"""
from __future__ import annotations

from . import odf
from .model import Cell, Document, Link, Paragraph, Row, Table

# header / footer elements of a style:master-page (ODF 16.10 / 16.11; header-first is ODF 1.3,
# loext:header-first its LibreOffice 4-7 spelling) -> (is footer, is the main header/footer)
_HF = {odf.qn("style:header"): (False, True), odf.qn("style:footer"): (True, True),
       odf.qn("style:header-left"): (False, False), odf.qn("style:footer-left"): (True, False),
       odf.qn("style:header-first"): (False, False), odf.qn("style:footer-first"): (True, False),
       odf.qn("loext:header-first"): (False, False), odf.qn("loext:footer-first"): (True, False)}


def _header_footer_elements(sheet: odf.StyleSheet) -> list:
    """Header/footer elements LibreOffice imports (xmloff XMLTextMasterPageContext): per master
    page (the first one of a name) only the first style:header / style:footer, and its left /
    first variants only after it (otherwise the header is off and their content is dropped)."""
    out = []
    names = set()
    for page in sheet.master_pages:
        name = odf.attr(page, "style:name", "")
        if name in names:
            continue
        names.add(name)
        seen = [False, False]
        for hf in page:
            kind = _HF.get(hf.tag)
            if kind is None:
                continue
            footer, main = kind
            if main:
                if seen[footer]:
                    continue
                seen[footer] = True
            elif not seen[footer]:
                continue
            out.append(hf)
    return out


_MAX_GRID = 0xFFFF           # USHRT_MAX rows / columns of a Writer table
_MAX_SPAN_COLS = 256         # xmltbli.cxx: larger number-columns-spanned / -repeated are ignored (1)
_MAX_SPAN_ROWS = 8192        # larger number-rows-spanned / -repeated are ignored (1)


def _span(el, name: str, cap: int) -> int:
    n = odf.int_attr(el, name, 1, 1, 1 << 31)
    return 1 if n > cap else n


class _Grid:
    """Writer's cell grid while a table is imported (sw/source/filter/xml/xmltbli.cxx
    SwXMLTableContext): the column count comes from table:table-column; cells are put at
    the next free column, spans are clipped to the grid and to cells already used, a
    row that ends early gets one empty cell spanning the rest, cells beyond the last
    column are dropped."""

    def __init__(self, ncols: int):
        self.ncols = ncols
        self.rows: list = []         # list of [entry per column]: None | Cell (origin) | False (covered)
        self.headers: list = []
        self.heights: list = []      # per row: "variable" | "min" | "fixed" (row style height type)
        self.formula = False         # a cell has table:formula
        self.cur_row = 0
        self.cur_col = 0

    def ensure_row(self, r: int) -> None:
        while len(self.rows) <= r:
            self.rows.append([None] * self.ncols)
            self.headers.append(False)
            self.heights.append("variable")

    def start_row(self, header: bool, height: str = "variable") -> bool:
        if self.cur_row >= _MAX_GRID:
            return False
        if self.cur_row == 0 and self.ncols == 0:
            self.ncols = 1
            for r in self.rows:
                r.append(None)
        self.ensure_row(self.cur_row)
        self.headers[self.cur_row] = header
        self.heights[self.cur_row] = height
        self.cur_col = 0
        self.skip_used()
        return True

    def skip_used(self) -> None:
        row = self.rows[self.cur_row]
        while self.cur_col < self.ncols and row[self.cur_col] is not None:
            self.cur_col += 1

    def can_insert(self) -> bool:
        return self.cur_col < self.ncols

    def place(self, cell: Cell, rowspan: int, colspan: int) -> None:
        r0, c0 = self.cur_row, self.cur_col
        cols_req = c0 + colspan
        if cols_req > self.ncols:
            colspan = self.ncols - c0
            cols_req = self.ncols
        if r0 > 0 and colspan > 1:
            row = self.rows[r0]
            for i in range(c0 + 1, cols_req):
                if row[i] is not None:
                    colspan = i - c0
                    cols_req = i
                    break
        rows_req = min(r0 + rowspan, _MAX_GRID)
        rowspan = rows_req - r0
        self.ensure_row(rows_req - 1)
        for r in range(r0, rows_req):
            row = self.rows[r]
            for c in range(c0, cols_req):
                row[c] = False
        cell.colspan, cell.rowspan = colspan, rowspan
        self.rows[r0][c0] = cell
        self.cur_col = cols_req
        self.skip_used()

    def finish_row(self) -> None:
        if self.cur_col < self.ncols:
            self.place(Cell(), 1, self.ncols - self.cur_col)
        self.cur_row += 1


class _OdtWalker(odf.Walker):
    """odf.Walker with Writer's table model (see _Grid and _flatten)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._subs: dict = {}

    def table(self, el, story: str) -> Table:
        grid = self._grid(el, story, 0)
        return _to_table(*self._layout(grid, self._convertible(grid)))

    def _grid(self, el, story: str, depth: int) -> "_Grid":
        ncols = 0
        for ch in el:
            if ch.tag == _COL:
                ncols += _span(ch, "table:number-columns-repeated", _MAX_SPAN_COLS)
            elif ch.tag in _COLS:
                for col in ch:
                    if col.tag == _COL:
                        ncols += _span(col, "table:number-columns-repeated", _MAX_SPAN_COLS)
        grid = _Grid(min(ncols, _MAX_GRID))
        for ch in el:
            if ch.tag == _ROW:
                self._row(grid, ch, False, story, depth)
            elif ch.tag in (_HROWS, _ROWS):
                for row in ch:
                    if row.tag == _ROW:
                        self._row(grid, row, ch.tag == _HROWS, story, depth)
        return grid

    def _cell(self, el, story: str, depth: int) -> Cell:
        c = Cell()
        sv = odf.attr(el, "office:string-value")
        if sv is not None and odf.attr(el, "office:value-type") == "string":
            # fdo#60842: office:string-value replaces the cell content
            c.blocks.append(Paragraph(sv))
            return c
        first = True
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            if ch.tag == _TABLE and odf.attr(ch, "table:is-sub-table") == "true":
                # SwXMLTableCellContext_Impl: a sub-table only as the cell's first content;
                # it becomes part of the table grid (_flatten), later content is dropped
                if first and depth < 16:
                    self._subs[id(c)] = self._grid(ch, story, depth + 1)
                break
            first = False
            self.block(ch, story, c.blocks)
        return c

    def _height(self, row_el) -> str:
        name = odf.attr(row_el, "table:style-name")
        if self.styles.lookup("table-row", name, "table-row-properties", "style:row-height", self.part):
            return "fixed"
        if self.styles.lookup("table-row", name, "table-row-properties", "style:min-row-height", self.part):
            return "min"
        return "variable"

    def _row(self, grid: "_Grid", row_el, header: bool, story: str, depth: int) -> None:
        height = self._height(row_el)
        if not self._spend() or not grid.start_row(header, height):
            return
        for cell_el in row_el:
            if cell_el.tag != _CELL or not grid.can_insert():
                continue   # covered cells take no grid position; cells past the last column are dropped
            if odf.attr(cell_el, "table:formula") is not None:
                grid.formula = True
            colspan = _span(cell_el, "table:number-columns-spanned", _MAX_SPAN_COLS)
            rowspan = _span(cell_el, "table:number-rows-spanned", _MAX_SPAN_ROWS)
            rep = _span(cell_el, "table:number-columns-repeated", _MAX_SPAN_COLS)
            grid.place(self._cell(cell_el, story, depth), rowspan, colspan)
            if colspan == 1:
                while rep > 1 and grid.can_insert() and self._spend():
                    grid.place(self._cell(cell_el, story, depth), 1, 1)
                    rep -= 1
        grid.finish_row()
        # xmltbli.cxx SwXMLTableRowContext_Impl reads number-rows-repeated in the *style*
        # namespace, so Writer ignores table:number-rows-repeated
        rep = _span(row_el, "style:number-rows-repeated", _MAX_SPAN_ROWS)
        while rep > 1 and self._spend() and grid.start_row(False, height):
            # InsertRepRows: copy every free cell of the previous row (row span 1)
            prev = grid.rows[grid.cur_row - 1]
            while grid.can_insert():
                src = prev[grid.cur_col]
                if isinstance(src, Cell):
                    copy = Cell(blocks=[_copy_block(b) for b in src.blocks])
                    grid.place(copy, 1, src.colspan)
                else:
                    grid.place(Cell(), 1, 1)
            grid.finish_row()
            rep -= 1

    def _convertible(self, grid: "_Grid") -> bool:
        """SwTable::CanConvertSubtables (called from SwXMLImport::endDocument): a table whose
        sub-tables can be merged into its grid. Not when a row holds two sub-tables, a
        sub-table holds another one, a cell has a formula, or a sub-table with rows of
        non-fixed height sits in a row of set height. Row spans in the outer table are kept
        as nested boxes by the old table model (xmltbli.cxx MakeTableLine), so a table with
        row spans and sub-tables is not converted here either."""
        found = False
        for r, row in enumerate(grid.rows):
            subs_in_row = 0
            for e in row:
                if not isinstance(e, Cell):
                    continue
                sub = self._subs.get(id(e))
                if e.rowspan > 1:
                    if sub is not None:
                        return False
                    continue
                if sub is None:
                    continue
                found = True
                subs_in_row += 1
                if subs_in_row > 1 or sub.formula:
                    return False
                if any(isinstance(x, Cell) and id(x) in self._subs for srow in sub.rows for x in srow):
                    return False
                if any(h != "fixed" for h in sub.heights) and grid.heights[r] != "variable":
                    return False
        if found and (grid.formula or any(isinstance(e, Cell) and e.rowspan > 1 for row in grid.rows for e in row)):
            return False
        return True

    def _layout(self, grid: "_Grid", convert: bool):
        """Cell placements (cell, row, col, rowspan, colspan), row and column count and header
        flags of a grid whose cells may hold sub-tables. Converted (SwTable::ConvertSubtables):
        the row a sub-table sits in is split into as many rows as the sub-table has, the other
        cells of the row span them; the column it sits in is split into its columns (the
        export computes one column grid from all cell edges). Otherwise a sub-table stays a
        table nested in its cell."""
        orig = []
        for r, row in enumerate(grid.rows):
            for c, e in enumerate(row):
                if isinstance(e, Cell):
                    orig.append((e, r, c))
                elif e is None:
                    row[c] = Cell()
                    orig.append((row[c], r, c))
        nr, nc = len(grid.rows), grid.ncols
        k = [1] * nc
        m = [1] * nr
        subs = {}
        for cell, r, c in orig:
            sub = self._subs.pop(id(cell), None)
            if sub is None:
                continue
            placed = self._layout(sub, False)
            if not convert or not placed[1] or not placed[2]:
                cell.blocks.append(_to_table(*placed))
                continue
            subs[id(cell)] = placed
        for _pass in (0, 1):
            for cell, r, c in orig:
                placed = subs.get(id(cell))
                if placed is None:
                    continue
                cs = max(1, min(cell.colspan, nc - c))
                have = sum(k[c:c + cs])
                if placed[2] > have:
                    k[c + cs - 1] += placed[2] - have
                if placed[1] > m[r]:
                    m[r] = placed[1]
        X = [0]
        for v in k:
            X.append(X[-1] + v)
        Y = [0]
        for v in m:
            Y.append(Y[-1] + v)
        headers = []
        for r in range(nr):
            headers.extend([grid.headers[r]] * m[r])
        out = []
        for cell, r, c in orig:
            cs = max(1, min(cell.colspan, nc - c))
            rs = max(1, min(cell.rowspan, nr - r))
            h, w = Y[r + rs] - Y[r], X[c + cs] - X[c]
            placed = subs.get(id(cell))
            if placed is None:
                out.append((cell, Y[r], X[c], h, w))
                continue
            sp, snr, snc, _hdr = placed
            for sc, sr, scc, srs, scs in sp:
                if sr + srs >= snr:
                    srs += h - snr
                if scc + scs >= snc:
                    scs += w - snc
                out.append((sc, Y[r] + sr, X[c] + scc, srs, scs))
        return out, Y[-1], X[-1], headers


def _to_table(placed, nrows, _ncols, headers) -> Table:
    rows = [Row(header=headers[r] if r < len(headers) else False) for r in range(nrows)]
    for cell, r, _c, rs, cs in sorted(placed, key=lambda x: (x[1], x[2])):
        cell.rowspan, cell.colspan = rs, cs
        rows[r].cells.append(cell)
    return Table(rows=rows)


def _copy_block(b):
    if isinstance(b, Table):
        return Table(rows=[Row(cells=[Cell(blocks=[_copy_block(x) for x in c.blocks], colspan=c.colspan,
                                           rowspan=c.rowspan, header=c.header) for c in r.cells], header=r.header)
                           for r in b.rows])
    return Paragraph(b.text, heading=b.heading, list_level=b.list_level, list_ordered=b.list_ordered,
                     list_number=b.list_number, images=list(b.images), notes=list(b.notes),
                     links=[Link(l.start, l.end, l.url) for l in b.links])


_COL = odf.qn("table:table-column")
_COLS = (odf.qn("table:table-columns"), odf.qn("table:table-header-columns"), odf.qn("table:table-column-group"))
_ROW, _ROWS, _HROWS = odf.qn("table:table-row"), odf.qn("table:table-rows"), odf.qn("table:table-header-rows")
_CELL = odf.qn("table:table-cell")
_TABLE = odf.qn("table:table")


def parse(pkg: odf.Package, images_enabled: bool = True) -> Document:
    """Parse an opened text package (see msdoc.odf.open_package)."""
    content = pkg.content()
    styles_root = pkg.styles()
    sheet = odf.StyleSheet(content, styles_root)
    doc = Document(format="odt")
    w = _OdtWalker(pkg, sheet, doc, images_enabled=images_enabled)
    meta = pkg.meta()
    gen = odf.find(meta, "office:meta", "meta:generator")
    # SvXMLImport::IsMSO: lists of Microsoft Office documents continue across other lists
    w.lists.mso = bool(gen is not None and (gen.text or "").startswith("MicrosoftOffice"))

    body = odf.find(content, "office:body")
    main: list = []
    if body is None or not len(body):
        doc.warnings.append("content.xml has no office:body content")
    else:
        # SwXMLBodyContext_Impl: every child of office:body (normally office:text) is
        # imported as body text
        w.part = "content"
        for ch in body:
            w.blocks(ch, "main", main)

    headers: list = []
    w.part = "styles"
    for hf in _header_footer_elements(sheet):
        w.within("header", w.blocks, hf, "headers", headers)
    w.part = "content"

    doc.blocks = main
    doc.stories["main"] = odf.flatten(main, w.synthetic)
    for kind in ("footnotes", "endnotes"):
        doc.notes[kind] = w.notes[kind]
        doc.stories[kind] = [t for note in w.notes[kind] for t in odf.flatten(note, w.synthetic)]
    for key, blocks in (("textboxes", w.textboxes), ("comments", w.comments), ("headers", headers)):
        doc.extra[key] = blocks
        doc.stories[key] = odf.flatten(blocks, w.synthetic)
    doc.warnings.extend(pkg.warnings)
    doc.warnings.extend(w.warnings)
    return doc
