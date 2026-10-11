# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for msdoc.blocks: the plain fallback, and headings/lists built from
the real paragraph/style/list structure of corpus documents."""
import struct
from pathlib import Path

import pytest

import common  # noqa: E402  (oracle/, added to sys.path by conftest.py)
import msdoc
from msdoc import blocks as B
from msdoc.blocks import build_blocks
from msdoc.model import IMAGE_MARK, NOTE_MARK, Document, Image, Paragraph, Table
from msdoc.stories import ParaInfo

CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "c3d3ae0680800703.doc"
LIST_CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "0b67898da842acf5.doc"
IMAGE_HEADING_CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "01760503385e413d.doc"
MULTI_PAR_NOTES_CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "7b1b265dad204bac.doc"
HEADER_CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "0b67898da842acf5.doc"


def _paragraphs(blocks):
    """All Paragraph blocks in document order, recursing into tables."""
    for b in blocks:
        if isinstance(b, Table):
            for row in b.rows:
                for cell in row.cells:
                    yield from _paragraphs(cell.blocks)
        else:
            yield b


def _bare_text(p):
    return p.text.replace(IMAGE_MARK, "")


def test_build_blocks_without_fib_falls_back_to_plain():
    d = Document(stories={"main": ["a", "b"], "footnotes": ["n"]})
    build_blocks(d)
    assert [p.text for p in d.blocks] == ["a", "b"]
    assert all(p.heading == 0 for p in d.blocks)


def test_build_blocks_skips_structure_on_mismatch(monkeypatch):
    import msdoc.blocks as blocks_mod
    from msdoc import stories as stories_mod

    monkeypatch.setattr(stories_mod, "build_main_structure", lambda *a, **k: [("wrong", -1, {}, 0, False)])
    d = Document(stories={"main": ["a", "b"]})
    blocks_mod.build_blocks(d, object(), b"", b"", object(), object())
    assert [p.text for p in d.blocks] == ["a", "b"]
    assert any("structure skipped" in w for w in d.warnings)


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus file not available")
def test_headings_from_real_document():
    from collections import Counter

    doc = msdoc.extract(str(CORPUS))
    paras = list(_paragraphs(doc.blocks))
    headings = [p.heading for p in paras if p.heading]
    # golden/c3d3ae0680800703.json (flattened over tables): 42 headings with
    # this level distribution.
    assert Counter(headings) == Counter({1: 19, 6: 8, 2: 7, 9: 3, 7: 2, 5: 2, 8: 1})
    # Table row-end marks (sprmPFTtp's "extra" empty terminator paragraph,
    # not a real cell) are dropped by table-building, so only non-empty
    # paragraphs are guaranteed to survive in order.
    assert [_bare_text(p) for p in paras if _bare_text(p)] == [t for t in doc.stories["main"] if t]


@pytest.mark.skipif(not IMAGE_HEADING_CORPUS.exists(), reason="corpus file not available")
def test_image_only_paragraph_heading_follows_style_not_content():
    # golden/01760503385e413d.json struct: an image-only paragraph is H3 if
    # its own paragraph style says so and plain (h=0) otherwise -- heading
    # level is never inferred from (or suppressed by) a paragraph holding
    # only a picture: stories.heading_level looks solely at style/sprmPOutLvl
    # (ww8par6.cxx Read_POutLvl), which _apply_headings applies independently
    # of _apply_images. This file has one of each case.
    doc = msdoc.extract(str(IMAGE_HEADING_CORPUS))
    paras = list(_paragraphs(doc.blocks))
    by_image_only = [(p.heading, len(p.images)) for p in paras if p.images and not _bare_text(p).strip()]
    assert (3, 1) in by_image_only
    assert (0, 1) in by_image_only


# ------------------------------------------------------------ lists


class FakeFib:
    def __init__(self, pairs):
        self.pairs = pairs

    def pair(self, name, n):
        return self.pairs.get(name)


def _lvl(nfc, text_len=0):
    """One LVL record ([MS-DOC] LVLF, no grpprlPapx/Chpx, empty number text)."""
    fixed = struct.pack("<iBB9sBiiBBH", 0, nfc, 0, b"\0" * 9, 0, 0, 0, 0, 0, 0)
    assert len(fixed) == 28
    return fixed + struct.pack("<H", text_len) + b"\0" * (2 * text_len)


def _lstf(lsid, simple):
    return struct.pack("<II18sBB", lsid, 0, b"\0" * 18, 1 if simple else 0, 0)


def _lfo(lsid, n_ov):
    out = struct.pack("<I8sB3s", lsid, b"\0" * 8, n_ov, b"\0")
    assert len(out) == 16
    return out


def _lfolvl(level, start_at=False, fmt=False):
    bits = (level & 0x0F) | (0x10 if start_at else 0) | (0x20 if fmt else 0)
    return struct.pack("<iB3s", 0, bits, b"\0")


def test_parse_lists_simple_bullet_list():
    lstf_part = struct.pack("<H", 1) + _lstf(100, simple=True)
    lvl_part = _lvl(23)  # msonfcBullet
    lfo_part = struct.pack("<i", 1) + _lfo(100, 0)
    table = lstf_part + lvl_part + lfo_part
    fib = FakeFib({"PlcfLst": (0, len(lstf_part)), "PlfLfo": (len(lstf_part) + len(lvl_part), len(lfo_part))})
    lists = B._parse_lists(fib, table)
    assert set(lists) == {0}
    assert set(lists[0]) == set(range(9))
    assert all(v.nfc == 23 for v in lists[0].values())


def test_list_info_bullet_vs_number_and_non_list():
    lists = {0: {0: B._LevelInfo(23), 1: B._LevelInfo(0)}}  # level 0 bullet, level 1 numbered
    ilfo1 = struct.pack("<h", 1)
    assert B._list_info({B.SPRM_PILFO: ilfo1}, lists) == (0, False)
    assert B._list_info({B.SPRM_PILFO: ilfo1, B.SPRM_PILVL: b"\x01"}, lists) == (1, True)
    assert B._list_info({}, lists) is None
    assert B._list_info({B.SPRM_PILFO: struct.pack("<h", 0)}, lists) is None
    assert B._list_info({B.SPRM_PILFO: struct.pack("<h", 99)}, lists) is None
    assert B._list_info({B.SPRM_PILFO: struct.pack("<h", 2047)}, lists) is None


def test_parse_lists_override_changes_nfc():
    lstf_part = struct.pack("<H", 1) + _lstf(200, simple=False)
    lvl_part = b"".join(_lvl(0) for _ in range(9))  # all numbered by default
    override_header = _lfolvl(level=2, fmt=True) + _lvl(23)  # level 2 overridden to bullet
    lfo_part = struct.pack("<i", 1) + _lfo(200, 1) + override_header
    table = lstf_part + lvl_part + lfo_part
    fib = FakeFib({"PlcfLst": (0, len(lstf_part)), "PlfLfo": (len(lstf_part) + len(lvl_part), len(lfo_part))})
    lists = B._parse_lists(fib, table)
    assert lists[0][1].nfc == 0 and lists[0][2].nfc == 23


@pytest.mark.skipif(not LIST_CORPUS.exists(), reason="corpus file not available")
def test_lists_from_real_document():
    doc = msdoc.extract(str(LIST_CORPUS))
    paras = list(_paragraphs(doc.blocks))
    n_list = sum(1 for p in paras if p.list_level is not None)
    assert n_list == 341
    assert [_bare_text(p) for p in paras if _bare_text(p)] == [t for t in doc.stories["main"] if t]


@pytest.mark.skipif(not LIST_CORPUS.exists(), reason="corpus file not available")
def test_list_number_rendered_for_every_ordered_item():
    # golden-independent sanity check (list_number isn't part of the
    # struct score): every ordered item gets a label, every bullet gets
    # none, and multilevel labels like "3.5.1" are composed correctly.
    doc = msdoc.extract(str(LIST_CORPUS))
    paras = [p for p in _paragraphs(doc.blocks) if p.list_level is not None]
    assert all(p.list_number is not None for p in paras if p.list_ordered)
    assert all(p.list_number is None for p in paras if not p.list_ordered)
    assert any(p.list_number and p.list_number.count(".") >= 2 for p in paras)


def test_lfolvl_start_at_override_without_full_format():
    lstf_part = struct.pack("<H", 1) + _lstf(300, simple=False)
    lvl_part = b"".join(_lvl(0) for _ in range(9))
    override = struct.pack("<i", 5) + _lfolvl(level=0, start_at=True, fmt=False)[4:]  # iStartAt = 5
    lfo_part = struct.pack("<i", 1) + _lfo(300, 1) + override
    table = lstf_part + lvl_part + lfo_part
    fib = FakeFib({"PlcfLst": (0, len(lstf_part)), "PlfLfo": (len(lstf_part) + len(lvl_part), len(lfo_part))})
    lists = B._parse_lists(fib, table)
    assert lists[0][0].start_at == 5
    assert lists[0][0].nfc == 0  # format not overridden, only start-at


# ------------------------------------------------------------ rendered labels


def test_roman_and_letter_formatting():
    assert B._roman(1994) == "MCMXCIV"
    assert B._roman(9) == "IX"
    assert B._letters(1) == "a" and B._letters(26) == "z" and B._letters(27) == "aa"


def test_format_number_unsupported_nfc_is_none():
    assert B._format_number(3, 0) == "3"
    assert B._format_number(3, 1) == "III"
    assert B._format_number(3, 4) == "c"
    assert B._format_number(3, 6) is None  # cardinalText -- not implemented


def test_render_level_text_simple_and_composite():
    simple = B._LevelInfo(nfc=0, start_at=1, text=chr(0) + ".", offsets=(1,) + (0,) * 8)
    assert B._render_level_text(simple.text, simple.offsets, {0: 3}, {0: 0}) == "3."
    composite = B._LevelInfo(nfc=0, text=chr(0) + "." + chr(1) + ".", offsets=(1, 3) + (0,) * 7)
    assert B._render_level_text(composite.text, composite.offsets, {0: 3, 1: 1}, {0: 0, 1: 0}) == "3.1."


def test_render_level_text_missing_counter_is_none():
    info = B._LevelInfo(nfc=0, text=chr(2) + ".", offsets=(1,) + (0,) * 8)
    assert B._render_level_text(info.text, info.offsets, {0: 1}, {0: 0}) is None


def test_list_label_restarts_deeper_levels_on_shallower_item():
    lvl0 = B._LevelInfo(nfc=0, start_at=1, text="", offsets=())
    lvl1 = B._LevelInfo(nfc=0, start_at=1, text=chr(0) + "." + chr(1), offsets=(1, 3) + (0,) * 7)
    levels = {0: lvl0, 1: lvl1}
    counters: dict = {}
    B._list_label(0, 0, lvl0, levels, counters)        # "1"
    assert B._list_label(0, 1, lvl1, levels, counters) == "1.1"
    assert B._list_label(0, 1, lvl1, levels, counters) == "1.2"
    B._list_label(0, 0, lvl0, levels, counters)         # "2" -- resets level 1
    assert B._list_label(0, 1, lvl1, levels, counters) == "2.1"


def test_list_label_bullet_and_no_number_are_none():
    bullet = B._LevelInfo(nfc=B._NFC_BULLET, text="•")
    none_fmt = B._LevelInfo(nfc=B._NFC_NONE, text="x")
    counters: dict = {}
    assert B._list_label(0, 0, bullet, {0: bullet}, counters) is None
    assert B._list_label(0, 0, none_fmt, {0: none_fmt}, counters) is None


# ------------------------------------------------------------ tables


class FakePieces:
    """mark CP -> "\x07" if it is a cell/row boundary, else a plain "\r"."""

    def __init__(self, cell_marks):
        self.cell_marks = set(cell_marks)

    def text(self, lo, hi):
        return "\x07" if lo in self.cell_marks else "\r"


def _tdeftable_op(ncols, bits_list):
    """sprmTDefTable operand bytes (as returned by props._iter_sprms: the
    2-byte length prefix included) for `ncols` columns with the given TC80
    aBits1 values (bFirstMerged=0x1, bMerged=0x2, bVertMerge=0x20)."""
    centers = struct.pack(f"<{ncols + 1}h", *range(ncols + 1))
    tcs = b"".join(struct.pack("<H", b) + b"\0" * 18 for b in bits_list)
    body = bytes([ncols]) + centers + tcs
    return struct.pack("<H", len(body) + 1) + body


def _run_build_tables(structured, marks07):
    texts = [info.text for info in structured]
    doc = Document(stories={"main": texts})
    doc.blocks = [Paragraph(t) for t in texts]
    B._build_tables(doc, structured, FakePieces(marks07))
    return doc


def test_build_tables_simple_grid():
    structured = [
        ParaInfo("a", 0, {}, 1, False), ParaInfo("b", 1, {}, 1, False), ParaInfo("", 2, {}, 1, True),
        ParaInfo("c", 3, {}, 1, False), ParaInfo("d", 4, {}, 1, False), ParaInfo("", 5, {}, 1, True),
    ]
    doc = _run_build_tables(structured, range(6))
    assert len(doc.blocks) == 1
    t = doc.blocks[0]
    assert isinstance(t, Table) and len(t.rows) == 2
    assert [c.blocks[0].text for c in t.rows[0].cells] == ["a", "b"]
    assert [c.blocks[0].text for c in t.rows[1].cells] == ["c", "d"]


def test_build_tables_horizontal_merge_colspan():
    row_sprms = {B.SPRM_TDEFTABLE: _tdeftable_op(2, [0x0001, 0x0002])}
    structured = [
        ParaInfo("a", 0, {}, 1, False),
        ParaInfo("b", 1, {}, 1, False),
        ParaInfo("", 2, row_sprms, 1, True),
    ]
    doc = _run_build_tables(structured, range(3))
    t = doc.blocks[0]
    assert len(t.rows[0].cells) == 1
    assert t.rows[0].cells[0].colspan == 2
    assert t.rows[0].cells[0].blocks[0].text == "a"


def test_build_tables_vertical_merge_rowspan():
    tdef1 = {B.SPRM_TDEFTABLE: _tdeftable_op(1, [0x0000])}
    tdef2 = {B.SPRM_TDEFTABLE: _tdeftable_op(1, [0x0020])}
    structured = [
        ParaInfo("a", 0, {}, 1, False), ParaInfo("", 1, tdef1, 1, True),
        ParaInfo("x", 2, {}, 1, False), ParaInfo("", 3, tdef2, 1, True),
    ]
    doc = _run_build_tables(structured, range(4))
    t = doc.blocks[0]
    assert len(t.rows) == 2
    assert len(t.rows[0].cells) == 1 and t.rows[0].cells[0].rowspan == 2
    assert t.rows[0].cells[0].blocks[0].text == "a"
    assert len(t.rows[1].cells) == 0


def test_build_tables_header_row():
    structured = [
        ParaInfo("a", 0, {}, 1, False),
        ParaInfo("", 1, {B.SPRM_TTABLEHEADER: b"\x01"}, 1, True),
    ]
    doc = _run_build_tables(structured, range(2))
    assert doc.blocks[0].rows[0].header is True


def test_build_tables_nested():
    structured = [
        ParaInfo("n1", 0, {}, 2, False),
        ParaInfo("", 1, {}, 2, True),
        ParaInfo("", 2, {}, 1, False),
        ParaInfo("", 3, {}, 1, True),
    ]
    doc = _run_build_tables(structured, range(4))
    outer = doc.blocks[0]
    assert len(outer.rows) == 1 and len(outer.rows[0].cells) == 1
    cell_a = outer.rows[0].cells[0]
    nested = next(b for b in cell_a.blocks if isinstance(b, Table))
    assert nested.rows[0].cells[0].blocks[0].text == "n1"


def test_build_tables_unterminated_keeps_content():
    structured = [ParaInfo("x", 0, {}, 1, False), ParaInfo("y", 1, {}, 1, False)]
    doc = _run_build_tables(structured, range(2))
    t = doc.blocks[0]
    texts = [p.text for row in t.rows for c in row.cells for p in c.blocks]
    assert texts == ["x", "y"]


# ------------------------------------------------------------ images


def _img(story="main", cp=None):
    return Image(data=b"", mime="image/png", kind="inline", story=story, cp=cp)


def test_apply_images_attaches_mark_and_index():
    structured = [
        ParaInfo("a", 4, {}, 0, False, start=0),
        ParaInfo("b", 9, {}, 0, False, start=5),
    ]
    doc = Document(stories={"main": ["a", "b"]})
    doc.blocks = [Paragraph("a"), Paragraph("b")]
    doc.images = [_img(cp=0), _img(cp=6)]
    B._apply_images(doc, structured)
    assert doc.blocks[0].text == "a" + IMAGE_MARK
    assert doc.blocks[0].images == [0]
    assert doc.blocks[1].text == "b" + IMAGE_MARK
    assert doc.blocks[1].images == [1]


def test_apply_images_multiple_in_one_paragraph_and_non_main_skipped():
    structured = [ParaInfo("a", 9, {}, 0, False)]
    doc = Document(stories={"main": ["a"]})
    doc.blocks = [Paragraph("a")]
    doc.images = [_img(cp=1), _img(cp=5), _img(story="headers", cp=2), _img(cp=None)]
    B._apply_images(doc, structured)
    assert doc.blocks[0].text == "a" + IMAGE_MARK + IMAGE_MARK
    assert doc.blocks[0].images == [0, 1]


def test_apply_images_skips_cp_outside_any_flow_paragraph():
    # CP 5 falls in the gap between paragraph 0 (ending at 2) and paragraph
    # 1 (starting at 6) -- e.g. a frame paragraph excluded from the flow.
    structured = [
        ParaInfo("a", 2, {}, 0, False, start=0),
        ParaInfo("b", 9, {}, 0, False, start=6),
    ]
    doc = Document(stories={"main": ["a", "b"]})
    doc.blocks = [Paragraph("a"), Paragraph("b")]
    doc.images = [_img(cp=5)]
    B._apply_images(doc, structured)
    assert doc.blocks[0].text == "a" and doc.blocks[1].text == "b"
    assert doc.blocks[0].images == [] and doc.blocks[1].images == []


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus file not available")
def test_images_from_real_document():
    doc = msdoc.extract(str(CORPUS))
    paras = list(_paragraphs(doc.blocks))
    n_marks = sum(p.text.count(IMAGE_MARK) for p in paras)
    n_images = sum(len(p.images) for p in paras)
    main_images = [i for i in doc.images if i.story == "main"]
    assert n_marks == n_images == len(main_images)


# ------------------------------------------------------------ links


def test_apply_links_attaches_link_to_paragraph():
    structured = [
        ParaInfo("plain", 5, {}, 0, False, links=()),
        ParaInfo("a link here", 11, {}, 0, False, links=((2, 6, "http://x"),)),
    ]
    doc = Document(stories={"main": ["plain", "a link here"]})
    doc.blocks = [Paragraph("plain"), Paragraph("a link here")]
    B._apply_links(doc, structured)
    assert doc.blocks[0].links == []
    assert len(doc.blocks[1].links) == 1
    link = doc.blocks[1].links[0]
    assert (link.start, link.end, link.url) == (2, 6, "http://x")


def test_apply_links_drops_out_of_range_span():
    structured = [ParaInfo("short", 5, {}, 0, False, links=((0, 99, "http://x"),))]
    doc = Document(stories={"main": ["short"]})
    doc.blocks = [Paragraph("short")]
    B._apply_links(doc, structured)
    assert doc.blocks[0].links == []


# ------------------------------------------------------------ notes


def _plc(cps, cb_data=2):
    """A plain Plc ([MS-DOC] 2.2.2) of the given CPs (no ranges): aCP has
    one trailing sentinel, followed by cb_data bytes of (unused) data per
    real entry."""
    return struct.pack(f"<{len(cps) + 1}i", *cps, 0) + b"\0" * (cb_data * len(cps))


def test_apply_notes_footnote_and_endnote_refs():
    fn_table = _plc([5, 15])
    en_table = _plc([25])
    table = fn_table + en_table
    fib = FakeFib({"PlcffndRef": (0, len(fn_table)), "PlcfendRef": (len(fn_table), len(en_table))})
    structured = [
        ParaInfo("a", 9, {}, 0, False, start=0),
        ParaInfo("b", 19, {}, 0, False, start=10),
        ParaInfo("c", 29, {}, 0, False, start=20),
    ]
    doc = Document(stories={"main": ["a", "b", "c"]})
    doc.blocks = [Paragraph(t) for t in ["a", "b", "c"]]
    doc.notes = {"footnotes": [[], []], "endnotes": [[]]}
    B._apply_notes(doc, structured, fib, table)
    assert doc.blocks[0].notes == [("footnote", 1)] and doc.blocks[0].text == "a" + NOTE_MARK
    assert doc.blocks[1].notes == [("footnote", 2)] and doc.blocks[1].text == "b" + NOTE_MARK
    assert doc.blocks[2].notes == [("endnote", 1)] and doc.blocks[2].text == "c" + NOTE_MARK


def test_apply_notes_clamps_refs_beyond_extracted_notes():
    # PlcffndRef claims 2 references, but Document.notes only has 1 real
    # footnote (seen on a corrupted ww8 crash-regression test file): the
    # extra reference must not be attached to anything.
    fn_table = _plc([5, 15])
    fib = FakeFib({"PlcffndRef": (0, len(fn_table)), "PlcfendRef": None})
    structured = [ParaInfo("a", 9, {}, 0, False, start=0), ParaInfo("b", 19, {}, 0, False, start=10)]
    doc = Document(stories={"main": ["a", "b"]})
    doc.blocks = [Paragraph(t) for t in ["a", "b"]]
    doc.notes = {"footnotes": [[]], "endnotes": []}
    B._apply_notes(doc, structured, fib, fn_table)
    assert doc.blocks[0].notes == [("footnote", 1)]
    assert doc.blocks[1].notes == []


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus file not available")
def test_tables_from_real_document():
    doc = msdoc.extract(str(CORPUS))
    tables = [b for b in doc.blocks if isinstance(b, Table)]
    # golden/c3d3ae0680800703.json has 5 tables (T13x5, T13x5, T13x4, T13x4,
    # T16x3); stories.build_main_structure (unchanged by this step) only
    # reports table depth > 0 for 4 of them, so the 5th is a pre-existing
    # gap in the table-depth detection, not something table-grouping lost.
    shapes = [(len(t.rows), max((sum(c.colspan for c in r.cells) for r in t.rows), default=0)) for t in tables]
    assert shapes == [(13, 5), (13, 5), (13, 4), (13, 4)]


# ------------------------------------------------------------ note grouping


@pytest.mark.skipif(not MULTI_PAR_NOTES_CORPUS.exists(), reason="corpus file not available")
def test_notes_grouped_by_real_footnote_not_by_paragraph():
    doc = msdoc.extract(str(MULTI_PAR_NOTES_CORPUS))
    notes = doc.notes["footnotes"]
    # plain_blocks would wrongly report one "note" per paragraph (90, the
    # length of stories["footnotes"]); grouped by stories.note_ranges this
    # document has 51 real footnotes, 11 of which span more than one
    # paragraph -- and the paragraphs, concatenated back in order, must
    # still be exactly stories["footnotes"].
    assert len(notes) == 51
    assert sum(1 for n in notes if len(n) > 1) == 11
    assert [p.text for n in notes for p in n] == doc.stories["footnotes"]


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus file not available")
def test_notes_fall_back_to_plain_on_mismatch(monkeypatch):
    from msdoc import stories as stories_mod

    monkeypatch.setattr(stories_mod, "build_note_structure", lambda *a, **k: [(["x"], [])])
    doc = msdoc.extract(str(CORPUS))
    # CORPUS has no footnotes, so stories["footnotes"] == [] != ["x"]: the
    # mismatch must be rejected rather than replacing an empty story with
    # a fabricated one-paragraph note.
    assert doc.notes["footnotes"] == []


# ------------------------------------------------------------ header blocks


@pytest.mark.skipif(not HEADER_CORPUS.exists(), reason="corpus file not available")
def test_header_blocks_preserve_tables_and_text():
    doc = msdoc.extract(str(HEADER_CORPUS))
    tables = [b for b in doc.extra["headers"] if isinstance(b, Table)]
    assert len(tables) == 3
    nonempty = [p.text for p in _paragraphs(doc.extra["headers"]) if p.text]
    assert nonempty == [t for t in doc.stories["headers"] if t]


# ------------------------------------------------------------ textbox blocks


def test_find_sublist():
    assert B._find_sublist([1, 2, 3, 4], [2, 3]) == 1
    assert B._find_sublist([1, 2, 3, 4], [9]) is None
    assert B._find_sublist([1, 2, 1, 2], [1, 2]) is None  # ambiguous
    assert B._find_sublist([], []) is None
    assert B._find_sublist([1], []) is None


TEXTBOX_TABLE_CORPUS = Path(common.data_dir()) / "corpus" / "dev" / "6fbcd400c0c7fcf7.doc"


@pytest.mark.skipif(not TEXTBOX_TABLE_CORPUS.exists(), reason="corpus file not available")
def test_textbox_blocks_recover_tables_without_losing_text():
    doc = msdoc.extract(str(TEXTBOX_TABLE_CORPUS))
    tables = [b for b in doc.extra["textboxes"] if isinstance(b, Table)]
    assert len(tables) == 2
    assert all(len(t.rows) == 3 for t in tables)
    nonempty = [p.text for p in _paragraphs(doc.extra["textboxes"]) if p.text]
    assert nonempty == [t for t in doc.stories["textboxes"] if t]


@pytest.mark.skipif(not HEADER_CORPUS.exists(), reason="corpus file not available")
def test_textbox_blocks_preserve_text_when_no_table_present():
    # this file's textboxes story is pure shape text with no table; the
    # splice must still be found and must not drop or reorder anything.
    doc = msdoc.extract(str(HEADER_CORPUS))
    paras = _paragraphs(doc.extra["textboxes"])
    assert [p.text for p in paras] == doc.stories["textboxes"]
