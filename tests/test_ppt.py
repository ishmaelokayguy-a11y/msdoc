# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""PowerPoint 97-2003 (.ppt): synthetic presentations, no corpus needed."""
import random
import struct

import pytest

import msdoc
from msdoc import EncryptedDocumentError, MsDocError, UnsupportedFormatError
from test_cfb import CfbBuilder


def rec(rtype, data=b"", ver=0, inst=0):
    return struct.pack("<HHI", ver | (inst << 4), rtype, len(data)) + data


def cont(rtype, *children, inst=0):
    return rec(rtype, b"".join(children), ver=0xF, inst=inst)


def text_header(kind):
    return rec(0x0F9F, struct.pack("<I", kind))


def chars(s):
    return rec(0x0FA0, s.encode("utf-16-le"))


def bytes_atom(s):
    return rec(0x0FA8, s.encode("latin-1"))


def persist_atom(ref, slide_id):
    return rec(0x03F3, struct.pack("<IIiI", ref, 0, 1, slide_id) + b"\x00" * 4)


def shape(*atoms_in_textbox, pib=None, client_data=None):
    kids = [rec(0xF00A, struct.pack("<II", 0, 0x0A00), ver=2, inst=202)]
    if pib is not None:
        kids.append(rec(0xF00B, struct.pack("<HI", 0x4104, pib), ver=3, inst=1))
    if client_data:
        kids.append(rec(0xF011, b"".join(client_data), ver=0xF))
    if atoms_in_textbox:
        kids.append(cont(0xF00D, *atoms_in_textbox))
    return cont(0xF004, *kids)


def drawing(*shapes):
    return cont(0x040C, cont(0xF002, *shapes))


def presentation(slide_shapes, notes_shapes=None, outline=(), user_edit_extra=b"", token=0xE391C05F, with_comment=None,
                 group=b"", slide_notes_ref=0, notes_sid=0, notes_slide_ref=None, extra_record=None):
    """Return (PowerPoint Document stream, Current User stream).

    persist ids: 1 = document, 2 = slide 1, 3 = notes page of slide 1."""
    slide_id = 256
    slwt0 = cont(0x0FF0, persist_atom(2, slide_id), *outline, inst=0)
    slwt2 = cont(0x0FF0, persist_atom(3, notes_sid), inst=2)
    document = cont(0x03E8, rec(0x03E9, b"\x00" * 40), group, slwt0, slwt2)
    extra = b""
    if with_comment:
        text = rec(0x0FBA, with_comment.encode("utf-16-le"), inst=1)
        comment = cont(0x2EE0, rec(0x0FBA, "Me".encode("utf-16-le")), text, rec(0x2EE1, b"\x00" * 28))
        extra = cont(0x1388, cont(0x138A, rec(0x0FBA, b"_\x00_\x00"), rec(0x138B, comment)))
    slide = cont(0x03EE, rec(0x03EF, b"\x00" * 16 + struct.pack("<I", slide_notes_ref) + b"\x00" * 4), drawing(*slide_shapes), extra)
    notes = cont(0x03F0, rec(0x03F1, struct.pack("<IH", slide_id if notes_slide_ref is None else notes_slide_ref, 0) + b"\x00\x00"), drawing(*(notes_shapes or [])))
    body = document + slide + notes
    off_doc, off_slide = 0, len(document)
    off_notes = off_slide + len(slide)
    offsets = [off_doc, off_slide, off_notes]
    if extra_record is not None:          # persist id 4: e.g. an ExOleObjStg
        offsets.append(len(body))
        body += extra_record
    directory = rec(0x1772, struct.pack("<I", 1 | (len(offsets) << 20)) + struct.pack("<%dI" % len(offsets), *offsets))
    off_dir = len(body)
    off_edit = off_dir + len(directory)
    edit = rec(0x0FF5, struct.pack("<IHBBIIIIHH", 256, 0, 0, 3, 0, off_dir, 1, 4, 1, 0) + user_edit_extra)
    stream = body + directory + edit
    cu_body = struct.pack("<IIIHHBBH", 0x14, token, off_edit, 0, 0x03F4, 3, 0, 0)
    return stream, rec(0x0FF6, cu_body)


