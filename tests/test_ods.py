# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""OpenDocument spreadsheet (.ods) extraction on synthetic packages built in code."""
import random
import time

import msdoc
from msdoc import ods
from msdoc.model import Table

import odf_helpers as H

ODS_MT = "application/vnd.oasis.opendocument.spreadsheet"
NS = H.NSDECL.replace(
    'office:version="1.3"',
    'xmlns:number="urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0" '
    'xmlns:calcext="urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0" '
    'xmlns:of="urn:oasis:names:tc:opendocument:xmlns:of:1.2" office:version="1.3"')


def content(body: str, auto: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-content {NS}>'
            f'<office:automatic-styles>{auto}</office:automatic-styles>'
            f'<office:body><office:spreadsheet>{body}</office:spreadsheet></office:body>'
            '</office:document-content>')


def styles(common: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-styles {NS}><office:styles>{common}</office:styles>'
            '</office:document-styles>')


def ods_bytes(body: str, auto: str = "", common: str = "", extra: dict | None = None) -> bytes:
    files = {"content.xml": content(body, auto), "styles.xml": styles(common)}
    files.update(extra or {})
    return H.package(files, mimetype=ODS_MT)


def ex(body, auto="", **kw):
    return msdoc.extract(ods_bytes(body, auto, **kw))


def table(rows: str, name="Sheet1", extra_attrs="") -> str:
    return f'<table:table table:name="{name}" {extra_attrs}>{rows}</table:table>'


def row(cells: str, attrs="") -> str:
    return f"<table:table-row {attrs}>{cells}</table:table-row>"


def cell(text=None, attrs="") -> str:
    if text is None:
        return f"<table:table-cell {attrs}/>"
    return f"<table:table-cell {attrs}><text:p>{text}</text:p></table:table-cell>"


# ------------------------------------------------------------------ basics
def test_format_sheets_and_markdown():
    doc = ex(table(row(cell("Name") + cell("Qty")) + row(cell("apple") + cell("3", 'office:value-type="float" office:value="3"'))))
    assert doc.format == "ods"
    sh = doc.sheets[0]
    assert sh.name == "Sheet1" and not sh.hidden
    assert sh.cells == {(0, 0): "Name", (0, 1): "Qty", (1, 0): "apple", (1, 1): "3"}
    md = doc.markdown
    assert "## Sheet1" in md and "<td>apple</td>" in md
    assert doc.stories["main"] == ["Sheet1", "Name\tQty", "apple\t3"]
    assert isinstance(doc.blocks[1], Table)


NUM_STYLES = (
    '<number:number-style style:name="N2"><number:number number:decimal-places="2" '
    'number:min-integer-digits="1" number:grouping="true"/></number:number-style>'
    '<number:percentage-style style:name="P1"><number:number number:decimal-places="1" '
    'number:min-integer-digits="1"/><number:text>%</number:text></number:percentage-style>'
    '<number:currency-style style:name="C1P0"><number:currency-symbol>$</number:currency-symbol>'
    '<number:number number:decimal-places="2" number:min-integer-digits="1" number:grouping="true"/></number:currency-style>'
    '<number:currency-style style:name="C1"><number:text>(</number:text><number:currency-symbol>$</number:currency-symbol>'
    '<number:number number:decimal-places="2" number:min-integer-digits="1" number:grouping="true"/><number:text>)</number:text>'
    '<style:map style:condition="value()&gt;=0" style:apply-style-name="C1P0"/></number:currency-style>'
    '<number:date-style style:name="D1"><number:year number:style="long"/><number:text>-</number:text>'
    '<number:month number:style="long"/><number:text>-</number:text><number:day number:style="long"/></number:date-style>'
    '<number:date-style style:name="D2" number:automatic-order="true"><number:day number:style="long"/><number:text>.</number:text>'
    '<number:month number:style="long"/><number:text>.</number:text><number:year number:style="long"/></number:date-style>'
    '<number:time-style style:name="T1" number:truncate-on-overflow="false"><number:hours/><number:text>:</number:text>'
    '<number:minutes number:style="long"/></number:time-style>'
    '<number:boolean-style style:name="B1"><number:text>[</number:text><number:boolean/><number:text>]</number:text></number:boolean-style>'
    '<number:number-style style:name="F1"><number:fraction number:min-integer-digits="1" number:min-numerator-digits="1" '
    'number:min-denominator-digits="1"/></number:number-style>'
    '<number:number-style style:name="E1"><number:scientific-number number:decimal-places="2" number:min-integer-digits="1" '
    'number:min-exponent-digits="2"/></number:number-style>')
