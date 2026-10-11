# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Portions of this file (the list-definition parsing) are derived from
# LibreOffice sw/source/filter/ww8/ww8par3.cxx (class WW8ListManager) (MPL-2.0).
"""Block view of a document. The plain fallback (every paragraph of each
story becomes a `Paragraph`) is always built first; structure (headings,
lists, tables, image anchors) is added incrementally on top of the main
story and must never lose or reorder its text. Each structural feature is
applied independently and falls back to the plain paragraphs for `doc.blocks`
if it cannot be matched 1:1 against `doc.stories["main"]`.
"""
from __future__ import annotations

import bisect
import struct
from typing import NamedTuple

from . import stories as stories_mod
from .model import IMAGE_MARK, NOTE_MARK, Cell, Link, Paragraph, Row, Table

# [MS-DOC] 2.6.2 sprmPIlfo (ilfo, 1-based LFO index; 0/absent = not a list
# item) and sprmPIlvl (ilvl, 0-based list level). ww8par6.cxx
# Read_LFOPosition / Read_ListLevel.
SPRM_PILFO = 0x460B
SPRM_PILVL = 0x260A
# [MS-DOC] 2.6.3 sprmTTableHeader: the row repeats as a header row on each
# printed page (TAP.fTableHeader).
SPRM_TTABLEHEADER = 0x3404
# ww8par3.cxx Read_LFOPosition: sprmPIlfo value 2047 is a WW6-compatibility
# sentinel for a broken list, not a real 1-based LFO index.
_WW6_BROKEN_LIST_ILFO = 2047

# [MS-DOC] list formatting ("2.4.6.? List Formatting"); sizes ported from
# ww8par3.cxx WW8ListManager::WW8ListManager / ReadLVL.
_LSTF_SIZE = 28      # LSTF: lsid(4) tplc(4) rgistd[9](18) bits1(1) reserved(1)
_LVLF_FIXED_SIZE = 28  # LVLF fixed part, before grpprlPapx/grpprlChpx/Xst
_LFO_SIZE = 16       # LFO: lsid(4) reserved(8) clfolvl(1) reserved(3)
_LFOLVL_SIZE = 8     # LFOLVL: iStartAt(4) bits1(1) reserved(3)
_MAX_LEVEL = 9
_MAX_LISTS = 0x7FFF
_MAX_LFO = 0x7FFF
# [MS-OSHARED] 2.2.1.3 MSONFC: msonfcBullet; "no number" is also rendered
# without a numeral, so both are treated as unordered list markers.
_NFC_BULLET = 23
_NFC_NONE = 255


class _LevelInfo(NamedTuple):
    """One list level's numbering definition: MSONFC `nfc`, the effective
    start-at value, the raw Xst numbering string (with embedded level-index
    placeholder characters), and LVLF.aOfsNumsXCH (the 1-based position of
    each level 0..8's placeholder in `text`, 0 = none, ww8par3.cxx
    ReadLVL's `aOfsNumsXCH` substitution loop)."""
    nfc: int
    start_at: int = 0
    text: str = ""
    offsets: tuple = ()


def _lvl_record(data: bytes, pos: int, end: int):
    """(_LevelInfo, byte length) of one LVL ([MS-DOC] LVLF + grpprlPapx +
    grpprlChpx + the Pascal-string numbering text) at data[pos:end), or
    None if it does not fit."""
    if pos + _LVLF_FIXED_SIZE > end:
        return None
    start_at = struct.unpack_from("<i", data, pos)[0]
    nfc = data[pos + 4]
    offsets = tuple(data[pos + 6:pos + 15])
    cb_chpx = data[pos + 24]
    cb_papx = data[pos + 25]
    p = pos + _LVLF_FIXED_SIZE + cb_papx + cb_chpx
    if p + 2 > end:
        return None
    cch = struct.unpack_from("<H", data, p)[0]
    p += 2
    if p + 2 * cch > end:
        return None
    text = data[p:p + 2 * cch].decode("utf-16-le", "replace")
    p += 2 * cch
    return _LevelInfo(nfc, start_at, text, offsets), p - pos


