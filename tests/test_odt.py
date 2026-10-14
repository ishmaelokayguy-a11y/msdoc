# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""OpenDocument text (.odt) extraction on synthetic packages (tests/odf_helpers.py)."""
import base64

import msdoc
from msdoc.model import IMAGE_MARK, NOTE_MARK, Paragraph, Table

import odf_helpers as H


def ex(body, auto="", **kw):
    return msdoc.extract(H.odt(body, auto, **kw))


def test_format_and_shape():
    doc = ex("<text:p>Hello</text:p>")
    assert doc.format == "odt"
    assert set(doc.stories) == set(msdoc.STORY_KEYS)
    assert doc.stories["main"] == ["Hello"]
    assert isinstance(doc.blocks[0], Paragraph)


def test_white_space_rules():
    doc = ex("<text:p>  a \n\t b<text:s/><text:s text:c=\"3\"/>c<text:tab/>d<text:line-break/>e "
             "<text:span> f</text:span></text:p><text:p><text:span>x </text:span> y</text:p>")
    assert doc.stories["main"] == ["a b    c\td\ne f", "x y"]


def test_headings():
    doc = ex('<text:h text:outline-level="3">H3</text:h><text:h>No level</text:h>'
             '<text:h text:outline-level="0">Zero</text:h><text:p text:style-name="Heading_20_1">Para</text:p>',
             styles='<style:style style:name="Heading_20_1" style:family="paragraph" style:default-outline-level="1"/>')
    assert [b.heading for b in doc.blocks] == [3, 1, 1, 0]


LISTS = ('<text:list-style style:name="L1">'
         '<text:list-level-style-number text:level="1" style:num-format="1" style:num-suffix="."/>'
         '<text:list-level-style-number text:level="2" style:num-format="a" style:num-suffix=")"/>'
         '</text:list-style>'
         '<text:list-style style:name="LB"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/></text:list-style>'
         '<text:list-style style:name="LR"><text:list-level-style-number text:level="1" style:num-format="i" '
         'style:num-prefix="(" style:num-suffix=")" text:start-value="3"/></text:list-style>')


def _items(doc):
    return [(b.text, b.list_level, b.list_ordered, b.list_number) for b in doc.blocks if isinstance(b, Paragraph)]


def test_list_numbering_and_nesting():
    body = ('<text:list text:style-name="L1" xml:id="list1">'
            '<text:list-item><text:p>one</text:p><text:p>one more</text:p>'
            '<text:list><text:list-item><text:p>sub a</text:p></text:list-item>'
            '<text:list-item><text:p>sub b</text:p></text:list-item></text:list></text:list-item>'
            '<text:list-header><text:p>header</text:p></text:list-header>'
            '<text:list-item><text:p>two</text:p><text:list><text:list-item><text:p>sub a2</text:p>'
            '</text:list-item></text:list></text:list-item></text:list>'
            '<text:p>between</text:p>'
            '<text:list text:style-name="L1" text:continue-list="list1"><text:list-item><text:p>three</text:p>'
            '</text:list-item></text:list>'
            '<text:list text:style-name="L1"><text:list-item text:start-value="5"><text:p>five</text:p>'
            '</text:list-item><text:list-item><text:p>six</text:p></text:list-item></text:list>'
            '<text:list text:style-name="L1" text:continue-numbering="true"><text:list-item><text:p>seven</text:p>'
            '</text:list-item></text:list>'
            '<text:list text:style-name="L1"><text:list-item><text:p>restart</text:p></text:list-item></text:list>'
            '<text:list text:style-name="LB"><text:list-item><text:p>bullet</text:p></text:list-item></text:list>'
            '<text:list text:style-name="LR"><text:list-item><text:p>roman</text:p></text:list-item></text:list>'
            '<text:list text:style-name="Missing"><text:list-item><text:p>default</text:p></text:list-item></text:list>')
    assert _items(ex(body, LISTS)) == [
        ("one", 0, True, "1."), ("one more", 0, True, None), ("sub a", 1, True, "a)"), ("sub b", 1, True, "b)"),
        ("header", 0, True, None), ("two", 0, True, "2."), ("sub a2", 1, True, "a)"),
        ("between", None, False, None), ("three", 0, True, "3."), ("five", 0, True, "5."), ("six", 0, True, "6."),
        ("seven", 0, True, "7."), ("restart", 0, True, "1."), ("bullet", 0, False, None),
        ("roman", 0, True, "(iii)"), ("default", 0, False, None)]


