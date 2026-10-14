# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""OpenDocument presentation (.odp) extraction on synthetic packages (tests/odf_helpers.py)."""
import base64
import io
import random
import zipfile

import pytest

import msdoc
from msdoc.errors import MsDocError
from msdoc.model import Paragraph, Table

import odf_helpers as H

ODP_MT = "application/vnd.oasis.opendocument.presentation"
NS = H.NSDECL + (
    ' xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'
    ' xmlns:officeooo="http://openoffice.org/2009/office"'
    ' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"'
    ' xmlns:script="urn:oasis:names:tc:opendocument:xmlns:script:1.0"'
    ' xmlns:anim="urn:oasis:names:tc:opendocument:xmlns:animation:1.0"')
LIST_STYLE = ('<text:list-style style:name="L1"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/>'
              '<text:list-level-style-bullet text:level="2" text:bullet-char="–"/></text:list-style>')


def odp(pages: str, auto: str = LIST_STYLE, extra_files=None, mimetype=ODP_MT, master: str = "") -> bytes:
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               f'<office:document-content {NS}><office:automatic-styles>{auto}</office:automatic-styles>'
               f'<office:body><office:presentation>{pages}</office:presentation></office:body>'
               '</office:document-content>')
    styles = ('<?xml version="1.0" encoding="UTF-8"?>'
              f'<office:document-styles {NS}><office:master-styles>{master}</office:master-styles>'
              '</office:document-styles>')
    files = {"content.xml": content, "styles.xml": styles}
    files.update(extra_files or {})
    return H.package(files, mimetype=mimetype)


def page(shapes: str, notes: str = "", attrs: str = "") -> str:
    n = ('<presentation:notes><draw:page-thumbnail draw:page-number="1" presentation:class="page"/>'
         f'<draw:frame presentation:class="notes"><draw:text-box>{notes}</draw:text-box></draw:frame>'
         '</presentation:notes>')
    return f'<draw:page draw:name="p" draw:master-page-name="Default"{attrs}>{shapes}{n}</draw:page>'


def frame(text: str, cls: str = "", extra: str = "") -> str:
    c = f' presentation:class="{cls}"' if cls else ""
    return f'<draw:frame{c}{extra}><draw:text-box>{text}</draw:text-box></draw:frame>'


def ex(pages, **kw):
    return msdoc.extract(odp(pages, **kw))


def test_titles_outline_bullets_markdown():
    outline = ('<text:list text:style-name="L1"><text:list-item><text:p>one</text:p>'
               '<text:list><text:list-item><text:p>one.a</text:p></text:list-item></text:list></text:list-item>'
               '<text:list-item><text:p>two</text:p></text:list-item></text:list>')
    doc = ex(page(frame("<text:p>Title<text:line-break/>line 2</text:p>", "title") + frame(outline, "outline")
                  + frame("<text:p>free  <text:s/>text</text:p><text:p/>")))
    assert doc.format == "odp"
    s = doc.slides[0]
    assert s.index == 1
    assert s.texts == ["Title\nline 2", "one", "one.a", "two", "free  text"]
    items = [(b.text, b.heading, b.list_level) for b in doc.blocks if isinstance(b, Paragraph)]
    assert items == [("Slide 1", 2, None), ("Title line 2", 3, None), ("one", 0, 0), ("one.a", 0, 1),
                     ("two", 0, 0), ("free  text", 0, None)]
    md = doc.to_markdown()
    assert "## Slide 1\n\n### Title line 2\n\n- one\n  - one.a\n- two\n\nfree  text" in md
    assert doc.stories["main"] == s.texts


def test_notes_comments_hidden_slide_and_order():
    pages = (page(frame("<text:p>A</text:p>", "title"), notes="<text:p>note 1</text:p><text:p/><text:p>note 2</text:p>")
             + page(frame("<text:p>B</text:p>"), attrs=' presentation:visibility="hidden"')[:-len("</draw:page>")]
             + '<officeooo:annotation><dc:creator>me</dc:creator><dc:date>2020-01-01T00:00:00</dc:date>'
               '<text:p>c1</text:p><text:p>c2</text:p></officeooo:annotation>'
               '<office:annotation><dc:creator>x</dc:creator><text:p>c3</text:p></office:annotation></draw:page>')
    doc = ex(pages)
    assert [s.index for s in doc.slides] == [1, 2]
    assert doc.slides[0].notes == ["note 1", "note 2"]
    assert doc.slides[0].texts == ["A"]
    assert doc.slides[1].texts == ["B"]
    assert doc.slides[1].comments == ["c1", "c2", "c3"]
    assert doc.stories["main"] == ["A", "note 1", "note 2", "B", "c1", "c2", "c3"]
    md = doc.to_markdown()
    assert "### Notes\n\nnote 1\n\nnote 2" in md and "### Comments\n\nc1" in md