def _parse_lists(fib, table_stream: bytes) -> dict[int, dict[int, _LevelInfo]]:
    """0-based LFO index -> {ilvl: _LevelInfo}, from PlcfLst (list
    definitions) and PlfLfo (per-use overrides). Never raises; returns {}
    on any structural problem (documents without lists have neither
    stream)."""
    out: dict[int, dict[int, _LevelInfo]] = {}
    try:
        lst = fib.pair("PlcfLst", len(table_stream))
        lfo = fib.pair("PlfLfo", len(table_stream))
        if not lst or not lfo or lst[1] < 2 or lfo[1] < 4:
            return out
        pos, lstf_end = lst
        lstf_end += pos
        n_lists = min(struct.unpack_from("<H", table_stream, pos)[0], _MAX_LISTS)
        pos += 2
        lists = []  # (lsid, simple)
        for _ in range(n_lists):
            if pos + _LSTF_SIZE > lstf_end:
                return out
            lsid = struct.unpack_from("<I", table_stream, pos)[0]
            simple = bool(table_stream[pos + 26] & 1)
            lists.append((lsid, simple))
            pos += _LSTF_SIZE
        # The LVL array for every list follows right after the LSTF array,
        # but (ww8par3.cxx: no length check against m_lcbPlcfLst while
        # reading it) is not counted in lcbPlcfLst: it fills the gap up to
        # fcPlfLfo, where LibreOffice unconditionally seeks next.
        end = lfo[0] if lfo[0] > pos else len(table_stream)
        by_lsid: dict[int, dict[int, _LevelInfo]] = {}
        for lsid, simple in lists:
            levels: dict[int, _LevelInfo] = {}
            for lvl in range(1 if simple else _MAX_LEVEL):
                rec = _lvl_record(table_stream, pos, end)
                if rec is None:
                    return out
                info, size = rec
                levels[lvl] = info
                pos += size
            if simple:
                for lvl in range(1, _MAX_LEVEL):
                    levels[lvl] = levels[0]
            by_lsid[lsid] = levels

        pos, end = lfo
        end += pos
        n_lfo = struct.unpack_from("<i", table_stream, pos)[0]
        pos += 4
        if n_lfo <= 0:
            return out
        n_lfo = min(n_lfo, _MAX_LFO)
        lfos = []  # (lsid, n_overrides)
        for _ in range(n_lfo):
            if pos + _LFO_SIZE > end:
                return out
            lsid = struct.unpack_from("<I", table_stream, pos)[0]
            n_ov = table_stream[pos + 12]
            lfos.append((lsid, n_ov))
            pos += _LFO_SIZE
        for i, (lsid, n_ov) in enumerate(lfos):
            levels = dict(by_lsid.get(lsid, {}))
            if n_ov:
                # ww8par3.cxx comment at i25438/i114412: a variable number of
                # 0xFFFFFFFF filler words precede the override records.
                while pos + 4 <= end and struct.unpack_from("<I", table_stream, pos)[0] == 0xFFFFFFFF:
                    pos += 4
                for _ in range(n_ov):
                    if pos + _LFOLVL_SIZE > end:
                        return out
                    # WW8LFOLVL: iStartAt(i32) then bits1 (level:4,
                    # fStartAt:1, fFormatting:1, ...). iStartAt is only
                    # meaningful when fStartAt is set and fFormatting is
                    # not (ww8par3.cxx struct WW8LFOLVL comment: "if
                    # bFormat==true, the start-at is stored in the LVL").
                    start_at_ov = struct.unpack_from("<i", table_stream, pos)[0]
                    bits1 = table_stream[pos + 4]
                    level = bits1 & 0x0F
                    has_start_at, has_format = bool(bits1 & 0x10), bool(bits1 & 0x20)
                    pos += _LFOLVL_SIZE
                    if has_format:
                        rec = _lvl_record(table_stream, pos, end)
                        if rec is None:
                            return out
                        info, size = rec
                        if level < _MAX_LEVEL:
                            levels[level] = info
                        pos += size
                    elif has_start_at and level < _MAX_LEVEL and level in levels:
                        levels[level] = levels[level]._replace(start_at=start_at_ov)
            out[i] = levels
        return out
    except (struct.error, IndexError):
        return out