def test_paragraph_style_list_outside_list_is_not_numbered():
    doc = ex('<text:p text:style-name="P1">x</text:p>', LISTS + '<style:style style:name="P1" style:family="paragraph" '
             'style:list-style-name="L1"/>')
    assert _items(doc) == [("x", None, False, None)]


def test_numbered_paragraph():
    doc = ex('<text:numbered-paragraph text:list-id="a" text:level="1" text:style-name="L1"><text:p>np1</text:p>'
             '</text:numbered-paragraph><text:numbered-paragraph text:list-id="a" text:level="1" text:style-name="L1">'
             '<text:p>np2</text:p></text:numbered-paragraph>', LISTS)
    assert _items(doc) == [("np1", 0, True, "1."), ("np2", 0, True, "2.")]


def test_tables_merges_nesting_repeats():
    body = ('<table:table table:name="T"><table:table-column table:number-columns-repeated="3"/>'
            '<table:table-header-rows><table:table-row><table:table-cell><text:p>h1</text:p></table:table-cell>'
            '<table:table-cell table:number-columns-spanned="2"><text:p>h23</text:p></table:table-cell>'
            '<table:covered-table-cell/></table:table-row></table:table-header-rows>'
            '<table:table-row><table:table-cell table:number-rows-spanned="2"><text:p>v</text:p></table:table-cell>'
            '<table:table-cell><table:table><table:table-row><table:table-cell><text:p>inner</text:p></table:table-cell>'
            '</table:table-row></table:table></table:table-cell><table:table-cell><text:p>c</text:p></table:table-cell>'
            '</table:table-row>'
            '<table:table-row><table:covered-table-cell><text:p>hidden</text:p></table:covered-table-cell>'
            '<table:table-cell table:number-columns-repeated="2"><text:p>r</text:p></table:table-cell></table:table-row>'
            '</table:table>')
    doc = ex(body)
    t = doc.blocks[0]
    assert isinstance(t, Table) and len(t.rows) == 3
    assert t.rows[0].header and not t.rows[1].header
    assert [(c.colspan, c.rowspan) for c in t.rows[0].cells] == [(1, 1), (2, 1)]
    assert [(c.colspan, c.rowspan) for c in t.rows[1].cells] == [(1, 2), (1, 1), (1, 1)]
    assert len(t.rows[2].cells) == 2
    inner = t.rows[1].cells[1].blocks[0]
    assert isinstance(inner, Table) and inner.rows[0].cells[0].blocks[0].text == "inner"
    assert doc.stories["main"] == ["h1", "h23", "v", "inner", "c", "r", "r"]
    md = doc.to_markdown()
    assert 'colspan="2"' in md and 'rowspan="2"' in md and "<th>" in md


def test_links_and_bookmarks():
    doc = ex('<text:p>see <text:bookmark text:name="b"/><text:a xlink:href="https://example.org/">the '
             '<text:span>site</text:span></text:a> now</text:p>')
    p = doc.blocks[0]
    assert p.text == "see the site now"
    assert [(l.start, l.end, l.url) for l in p.links] == [(4, 12, "https://example.org/")]
    assert "[the site](https://example.org/)" in doc.to_markdown()


def test_footnotes_endnotes():
    doc = ex('<text:p>a<text:note text:id="n1" text:note-class="footnote"><text:note-citation>1</text:note-citation>'
             '<text:note-body><text:p>foot</text:p><text:p>two</text:p></text:note-body></text:note> b'
             '<text:note text:note-class="endnote"><text:note-citation>i</text:note-citation><text:note-body>'
             '<text:p>end</text:p></text:note-body></text:note></text:p>')
    p = doc.blocks[0]
    assert p.text == "a" + NOTE_MARK + " b" + NOTE_MARK
    assert p.notes == [("footnote", 1), ("endnote", 1)]
    assert doc.stories["main"] == ["a b"]
    assert doc.stories["footnotes"] == ["foot", "two"] and doc.stories["endnotes"] == ["end"]
    assert len(doc.notes["footnotes"]) == 1 and len(doc.notes["footnotes"][0]) == 2
    assert "[^1]: foot" in doc.to_markdown()


