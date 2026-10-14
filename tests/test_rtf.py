# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Rich Text Format (.rtf): synthetic documents built in code, no corpus needed."""
import random
import struct
import zlib

import pytest

import msdoc
from msdoc import MsDocError, NotOleFileError, Table
from msdoc.model import IMAGE_MARK, NOTE_MARK


def rtf(body: bytes, head: bytes = b"\\ansi\\deff0{\\fonttbl{\\f0\\fnil Arial;}}") -> bytes:
    return b"{\\rtf1" + head + b"\n" + body + b"}"


def u(n: int) -> bytes:
    """An RTF \\uN control word (kept out of the source text on purpose)."""
    return b"\\" + b"u" + str(n).encode()


def main(data: bytes) -> list:
    return [p for p in msdoc.extract(data).stories["main"] if p]


def png_1x1() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


# ------------------------------------------------------------------ detection

def test_signature_bom_and_whitespace():
    for prefix in (b"", b"\xef\xbb\xbf", b"\r\n  "):
        doc = msdoc.extract(prefix + rtf(b"Hello\\par World"))
        assert doc.format == "rtf"
        assert doc.stories["main"] == ["Hello", "World"]


def test_not_rtf_is_not_ole():
    with pytest.raises(NotOleFileError):
        msdoc.extract(b"{\\rt1 not really}")


def test_path_and_markdown(tmp_path):
    p = tmp_path / "a.rtf"
    p.write_bytes(rtf(b"{\\pard\\outlinelevel0 Title\\par}\\pard Body text\\par"))
    text, media = msdoc.to_markdown(str(p))
    assert text.startswith("# Title") and "Body text" in text and media == []


# ------------------------------------------------------------------ text and encodings

def test_cyrillic_by_ansicpg_and_fcharset():
    doc = rtf(b"\\f1 \\'cf\\'f0\\'e8\\'e2\\'e5\\'f2\\par",
              head=b"\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}{\\f1\\fcharset204 Times;}}")
    assert main(doc) == ["Привет"]
    doc = rtf(b"\\'cf\\'f0\\'e8\\par", head=b"\\ansi\\ansicpg1251")
    assert main(doc) == ["При"]


def test_font_name_suffix_sets_code_page():
    doc = rtf(b"\\f1 \\'e0\\par", head=b"\\ansi{\\fonttbl{\\f1 Arial Cyr;}}")
    assert main(doc) == ["а"]


def test_shift_jis_pairs_hex_and_raw():
    head = b"\\ansi{\\fonttbl{\\f1\\fcharset128 MS Mincho;}}"
    assert main(rtf(b"\\f1 \\'82\\'a0\\par", head)) == ["あ"]
    # a raw Shift-JIS pair whose trail byte is a backslash (0x95 0x5C)
    assert main(rtf(b"\\f1 \x95\\\\par", head)) == ["表"]


def test_unicode_and_uc_skipping():
    body = u(1055) + b"?" + b"\\uc2 " + u(1088) + b"\\'3f\\'3f" + b"\\uc0 " + u(1080) + b"\\par"
    assert main(rtf(body)) == ["При"]
    # negative values and a surrogate pair
    assert main(rtf(u(-3913) + b"?" + u(-10179) + b"?" + u(-8704) + b"?\\par")) == ["\U0001f600"]
    # an unpaired surrogate is dropped
    assert main(rtf(b"a" + u(-10240) + b"? b\\par")) == ["a b"]


def test_symbols_and_special_characters():
    body = (b"a\\~b\\_c\\-d\\{\\}\\\\\\emdash\\endash\\bullet\\lquote\\rquote\\ldblquote\\rdblquote"
            b"\\tab x\\line y\\par")
    assert main(rtf(body)) == ["a b‑c­d{}\\—–•‘’“”\tx\ny"]