CELL_STYLES = "".join(f'<style:style style:name="c{n}" style:family="table-cell" style:data-style-name="{n}"/>'
                      for n in ("N2", "P1", "C1", "D1", "D2", "T1", "B1", "F1", "E1"))


def _values(cells_xml):
    doc = ex(table(row(cells_xml)), NUM_STYLES + CELL_STYLES)
    sh = doc.sheets[0]
    return [sh.cells.get((0, c), "") for c in range(max(c for _, c in sh.cells) + 1)]


def test_values_without_text_are_formatted_with_the_data_style():
    got = _values(
        '<table:table-cell table:style-name="cN2" office:value-type="float" office:value="1234.5"/>'
        '<table:table-cell table:style-name="cP1" office:value-type="percentage" office:value="0.1234"/>'
        '<table:table-cell table:style-name="cC1" office:value-type="currency" office:value="-1234.5"/>'
        '<table:table-cell table:style-name="cC1" office:value-type="currency" office:value="7"/>'
        '<table:table-cell table:style-name="cD1" office:value-type="date" office:date-value="2020-05-01"/>'
        '<table:table-cell table:style-name="cD2" office:value-type="date" office:date-value="2020-05-01"/>'
        '<table:table-cell table:style-name="cT1" office:value-type="time" office:time-value="PT27H05M00S"/>'
        '<table:table-cell table:style-name="cB1" office:value-type="boolean" office:boolean-value="true"/>'
        '<table:table-cell table:style-name="cF1" office:value-type="float" office:value="7.5"/>'
        '<table:table-cell table:style-name="cE1" office:value-type="float" office:value="12345"/>'
        '<table:table-cell office:value-type="float" office:value="0.1"/>'
        '<table:table-cell office:value-type="boolean" office:boolean-value="false"/>'
        '<table:table-cell office:value-type="date" office:date-value="2014-02-20"/>'
        '<table:table-cell office:value-type="string" office:string-value="sv"/>')
    assert got == ["1,234.50", "12.3%", "($1,234.50)", "$7.00", "2020-05-01", "05/01/2020", "27:05",
                   "[TRUE]", "7 1/2", "1.23E+04", "0.1", "FALSE", "02/20/14", "sv"]


def test_display_text_versus_value():
    got = _values(
        # the stored text is LibreOffice's rendering: kept
        '<table:table-cell office:value-type="float" office:value="154.719066107646"><text:p>154.72</text:p></table:table-cell>'
        # stale or empty paragraphs: the value is shown
        '<table:table-cell table:style-name="cN2" office:value-type="float" office:value="2"><text:p>1</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="2.5"><text:p/></table:table-cell>'
        '<table:table-cell office:value-type="date" office:date-value="2014-02-20"><text:p>41690</text:p></table:table-cell>'
        # another locale's separators for a locale-neutral style; booleans in en-US
        '<table:table-cell table:style-name="cN2" office:value-type="float" office:value="1234.5"><text:p>1.234,50</text:p></table:table-cell>'
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"><text:p>WAHR</text:p></table:table-cell>'
        # error results and empty string results of formulas
        '<table:table-cell table:formula="of:=1/0" office:value-type="float" office:value="0"><text:p>#DIV/0!</text:p></table:table-cell>'
        '<table:table-cell table:formula="of:=&quot;&quot;" table:style-name="cN2" office:value-type="float" office:value="0" '
        'calcext:value-type="float"><text:p/></table:table-cell>'
        '<table:table-cell table:formula="of:=A1" office:value-type="float" office:value="3.5"><text:p>3,5</text:p></table:table-cell>'
        '<table:table-cell><text:p>end</text:p></table:table-cell>')
    assert got == ["154.72", "2.00", "2.5", "02/20/14", "1,234.50", "TRUE", "#DIV/0!", "", "3.5", "end"]