def test_tracked_changes():
    body = ('<text:tracked-changes><text:changed-region text:id="c1"><text:deletion><office:change-info>'
            '<dc:creator>x</dc:creator></office:change-info><text:p>deleted text</text:p></text:deletion>'
            '</text:changed-region><text:changed-region text:id="c2"><text:insertion><office:change-info/>'
            '</text:insertion></text:changed-region></text:tracked-changes>'
            '<text:p>keep<text:change text:change-id="c1"/> <text:change-start text:change-id="c2"/>inserted'
            '<text:change-end text:change-id="c2"/></text:p>')
    assert ex(body).stories["main"] == ["keep inserted"]


def test_header_footer_and_comments():
    master = ('<style:master-page style:name="Standard"><style:header><text:p text:style-name="MP1">Head '
              '<text:page-number>1</text:page-number></text:p></style:header>'
              '<style:footer><text:p>Foot</text:p></style:footer>'
              '<style:footer-left><text:p>Left foot</text:p></style:footer-left></style:master-page>'
              # XMLTextMasterPageContext: a left header without the main header is not imported,
              # nor a second master page of the same name
              '<style:master-page style:name="Other"><style:header-left><text:p>orphan</text:p></style:header-left>'
              '</style:master-page><style:master-page style:name="Standard"><style:header><text:p>dup</text:p>'
              '</style:header></style:master-page>')
    body = ('<text:p>x<office:annotation><dc:creator>Me</dc:creator><dc:date>2020-01-01</dc:date>'
            '<text:p>a comment</text:p></office:annotation>y</text:p>')
    doc = ex(body, master=master, styles_auto='<style:style style:name="MP1" style:family="paragraph"/>')
    assert doc.stories["main"] == ["xy"]
    assert doc.stories["comments"] == ["a comment"]
    # a page-number field outside the first laid-out page shows page 0 (SwPageNumberField)
    assert sorted(doc.stories["headers"]) == ["Foot", "Head 0", "Left foot"]
    assert len(doc.extra["headers"]) == 3


def test_fields_show_cached_text():
    doc = ex('<text:p>Date: <text:date text:date-value="2020-01-02">02.01.2020</text:date>, '
             '<text:sequence text:name="Figure">3</text:sequence><unknown:x xmlns:unknown="urn:x">zz</unknown:x></text:p>')
    assert doc.stories["main"] == ["Date: 02.01.2020, 3"]


def test_images_package_and_base64():
    p1, p2 = H.png(seed=1), H.png(2, 2, seed=2)
    b64 = base64.encodebytes(p2).decode()
    body = ('<text:p>a<draw:frame draw:name="F1" text:anchor-type="as-char"><draw:image xlink:href="Pictures/one.png"/>'
            '</draw:frame>b</text:p>'
            '<text:p><draw:frame text:anchor-type="paragraph"><draw:image><office:binary-data>' + b64 +
            '</office:binary-data></draw:image></draw:frame></text:p>'
            '<text:p><draw:frame><draw:image xlink:href="http://example.org/x.png"/></draw:frame></text:p>'
            '<text:p><draw:frame><draw:object xlink:href="./Object 1"/><draw:image xlink:href="./ObjectReplacements/Object 1"/>'
            '</draw:frame></text:p>'
            '<text:p><draw:frame><draw:image xlink:href="Pictures/missing.png"/></draw:frame></text:p>')
    doc = ex(body, extra_files={"Pictures/one.png": p1, "ObjectReplacements/Object 1": p1,
                                "Object 1/content.xml": "<x/>"})
    assert [(i.data, i.kind, i.story, i.mime) for i in doc.images] == [
        (p1, "inline", "main", "image/png"), (p2, "floating", "main", "image/png")]
    assert doc.images[0].name == "F1"
    assert doc.blocks[0].text == "a" + IMAGE_MARK + "b" and doc.blocks[0].images == [0]
    assert doc.blocks[1].images == [1]
    assert doc.stories["main"][:2] == ["ab", ""]
    assert "![Media 0](image:0)" in doc.to_markdown()
    assert msdoc.extract(H.odt(body, extra_files={"Pictures/one.png": p1}), images_enabled=False).images == []