def as_cfb(stream, current_user, pictures=None):
    b = CfbBuilder()
    b.add_stream("PowerPoint Document", stream)
    b.add_stream("Current User", current_user)
    if pictures is not None:
        b.add_stream("Pictures", pictures)
    return b.build()


def test_slide_text_notes_and_outline_placeholder():
    title = shape(text_header(0), chars("Quarterly plan"))
    placeholder = shape(rec(0x0F9E, struct.pack("<I", 0)))  # body text lives in the SlideListWithText
    textbox = shape(text_header(4), chars("Free text\rsecond paragraph\x0bsame paragraph"))
    notes = shape(text_header(2), bytes_atom("Say hello"))
    outline = [text_header(1), chars("First point\rSecond point")]
    stream, cu = presentation([title, placeholder, textbox], [notes], outline)
    doc = msdoc.extract(as_cfb(stream, cu))
    assert doc.format == "ppt" and len(doc.slides) == 1
    s = doc.slides[0]
    assert s.texts == ["Quarterly plan", "First point", "Second point", "Free text", "second paragraph\nsame paragraph"]
    assert s.notes == ["Say hello"]
    md = msdoc.to_markdown(as_cfb(stream, cu)).text
    assert "## Slide 1" in md and "### Quarterly plan" in md and "- First point" in md and "Say hello" in md


def test_slide_number_field_is_resolved():
    mc = rec(0x0FD8, struct.pack("<I", 5) + b"\x00" * 4)
    box = shape(text_header(4), chars("Page *"), mc)
    stream, cu = presentation([box])
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["Page 1"]


def test_comment_text():
    stream, cu = presentation([shape(text_header(0), chars("T"))], with_comment="Please check\rthis")
    s = msdoc.extract(as_cfb(stream, cu)).slides[0]
    assert s.comments == ["Please check", "this"]
    assert "Please check" in msdoc.to_markdown(as_cfb(stream, cu)).text


def test_picture_from_pictures_stream():
    import zlib
    png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
           + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) & 0xFFFFFFFF))
    blip = rec(0xF01E, b"\x00" * 16 + b"\xff" + png, inst=0x6E0)   # PNG, one UID
    fbse = rec(0xF007, bytes([6, 6]) + b"\x00" * 16 + struct.pack("<HIIIBBBB", 0, len(blip), 1, 0, 0, 0, 0, 0) + blip,
               ver=2, inst=6)
    group = cont(0x040B, cont(0xF000, cont(0xF001, fbse, inst=1)))
    # a drawing group record is a child of the Document container: rebuild it with the group appended
    stream, cu = presentation([shape(pib=1)], group=group)
    doc = msdoc.extract(as_cfb(stream, cu))
    assert len(doc.images) == 1 and doc.images[0].mime == "image/png"
    assert doc.slides[0].images == [0]


def test_encrypted_current_user_token():
    stream, cu = presentation([shape(text_header(0), chars("x"))], token=0xF3D1C4DF)
    with pytest.raises(EncryptedDocumentError):
        msdoc.extract(as_cfb(stream, cu))


def test_powerpoint_95_is_unsupported():
    stream, cu = presentation([shape(text_header(0), chars("x"))], token=0x12345678)
    with pytest.raises(UnsupportedFormatError):
        msdoc.extract(as_cfb(stream, cu))
    b = CfbBuilder()
    b.add_stream("PP40", b"\x00" * 64)
    with pytest.raises(UnsupportedFormatError):
        msdoc.extract(b.build())


def test_damaged_edit_chain_is_rebuilt_by_scanning():
    stream, cu = presentation([shape(text_header(0), chars("Recovered"))])
    bad_cu = rec(0x0FF6, struct.pack("<IIIHHBBH", 0x14, 0xE391C05F, 0x7FFFFFF0, 0, 0x03F4, 3, 0, 0))
    doc = msdoc.extract(as_cfb(stream, bad_cu))
    assert doc.slides[0].texts == ["Recovered"]
    assert any("edit chain" in w for w in doc.warnings)