def test_value_helpers():
    assert ods.date_serial("1899-12-31") == 1
    assert ods.date_serial("2000-01-01T12:00:00") == 36526.5
    assert ods.date_serial("garbage") is None and ods.date_serial("2000-13-01") is None
    assert ods.duration_days("PT36H") == 1.5 and ods.duration_days("-PT12H") == -0.5
    assert ods.duration_days("P1Y2M3DT4H5M6S") is not None and ods.duration_days("PTxH") is None
    assert ods.is_external_cache("'file:///tmp/a.ods'#Sheet1")
    assert not ods.is_external_cache("Sheet1") and not ods.is_external_cache("'quoted'")
    assert ods.is_package_url("Object 1") and ods.is_package_url("./Pictures/a.png")
    assert not ods.is_package_url("file:///x.xlsx") and not ods.is_package_url("/abs/x") and not ods.is_package_url("../x")


# ------------------------------------------------------------------ structure
def test_paragraphs_spans_and_lost_elements():
    doc = ex(table(row(
        '<table:table-cell><text:p>one<text:s text:c="2"/>two</text:p><text:p>second<text:tab/>t'
        '<text:line-break/>lb</text:p></table:table-cell>'
        '<table:table-cell><text:p><text:span>a<text:span>lost</text:span>b</text:span>'
        '<text:ruby><text:ruby-base>base</text:ruby-base><text:ruby-text>ruby</text:ruby-text></text:ruby>'
        '<text:sheet-name>?</text:sheet-name><text:page-number>9</text:page-number>!</text:p></table:table-cell>'
        '<table:table-cell><text:h>heading is not cell text</text:h></table:table-cell>'), name="Tab"))
    sh = doc.sheets[0]
    assert sh.cells[(0, 0)] == "one  two\nsecond\tt\nlb"
    assert sh.cells[(0, 1)] == "abbaseTab!"
    assert (0, 2) not in sh.cells
    md = doc.markdown
    assert "one  two" in md


def test_repeats_and_trailing_empties():
    doc = ex(table(
        row(cell("x", 'table:number-columns-repeated="3"') + cell(None, 'table:number-columns-repeated="16000"'))
        + row(cell("y"), 'table:number-rows-repeated="2"')
        + row(cell(None, 'table:number-columns-repeated="1024"'), 'table:number-rows-repeated="1048000"')
        + row(cell("last"))))
    sh = doc.sheets[0]
    assert {k: v for k, v in sh.cells.items() if v == "x"} == {(0, 0): "x", (0, 1): "x", (0, 2): "x"}
    assert sh.cells[(1, 0)] == sh.cells[(2, 0)] == "y"
    assert sh.cells[(3 + 1048000, 0)] == "last"
    assert len(sh.cells) == 6


def test_hostile_repeats_are_bounded():
    body = table(
        row(cell("a", 'table:number-columns-repeated="1000000000"'), 'table:number-rows-repeated="1000000000"')
        + row(cell(None, 'table:number-columns-repeated="1000000000"'), 'table:number-rows-repeated="1000000000"'))
    t0 = time.time()
    doc = ex(body)
    assert time.time() - t0 < 30
    sh = doc.sheets[0]
    assert 0 < len(sh.cells) <= ods.MAX_CELLS
    assert max(r for r, _ in sh.cells) < ods.MAX_EXPAND and max(c for _, c in sh.cells) < ods.MAX_COL
    assert any("truncated" in w for w in doc.warnings)