def _list_slot(sp: dict, lists: dict[int, dict[int, _LevelInfo]]):
    """(0-based ilfo, ilvl, _LevelInfo) for a paragraph's merged sprms, or
    None if it is not a list item."""
    op = sp.get(SPRM_PILFO)
    if not op or len(op) < 2:
        return None
    ilfo = struct.unpack_from("<h", op, 0)[0]
    if ilfo <= 0 or ilfo == _WW6_BROKEN_LIST_ILFO:
        return None
    levels = lists.get(ilfo - 1)
    if not levels:
        return None
    ilvl_op = sp.get(SPRM_PILVL)
    ilvl = ilvl_op[0] if ilvl_op else 0
    if ilvl >= _MAX_LEVEL:
        ilvl = 0
    info = levels.get(ilvl)
    if info is None:
        return None
    return ilfo - 1, ilvl, info


def _list_info(sp: dict, lists: dict[int, dict[int, _LevelInfo]]):
    """(ilvl, ordered) for a paragraph's merged sprms, or None if it is not
    a list item."""
    slot = _list_slot(sp, lists)
    if slot is None:
        return None
    _ilfo, ilvl, info = slot
    return ilvl, info.nfc not in (_NFC_BULLET, _NFC_NONE)


# ------------------------------------------------------------ rendered list labels

_ROMAN_NUMERALS = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def _roman(n: int) -> str:
    if not 0 < n < 4000:
        return str(n)
    out = []
    for value, sym in _ROMAN_NUMERALS:
        count, n = divmod(n, value)
        out.append(sym * count)
    return "".join(out)


def _letters(n: int) -> str:
    """Bijective base-26 letters (a, b, ..., z, aa, ab, ...), 1-based, as
    Word's lowerLetter/upperLetter numbering formats use."""
    if n <= 0:
        return str(n)
    out = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        out.append(chr(ord("a") + r))
    return "".join(reversed(out))


def _format_number(n: int, nfc: int):
    """Rendered numeral for one of the common MSONFC formats ([MS-OSHARED]
    2.2.1.3), or None for formats not implemented here (text/hex/chicago/
    etc.) -- the caller treats None as "cannot reliably render"."""
    if nfc in (0, 22):  # arabic / arabicLZ-ish decimalZero (no zero-padding here)
        return str(n)
    if nfc == 1:
        return _roman(n)
    if nfc == 2:
        return _roman(n).lower()
    if nfc == 3:
        return _letters(n).upper()
    if nfc == 4:
        return _letters(n)
    return None


def _render_level_text(raw: str, offsets: tuple, counters: dict[int, int], nfcs: dict[int, int]):
    """Substitute each level-number placeholder in a raw LVL numbering
    string with the formatted counter for the level it references
    (ww8par3.cxx ReadLVL: `offsets[i]` is the 1-based position in `raw` of
    level i's placeholder, contiguous from level 0 and 0-terminated; the
    character AT that position is the 0-based level index whose counter
    goes there, which is usually `i` itself but can be an ancestor level
    for composite labels like "1.1."). None if any referenced level's
    counter or number format is unavailable/unsupported."""
    out = list(raw)
    for off in offsets:
        if not off:
            break
        i = off - 1
        if i >= len(out):
            return None
        level = ord(out[i])
        if level not in counters:
            return None
        rendered = _format_number(counters[level], nfcs.get(level, 0))
        if rendered is None:
            return None
        out[i] = rendered
    return "".join(out)


def _list_label(ilfo0: int, ilvl: int, info: _LevelInfo, levels: dict[int, _LevelInfo],
                counters: dict[int, dict[int, int]]):
    """Advance `counters` (per-ilfo, per-level, mutated in place: a new
    item at `ilvl` increments that level's counter and resets every deeper
    level so it restarts from its own start-at next time, the standard
    Word outline-numbering rule) and render this item's label, or None for
    bullets/"no number" items or a label that can't be reliably rendered
    (see _render_level_text)."""
    state = counters.setdefault(ilfo0, {})
    state[ilvl] = state[ilvl] + 1 if ilvl in state else info.start_at
    for deeper in [lvl for lvl in state if lvl > ilvl]:
        del state[deeper]
    if info.nfc in (_NFC_BULLET, _NFC_NONE) or not info.text:
        return None
    nfcs = {lvl: lv.nfc for lvl, lv in levels.items()}
    return _render_level_text(info.text, info.offsets, state, nfcs)


