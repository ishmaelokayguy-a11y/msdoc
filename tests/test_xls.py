# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Excel 97-2003 (.xls): synthetic BIFF8 workbooks, no corpus needed."""
import random
import struct

import pytest

import msdoc
from msdoc import EncryptedDocumentError, MsDocError, UnsupportedFormatError
from test_cfb import CfbBuilder


def rec(rid, data=b""):
    return struct.pack("<HH", rid, len(data)) + data


def xlstr(s, cch_bytes=2):
    head = struct.pack("<H" if cch_bytes == 2 else "<B", len(s))
    return head + (b"\x00" + s.encode("latin-1") if s.isascii() else b"\x01" + s.encode("utf-16-le"))


def xf(ifmt):
    return rec(0x00E0, struct.pack("<HH16x", 0, ifmt))


def workbook(cells=(), extra_sheet=(), sst=("Name", "Привет"), globals_extra=b"", state=0, sst_raw=None):
    """Return the Workbook stream: one sheet "Sheet1" with the given cell records."""
    def build(pos):
        sst_data = struct.pack("<II", len(sst), len(sst)) + b"".join(xlstr(s) for s in sst)
        g = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0, 0, 0, 6))
        g += rec(0x0022, struct.pack("<H", 0))
        g += rec(0x041E, struct.pack("<H", 164) + xlstr('0.00" kg"'))
        g += xf(0) + xf(14) + xf(164) + globals_extra
        g += rec(0x0085, struct.pack("<IBB", pos, state, 0) + xlstr("Sheet1", 1))
        g += (sst_raw if sst_raw is not None else rec(0x00FC, sst_data)) + rec(0x000A)
        return g
    glen = len(build(0))
    s = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0, 0, 0, 6))
    s += b"".join(cells) + b"".join(extra_sheet) + rec(0x000A)
    return build(glen) + s


def label(r, c, isst, ixfe=0):
    return rec(0x00FD, struct.pack("<HHHI", r, c, ixfe, isst))


def number(r, c, v, ixfe=0):
    return rec(0x0203, struct.pack("<HHHd", r, c, ixfe, v))


def boolerr(r, c, val, is_err, ixfe=0):
    return rec(0x0205, struct.pack("<HHHBB", r, c, ixfe, val, is_err))


def as_cfb(stream, name="Workbook"):
    b = CfbBuilder()
    b.add_stream(name, stream)
    return b.build()


def test_cells_numbers_formats_and_merges():
    merge = rec(0x00E5, struct.pack("<HHHHH", 1, 0, 0, 0, 1))
    wb = workbook([
        label(0, 0, 0), label(0, 2, 1),
        number(1, 0, 44927.0, 1), number(1, 1, 2.5, 2), number(1, 2, 3.5),
        boolerr(2, 0, 1, 0), boolerr(2, 1, 0x2A, 1),
    ], [merge])
    doc = msdoc.extract(as_cfb(wb))
    assert doc.format == "xls"
    sh = doc.sheets[0]
    assert sh.name == "Sheet1" and not sh.hidden
    assert sh.cells == {
        (0, 0): "Name", (0, 2): "Привет", (1, 0): "1/1/2023", (1, 1): "2.50 kg", (1, 2): "3.5",
        (2, 0): "1", (2, 1): "#N/A",
    }
    assert sh.merges == [(0, 0, 1, 2)]


def test_sst_string_continued_across_records():
    sst = struct.pack("<II", 1, 1) + struct.pack("<HB", 8, 0) + b"ABCD"
    # one SST split over a CONTINUE record (a flag byte restarts the string data)
    wb = workbook([label(0, 0, 0)], sst_raw=rec(0x00FC, sst) + rec(0x003C, b"\x00EFGH"))
    doc = msdoc.extract(as_cfb(wb))
    assert doc.sheets[0].cells[(0, 0)] == "ABCDEFGH"