def test_symbol_font_maps_to_private_use():
    doc = rtf(b"\\f1 \\'b7\\par", head=b"\\ansi{\\fonttbl{\\f1\\fcharset2 Symbol;}}")
    assert main(doc) == [""]


def test_hex_cr_lf_is_a_paragraph_break():
    assert main(rtf(b"a\\'0db\\par")) == ["a", "b"]


def test_destinations_are_skipped():
    body = (b"{\\info{\\title T}{\\author A}}{\\colortbl;\\red1\\green2\\blue3;}"
            b"{\\stylesheet{\\s1 heading;}}{\\*\\unknowndest secret}{\\*\\bkmkstart bm}{\\xmlnstbl skipped}"
            b"{\\listtext 1.\\tab}{\\pntext 2.\\tab}{\\*\\generator Word;}"
            b"\\unknownword kept\\par")
    assert main(rtf(body)) == ["kept"]


def test_hidden_kept_deleted_dropped():
    assert main(rtf(b"a{\\v hidden}{\\deleted gone}b\\par")) == ["ahiddenb"]


def test_text_after_document_end_is_ignored():
    assert msdoc.extract(rtf(b"in\\par") + b" trailing junk {\\par}").stories["main"] == ["in"]


# ------------------------------------------------------------------ tables

def test_simple_table_and_reading_order():
    body = (b"before\\par\\trowd\\cellx1000\\cellx2000\\pard\\intbl a\\cell b\\cell\\row"
            b"\\trowd\\cellx1000\\cellx2000\\pard\\intbl c\\cell d\\cell\\row\\pard after\\par")
    doc = msdoc.extract(rtf(body))
    assert doc.stories["main"] == ["before", "a", "b", "c", "d", "after"]
    t = doc.blocks[1]
    assert isinstance(t, Table) and len(t.rows) == 2 and [c.blocks[0].text for c in t.rows[1].cells] == ["c", "d"]


def test_row_definition_after_content():
    body = b"\\pard\\intbl a\\cell b\\cell{\\trowd\\cellx1000\\cellx2000\\row}\\pard x\\par"
    doc = msdoc.extract(rtf(body))
    assert [len(r.cells) for r in doc.blocks[0].rows] == [2]


def test_horizontal_and_vertical_merges():
    body = (b"\\trowd\\clmgf\\cellx1000\\clmrg\\cellx2000\\cellx3000\\pard\\intbl ab\\cell\\cell c\\cell\\row"
            b"\\trowd\\clvmgf\\cellx1000\\cellx2000\\cellx3000\\pard\\intbl d\\cell e\\cell f\\cell\\row"
            b"\\trowd\\clvmrg\\cellx1000\\cellx2000\\cellx3000\\pard\\intbl \\cell g\\cell h\\cell\\row")
    t = msdoc.extract(rtf(body)).blocks[0]
    assert [(c.colspan, c.rowspan) for c in t.rows[0].cells] == [(2, 1), (1, 1)]
    assert [(c.colspan, c.rowspan) for c in t.rows[1].cells] == [(1, 2), (1, 1), (1, 1)]
    assert len(t.rows[2].cells) == 2
    md = msdoc.to_markdown(rtf(body)).text
    assert 'colspan="2"' in md and 'rowspan="2"' in md


def test_nested_table():
    body = (b"\\trowd\\cellx5000\\pard\\intbl\\itap1 outer\\par"
            b"\\pard\\intbl\\itap2 x\\nestcell y\\nestcell{\\*\\nesttableprops\\trowd\\cellx1000\\cellx2000\\nestrow}"
            b"{\\nonesttables \\par}\\pard\\intbl\\itap1 \\cell\\row")
    doc = msdoc.extract(rtf(body))
    cell = doc.blocks[0].rows[0].cells[0]
    inner = [b for b in cell.blocks if isinstance(b, Table)]
    assert len(inner) == 1 and [c.blocks[0].text for c in inner[0].rows[0].cells] == ["x", "y"]
    assert doc.stories["main"] == ["outer", "x", "y", ""]