def plain_blocks(stories: dict) -> tuple[list, dict, dict]:
    main = [Paragraph(t) for t in stories.get("main", [])]
    notes = {k: [[Paragraph(t)] for t in stories.get(k, [])] for k in ("footnotes", "endnotes")}
    extra = {k: [Paragraph(t) for t in stories.get(k, [])] for k in ("textboxes", "comments", "headers")}
    return main, notes, extra


def _main_structure(doc, fib, word_document: bytes, table_stream: bytes, pieces, props):
    """stories.build_main_structure's output, cross-checked paragraph-by-
    paragraph against doc.stories["main"] (the oracle-checked text); None if
    they do not match 1:1, so callers can skip structure instead of
    mis-attributing it to the wrong paragraph."""
    structured = stories_mod.build_main_structure(fib, word_document, table_stream, pieces, props)
    main = doc.stories.get("main", [])
    if len(structured) != len(main) or any(p.text != t for p, t in zip(structured, main)):
        doc.warnings.append(
            "blocks: structured main story (%d pars) does not match stories['main'] (%d pars); "
            "structure skipped" % (len(structured), len(main)))
        return None
    return structured


def _headings_into(paras, structured) -> None:
    """heading_level is a pure function of the paragraph's style/sprmPOutLvl
    (ww8par6.cxx Read_POutLvl), independent of the paragraph's content: a
    paragraph whose only content is a picture keeps the heading level its
    style says it has, and gets none otherwise -- there is no "content
    looks like a caption, demote/promote the heading" rule in WW8 import,
    so this applies before and independently of _apply_images."""
    for para, info in zip(paras, structured):
        para.heading = stories_mod.heading_level(info.sprms)


def _apply_headings(doc, structured) -> None:
    _headings_into(doc.blocks, structured)


def _lists_into(paras, structured, lists: dict[int, dict[int, _LevelInfo]]) -> None:
    """list_level/list_ordered for every list-item paragraph, plus
    list_number (Word's rendered label, e.g. "3.", "b)", "IV.") when
    _list_label can compute it; a fresh counters state per call, so
    numbering restarts for each independently-structured run of
    paragraphs (one call per document flow: main, one per header/footer
    story, one per footnote/endnote)."""
    if not lists:
        return
    counters: dict[int, dict[int, int]] = {}
    for para, info in zip(paras, structured):
        slot = _list_slot(info.sprms, lists)
        if slot is None:
            continue
        ilfo0, ilvl, level_info = slot
        para.list_level = ilvl
        para.list_ordered = level_info.nfc not in (_NFC_BULLET, _NFC_NONE)
        para.list_number = _list_label(ilfo0, ilvl, level_info, lists[ilfo0], counters)


def _apply_lists(doc, structured, fib, table_stream: bytes) -> None:
    _lists_into(doc.blocks, structured, _parse_lists(fib, table_stream))


def _flow_ranges(structured):
    """(starts, ends) CP bounds of every flow paragraph, for mapping an
    arbitrary CP to the paragraph that contains it (bisect on `starts`, then
    check `cp <= ends[i]`). stories.ParaInfo.start reflects frame paragraphs
    excluded from the flow too (it is not simply the previous flow
    paragraph's mark + 1), so a CP that falls in such a gap correctly
    matches no paragraph."""
    starts = [info.start for info in structured]
    ends = [info.mark if info.mark >= 0 else (1 << 62) for info in structured]
    return starts, ends


def _flow_paragraph_at(starts, ends, cp: int) -> int:
    """Index into `starts`/`ends` (and the parallel doc.blocks) of the flow
    paragraph containing `cp`, or -1 if none does."""
    i = bisect.bisect_right(starts, cp) - 1
    if i < 0 or i >= len(ends) or cp > ends[i]:
        return -1
    return i


def _apply_images(doc, structured) -> None:
    """Append one IMAGE_MARK to the paragraph anchoring each main-story
    picture, and record its Document.images index in Paragraph.images.

    Exact in-paragraph placement of the mark is not reconstructed (it would
    need the fully marked-up character stream, not just paragraph-level CP
    ranges); the mark is appended at the end of its paragraph's text. Struct
    scoring only counts images per paragraph, so this does not affect it —
    only the cosmetic position of the placeholder in rendered Markdown.
    """
    paras = doc.blocks
    if len(paras) != len(structured):
        return
    starts, ends = _flow_ranges(structured)
    for idx, img in enumerate(doc.images):
        if img.story != "main" or img.cp is None:
            continue
        i = _flow_paragraph_at(starts, ends, img.cp)
        if i < 0:
            continue  # no flow paragraph covers this CP (e.g. inside a frame)
        paras[i].text += IMAGE_MARK
        paras[i].images.append(idx)