def test_markdown_has_sheet_heading_and_table():
    wb = workbook([label(0, 0, 0), number(0, 1, 7.0), number(1, 1, 8.0)])
    md = msdoc.to_markdown(as_cfb(wb)).text
    assert "## Sheet1" in md and "Name" in md and "<table" in md and "<td>8</td>" in md.replace(" ", "")


def test_hidden_sheet_flag():
    wb = workbook([label(0, 0, 0)], state=1)
    doc = msdoc.extract(as_cfb(wb))
    assert doc.sheets[0].hidden and doc.sheets[0].cells[(0, 0)] == "Name"


def test_filepass_is_encrypted():
    wb = workbook([label(0, 0, 0)], globals_extra=rec(0x002F, b"\x00" * 6))
    with pytest.raises(EncryptedDocumentError):
        msdoc.extract(as_cfb(wb))


def test_biff5_book_stream_is_unsupported():
    with pytest.raises(UnsupportedFormatError):
        msdoc.extract(as_cfb(rec(0x0809, struct.pack("<HHHH", 0x0500, 0x0005, 0, 0)), name="Book"))


def test_flat_biff4_is_unsupported():
    with pytest.raises(UnsupportedFormatError):
        msdoc.extract(rec(0x0409, struct.pack("<HHH", 0, 0x0010, 0)) + rec(0x000A))


def test_dense_huge_sheet_does_not_blow_up():
    cells = [number(0, 0, 1.0), number(65535, 255, 2.0)]
    doc = msdoc.extract(as_cfb(workbook(cells)))
    assert doc.sheets[0].cells[(65535, 255)] == "2"
    assert len(msdoc.to_markdown(as_cfb(workbook(cells))).text) < 10_000