def test_width_after_adds_a_cell():
    body = b"\\trowd\\trwWidthA500\\cellx1000\\pard\\intbl a\\cell\\row"
    assert len(msdoc.extract(rtf(body)).blocks[0].rows[0].cells) == 2


# ------------------------------------------------------------------ lists, headings

LISTS = (b"{\\*\\listtable{\\list\\listid7{\\listlevel\\levelnfc0\\levelstartat1{\\leveltext\\'02\\'00.;}{\\levelnumbers\\'01;}}"
         b"{\\listlevel\\levelnfc4\\levelstartat1{\\leveltext\\'02\\'01);}{\\levelnumbers\\'01;}}}"
         b"{\\list\\listid8{\\listlevel\\levelnfc23{\\leveltext\\'01\\'b7;}}}}"
         b"{\\*\\listoverridetable{\\listoverride\\listid7\\ls1}{\\listoverride\\listid8\\ls2}}")


def test_list_numbers_levels_and_bullets():
    body = (LISTS + b"\\pard\\ls1\\ilvl0 one\\par\\pard\\ls1\\ilvl1 sub\\par\\pard\\ls1\\ilvl0 two\\par"
            b"\\pard\\ls2 bullet\\par\\pard plain\\par")
    bl = msdoc.extract(rtf(body)).blocks
    assert [(p.list_level, p.list_ordered, p.list_number) for p in bl] == [
        (0, True, "1."), (1, True, "a)"), (0, True, "2."), (0, False, None), (None, False, None)]


def test_legacy_paragraph_numbering():
    body = (b"{\\pntext 1.\\tab}{\\*\\pn\\pnlvlbody\\pndec\\pnstart1{\\pntxta .}}one\\par"
            b"\\pard{\\*\\pn\\pnlvlblt{\\pntxtb \\'b7}}bul\\par")
    bl = msdoc.extract(rtf(body)).blocks
    assert (bl[0].list_ordered, bl[0].list_number, bl[0].text) == (True, "1.", "one")
    assert (bl[1].list_level, bl[1].list_ordered) == (0, False)


def test_heading_from_style_outline_level():
    head = b"\\ansi{\\stylesheet{\\s0 Normal;}{\\s1\\outlinelevel1\\sbasedon0 heading 2;}{\\s2\\sbasedon1 Sub;}}"
    bl = msdoc.extract(rtf(b"\\pard\\s2 H\\par\\pard\\s0 N\\par\\pard\\outlinelevel9 B\\par", head)).blocks
    assert [p.heading for p in bl] == [2, 0, 0]


# ------------------------------------------------------------------ fields, notes, stories

def test_hyperlink_field():
    body = (b"see {\\field{\\*\\fldinst {HYPERLINK \"http://example.org/\" \\\\l \"top\"}}{\\fldrslt {link}}} now\\par"
            b"{\\field{\\*\\fldinst HYPERLINK \\\\l \"x\"}{\\fldrslt a\\par b}}\\par")
    doc = msdoc.extract(rtf(body))
    p = doc.blocks[0]
    assert p.text == "see link now"
    assert [(ln.start, ln.end, ln.url) for ln in p.links] == [(4, 8, "http://example.org/#top")]
    assert [ln.url for ln in doc.blocks[1].links] == ["#x"] and [ln.url for ln in doc.blocks[2].links] == ["#x"]
    assert "[link](http://example.org/#top)" in doc.to_markdown()


def test_computed_fields():
    body = (b"{\\field{\\*\\fldinst SYMBOL 65 \\\\f \"Arial\"}{\\fldrslt x}}"
            b"{\\field{\\*\\fldinst TITLE}}{\\field{\\*\\fldinst FORMCHECKBOX}{\\fldrslt yy}}\\par"
            b"{\\header {\\field{\\*\\fldinst PAGE}{\\fldrslt 7}}\\par}")
    doc = msdoc.extract(rtf(body, head=b"\\ansi{\\info{\\title Doc}}"))
    assert doc.stories["main"] == ["ADoc"]
    assert doc.stories["headers"] == ["1"]