def _apply_links(doc, structured) -> None:
    """Paragraph.links from HYPERLINK field results (stories._hyperlink_url
    / ParaInfo.links): a (start, end) character range of `Paragraph.text`
    plus the target URL, one per field."""
    paras = doc.blocks
    if len(paras) != len(structured):
        return
    for para, info in zip(paras, structured):
        for start, end, url in info.links:
            if 0 <= start < end <= len(para.text) and url:
                para.links.append(Link(start, end, url))


def _apply_notes(doc, structured, fib, table_stream: bytes) -> None:
    """Append one NOTE_MARK to the paragraph holding each footnote/endnote
    reference, and record (kind, n) in Paragraph.notes (n is 1-based,
    document order — the index into Document.notes[kind]).

    [MS-DOC] 2.9.? PlcffndRef / PlcfendRef: a plain Plc of the reference CPs
    in the main story, in the same document order as the note text ranges
    in PlcffndTxt/PlcfendTxt (stories.py), so position in this array is
    exactly the 1-based note number.
    """
    paras = doc.blocks
    if len(paras) != len(structured):
        return
    # Clamp to the number of notes actually extracted: a PlcffndRef/
    # PlcfendRef longer than Document.notes (seen on at least one
    # corrupted/crash-regression test file) means the extra references
    # don't point to any real note text; keeping them would attach
    # meaningless (kind, n) pairs with n out of range.
    fn = stories_mod._plc_cps(fib, table_stream, "PlcffndRef", 2)[:-1][:len(doc.notes.get("footnotes", ()))]
    en = stories_mod._plc_cps(fib, table_stream, "PlcfendRef", 2)[:-1][:len(doc.notes.get("endnotes", ()))]
    if not fn and not en:
        return
    refs = sorted(
        [(cp, "footnote", i + 1) for i, cp in enumerate(fn)]
        + [(cp, "endnote", i + 1) for i, cp in enumerate(en)]
    )
    starts, ends = _flow_ranges(structured)
    for cp, kind, n in refs:
        i = _flow_paragraph_at(starts, ends, cp)
        if i < 0:
            continue
        paras[i].text += NOTE_MARK
        paras[i].notes.append((kind, n))


_MAX_TABLE_DEPTH = 64  # matches stories._table_depth's own clamp
# [MS-DOC] 2.9.? TDefTable / TC80 (Ver8, i.e. Word 97+ — this project's
# scope starts at nFib 0x00C1, so the older 10-byte Ver6/7 TC is not read).
# ww8par2.cxx WW8TabBandDesc::ReadDef.
SPRM_TDEFTABLE = 0xD608
_TC_SIZE = 20
_MAX_COLS = 63  # ww8par2.cxx MAX_COL
_TC_FIRST_MERGED = 0x0001
_TC_MERGED = 0x0002
_TC_VERT_MERGE = 0x0020
_TC_VERT_RESTART = 0x0040


def _table_def(sp: dict):
    """(ncols, merge_bits[ncols]) from a row-end paragraph's sprmTDefTable,
    or None. merge_bits[i] is TC80.aBits1 of grid column i (bFirstMerged/
    bMerged/bVertMerge/bVertRestart); the dxaCenter boundaries are not
    needed since colspan/rowspan come directly from the merge flags."""
    op = sp.get(SPRM_TDEFTABLE)
    if not op or len(op) < 2:
        return None
    # props.py's sprm_operand_size special-cases sprmTDefTable's "2-byte
    # count, then count-1 more bytes" length encoding; strip that count.
    body = op[2:]
    if not body:
        return None
    ncols = body[0]
    if ncols <= 0 or ncols > _MAX_COLS:
        return None
    tc_off = 1 + 2 * (ncols + 1)
    bits = []
    for i in range(ncols):
        off = tc_off + i * _TC_SIZE
        bits.append(struct.unpack_from("<H", body, off)[0] if off + 2 <= len(body) else 0)
    return ncols, bits