def test_mutations_only_raise_msdoc_errors():
    base = workbook([label(0, 0, 0), number(1, 1, 2.5, 2), boolerr(2, 0, 1, 0)],
                    [rec(0x00E5, struct.pack("<HHHHH", 1, 0, 0, 0, 1))])
    rng = random.Random(1234)
    for i in range(250):
        data = bytearray(base)
        if i % 5 == 0:
            data = data[: rng.randrange(0, len(data))]
        else:
            for _ in range(rng.randrange(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            msdoc.to_markdown(as_cfb(bytes(data)))
        except MsDocError:
            pass


def test_boolean_under_date_format_is_general_text():
    # LibreOffice gives BOOLERR cells the Standard format whatever the XF says
    doc = msdoc.extract(as_cfb(workbook([boolerr(0, 0, 1, 0, ixfe=1), boolerr(0, 1, 0, 0, ixfe=2)])))
    assert doc.sheets[0].cells[(0, 0)] in ("1", "TRUE") and doc.sheets[0].cells[(0, 1)] in ("0", "FALSE")


def test_text_cell_under_text_section_format():
    wb = workbook([label(0, 0, 0, ixfe=3)], globals_extra=xf(0))
    assert msdoc.extract(as_cfb(wb)).sheets[0].cells[(0, 0)] == "Name"


def test_error_under_date_format_is_error_text():
    # BOOLERR cells with an error value show the error literal whatever the XF (date) says
    doc = msdoc.extract(as_cfb(workbook([boolerr(0, 0, 0x2A, 1, ixfe=1), boolerr(0, 1, 0x07, 1, ixfe=1)])))
    assert doc.sheets[0].cells[(0, 0)] == "#N/A" and doc.sheets[0].cells[(0, 1)] == "#DIV/0!"


def test_text_cell_under_four_section_format():
    # [MS-XLS] 2.4.126: the fourth section of the number format applies to text
    four = rec(0x041E, struct.pack("<H", 170) + xlstr('0;-0;0;"[" @ "]"'))
    wb = workbook([label(0, 0, 0, ixfe=3)], globals_extra=four + xf(170))
    assert msdoc.extract(as_cfb(wb)).sheets[0].cells[(0, 0)] == "[ Name ]"


# ---------------------------------------------------------------- pictures

def _png(truncated=False):
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = zlib.compress(b"\x00\xff\x00\x00")
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw)
    return out if truncated else out + chunk(b"IEND", b"")


def _eh(ver, inst, rt, body=b""):
    return struct.pack("<HHI", ver | (inst << 4), rt, len(body)) + body


def _group(*pics):
    """MSODRAWINGGROUP record whose BStore holds the given PNG/GIF byte strings."""
    store = b""
    for data in pics:
        blip = _eh(0, 0x6E0, 0xF01E, b"\x00" * 16 + b"\xff" + data)
        store += _eh(2, 6, 0xF007, struct.pack("<BB16sHIIIBBBB", 6, 6, b"", 0, len(blip), 1, 0, 0, 0, 0, 0) + blip)
    return rec(0x00EB, _eh(0xF, 0, 0xF000, _eh(0xF, 1, 0xF001, store)))


def _dg(pib=1, anchor=(3, 0, 5, 0, 5, 0, 7, 0), flags=0xA00):
    """DG container with one picture shape (pib 1-based) and its ClientAnchor/ClientData."""
    c1, dx1, r1, dy1, c2, dx2, r2, dy2 = anchor
    sp = _eh(0xF, 0, 0xF004,
             _eh(2, 75, 0xF00A, struct.pack("<II", 1025, flags))
             + _eh(3, 1, 0xF00B, struct.pack("<HI", 0x4104, pib))
             + _eh(0, 0, 0xF010, struct.pack("<9H", 0, c1, dx1, r1, dy1, c2, dx2, r2, dy2))
             + _eh(0, 0, 0xF011))
    return _eh(0xF, 0, 0xF002, _eh(0, 1, 0xF008, b"\x00" * 8) + _eh(0xF, 0, 0xF003, sp))


def _obj(ot=8, ole_storage=0):
    body = struct.pack("<HHH12x", ot, 1, 0x6011)
    out = struct.pack("<HH", 0x15, len(body)) + body
    if ole_storage:  # ftPictFmla with a ptgTbl link formula = embedded OLE object
        link = struct.pack("<H", 5) + b"\x00" * 4 + b"\x02" + b"\x00" * 4 + b"\x00"
        pf = struct.pack("<H", len(link)) + link + struct.pack("<I", ole_storage)
        out += struct.pack("<HH", 0x09, len(pf)) + pf
    return rec(0x005D, out + struct.pack("<HH", 0, 0))


def _pic_doc(drawing, obj, group=None, storages=None, extra=()):
    from msdoc import xls
    wb = workbook(extra_sheet=[rec(0x00EC, drawing), obj, *extra], globals_extra=group or _group(_png()))
    return xls.parse(wb, storages)


def test_picture_is_reported_at_its_anchor():
    doc = _pic_doc(_dg(), _obj())
    assert [i.mime for i in doc.images] == ["image/png"]
    assert doc.sheets[0].images == [(5, 3, 0)]


@pytest.mark.parametrize("anchor", [
    (3, 0, 5, 40, 5, 0, 5, 40),   # zero height: bottom edge == top edge
    (3, 0, 5, 40, 5, 0, 4, 0),    # negative height
    (3, 100, 5, 0, 3, 50, 7, 0),  # right edge left of the left edge (width <= 0)
])
def test_empty_anchor_picture_is_skipped(anchor):
    # xiescher.cxx XclImpDrawObjBase::IsValidSize
    doc = _pic_doc(_dg(anchor=anchor), _obj())
    assert doc.images == [] and doc.sheets[0].images == []


def test_truncated_png_is_not_reported():
    # LibreOffice drops pictures it cannot decode (emptyAnchor.xls: PNG cut off before IEND)
    assert _pic_doc(_dg(), _obj(), group=_group(_png(truncated=True))).images == []
    assert _pic_doc(_dg(), _obj(), group=_group(b"\x89PNG\r\n\x1a\n\x00")).images == []


def test_blip_validity_checks():
    from msdoc.images import blip_looks_valid
    gif = b"GIF89a" + struct.pack("<HH", 4, 4) + b"\x00\x00\x00"
    assert blip_looks_valid(gif, "image/png")          # content is sniffed, not the BLIP type
    assert not blip_looks_valid(b"GIF89a", "image/gif")
    jpg = b"\xff\xd8\xff\xc0" + struct.pack(">HBHH", 11, 8, 4, 4) + b"\x00" * 8
    assert blip_looks_valid(jpg, "image/jpeg") and not blip_looks_valid(jpg[:6], "image/jpeg")
    assert blip_looks_valid(b"anything else", "image/x-emf")


def test_embedded_ole_picture_is_not_a_picture():
    # XclImpDffConverter::CreateSdrObject(XclImpPictureObj): ptgTbl link + existing MBD storage
    obj = _obj(ole_storage=0x0435D8BE)
    assert _pic_doc(_dg(flags=0xA10), obj, storages={"MBD0435D8BE"}).images == []
    assert len(_pic_doc(_dg(flags=0xA10), obj, storages={"MBD00000001"}).images) == 1
    assert len(_pic_doc(_dg(flags=0xA10), obj, storages=None).images) == 1


def test_continue_after_obj_is_more_drawing_data():
    # ReadMsoDrawing: CONTINUE records following an OBJ continue the DFF stream
    dg = _dg()
    cut = len(dg) - 8 - 8 - 18  # leave out ClientAnchor + ClientData
    doc = _pic_doc(dg[:cut], _obj(), extra=[rec(0x003C, dg[cut:])])
    assert doc.sheets[0].images == [(5, 3, 0)]


def _old_obj(anchor=(1, 0, 2, 0, 4, 0, 6, 0), name=b"", flags=0x0600):
    d = struct.pack("<IHHH", 1, 8, 1, flags) + struct.pack("<8H", *anchor) + struct.pack("<HHHH", 0, 0, len(name), 0)
    d += b"\x00" * 26 + (bytes([len(name)]) + name if name else b"")
    return rec(0x005D, d)


def _imgdata(fmt, payload, extra=b""):
    return rec(0x007F, struct.pack("<HHI", fmt, 1, len(extra + payload)) + extra + payload)


def _dib(w=2, h=2, short=0):
    stride = ((w * 24 + 31) // 32) * 4
    return struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0) + b"\x80" * (stride * h - short)


def _old_doc(*recs):
    from msdoc import xls
    return xls.parse(workbook(extra_sheet=list(recs)))


def test_old_style_obj_with_imgdata_bitmap_is_reported():
    # BIFF5-layout OBJ without preceding MSODRAWING + IMGDATA (OddStyleRecord.xls)
    doc = _old_doc(_old_obj(), _imgdata(9, _dib()))
    assert [i.mime for i in doc.images] == ["image/bmp"] and doc.images[0].data[:2] == b"BM"
    assert doc.sheets[0].images == [(2, 1, 0)]


def test_old_style_obj_wmf_is_reported():
    wmf = struct.pack("<HHHIHIH", 1, 9, 0x0300, 18, 0, 6, 0)
    doc = _old_doc(_old_obj(), _imgdata(2, wmf, extra=b"\x00" * 8))
    assert [i.mime for i in doc.images] == ["image/x-wmf"]


def test_old_style_obj_invalid_is_dropped():
    assert _old_doc(_old_obj(), _imgdata(9, _dib(short=4))).images == []            # short pixel data
    assert _old_doc(_old_obj(anchor=(1, 0, 2, 0, 1, 0, 2, 0)), _imgdata(9, _dib())).images == []  # empty anchor
    assert _old_doc(_old_obj(), _imgdata(2, b"\x00" * 30, extra=b"\x00" * 8)).images == []        # not a WMF
    assert _old_doc(_old_obj()).images == []                                                       # no IMGDATA
    # the page background is a hidden picture named "__BkgndObj" (DoReadObj5), not a picture
    assert _old_doc(_old_obj(name=b"__BkgndObj", flags=0x0700), _imgdata(9, _dib())).images == []
    assert len(_old_doc(_old_obj(name=b"__BkgndObj"), _imgdata(9, _dib())).images) == 1