def test_merges_covered_cells_and_groups():
    doc = ex(table(
        '<table:table-header-rows>' + row(
            '<table:table-cell table:number-columns-spanned="2" table:number-rows-spanned="2"><text:p>M</text:p></table:table-cell>'
            '<table:covered-table-cell><text:p>hidden by merge</text:p></table:covered-table-cell>' + cell("h")) + '</table:table-header-rows>'
        + '<table:table-row-group><table:table-row-group>' + row('<table:covered-table-cell table:number-columns-repeated="2"/>' + cell("z"))
        + '</table:table-row-group></table:table-row-group>' + row(cell("p") + cell("q"))))
    sh = doc.sheets[0]
    assert sh.merges == [(0, 0, 2, 2)]
    assert sh.cells == {(0, 0): "M", (0, 2): "h", (1, 2): "z", (2, 0): "p", (2, 1): "q"}
    assert 'colspan="2"' in doc.markdown and 'rowspan="2"' in doc.markdown


def test_hidden_sheet_hidden_rows_and_external_cache():
    auto = ('<style:style style:name="ta2" style:family="table"><style:table-properties table:display="false"/></style:style>'
            '<style:style style:name="ro2" style:family="table-row"><style:table-row-properties/></style:style>')
    doc = ex(table(row(cell("shown"))) + table(row(cell("secret")), "Hidden", 'table:style-name="ta2"')
             + table(row(cell("filtered"), 'table:visibility="filter"') + row(cell("collapsed"), 'table:visibility="collapse"'), "Rows")
             + table(row(cell("cache")), "'file:///tmp/x.ods'#Sheet1"), auto)
    assert [s.name for s in doc.sheets] == ["Sheet1", "Hidden", "Rows"]
    assert [s.hidden for s in doc.sheets] == [False, True, False]
    assert doc.sheets[1].cells == {(0, 0): "secret"}
    assert doc.sheets[2].cells == {(0, 0): "filtered", (1, 0): "collapsed"}  # LibreOffice keeps their text
    assert "secret" not in doc.markdown and "## Rows" in doc.markdown


def test_links_annotations_and_subtables():
    doc = ex(table(row(
        '<table:table-cell><text:p>see <text:a xlink:href="http://example.com/">site</text:a></text:p></table:table-cell>'
        '<table:table-cell office:value-type="string"><office:annotation><dc:creator>me</dc:creator>'
        '<text:p>a note</text:p></office:annotation><text:p>with comment</text:p></table:table-cell>'
        '<table:table-cell><table:table><table:table-row><table:table-cell><text:p>sub</text:p></table:table-cell>'
        '</table:table-row></table:table><text:p>outer</text:p></table:table-cell>')))
    sh = doc.sheets[0]
    assert sh.cells == {(0, 0): "see site", (0, 1): "with comment", (0, 2): "outer"}
    assert sh.links == {(0, 0): "http://example.com/"}
    assert doc.stories["comments"] == ["a note"]
    assert "http://example.com/" in doc.markdown