def test_multiple_images_in_one_frame():
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    body = ('<text:p><draw:frame><draw:image xlink:href="Pictures/a.svg" draw:mime-type="image/svg+xml"/>'
            '<draw:image xlink:href="Pictures/a.png" draw:mime-type="image/png"/></draw:frame></text:p>')
    doc = ex(body, extra_files={"Pictures/a.svg": svg, "Pictures/a.png": H.png()})
    assert [i.mime for i in doc.images] == ["image/svg+xml"]


def test_text_boxes_shapes_and_page_frames():
    body = ('<draw:frame text:anchor-type="page"><draw:image xlink:href="Pictures/p.png"/></draw:frame>'
            '<text:p>anchor<draw:frame text:anchor-type="paragraph"><draw:text-box><text:p>in box'
            '<text:note><text:note-body><text:p>dropped note</text:p></text:note-body></text:note></text:p>'
            '<text:p><draw:frame text:anchor-type="as-char"><draw:image xlink:href="Pictures/q.png"/></draw:frame></text:p>'
            '</draw:text-box></draw:frame>'
            '<draw:custom-shape><text:p>shape text</text:p></draw:custom-shape>'
            '<draw:g><draw:rect><text:p>grouped</text:p></draw:rect></draw:g></text:p>')
    doc = ex(body, extra_files={"Pictures/p.png": H.png(seed=5), "Pictures/q.png": H.png(seed=6)})
    assert doc.stories["main"] == ["anchor"]
    assert doc.stories["textboxes"] == ["in box", "", "shape text", "grouped"]
    assert doc.stories["footnotes"] == []
    assert [(i.story, i.kind) for i in doc.images] == [("main", "floating"), ("textboxes", "inline")]
    assert doc.blocks[0].images == [0] and doc.blocks[0].text == IMAGE_MARK


def test_sections_and_indexes():
    body = ('<text:table-of-content text:name="TOC"><text:table-of-content-source><text:index-title-template>'
            'Contents</text:index-title-template></text:table-of-content-source><text:index-body>'
            '<text:index-title><text:p>Contents</text:p></text:index-title><text:p>Entry<text:tab/>1</text:p>'
            '</text:index-body></text:table-of-content>'
            '<text:section text:name="S"><text:p>in section</text:p></text:section>'
            '<text:sequence-decls><text:sequence-decl text:name="x"/></text:sequence-decls>')
    assert ex(body).stories["main"] == ["Contents", "Entry\t1", "in section"]


def test_markdown_end_to_end():
    body = ('<text:h text:outline-level="1">Title</text:h><text:list text:style-name="L1"><text:list-item>'
            '<text:p>first</text:p></text:list-item></text:list>')
    md = msdoc.to_markdown(H.odt(body, LISTS)).text
    assert md.startswith("# Title") and "1. first" in md