def test_master_page_text_is_not_slide_text():
    master = ('<style:master-page style:name="Default"><draw:frame presentation:class="footer"><draw:text-box>'
              '<text:p>MASTER FOOTER</text:p></draw:text-box></draw:frame></style:master-page>')
    doc = ex(page(frame("<text:p>body</text:p>")), master=master)
    assert doc.slides[0].texts == ["body"]


def test_table_groups_custom_shapes_and_links():
    table = ('<draw:frame><table:table><table:table-column table:number-columns-repeated="2"/>'
             '<table:table-row><table:table-cell><text:p>a1</text:p></table:table-cell>'
             '<table:table-cell><text:p>b1</text:p></table:table-cell></table:table-row>'
             '<table:table-row><table:table-cell table:number-columns-spanned="2"><text:p>a2</text:p></table:table-cell>'
             '<table:covered-table-cell/></table:table-row></table:table>'
             '<draw:image xlink:href="Pictures/TablePreview1.png"/></draw:frame>')
    group = ('<draw:g><draw:custom-shape><text:p>in shape</text:p><draw:enhanced-geometry/></draw:custom-shape>'
             '<draw:g><draw:rect><text:p>deep <text:a xlink:href="http://x.org/">link</text:a></text:p></draw:rect></draw:g>'
             '</draw:g>')
    shape_link = ('<draw:a xlink:href="http://y.org/"><draw:custom-shape><text:p>click me</text:p></draw:custom-shape></draw:a>'
                  '<draw:custom-shape draw:name="btn"><office:event-listeners>'
                  '<presentation:event-listener script:event-name="dom:click" presentation:action="show" xlink:href="#p2"/>'
                  '<presentation:event-listener script:event-name="dom:click" presentation:action="next-page"/>'
                  '</office:event-listeners></draw:custom-shape>')
    doc = ex(page(table + group + shape_link),
             extra_files={"Pictures/TablePreview1.png": H.png(seed=9)})
    s = doc.slides[0]
    assert s.texts == ["a1", "b1", "a2", "in shape", "deep link", "click me"]
    assert s.links == [("link", "http://x.org/"), ("click me", "http://y.org/"), ("btn", "#p2")]
    assert doc.images == []          # the table's preview is not a picture
    t = [b for b in doc.blocks if isinstance(b, Table)][0]
    assert [[c.colspan for c in r.cells] for r in t.rows] == [[1, 1], [2]]
    assert "[link](http://x.org/)" in doc.to_markdown()


def test_pictures_package_base64_dedupe_and_objects():
    b64 = base64.b64encode(H.png(seed=2)).decode()
    pic = '<draw:frame draw:name="{n}"><draw:image xlink:href="{h}"/></draw:frame>'
    pages = (page(pic.format(n="a", h="Pictures/a.png") + pic.format(n="b", h="Pictures/copy.png")
                  + f'<draw:frame><draw:image><office:binary-data>{b64}</office:binary-data></draw:image></draw:frame>'
                  # an embedded object and its replacement graphic: neither text nor picture
                  + '<draw:frame><draw:object xlink:href="./Object 1"/>'
                    '<draw:image xlink:href="./ObjectReplacements/Object 1"/></draw:frame>'
                  # several alternatives: the best one (SVG over PNG) only
                  + '<draw:frame><draw:image xlink:href="Pictures/v.svg" draw:mime-type="image/svg+xml"/>'
                    '<draw:image xlink:href="Pictures/a.png"/></draw:frame>')
             + page(pic.format(n="c", h="Pictures/a.png")))
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
    doc = ex(pages, extra_files={"Pictures/a.png": H.png(seed=1), "Pictures/copy.png": H.png(seed=1),
                                 "Pictures/v.svg": svg, "ObjectReplacements/Object 1": H.png(seed=5)})
    assert [i.data for i in doc.images] == [H.png(seed=1), H.png(seed=2), svg]
    assert doc.slides[0].images == [0, 1, 2]
    assert doc.slides[1].images == [0]
    assert msdoc.extract(odp(pages, extra_files={"Pictures/a.png": H.png(seed=1)}), images_enabled=False).images == []