def test_pictures_in_cells_and_shapes():
    png1, png2 = H.png(2, 2, seed=1), H.png(2, 2, seed=2)
    frame = '<draw:frame draw:name="{n}"><draw:image xlink:href="Pictures/{f}"/></draw:frame>'
    body = table(
        '<table:shapes>' + frame.format(n="s", f="b.png")
        + '<draw:frame><draw:object xlink:href="./Object 1"/><draw:image xlink:href="./ObjectReplacements/Object 1"/></draw:frame>'
        + '<draw:frame><draw:object-ole xlink:href="file:///elsewhere/x.xlsx"/>'
          '<draw:image xlink:href="./ObjectReplacements/Object 2"/></draw:frame></table:shapes>'
        + row('<table:table-cell>' + frame.format(n="c", f="a.png") + '<text:p>pic</text:p></table:table-cell>'
              + '<table:table-cell>' + frame.format(n="d", f="a.png") + '</table:table-cell>'))
    doc = ex(body, extra={"Pictures/a.png": png1, "Pictures/b.png": png2,
                          "ObjectReplacements/Object 1": H.png(3, 3, seed=3),
                          "ObjectReplacements/Object 2": H.png(4, 4, seed=4)})
    sh = doc.sheets[0]
    # one picture per package file; the embedded object's preview is not reported, the
    # replacement of an object linked to an outside file is (LibreOffice keeps it as a graphic)
    assert len(doc.images) == 3
    assert {(r, c) for r, c, _ in sh.images} == {(0, 0), (0, 1)}
    assert [doc.images[i].data for r, c, i in sh.images if (r, c) == (0, 1)] == [png1]
    assert sh.cells == {(0, 0): "pic"}
    assert doc.markdown.count("![") >= 2 or doc.markdown.count("<img") >= 2
    assert msdoc.extract(ods_bytes(body, extra={"Pictures/a.png": png1}), images_enabled=False).images == []


def test_empty_and_odd_documents():
    assert ex("").sheets == []
    doc = ex(table("") + '<table:named-expressions/><table:database-ranges/>')
    assert doc.sheets[0].cells == {} and doc.markdown.strip() == "## Sheet1"


# ------------------------------------------------------------------ fuzz
def _seed():
    body = table(
        row('<table:table-cell table:number-columns-spanned="2" table:style-name="cN2" office:value-type="float" '
            'office:value="3.25"><text:p>3.25</text:p></table:table-cell><table:covered-table-cell/>'
            + cell("t", 'table:number-columns-repeated="3"'), 'table:number-rows-repeated="2"')
        + row('<table:table-cell office:value-type="date" office:date-value="2020-01-02"/>'
              '<table:table-cell table:style-name="cD2" office:value-type="time" office:time-value="PT1H"/>'
              '<table:table-cell><text:p><text:a xlink:href="http://x">l</text:a></text:p>'
              '<draw:frame><draw:image xlink:href="Pictures/p.png"/></draw:frame></table:table-cell>'))
    return body, NUM_STYLES + CELL_STYLES


def test_mutation_fuzz_package_bytes():
    body, auto = _seed()
    data = ods_bytes(body, auto, extra={"Pictures/p.png": H.png()})
    rnd = random.Random(4321)
    for _ in range(250):
        b = bytearray(data)
        for _ in range(rnd.randint(1, 20)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        if rnd.random() < 0.2:
            b = b[:rnd.randrange(len(b))]
        try:
            msdoc.extract(bytes(b))
        except msdoc.MsDocError:
            pass


def test_mutation_fuzz_xml():
    body, auto = _seed()
    xml = content(body, auto)
    rnd = random.Random(77)
    alphabet = '<>/="&; :-0123456789eE.PTHMSabtextpcelrownumber\x00\xff'
    for _ in range(300):
        s = list(xml)
        for _ in range(rnd.randint(1, 8)):
            i = rnd.randrange(len(s))
            op = rnd.random()
            if op < 0.4:
                s[i] = rnd.choice(alphabet)
            elif op < 0.7:
                del s[i]
            else:
                s.insert(i, rnd.choice(alphabet))
        data = H.package({"content.xml": "".join(s).encode("utf-8", "replace"), "styles.xml": styles(),
                          "Pictures/p.png": H.png()}, mimetype=ODS_MT)
        try:
            d = msdoc.extract(data)
            d.markdown
        except msdoc.MsDocError:
            pass


def test_sheet_names_as_libreoffice_loads_them():
    doc = ex(table(row(cell("a")), name="") + table(row(cell("b")), name="Data") + table(row(cell("c")), name="data")
             + table(row(cell("d")), name="bad:name"))
    assert [s.name for s in doc.sheets] == ["Sheet1", "Data", "data_2", "Sheet4"]