def test_sub_tables_merge_into_the_grid():
    # SwTable::ConvertSubtables: one 1-column sub-table in a row -> rows of the outer table
    body = ('<table:table><table:table-column/><table:table-column/><table:table-row><table:table-cell>'
            '<table:table table:is-sub-table="true"><table:table-column/>'
            '<table:table-row><table:table-cell><text:p>A</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell><text:p>B</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell><text:p>C</text:p></table:table-cell></table:table-row>'
            '</table:table></table:table-cell><table:table-cell><text:p>E</text:p></table:table-cell>'
            '</table:table-row></table:table>')
    doc = ex(body)
    t = doc.blocks[0]
    assert [[(c.rowspan, c.colspan) for c in r.cells] for r in t.rows] == [[(1, 1), (3, 1)], [(1, 1)], [(1, 1)]]
    assert doc.stories["main"] == ["A", "E", "B", "C"]
    # two sub-tables in one row cannot be converted: they stay nested tables
    two = body.replace('<table:table-cell><text:p>E</text:p></table:table-cell>',
                       '<table:table-cell><table:table table:is-sub-table="true"><table:table-column/><table:table-row>'
                       '<table:table-cell><text:p>X</text:p></table:table-cell></table:table-row></table:table>'
                       '</table:table-cell>')
    t = ex(two).blocks[0]
    assert len(t.rows) == 1 and all(isinstance(c.blocks[0], Table) for c in t.rows[0].cells)


def test_writer_table_grid():
    # cells beyond the declared columns are dropped, a short row is filled by one spanning cell,
    # table:number-rows-repeated is not read by Writer (it reads style:number-rows-repeated)
    body = ('<table:table><table:table-column table:number-columns-repeated="3"/>'
            '<table:table-row><table:table-cell><text:p>a</text:p></table:table-cell></table:table-row>'
            '<table:table-row table:number-rows-repeated="5"><table:table-cell table:number-columns-repeated="4">'
            '<text:p>r</text:p></table:table-cell></table:table-row></table:table>')
    t = ex(body).blocks[0]
    assert [[c.colspan for c in r.cells] for r in t.rows] == [[1, 2], [1, 1, 1]]


def test_block_elements_by_context():
    # page-anchored frames only before the first paragraph; no tables in footnotes;
    # a text:p nested in a paragraph is not imported (its tail is)
    body = ('<draw:frame text:anchor-type="page"><draw:image xlink:href="Pictures/a.png"/></draw:frame>'
            '<text:p>x<text:note><text:note-body><text:p>n</text:p><table:table><table:table-column/>'
            '<table:table-row><table:table-cell><text:p>in note table</text:p></table:table-cell></table:table-row>'
            '</table:table></text:note-body></text:note>1<text:p>nested</text:p>1</text:p>'
            '<draw:frame text:anchor-type="page"><draw:image xlink:href="Pictures/b.png"/></draw:frame>')
    doc = ex(body, extra_files={"Pictures/a.png": H.png(seed=1), "Pictures/b.png": H.png(seed=2)})
    assert doc.stories["main"] == ["x11"]
    assert doc.stories["footnotes"] == ["n"]
    assert [i.data for i in doc.images] == [H.png(seed=1)]


def test_body_other_than_office_text():
    doc = msdoc.extract(H.package({"content.xml": H.content_xml("<text:p>odd body</text:p>", kind="texture")}))
    assert doc.stories["main"] == ["odd body"]


def test_layout_dependent_fields():
    # SwPageNumberField / SwChapterField are expanded by the layout: only the first page is
    # laid out before saving, so the first body page number keeps its value, others show 0
    body = ('<text:h text:outline-level="1">Intro</text:h><text:p>p<text:page-number>1</text:page-number> '
            '<text:chapter text:display="name" text:outline-level="1">Intro</text:chapter>.</text:p>'
            '<text:p>q<text:page-number>7</text:page-number>'
            '<text:page-number text:select-page="next">8</text:page-number></text:p>')
    assert ex(body).stories["main"] == ["Intro", "p1 .", "q0"]


def test_hyperlink_runs():
    # nested links: the inner one wins inside its range; adjacent equal links merge;
    # an empty link stays only in an empty paragraph
    doc = ex('<text:p><text:a xlink:href="u1">a<text:a xlink:href="u2">b</text:a>c</text:a>'
             '<text:a xlink:href="u1">d</text:a><text:a xlink:href="u3"/></text:p>'
             '<text:p><text:a xlink:href="u4"/></text:p>')
    p, q = doc.blocks
    assert [(l.start, l.end, l.url) for l in p.links] == [(0, 1, "u1"), (1, 2, "u2"), (2, 4, "u1")]
    assert [(l.start, l.end, l.url) for l in q.links] == [(0, 0, "u4")]