def test_images_disabled():
    stream, cu = presentation([shape(pib=1, *[text_header(0), chars("T")])])
    doc = msdoc.extract(as_cfb(stream, cu), images_enabled=False)
    assert doc.images == [] and doc.slides[0].texts == ["T"]


def test_mutations_only_raise_msdoc_errors():
    stream, cu = presentation(
        [shape(text_header(0), chars("Title")), shape(rec(0x0F9E, struct.pack("<I", 0)))],
        [shape(text_header(2), bytes_atom("notes"))], [text_header(1), chars("a\rb")], with_comment="c")
    rng = random.Random(4321)
    for i in range(300):
        data = bytearray(stream)
        if i % 5 == 0:
            data = data[: rng.randrange(0, len(data))]
        else:
            for _ in range(rng.randrange(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            msdoc.to_markdown(as_cfb(bytes(data), cu))
        except MsDocError:
            pass


def opt(*props, complex_data=b""):
    """OPT record; props = (pid, value, is_complex)."""
    body = b"".join(struct.pack("<HI", pid | (0x8000 if cplx else 0), val) for pid, val, cplx in props)
    return rec(0xF00B, body + complex_data, ver=3, inst=len(props))


def raw_shape(*kids):
    return cont(0xF004, rec(0xF00A, struct.pack("<II", 0, 0x0A00), ver=2, inst=202), *kids)


def child_anchor(left, top):
    return rec(0xF00F, struct.pack("<iiii", left, top, left + 10, top + 10))


def cell(left, top, text):
    return raw_shape(child_anchor(left, top), cont(0xF00D, text_header(4), chars(text)))


def table_group(*cells, table=True):
    props = [(927, 3 if table else 0, False)]
    head = raw_shape(rec(0xF122, b"".join(struct.pack("<HI", pid, val) for pid, val, _c in props), ver=3, inst=1))
    return cont(0xF003, head, *cells)


def test_table_cells_are_read_in_row_major_order():
    # the file lists the cells in reverse (z-order); LibreOffice places them by Top / Left
    cells = [cell(20, 10, "b2"), cell(10, 10, "b1"), cell(20, 0, "a2"), cell(10, 0, "a1")]
    stream, cu = presentation([table_group(*cells)])
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["a1", "a2", "b1", "b2"]
    # a plain group keeps the file order
    stream, cu = presentation([table_group(*cells, table=False)])
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["b2", "b1", "a2", "a1"]


def test_wordart_text_from_gtext_unicode():
    word = "Fin\x00".encode("utf-16-le")
    art = raw_shape(opt((0xC0, len(word), True), (0xFF, 0x00004000, False), complex_data=word))
    plain = raw_shape(opt((0xC0, len(word), True), (0xFF, 0, False), complex_data=word))   # fGtext clear
    stream, cu = presentation([art, plain])
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["Fin"]


def test_outline_ref_matches_text_header_instance():
    # the SlideListWithText headers carry the index in recInstance; a reference selects by it
    outline = [rec(0x0F9F, struct.pack("<I", 1), inst=3), chars("third"), rec(0x0F9F, struct.pack("<I", 1), inst=7), chars("seventh")]
    ref = shape(rec(0x0F9E, struct.pack("<I", 7)))
    stream, cu = presentation([ref], outline=outline)
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["seventh"]
    ref = shape(rec(0x0F9E, struct.pack("<I", 1)))      # no recInstance 1: 0-based position
    stream, cu = presentation([ref], outline=outline)
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["seventh"]


def test_notes_follow_slide_atom_notes_id_ref():
    notes = shape(text_header(2), chars("my notes"))
    # NotesAtom.slideIdRef is wrong (0): the link is SlideAtom.notesIdRef == the notes SlidePersistAtom id
    stream, cu = presentation([shape(text_header(0), chars("T"))], [notes], slide_notes_ref=777, notes_sid=777,
                              notes_slide_ref=0)
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].notes == ["my notes"]


def test_text_bytes_are_windows_1252():
    stream, cu = presentation([shape(text_header(0), rec(0x0FA8, b"\x93quoted\x94 \x81"))])
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].texts == ["\u201cquoted\u201d \x81"]