def test_footnotes_endnotes_comments_headers_textboxes():
    body = (b"{\\header Head\\par}{\\footerf First\\par}"
            b"Text{\\super\\chftn}{\\footnote\\pard{\\super\\chftn} note one}"
            b" end{\\footnote\\ftnalt\\pard en}{\\*\\atnid AB}{\\*\\annotation\\pard comment}\\par"
            b"{\\shp{\\*\\shpinst{\\sp{\\sn shapeType}{\\sv 202}}{\\shptxt\\pard box text\\par}}}\\par")
    doc = msdoc.extract(rtf(body))
    assert doc.stories["main"] == ["Text end", ""]
    assert doc.stories["footnotes"] == [" note one"] and doc.stories["endnotes"] == ["en"]
    assert doc.stories["comments"] == ["comment"]
    assert sorted(doc.stories["headers"]) == ["First", "Head"]
    assert doc.stories["textboxes"] == ["box text"]
    p = doc.blocks[0]
    assert p.text.count(NOTE_MARK) == 2 and p.notes == [("footnote", 1), ("endnote", 1)]
    md = doc.to_markdown()
    assert "[^1]" in md and "[^e1]: en" in md


def test_headers_need_a_body():
    assert msdoc.extract(rtf(b"{\\header Head\\par}")).stories["headers"] == []


# ------------------------------------------------------------------ pictures

def test_png_hex_and_bin_and_shppict():
    png = png_1x1()
    hexpng = png.hex().encode()
    body = (b"a{\\pict\\pngblip\\picw1\\pich1 " + hexpng[:20] + b"\r\n" + hexpng[20:] + b"}"
            b"{\\*\\shppict{\\pict\\pngblip\\bin" + str(len(png)).encode() + b" " + png + b"}}"
            b"{\\nonshppict{\\pict\\wmetafile8 0100}}b\\par")
    doc = msdoc.extract(rtf(body))
    assert [im.mime for im in doc.images] == ["image/png", "image/png"]
    assert doc.images[0].data == png and doc.images[1].data == png
    p = doc.blocks[0]
    assert p.text == "a" + IMAGE_MARK * 2 + "b" and p.images == [0, 1]
    text, media = msdoc.to_markdown(rtf(body))
    assert "![Media 0](image:0)" in text and len(media) == 2
    assert msdoc.extract(rtf(body), images_enabled=False).images == []


def test_bad_hex_and_unknown_data_drop_the_picture():
    assert msdoc.extract(rtf(b"{\\pict\\pngblip 89zz}x\\par")).images == []
    assert msdoc.extract(rtf(b"{\\pict\\pngblip 0102030405}x\\par")).images == []


def test_dib_and_wmf_pictures():
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    wmf = struct.pack("<HHHIHIH", 1, 9, 0x300, 12, 0, 3, 0) + struct.pack("<IH", 3, 0)
    body = (b"{\\pict\\dibitmap0 " + dib.hex().encode() + b"}"
            b"{\\pict\\wmetafile8\\picwgoal1440\\pichgoal720 " + wmf.hex().encode() + b"}\\par")
    ims = msdoc.extract(rtf(body)).images
    assert [im.mime for im in ims] == ["image/bmp", "image/x-wmf"]
    assert ims[0].data[:2] == b"BM" and ims[1].data[:4] == b"\xd7\xcd\xc6\x9a"


def test_shape_picture_is_floating_and_fake_pict_is_dropped():
    hexpng = png_1x1().hex().encode()
    body = (b"{\\shp{\\*\\shpinst{\\sp{\\sn shapeType}{\\sv 75}}{\\sp{\\sn pib}{\\sv {\\pict\\pngblip " + hexpng + b"}}}}"
            b"{\\shprslt {\\pict\\pngblip " + hexpng + b"}}}"
            b"{\\pict{\\*\\picprop{\\sp{\\sn shapeType}{\\sv 1}}}\\pngblip " + hexpng + b"}x\\par")
    ims = msdoc.extract(rtf(body)).images
    assert [im.kind for im in ims] == ["floating"]