def _merge_row(raw_cells: list, merge_bits, vmerge_head: dict) -> list:
    """Apply horizontal (bFirstMerged/bMerged) and vertical (bVertMerge)
    merges to one row's raw, 1-per-grid-column cells, in place on
    `vmerge_head` (grid column index -> the open merge's head Cell, carried
    across rows of the same table)."""
    ncols = len(raw_cells)
    if merge_bits is None or len(merge_bits) != ncols:
        return raw_cells
    slots: list = list(raw_cells)
    for i in range(ncols):
        if merge_bits[i] & _TC_VERT_MERGE:
            head = vmerge_head.get(i)
            if head is not None:
                head.rowspan += 1
                slots[i] = None
                continue
        vmerge_head[i] = raw_cells[i]
    out = []
    for i in range(ncols):
        cell = slots[i]
        if cell is None:
            continue
        if (merge_bits[i] & _TC_MERGED) and out:
            out[-1].colspan += 1
        else:
            out.append(cell)
    return out


def _group_into_tables(paras: list, structured, pieces) -> list:
    """Regroup a flat list of Paragraph blocks (1:1 with `structured`) into
    nested Table/Row/Cell blocks, using each paragraph's table depth and
    whether its mark is a cell/row boundary (0x07 — [MS-DOC] 2.3 Special
    Characters; a paragraph mark inside a cell is a plain CR). A row ends
    where sprmPFTtp (depth 1) / sprmPFInnerTtp (depth > 1) is set on the
    mark, exactly as stories._route uses them to route whole rows to
    frames. Colspan/rowspan come from the row-end mark's sprmTDefTable TC
    flags (_merge_row); without a usable sprmTDefTable, a row's actual
    cell count is its width. Shared by main-story table building and by
    the header/footnote/endnote block builders (any flat run of ParaInfo
    with table-depth metadata).
    """
    if len(paras) != len(structured):
        return paras
    top: list = []
    table_rows: list[list[Row]] = []
    row_cells: list[list[Cell]] = []
    cell_blocks: list[list] = []
    row_header: list[bool] = []
    vmerge_head: list[dict] = []

    def container(d: int):
        return top if d == 0 else cell_blocks[d - 1]

    def close_cell(d: int) -> None:
        row_cells[d - 1].append(Cell(blocks=cell_blocks[d - 1]))
        cell_blocks[d - 1] = []

    def close_row(d: int, sp: dict, has_terminator: bool = True) -> None:
        if cell_blocks[d - 1]:
            close_cell(d)
        cells = row_cells[d - 1]
        if has_terminator and len(cells) > 1:
            # [MS-DOC] sprmPFTtp: "part of the TTP extra end-of-row
            # paragraph that marks the end of the row" — every real row has
            # one more 0x07-terminated "cell" than its column count: an
            # empty paragraph carrying the row's TAP (sprmTDefTable, ...).
            # A lone accumulated cell (no 0x07 seen) means sprmPFInTable/
            # sprmPFInnerTtp fired without any real table content (seen on
            # stale/leftover table sprms on ordinary paragraphs); keep it
            # rather than discard the only content there is.
            cells = cells[:-1]
        tdef = _table_def(sp)
        if tdef is not None:
            ncols, bits = tdef
            if ncols == len(cells):
                cells = _merge_row(cells, bits, vmerge_head[d - 1])
        table_rows[d - 1].append(Row(cells=cells, header=row_header[d - 1]))
        row_cells[d - 1] = []
        row_header[d - 1] = False

    def close_table(d: int) -> None:
        if row_cells[d - 1] or cell_blocks[d - 1]:
            close_row(d, {}, has_terminator=False)
        t = Table(rows=table_rows[d - 1])
        table_rows.pop()
        row_cells.pop()
        cell_blocks.pop()
        row_header.pop()
        vmerge_head.pop()
        container(d - 1).append(t)

    cur_depth = 0
    for para, info in zip(paras, structured):
        d = max(0, min(info.depth, _MAX_TABLE_DEPTH))
        while cur_depth > d:
            close_table(cur_depth)
            cur_depth -= 1
        while cur_depth < d:
            cur_depth += 1
            table_rows.append([])
            row_cells.append([])
            cell_blocks.append([])
            row_header.append(False)
            vmerge_head.append({})
        if d == 0:
            top.append(para)
            continue
        cell_blocks[d - 1].append(para)
        is_cell_mark = info.mark >= 0 and pieces.text(info.mark, info.mark + 1) == "\x07"
        if info.row_end and info.sprms.get(SPRM_TTABLEHEADER, b"\0")[:1] not in (b"", b"\0"):
            row_header[d - 1] = True
        if is_cell_mark:
            close_cell(d)
        if info.row_end:
            close_row(d, info.sprms)
    while cur_depth > 0:
        close_table(cur_depth)
        cur_depth -= 1
    return top