def test_blip_decoded_once_for_shared_offsets():
    import msdoc.ppt as pp
    calls = []
    real = pp.decode_blip
    pp.decode_blip = lambda *a: (calls.append(a[1]), None)[1]
    try:
        fbse = rec(0xF007, bytes([6, 6]) + b"\x00" * 16 + struct.pack("<HIIIBBBB", 0, 8, 1, 0, 0, 0, 0, 0), ver=2, inst=6)
        group = cont(0xF000, cont(0xF001, *([fbse] * 50), inst=50))
        assert pp._bstore(group, b"\x00" * 64) == [None] * 50
    finally:
        pp.decode_blip = real
    assert len(calls) <= 1


# ------------------------------------------------------------------ hyperlinks

def cstr(inst, s):
    return rec(0x0FBA, s.encode("utf-16-le"), inst=inst)


def ex_hyperlink(hid, name, url, location=None):
    kids = [rec(0x0FD3, struct.pack("<I", hid)), cstr(0, name), cstr(1, url)]
    if location is not None:
        kids.append(cstr(3, location))
    return cont(0x0FD7, *kids)


def interactive(hid, action=4):
    atom = rec(0x0FF3, struct.pack("<IIBBBBBBBB", 0, hid, action, 0, 0, 0, 8, 0, 0, 0))
    return cont(0x0FF2, atom)


def tx_range(start, end):
    return rec(0x0FDF, struct.pack("<II", start, end))


def exobjlist(*links):
    return cont(0x0409, *links)


def test_text_run_hyperlink_becomes_markdown_link():
    box = shape(text_header(4), chars("See the site now\rnext"), interactive(7), tx_range(8, 12))
    stream, cu = presentation([box], group=exobjlist(ex_hyperlink(7, "site", "https://example.com/a")))
    doc = msdoc.extract(as_cfb(stream, cu))
    s = doc.slides[0]
    assert s.texts == ["See the site now", "next"]          # flat text unchanged
    assert s.links == [("site", "https://example.com/a")]
    assert "See the [site](https://example.com/a) now" in msdoc.to_markdown(as_cfb(stream, cu)).text


def test_run_link_crossing_paragraphs_and_fields():
    box = shape(text_header(4), chars("ab\r\ncd"), interactive(2), tx_range(1, 5))
    stream, cu = presentation([box], group=exobjlist(ex_hyperlink(2, "n", "mailto:a@b.c")))
    s = msdoc.extract(as_cfb(stream, cu)).slides[0]
    assert s.texts == ["ab", "cd"]
    assert s.links == [("b", "mailto:a@b.c"), ("c", "mailto:a@b.c")]


def test_shape_level_link_is_listed_after_content():
    box = shape(text_header(4), chars("Click me"), client_data=[interactive(3)])
    pic = shape(client_data=[interactive(4)])
    stream, cu = presentation([box, pic], group=exobjlist(
        ex_hyperlink(3, "friendly", "http://x.test/"), ex_hyperlink(4, "Picture link", "file:///c:/a.doc")))
    doc = msdoc.extract(as_cfb(stream, cu))
    assert doc.slides[0].links == [("Click me", "http://x.test/"), ("Picture link", "file:///c:/a.doc")]
    md = msdoc.to_markdown(as_cfb(stream, cu)).text
    assert "### Links" in md and "[Click me](http://x.test/)" in md and "[Picture link](file:///c:/a.doc)" in md


def test_slide_location_link_and_ignored_cases():
    box = shape(text_header(4), chars("go there"), interactive(5), tx_range(0, 2),
                interactive(99), tx_range(3, 8),          # unknown hyperlink id
                interactive(5, action=0), rec(0x0FDF, b"\x00"))   # short range atom
    ptr = shape(text_header(4), chars("x"), client_data=[interactive(5, action=3)])   # not a hyperlink action
    stream, cu = presentation([box, ptr], group=exobjlist(ex_hyperlink(5, "s", "", location="258,2,Next slide")))
    s = msdoc.extract(as_cfb(stream, cu)).slides[0]
    assert s.links == [("go", "#Next slide")]