def test_media_preview_and_frame_first_child_decides():
    pages = page('<draw:frame><draw:plugin xlink:href="Media/v.mp4" draw:mime-type="application/vnd.sun.star.media"/>'
                 '<draw:image xlink:href="Pictures/prev.png"/></draw:frame>'
                 '<draw:frame><draw:text-box><text:p>box</text:p></draw:text-box>'
                 '<draw:image xlink:href="Pictures/ignored.png"/></draw:frame>')
    doc = ex(pages, extra_files={"Pictures/prev.png": H.png(seed=3), "Pictures/ignored.png": H.png(seed=4)})
    assert [i.data for i in doc.images] == [H.png(seed=3)]
    assert doc.slides[0].texts == ["box"]


def test_fields_and_foreign_elements():
    p = ('<text:p>n=<text:page-number>7</text:page-number> c=<text:page-count>3</text:page-count> '
         '<presentation:footer/>d=<text:date text:fixed="true">1.2.2020</text:date></text:p>'
         '<text:p>a <x:wrap xmlns:x="urn:foreign">inner <x:deep>text</x:deep></x:wrap> b</text:p>')
    doc = ex(page(frame(p)))
    assert doc.slides[0].texts == ["n=<number> c=3 d=1.2.2020", "a inner text b"]


def test_empty_presentation_has_one_slide():
    doc = ex("")
    assert len(doc.slides) == 1 and doc.slides[0].texts == []
    assert "## Slide 1" in doc.to_markdown()


def test_template_mimetype():
    doc = msdoc.extract(odp(page(frame("<text:p>t</text:p>")),
                            mimetype="application/vnd.oasis.opendocument.presentation-template"))
    assert doc.format == "odp" and doc.slides[0].texts == ["t"]


def test_hostile_nesting_and_many_shapes():
    deep = "<draw:g>" * 150 + frame("<text:p>deep</text:p>") + "</draw:g>" * 150
    many = frame("<text:p>x</text:p>") * 3000
    doc = ex(page(deep) + page(many) * 3)
    assert len(doc.slides) == 4
    assert doc.slides[1].texts == ["x"] * 3000


def _mutate(data: bytes, rnd: random.Random) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in zin.infolist():
            d = zin.read(info.filename)
            if info.filename == "content.xml":
                d = bytearray(d)
                for _ in range(rnd.randint(1, 6)):
                    k = rnd.randrange(len(d))
                    op = rnd.random()
                    if op < 0.4:
                        d[k] = rnd.randrange(256)
                    elif op < 0.7:
                        del d[k:k + rnd.randint(1, 40)]
                    else:
                        j = rnd.randrange(len(d))
                        d[k:k] = d[j:j + rnd.randint(1, 200)]
                d = bytes(d)
            z.writestr(info.filename, d)
    return out.getvalue()


def test_mutation_fuzz_only_msdoc_errors():
    base = odp(page(frame("<text:p>Title</text:p>", "title")
                    + frame('<text:list text:style-name="L1"><text:list-item><text:p>a <text:a xlink:href="u">b</text:a>'
                            '</text:p></text:list-item></text:list>', "outline")
                    + '<draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame>',
                    notes="<text:p>n</text:p>"),
               extra_files={"Pictures/a.png": H.png(seed=1)})
    rnd = random.Random(1234)
    for _ in range(300):
        try:
            msdoc.extract(_mutate(base, rnd)).to_markdown()
        except MsDocError:
            pass


@pytest.mark.parametrize("junk", [b"", b"PK\x03\x04", b"PK\x03\x04" + b"\0" * 100])
def test_garbage_raises_msdoc_error(junk):
    with pytest.raises(MsDocError):
        msdoc.extract(junk)
