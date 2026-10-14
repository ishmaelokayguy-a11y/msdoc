# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""ODF core (msdoc.odf): package opening, detection, hardened XML, styles, numbering,
pictures, repeats and hostile input. Packages are built in memory (tests/odf_helpers.py)."""
import io
import random
import zipfile

import pytest

import msdoc
from msdoc import odf

import odf_helpers as H


def _text(data):
    return msdoc.extract(data).stories["main"]


# ------------------------------------------------------------------ detection / dispatch
def test_detect_odt_ods_odp_and_templates():
    for mt in ("text", "text-template", "spreadsheet", "presentation-template"):
        full = "application/vnd.oasis.opendocument." + mt
        assert odf.detect(H.package({"content.xml": H.content_xml("")}, mimetype=full)) == full


def test_detect_mimetype_not_first_and_manifest_only():
    data = H.package({"content.xml": H.content_xml("<text:p>x</text:p>")}, mimetype_first=False)
    assert odf.detect(data) == H.TEXT_MT
    assert _text(data) == ["x"]
    # no mimetype entry at all: the manifest's "/" media type decides
    data = H.package({"content.xml": H.content_xml("<text:p>y</text:p>")}, mimetype=None,
                     manifest=H.manifest_xml(H.TEXT_MT))
    assert _text(data) == ["y"]


def test_non_odf_zips_keep_not_ole_error():
    docx = H.package({"[Content_Types].xml": "<Types/>", "word/document.xml": "<w:document/>"},
                     mimetype=None)
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(docx)
    plain = H.package({"a.txt": "hello"}, mimetype=None)
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(plain)
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(H.package({"content.xml": "<x/>"}, mimetype="application/zip"))


def test_other_odf_types_unsupported():
    data = H.package({"content.xml": H.content_xml("", kind="drawing")},
                     mimetype="application/vnd.oasis.opendocument.graphics")
    with pytest.raises(msdoc.UnsupportedFormatError):
        msdoc.extract(data)


@pytest.mark.parametrize("kind", ["spreadsheet", "presentation"])
def test_ods_odp_dispatch(kind):
    data = H.package({"content.xml": H.content_xml("", kind=kind)},
                     mimetype="application/vnd.oasis.opendocument." + kind)
    try:
        doc = msdoc.extract(data)
    except msdoc.UnsupportedFormatError:
        return  # placeholder module
    assert doc.format == {"spreadsheet": "ods", "presentation": "odp"}[kind]


# ------------------------------------------------------------------ encryption
def test_encrypted_manifest():
    enc = ('<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">'
           '<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="AAAA">'
           '<manifest:algorithm manifest:algorithm-name="Blowfish CFB" manifest:initialisation-vector="AAAA"/>'
           '</manifest:encryption-data></manifest:file-entry>')
    data = H.package({"content.xml": b"\x8a\x13\x00garbage"}, manifest=H.manifest_xml(H.TEXT_MT, extra=enc))
    with pytest.raises(msdoc.EncryptedDocumentError):
        msdoc.extract(data)


def test_encrypted_basic_library_is_not_an_encrypted_document():
    enc = ('<manifest:file-entry manifest:full-path="Basic/Lib/Module1.xml" manifest:media-type="text/xml">'
           '<manifest:encryption-data manifest:checksum-type="x" manifest:checksum="AA"/></manifest:file-entry>')
    data = H.package({"content.xml": H.content_xml("<text:p>readable</text:p>"), "Basic/Lib/Module1.xml": b"\x01"},
                     manifest=H.manifest_xml(H.TEXT_MT, extra=enc))
    assert _text(data) == ["readable"]


def test_wholesome_encryption_placeholder():
    # LibreOffice 7.x: only 'encrypted-package' is encrypted, content.xml is a placeholder
    enc = ('<manifest:file-entry manifest:full-path="encrypted-package" manifest:media-type="">'
           '<manifest:encryption-data manifest:checksum-type="x" manifest:checksum="AA"/></manifest:file-entry>')
    data = H.package({"content.xml": H.content_xml(""), "encrypted-package": b"\x01" * 64},
                     manifest=H.manifest_xml(H.TEXT_MT, extra=enc))
    with pytest.raises(msdoc.EncryptedDocumentError):
        msdoc.extract(data)


# ------------------------------------------------------------------ hostile packages
def test_truncated_zip_is_corrupt():
    data = H.odt("<text:p>hello world</text:p>" * 50)
    for cut in (len(data) // 2, len(data) - 30, 200, 60):
        with pytest.raises(msdoc.MsDocError):
            msdoc.extract(data[:cut])
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(data[:len(data) // 2])


def test_zip_bomb_ratio_refused():
    big = "<text:p>" + " " * (20 << 20) + "</text:p>"
    data = H.odt(big)
    assert len(data) < 200_000
    with pytest.raises(msdoc.CorruptDocumentError, match="ratio"):
        msdoc.extract(data)


def test_entry_size_limits(monkeypatch):
    data = H.odt("<text:p>" + "x" * 5000 + "</text:p>")
    monkeypatch.setattr(odf, "MAX_XML_SIZE", 1000)
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(data)
    monkeypatch.undo()
    monkeypatch.setattr(odf, "MAX_TOTAL_SIZE", 2000)
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(data)


def test_too_many_entries(monkeypatch):
    monkeypatch.setattr(odf, "MAX_ENTRIES", 5)
    files = {"content.xml": H.content_xml("<text:p>a</text:p>")}
    files.update({"x%d" % i: "x" for i in range(10)})
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(H.package(files))


def test_path_traversal_names_are_ignored():
    files = {"content.xml": H.content_xml(
        '<text:p><draw:frame><draw:image xlink:href="../evil.png"/></draw:frame>'
        '<draw:frame><draw:image xlink:href="Pictures/../../evil.png"/></draw:frame>t</text:p>'),
        "../evil.png": H.png(), "Pictures/../../evil.png": H.png(seed=3), "/abs.png": H.png()}
    doc = msdoc.extract(H.package(files))
    assert doc.images == []
    assert doc.stories["main"] == ["t"]
    pkg = odf.open_package(H.package(files))
    assert "../evil.png" not in pkg.names() and "/abs.png" not in pkg.names()
    assert any("unsafe" in w for w in pkg.warnings)


def test_zip64_and_data_descriptor():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), H.TEXT_MT, compress_type=zipfile.ZIP_STORED)
        with z.open("content.xml", "w", force_zip64=True) as f:   # streamed: data descriptor + zip64
            f.write(H.content_xml("<text:p>streamed</text:p>").encode())
    assert _text(buf.getvalue()) == ["streamed"]


# ------------------------------------------------------------------ hardened XML
def test_billion_laughs_refused():
    dtd = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
           + "".join('<!ENTITY lol%d "%s">' % (i, ("&lol%d;" % (i - 1) if i else "&lol;") * 10) for i in range(9))
           + ']>')
    xml = dtd + H.content_xml("<text:p>&lol8;</text:p>").split("?>", 1)[1]
    with pytest.raises(msdoc.CorruptDocumentError, match="entit"):
        msdoc.extract(H.package({"content.xml": xml}))


def test_external_entity_not_loaded():
    xml = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>'
           + H.content_xml("<text:p>Test: &ext;</text:p>").split("?>", 1)[1])
    assert _text(H.package({"content.xml": xml})) == ["Test: "]


def test_simple_internal_entity_and_amplification_cap():
    xml = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "ok">]>'
           + H.content_xml("<text:p>&e;</text:p>").split("?>", 1)[1])
    assert _text(H.package({"content.xml": xml})) == ["ok"]
    xml = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "%s">]>' % ("x" * 60000)
           + H.content_xml("<text:p>" + "&e;" * 200 + "</text:p>").split("?>", 1)[1])
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(H.package({"content.xml": xml}))
    xml = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY % p "x">]>'
           + H.content_xml("<text:p>a</text:p>").split("?>", 1)[1])
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(H.package({"content.xml": xml}))


def test_doctype_without_entities_is_fine():
    xml = ('<?xml version="1.0"?><!DOCTYPE office:document-content PUBLIC "-//x//EN" "office.dtd">'
           + H.content_xml("<text:p>ok</text:p>").split("?>", 1)[1])
    assert _text(H.package({"content.xml": xml})) == ["ok"]


def test_deep_nesting_is_flattened():
    n = 100_000
    body = "<text:p>" + "<text:span>" * n + "deep" + "</text:span>" * n + "</text:p>"
    doc = msdoc.extract(H.odt(body))
    assert doc.stories["main"] == ["deep"]
    assert any("flattened" in w for w in doc.warnings)


def test_malformed_xml_is_corrupt():
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(H.package({"content.xml": "<office:document-content><unclosed>"}))
    with pytest.raises(msdoc.CorruptDocumentError):
        msdoc.extract(H.package({"styles.xml": "x"}))   # content.xml missing


def test_parse_xml_helpers():
    root = odf.parse_xml(H.content_xml('<text:p text:style-name="P1">a</text:p>').encode())
    p = odf.find(root, "office:body", "office:text", "text:p")
    assert odf.is_(p, "text:p") and odf.local(p) == "p" and odf.ns_of(p) == odf.NS["text"]
    assert odf.attr(p, "text:style-name") == "P1"
    assert odf.int_attr(p, "text:style-name", 7) == 7
    assert odf.children(odf.find(root, "office:body", "office:text"), "text:p") == [p]


# ------------------------------------------------------------------ styles and numbering
STYLES = ('<style:default-style style:family="paragraph"><style:text-properties fo:font-size="12pt"/></style:default-style>'
          '<style:style style:name="Base" style:family="paragraph" style:default-outline-level="2">'
          '<style:text-properties text:display="none"/></style:style>'
          '<style:style style:name="Child" style:family="paragraph" style:parent-style-name="Base" '
          'style:list-style-name="L9"/>'
          '<style:style style:name="Loop" style:family="paragraph" style:parent-style-name="Loop"/>')


def test_stylesheet_chain_and_lookups():
    content = odf.parse_xml(H.content_xml("", auto_styles='<style:style style:name="P1" style:family="paragraph" '
                                          'style:parent-style-name="Child"/>').encode())
    styles = odf.parse_xml(H.styles_xml(STYLES, auto='<style:style style:name="P1" style:family="paragraph"/>').encode())
    ss = odf.StyleSheet(content, styles)
    assert [s.name for s in ss.chain("paragraph", "P1")][:3] == ["P1", "Child", "Base"]
    assert ss.chain("paragraph", "P1", "styles")[0].parent is None      # same name, other file
    assert ss.outline_level("P1") == 2
    assert ss.paragraph_list_style("P1") == "L9"
    assert ss.text_hidden("paragraph", "P1") is True
    assert ss.lookup("paragraph", "P1", "text-properties", "fo:font-size") == "12pt"
    assert len(ss.chain("paragraph", "Loop")) <= odf.StyleSheet.MAX_CHAIN + 1


def test_format_number():
    f = odf.format_number
    assert [f(n, "1") for n in (1, 10)] == ["1", "10"]
    assert [f(n, "a") for n in (1, 26, 27, 28, 53)] == ["a", "z", "aa", "ab", "ba"]
    assert [f(n, "A", True) for n in (1, 27, 28)] == ["A", "AA", "BB"]
    assert [f(n, "i") for n in (4, 9, 14, 1999)] == ["iv", "ix", "xiv", "mcmxcix"]
    assert f(3, "I") == "III" and f(3, "") == ""


def test_list_tracker_labels():
    ls = odf.ListStyle(odf.parse_xml(
        ('<text:list-style xmlns:text="%s" xmlns:style="%s" xmlns:loext="%s">'
         '<text:list-level-style-number text:level="1" style:num-format="I" style:num-suffix="."/>'
         '<text:list-level-style-number text:level="2" style:num-format="1" text:display-levels="2" style:num-suffix=")"/>'
         '<text:list-level-style-number text:level="3" style:num-format="a" loext:num-list-format="%%1%%-%%3%%"/>'
         '<text:list-level-style-bullet text:level="4" text:bullet-char="*"/>'
         '</text:list-style>' % (odf.NS["text"], odf.NS["style"], odf.NS["loext"])).encode()))
    t = odf.ListTracker()
    lid = t.new_id()
    assert t.count(lid, 1, ls) == "I."
    assert t.count(lid, 2, ls) == "I.1)"
    assert t.count(lid, 2, ls) == "I.2)"
    assert t.count(lid, 3, ls) == "I-a"
    assert t.count(lid, 4, ls) is None
    assert t.count(lid, 1, ls) == "II."
    assert t.count(lid, 2, ls) == "II.1)"          # deeper levels restart
    assert t.count(lid, 1, ls, start=7) == "VII."


# ------------------------------------------------------------------ pictures / repeats
def test_sniff_and_image_bytes():
    assert odf.sniff_image_mime(H.png()) == "image/png"
    assert odf.sniff_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert odf.sniff_image_mime(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>') == "image/svg+xml"
    assert odf.sniff_image_mime(b"VCLMTF\x00") == "image/x-svm"
    assert odf.sniff_image_mime(b"hello") is None
    assert odf.is_external_href("http://x/y.png") and odf.is_external_href("../y.png")
    assert not odf.is_external_href("Pictures/y.png")
    assert odf.package_path("./Pictures/a%20b.png") == "Pictures/a%20b.png"


def test_iter_rows_cells_and_column_defaults():
    t = odf.parse_xml(('<table:table xmlns:table="%s">'
                       '<table:table-column table:number-columns-repeated="3" table:default-cell-style-name="ce1"/>'
                       '<table:table-column/>'
                       '<table:table-header-rows><table:table-row><table:table-cell/></table:table-row></table:table-header-rows>'
                       '<table:table-row-group><table:table-row table:number-rows-repeated="99999">'
                       '<table:table-cell table:number-columns-repeated="5"/><table:covered-table-cell/>'
                       '</table:table-row></table:table-row-group></table:table>' % odf.NS["table"]).encode())
    rows = list(odf.iter_rows(t, cap=100))
    assert [(h, r) for _, h, r in rows] == [(True, 1), (False, 100)]
    cells = list(odf.iter_cells(rows[1][0]))
    assert [(c, r) for _, c, r in cells] == [(False, 5), (True, 1)]
    assert odf.column_default_styles(t) == ["ce1", "ce1", "ce1", None]


# ------------------------------------------------------------------ fuzz
def _seed_doc():
    body = ('<text:h text:outline-level="1">Title</text:h>'
            '<text:p>a<text:s text:c="2"/>b<text:note><text:note-body><text:p>n</text:p></text:note-body></text:note>'
            '<text:a xlink:href="http://x">l</text:a><draw:frame><draw:image xlink:href="Pictures/p.png"/></draw:frame></text:p>'
            '<text:list text:style-name="L1"><text:list-item><text:p>i</text:p></text:list-item></text:list>'
            '<table:table><table:table-row><table:table-cell table:number-columns-spanned="2"><text:p>c</text:p>'
            '</table:table-cell><table:covered-table-cell/></table:table-row></table:table>')
    auto = '<text:list-style style:name="L1"><text:list-level-style-number text:level="1" style:num-format="1"/></text:list-style>'
    return body, auto


def test_mutation_fuzz_package_bytes():
    body, auto = _seed_doc()
    data = H.odt(body, auto, extra_files={"Pictures/p.png": H.png()})
    rnd = random.Random(1234)
    for _ in range(300):
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
    body, auto = _seed_doc()
    xml = H.content_xml(body, auto)
    rnd = random.Random(99)
    alphabet = '<>/="&; :abcdtextpspanlist-1\x00\xff'
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
        data = H.package({"content.xml": "".join(s).encode("utf-8", "replace"), "Pictures/p.png": H.png()})
        try:
            msdoc.extract(data)
        except msdoc.MsDocError:
            pass