def test_corrupt_hyperlink_records_only_raise_msdoc_errors():
    base = shape(text_header(4), chars("hello world"), interactive(7), tx_range(2, 99), client_data=[interactive(7)])
    stream, cu = presentation([base], group=exobjlist(ex_hyperlink(7, "a", "http://e/")))
    assert msdoc.extract(as_cfb(stream, cu)).slides[0].links[0][1] == "http://e/"
    rnd = random.Random(7)
    for _ in range(60):
        data = bytearray(stream)
        for _k in range(6):
            data[rnd.randrange(len(data))] = rnd.randrange(256)
        try:
            msdoc.to_markdown(as_cfb(bytes(data), cu))
        except MsDocError:
            pass


# ------------------------------------------------------------------ OLE shapes

def ole_storage_record(clsid_prefix=0x00020803, streams=("\x01Ole", "Workbook", "\x02OlePres000"), corrupt=False):
    import zlib
    b = CfbBuilder()
    for name in streams:
        b.add_stream(name, b"\x01\x00\x00\x02" + b"\x00" * 40)
    raw = b.build()
    z = zlib.compress(raw)
    if corrupt:
        z = z[:2] + bytes(x ^ 0xFF for x in z[2:12]) + z[12:]
    return rec(0x1011, struct.pack("<I", len(raw)) + z)


def ole_list(obj_id, persist_id):
    atom = rec(0x0FC3, struct.pack("<IIIIII", 1, 0, obj_id, 4, persist_id, 0))
    return cont(0x0409, cont(0x0FCC, atom))


def ole_shape(obj_id, pib=1, with_ref=True):
    kids = [rec(0xF00A, struct.pack("<II", 0, 0x0A10), ver=2, inst=202),
            rec(0xF00B, struct.pack("<HIHI", 0x4104, pib, 0x010B, obj_id), ver=3, inst=2)]
    if with_ref:
        kids.append(rec(0xF011, rec(0x0BC1, struct.pack("<I", obj_id)), ver=0xF))
    return cont(0xF004, *kids)


def _picture_group():
    import zlib
    png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
           + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) & 0xFFFFFFFF))
    blip = rec(0xF01E, b"\x00" * 16 + b"\xff" + png, inst=0x6E0)
    fbse = rec(0xF007, bytes([6, 6]) + b"\x00" * 16 + struct.pack("<HIIIBBBB", 0, len(blip), 1, 0, 0, 0, 0, 0) + blip,
               ver=2, inst=6)
    return cont(0x040B, cont(0xF000, cont(0xF001, fbse, inst=1)))


def _ole_images(shape_bytes, storage, obj_id=7):
    stream, cu = presentation([shape_bytes], group=_picture_group() + ole_list(obj_id, 4), extra_record=storage)
    return len(msdoc.extract(as_cfb(stream, cu)).slides[0].images)


def test_ole_shape_picture_follows_importole_rule():
    ok = ole_storage_record()
    # a readable OLE storage: the shape is an OLE object, its replacement graphic is not a picture
    assert _ole_images(ole_shape(7), ok) == 0
    # other storage layouts are OLE objects as well
    assert _ole_images(ole_shape(7), ole_storage_record(streams=("\x01Ole", "Contents"))) == 0
    # corrupt zlib data: ImportOLE fails, the plain picture stays
    assert _ole_images(ole_shape(7), ole_storage_record(corrupt=True)) == 1
    # pictureId matches no ExOleObjAtom: plain picture
    assert _ole_images(ole_shape(99), ok) == 1
    # a storage without the \x01Ole / Contents stream is not an OLE object either
    assert _ole_images(ole_shape(7), ole_storage_record(streams=("Other",))) == 1
    # ExObjRefAtom without pictureId (media object): no picture
    plain_ref = cont(0xF004, rec(0xF00A, struct.pack("<II", 0, 0xA00), ver=2, inst=202),
                     rec(0xF00B, struct.pack("<HI", 0x4104, 1), ver=3, inst=1),
                     rec(0xF011, rec(0x0BC1, struct.pack("<I", 5)), ver=0xF))
    assert _ole_images(plain_ref, ok) == 0