def _build_tables(doc, structured, pieces) -> None:
    doc.blocks = _group_into_tables(doc.blocks, structured, pieces)


def _structured_note(flat: list, structured, lists, pieces) -> list:
    """One note's blocks: headings/lists applied and cells grouped into
    tables when the structured (un-flattened) paragraphs match the flat
    text LibreOffice actually shows 1:1 (stories.build_note_structure); a
    table inside the note (LibreOffice flattens those into plain joined
    paragraphs, see stories._flatten_cells) breaks that match, so the
    plain, already-correct flat paragraphs are used instead rather than
    mis-attribute heading/list metadata to the wrong paragraph."""
    texts = [info.text for info in structured]
    if texts != flat:
        return [Paragraph(t) for t in flat]
    paras = [Paragraph(t) for t in flat]
    _headings_into(paras, structured)
    _lists_into(paras, structured, lists)
    return paras


def _build_note_blocks(doc, fib, word_document: bytes, table_stream: bytes, pieces, props) -> None:
    """Replace the plain_blocks grouping of doc.notes (one Paragraph per
    *paragraph*, which over-counts a multi-paragraph note as that many
    notes) with one entry per real footnote/endnote, each a list of
    Paragraph/Table blocks with heading/list structure where available."""
    lists = _parse_lists(fib, table_stream)
    for kind in ("footnotes", "endnotes"):
        pairs = stories_mod.build_note_structure(fib, word_document, table_stream, pieces, props, kind)
        if [t for flat, _ in pairs for t in flat] != doc.stories.get(kind, []):
            continue  # mismatch against the oracle-checked flat story: keep the plain_blocks grouping
        doc.notes[kind] = [_structured_note(flat, structured, lists, pieces) for flat, structured in pairs]


def _build_header_blocks(doc, fib, word_document: bytes, table_stream: bytes, pieces, props) -> None:
    """doc.extra["headers"] with heading/list/table structure, built from
    the same header/footer ranges stories.build_stories uses for
    stories["headers"]; falls back to the plain_blocks paragraphs (already
    in doc.extra["headers"]) if the structured text doesn't match that
    flat story 1:1."""
    ranges = stories_mod.header_ranges(fib, word_document, table_stream)
    if not ranges:
        return
    per_range = stories_mod.build_structured_ranges(fib, word_document, table_stream, pieces, props, ranges)
    structured = [info for rng in per_range for info in rng]
    if [info.text for info in structured] != doc.stories.get("headers", []):
        return
    lists = _parse_lists(fib, table_stream)
    paras = [Paragraph(info.text) for info in structured]
    _headings_into(paras, structured)
    _lists_into(paras, structured, lists)
    doc.extra["headers"] = _group_into_tables(paras, structured, pieces)


_MAX_TEXTBOX_SPLICE_SEARCH = 3000  # bounds the splice search below


def _find_sublist(haystack: list, needle: list):
    """Start index of `needle` as a contiguous run in `haystack` if it
    occurs exactly once, else None. Anchored on needle[0] so the common
    case (few, if any, accidental matches of the first element) is cheap;
    stops as soon as a second occurrence is seen since only uniqueness
    matters to the caller."""
    if not needle:
        return None
    first = needle[0]
    found = None
    for p in range(len(haystack) - len(needle) + 1):
        if haystack[p] == first and haystack[p:p + len(needle)] == needle:
            if found is not None:
                return None  # not unique
            found = p
    return found