def test_ole_object_result_picture():
    hexpng = png_1x1().hex().encode()
    obj = b"{\\object\\objemb{\\*\\objdata 0105}{\\result{\\pict\\pngblip " + hexpng + b"}}}"
    assert msdoc.extract(rtf(obj + b"x\\par")).images == []      # replacement graphic of an OLE object
    in_table = b"\\trowd\\cellx1000\\pard\\intbl " + obj + b"\\cell\\row"
    assert len(msdoc.extract(rtf(in_table)).images) == 1        # inside a table only \result is used


# ------------------------------------------------------------------ hostile input

def test_deep_nesting_and_unbalanced_braces():
    deep = b"{\\rtf1 " + b"{" * 200_000 + b"x" + b"}" * 200_000 + b"}"
    assert msdoc.extract(deep).stories["main"] == ["x"]
    assert msdoc.extract(b"{\\rtf1 " + b"{\\b " * 50_000 + b"open").stories["main"] == ["open"]
    assert msdoc.extract(b"{\\rtf1 a}}}} b").stories["main"] == ["a"]


def test_truncated_inputs():
    full = rtf(LISTS + b"\\trowd\\cellx100\\pard\\intbl a\\cell\\row{\\pict\\pngblip 89504e}{\\field{\\*\\fldinst HYPERLINK x}")
    for n in range(len(full)):
        msdoc.extract(full[:n] if full[:n].startswith(b"{\\rtf") else b"{\\rtf1 " + full[:n])


def test_huge_bin_and_long_tokens():
    doc = msdoc.extract(b"{\\rtf1 {\\pict\\pngblip\\bin2147483647 abc}tail\\par}")
    assert doc.images == []
    msdoc.extract(b"{\\rtf1 \\" + b"a" * 100_000 + b" \\u" + b"9" * 100_000 + b" x}")
    msdoc.extract(b"{\\rtf1 \\itap2147483647 a\\nestcell\\nestrow\\cell\\row\\nestrow}")


def test_mutations_only_raise_msdoc_errors():
    hexpng = png_1x1().hex().encode()
    base = rtf(LISTS + b"{\\header h\\par}\\trowd\\clmgf\\cellx100\\clmrg\\cellx200\\pard\\intbl a\\cell b\\cell\\row"
               b"\\pard\\ls1 {\\field{\\*\\fldinst HYPERLINK \"u\"}{\\fldrslt l}}{\\footnote f}"
               b"{\\pict\\pngblip " + hexpng + b"}" + u(1055) + b"?\\'e0\\par",
               head=b"\\ansi\\ansicpg1251{\\fonttbl{\\f0\\fcharset128 M;}}")
    rng = random.Random(4321)
    tokens = [b"{", b"}", b"\\", b"\\par", b"\\cell", b"\\row", b"\\nestrow", b"\\itap3 ", b"\\bin5 ",
              b"\\'", b"{\\*\\fldinst ", b"\\trowd", b"{\\pict ", b"\\uc5 "]
    for i in range(600):
        data = bytearray(base)
        for _ in range(rng.randrange(1, 10)):
            pos = rng.randrange(6, len(data)) if len(data) > 6 else 6
            r = rng.random()
            if r < 0.4 and pos < len(data):
                data[pos] = rng.randrange(256)
            elif r < 0.7:
                data[pos:pos] = rng.choice(tokens)
            elif r < 0.9:
                del data[pos:pos + rng.randrange(1, 40)]
            else:
                data = data[:pos]
        try:
            msdoc.to_markdown(bytes(data))
        except MsDocError:
            pass