def _build_textbox_blocks(doc, fib, word_document: bytes, table_stream: bytes, pieces, props) -> None:
    """doc.extra["textboxes"] with heading/list/table structure for the
    genuinely CP-backed text-box shapes (main-story and header/footer
    PlcftxbxTxt/PlcfHdrtxbxTxt). Everything else stories.build_stories
    also folds into stories["textboxes"] -- frame paragraphs rerouted from
    main/headers (floating tables, APO paragraphs) and synthetic WordArt
    text -- has no per-CP structure to recover, so it stays as the
    plain_blocks paragraphs on either side of the structured run.

    build_stories interleaves all of those sources with no separators, so
    the structured run's position in the flat story isn't tracked; it is
    instead found by matching its flattened text as a contiguous run
    inside stories["textboxes"] with _find_sublist (unambiguous in every
    corpus file this was checked against). Falls back to the existing
    plain paragraphs if that run is empty, not found, or not unique, or
    if the story is too long to search inexpensively.
    """
    story = doc.stories.get("textboxes", [])
    if not story or len(story) > _MAX_TEXTBOX_SPLICE_SEARCH:
        return
    main_ranges = stories_mod.textbox_ranges(fib, table_stream)
    hdr_ranges = stories_mod.header_textbox_ranges(fib, word_document, table_stream)
    if not main_ranges and not hdr_ranges:
        return
    shapes = (stories_mod.build_structured_ranges(fib, word_document, table_stream, pieces, props, main_ranges)
             + stories_mod.build_structured_ranges(fib, word_document, table_stream, pieces, props, hdr_ranges))
    flat = [info.text for shape in shapes for info in shape]
    if not flat:
        return
    start = _find_sublist(story, flat)
    if start is None:
        return
    lists = _parse_lists(fib, table_stream)
    middle: list = []
    for shape in shapes:
        paras = [Paragraph(info.text) for info in shape]
        _headings_into(paras, shape)
        _lists_into(paras, shape, lists)
        middle.extend(_group_into_tables(paras, shape, pieces))
    doc.extra["textboxes"] = ([Paragraph(t) for t in story[:start]] + middle
                              + [Paragraph(t) for t in story[start + len(flat):]])


def build_blocks(doc, fib=None, word_document=None, table_stream=None, pieces=None, props=None) -> None:
    """Fill doc.blocks / doc.notes / doc.extra in place; never raises.

    `fib`/`word_document`/`table_stream`/`pieces`/`props` are the low-level
    structures from msdoc.api.extract; without them only the plain fallback
    is built (used directly by tests and by the markdown-only API).
    """
    try:
        doc.blocks, doc.notes, doc.extra = plain_blocks(doc.stories)
    except Exception as e:  # pragma: no cover
        doc.warnings.append("blocks: %r" % (e,))
        return
    if fib is None or word_document is None or table_stream is None or pieces is None or props is None:
        return
    try:
        structured = _main_structure(doc, fib, word_document, table_stream, pieces, props)
    except Exception as e:
        doc.warnings.append("blocks: structure: %r" % (e,))
        structured = None
    if structured is not None:
        try:
            _apply_headings(doc, structured)
        except Exception as e:
            doc.warnings.append("blocks: headings: %r" % (e,))
        try:
            _apply_lists(doc, structured, fib, table_stream)
        except Exception as e:
            doc.warnings.append("blocks: lists: %r" % (e,))
        try:
            _apply_images(doc, structured)
        except Exception as e:
            doc.warnings.append("blocks: images: %r" % (e,))
        try:
            _apply_links(doc, structured)
        except Exception as e:
            doc.warnings.append("blocks: links: %r" % (e,))
        try:
            _apply_notes(doc, structured, fib, table_stream)
        except Exception as e:
            doc.warnings.append("blocks: notes: %r" % (e,))
        try:
            _build_tables(doc, structured, pieces)
        except Exception as e:
            doc.warnings.append("blocks: tables: %r" % (e,))
    # Footnote/endnote and header/footer structure (headings/lists, tables
    # where LibreOffice keeps them) is independent of the main story's and
    # is attempted even if that one failed to match stories["main"].
    try:
        _build_note_blocks(doc, fib, word_document, table_stream, pieces, props)
    except Exception as e:
        doc.warnings.append("blocks: note structure: %r" % (e,))
    try:
        _build_header_blocks(doc, fib, word_document, table_stream, pieces, props)
    except Exception as e:
        doc.warnings.append("blocks: header structure: %r" % (e,))
    try:
        _build_textbox_blocks(doc, fib, word_document, table_stream, pieces, props)
    except Exception as e:
        doc.warnings.append("blocks: textbox structure: %r" % (e,))
