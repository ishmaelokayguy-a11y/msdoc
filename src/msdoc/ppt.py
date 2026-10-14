# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file follow LibreOffice sd/source/filter/ppt/pptin.cxx and
# filter/source/msfilter/svdfppt.cxx (MPL-2.0).
"""PowerPoint 97-2003 presentations: [MS-PPT] -> Document with slides, notes and pictures.

Read: the "Current User" / UserEditAtom chain and the persist directory (so the
newest version of every slide is used), the slide order of the
SlideListWithText, slide and notes containers (PPDrawing shapes with their
ClientTextbox text, OutlineTextRefAtom placeholders), and the pictures of the
drawing group (BLIPs stored in the "Pictures" stream). When the edit chain is
damaged the persist directories are collected by scanning the stream, as
LibreOffice does. Everything read from the file is untrusted: all loops are
bounded by the stream length.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

from .cfb import CompoundFile
from .errors import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError
from .images import decode_blip
from .model import IMAGE_MARK, Document, Image, Link, Paragraph, Slide

RT_DOCUMENT, RT_SLIDE, RT_NOTES = 0x03E8, 0x03EE, 0x03F0
RT_SLIDE_PERSIST, RT_NOTES_ATOM, RT_SLIDE_ATOM = 0x03F3, 0x03F1, 0x03EF
RT_SLWT, RT_PPDRAWING, RT_PPDRAWINGGROUP = 0x0FF0, 0x040C, 0x040B
RT_TEXT_HEADER, RT_TEXT_CHARS, RT_TEXT_BYTES, RT_OUTLINE_REF = 0x0F9F, 0x0FA0, 0x0FA8, 0x0F9E
RT_USER_EDIT, RT_PERSIST_DIR, RT_EXOBJREF = 0x0FF5, 0x1772, 0x0BC1
RT_COMMENT10, RT_CSTRING, RT_PROGBINARY_DATA = 0x2EE0, 0x0FBA, 0x138B
RT_MC_SLIDENUMBER, RT_MC_DATETIME, RT_MC_GENERICDATE, RT_MC_HEADER, RT_MC_FOOTER = 0x0FD8, 0x0FF7, 0x0FF8, 0x0FF9, 0x0FFA
_MC = (RT_MC_SLIDENUMBER, RT_MC_DATETIME, RT_MC_GENERICDATE, RT_MC_HEADER, RT_MC_FOOTER)
_DGG, _BSTORE, _FBSE, _DG, _SPGR, _SP = 0xF000, 0xF001, 0xF007, 0xF002, 0xF003, 0xF004
_FSP, _OPT, _CLIENT_TEXTBOX, _CLIENT_DATA = 0xF00A, 0xF00B, 0xF00D, 0xF011
RT_EXOBJLIST, RT_EXHYPERLINK, RT_EXHYPERLINK_ATOM = 0x0409, 0x0FD7, 0x0FD3
RT_INTERACTIVE, RT_INTERACTIVE_ATOM, RT_TX_INTERACTIVE = 0x0FF2, 0x0FF3, 0x0FDF
_ACTION_HYPERLINK = 4           # InteractiveInfoAtom.action: hyperlink
_MAX_LINKS = 2000               # hyperlinks / link ranges we are willing to read
_PID_PIB = 0x0104
_PID_OLE_ID = 267          # ESCHER_Prop_pictureId: host-defined id of an OLE object (= ExOleObjAtom.exObjId)
RT_EXEMBED, RT_EXCONTROL, RT_EXOLEOBJATOM, RT_EXOLEOBJSTG = 0x0FCC, 0x0FEE, 0x0FC3, 0x1011
_PID_GTEXT_UNICODE, _PID_GTEXT_BOOLS = 0xC0, 0xFF   # WordArt: gtextUNICODE, gtextFStrikethrough bool group
_PID_TABLE_PROPS = 927          # ESCHER_Prop_tableProperties (UDefProp of the table group shape)
_UDEFPROP, _CHILD_ANCHOR, _CLIENT_ANCHOR = 0xF122, 0xF00F, 0xF010
USER_TOKEN, ENCRYPTED_TOKEN = 0xE391C05F, 0xF3D1C4DF
TX_NOTES = 2
_TITLE_TYPES = (0, 6)
_BODY_TYPES = (1, 5, 7, 8)
_MAX_SLIDES = 20000


def _recs(d: bytes, lo: int, hi: int):
    """Yield (verinst, type, body_start, body_end) of the records in d[lo:hi]."""
    hi = min(hi, len(d))
    pos = lo
    while pos + 8 <= hi:
        vi, rt, ln = struct.unpack_from("<HHI", d, pos)
        yield vi, rt, pos + 8, min(pos + 8 + ln, hi)
        pos += 8 + ln


def _u32(d: bytes, p: int) -> int:
    return struct.unpack_from("<I", d, p)[0] if 0 <= p and p + 4 <= len(d) else 0


# ------------------------------------------------------------------- text

def _cp1252_table() -> dict:
    t = {}
    for b in range(0x80, 0xA0):
        try:
            t[b] = bytes([b]).decode("cp1252")
        except UnicodeDecodeError:
            pass          # 0x81 0x8D 0x8F 0x90 0x9D stay C1 controls
    return t


_CP1252 = _cp1252_table()


def _text(d: bytes, rt: int, b: int, e: int) -> str:
    raw = d[b:e]
    if rt == RT_TEXT_CHARS:
        return raw[: len(raw) // 2 * 2].decode("utf-16-le", "replace")
    # TextBytesAtom: [MS-PPT] says the low bytes of UTF-16 code units; real files hold Windows-1252
    # text and LibreOffice reads it as such (svdfppt.cxx, RTL_TEXTENCODING_MS_1252)
    return raw.decode("latin-1").translate(_CP1252)


# a text block is [text_type, text, outline_ref, fields, header recInstance, link ranges];
# fields = [(char index, record type)], link ranges = [[hyperlink id, start, end]] ([start, end) in chars of the text)
def _feed(blocks: list, d: bytes, rt: int, b: int, e: int, inst: int = 0) -> None:
    """Collect text blocks: a TextHeaderAtom starts one."""
    if rt == RT_TEXT_HEADER:
        blocks.append([_u32(d, b) if e - b >= 4 else 4, None, None, [], inst, []])
    elif rt in (RT_TEXT_CHARS, RT_TEXT_BYTES):
        if not blocks or blocks[-1][1] is not None or blocks[-1][2] is not None:
            blocks.append([4, None, None, [], -1, []])
        blocks[-1][1] = _text(d, rt, b, e)
    elif rt == RT_OUTLINE_REF:
        idx = _u32(d, b) if e - b >= 4 else 0
        if blocks and blocks[-1][1] is None and blocks[-1][2] is None:
            blocks[-1][2] = idx
        else:
            blocks.append([None, None, idx, [], -1, []])
    elif rt in _MC and blocks and e - b >= 4:
        blocks[-1][3].append((_u32(d, b), rt))
    elif rt == RT_INTERACTIVE and blocks:
        # InteractiveInfo (hyperlink id) followed by a TxInteractiveInfoAtom (the character range)
        hid, _action = _interactive(d, b, e)
        if hid and len(blocks[-1][5]) < _MAX_LINKS:
            blocks[-1][5].append([hid, None, None])
    elif rt == RT_TX_INTERACTIVE and blocks and e - b >= 8:
        pend = blocks[-1][5]
        if pend and pend[-1][1] is None:
            pend[-1][1], pend[-1][2] = _u32(d, b), _u32(d, b + 4)


def _interactive(d: bytes, b: int, e: int):
    """(hyperlink id, action) of the InteractiveInfoAtom inside an InteractiveInfo container
    (soundIdRef u32, exHyperlinkIdRef u32, action u8, ...); (0, 0) when absent."""
    for _vi, rt, b2, e2 in _recs(d, b, e):
        if rt == RT_INTERACTIVE_ATOM and e2 - b2 >= 9:
            return _u32(d, b2 + 4), d[b2 + 8]
    return 0, 0


def _outline_block(outline: list, ref: int):
    """The SlideListWithText text block an OutlineTextRefAtom points at. LibreOffice
    (svdfppt.cxx, PPTTextObj) takes the first TextHeaderAtom of the slide whose recInstance
    equals the reference; PowerPoint numbers them 0, 1, 2, ... so the 0-based position is
    the fallback when no recInstance matches."""
    for blk in outline:
        if blk[4] == ref and blk[1] is not None:
            return blk
    return outline[ref] if 0 <= ref < len(outline) else None


def _paragraphs(text: str, fields=(), slide_no: int = 0) -> list[str]:
    """Paragraphs of a text run: fields (slide number, date, header, footer) are resolved,
    \\r ends a paragraph, \\v is a line break."""
    if fields:
        chars = list(text)
        for pos, rt in sorted(fields, reverse=True):
            if 0 <= pos < len(chars):
                chars[pos] = str(slide_no) if rt == RT_MC_SLIDENUMBER else ""
        text = "".join(chars)
    out = []
    for p in text.replace("\r\n", "\r").replace("\n", "\r").split("\r"):
        p = p.replace("\x0b", "\n").replace("\x00", "")
        if p.strip():
            out.append(p)
    return out


def _paragraphs_ranges(text: str, fields, slide_no: int, ranges) -> list:
    """Like _paragraphs, but returns [(paragraph, [(start, end, hyperlink id)])]: the character
    ranges of `text` are mapped onto the emitted paragraphs (a range crossing paragraphs gives
    one span per paragraph). Without ranges it is exactly _paragraphs."""
    ranges = [r for r in ranges if r[1] is not None and r[2] is not None and r[2] > r[1]]
    if not ranges:
        return [(p, []) for p in _paragraphs(text, fields, slide_no)]
    chars = list(text)
    for pos, rt in fields:
        if 0 <= pos < len(chars):
            chars[pos] = str(slide_no) if rt == RT_MC_SLIDENUMBER else ""
    paras: list = [[]]
    contrib: list = []          # per source char: (paragraph number, start, end) or None
    prev_cr = False
    for c in chars:
        if c == "\r" or (c == "\n" and not prev_cr):
            paras.append([])
            contrib.append(None)
            prev_cr = c == "\r"
            continue
        if c == "\n":          # the \n of a \r\n pair
            contrib.append(None)
            prev_cr = False
            continue
        prev_cr = False
        cur = paras[-1]
        start = len(cur)
        for ch in c:
            if ch == "\x00":
                continue
            cur.append("\n" if ch == "\x0b" else ch)
        contrib.append((len(paras) - 1, start, len(cur)) if len(cur) > start else None)
    spans: dict = {}
    for hid, s, e in ranges:
        for i in range(max(0, s), min(e, len(contrib))):
            c = contrib[i]
            if c is not None:
                lo, hi = spans.setdefault((c[0], hid), [c[1], c[2]])
                spans[(c[0], hid)] = [min(lo, c[1]), max(hi, c[2])]
    out = []
    for n, cur in enumerate(paras):
        p = "".join(cur)
        if p.strip():
            out.append((p, sorted((lo, hi, hid) for (pn, hid), (lo, hi) in spans.items()
                                  if pn == n and p[lo:hi].strip())))
    return out


# ----------------------------------------------------- persist / edit chain

def _read_directory(d: bytes, dir_off: int, persist: dict, budget: list, override: bool) -> None:
    if dir_off < 0 or dir_off + 8 > len(d):
        return
    _vi, rt, ln = struct.unpack_from("<HHI", d, dir_off)
    if rt != RT_PERSIST_DIR:
        return
    p, end = dir_off + 8, min(dir_off + 8 + ln, len(d))
    while p + 4 <= end and budget[0] > 0:
        info = _u32(d, p)
        first, cnt = info & 0xFFFFF, info >> 20
        p += 4
        for k in range(cnt):
            if p + 4 > end or budget[0] <= 0:
                break
            if override:
                persist[first + k] = _u32(d, p)
            else:
                persist.setdefault(first + k, _u32(d, p))
            p += 4
            budget[0] -= 1


def _persist_map(d: bytes, edit_off: int):
    """Follow the UserEditAtom chain (newest first); returns (persist id -> offset, doc persist id)."""
    persist: dict[int, int] = {}
    doc_id = None
    seen: set[int] = set()
    off = edit_off
    budget = [len(d) // 4 + 16]
    while 0 <= off and off + 8 <= len(d) and off not in seen and len(seen) < 10000:
        seen.add(off)
        vi, rt, ln = struct.unpack_from("<HHI", d, off)
        if rt != RT_USER_EDIT or ln < 28 or off + 8 + ln > len(d):
            break
        b = off + 8
        last_edit, dir_off, doc_ref = struct.unpack_from("<III", d, b + 8)
        if ln >= 32 and _u32(d, b + 28):
            raise EncryptedDocumentError("PowerPoint presentation is encrypted (UserEditAtom)")
        if doc_id is None:
            doc_id = doc_ref
        _read_directory(d, dir_off, persist, budget, override=False)
        if last_edit == 0 or last_edit >= off:
            break
        off = last_edit
    return persist, doc_id


def _scan_top_level(d: bytes):
    """Damaged edit chain: persist directories in file order (later ones win), the last
    Document container, and the slide containers in file order."""
    persist: dict[int, int] = {}
    budget = [len(d) // 4 + 16]
    doc = None
    slides = []
    pos = 0
    n = len(d)
    count = 0
    while pos + 8 <= n and count < 1_000_000:
        count += 1
        vi, rt, ln = struct.unpack_from("<HHI", d, pos)
        b, e = pos + 8, min(pos + 8 + ln, n)
        if rt == RT_PERSIST_DIR:
            _read_directory(d, pos, persist, budget, override=True)
        elif rt == RT_DOCUMENT:
            doc = (b, e)
        elif rt == RT_SLIDE and len(slides) < _MAX_SLIDES:
            slides.append((b, e))
        pos += 8 + ln
    return persist, doc, slides


# ------------------------------------------------------------------ shapes

def _opt_props(d: bytes, b: int, e: int, count: int) -> dict:
    """Escher property table of an OPT/UDefProp record: {pid: (value, complex bytes or None)}.
    The complex data follows the fixed 6-byte entries, in property order."""
    props: dict = {}
    count = min(count, (e - b) // 6)
    cpos = b + 6 * count
    for i in range(count):
        opid, op = struct.unpack_from("<HI", d, b + 6 * i)
        pid = opid & 0x3FFF
        if opid & 0x8000:
            end = min(cpos + op, e)
            props[pid] = (op, d[cpos:end])
            cpos = end
        else:
            props[pid] = (op, None)
    return props


def _is_table_group(d: bytes, lo: int, hi: int) -> bool:
    """A group shape whose own shape record carries tableProperties & 3 in its UDefProp is a
    table (svdfppt.cxx ImportObj: DFF_msofbtUDefProp, DFF_Prop_tableProperties)."""
    for _vi, rt, b, e in _recs(d, lo, hi):
        if rt != _SP:
            continue
        for vi2, rt2, b2, e2 in _recs(d, b, e):
            if rt2 == _UDEFPROP:
                for i in range(min(vi2 >> 4, (e2 - b2) // 6)):
                    opid, op = struct.unpack_from("<HI", d, b2 + 6 * i)
                    if opid & 0x3FFF == _PID_TABLE_PROPS and not opid & 0x8000 and op & 3:
                        return True
        return False          # only the first shape record of the group describes the group
    return False


def _anchor(d: bytes, rt: int, b: int, e: int):
    """(left, top) of a ChildAnchor / ClientAnchor record, or None."""
    n = e - b
    if rt == _CHILD_ANCHOR and n >= 16:
        left, top = struct.unpack_from("<ii", d, b)
        return left, top
    if rt == _CLIENT_ANCHOR:
        if n >= 16:
            top, left = struct.unpack_from("<ii", d, b)
            return left, top
        if n >= 8:
            top, left = struct.unpack_from("<hh", d, b)
            return left, top
    return None


def _sort_table_cells(group_shapes: list) -> list:
    """The cells of a table group in row-major order: LibreOffice's CreateTable places every
    cell by the sorted distinct Top / Left of its snap rectangle, whatever the file order."""
    cells = [sh for sh in group_shapes if sh.get("anchor") is not None]
    if not cells:
        return group_shapes
    rows = {t: i for i, t in enumerate(sorted({sh["anchor"][1] for sh in cells}))}
    cols = {l: i for i, l in enumerate(sorted({sh["anchor"][0] for sh in cells}))}
    ordered = sorted(cells, key=lambda sh: (rows[sh["anchor"][1]], cols[sh["anchor"][0]]))
    return ordered + [sh for sh in group_shapes if sh.get("anchor") is None]


def _walk_shapes(d: bytes, lo: int, hi: int, out: list, depth: int = 0) -> None:
    if depth > 16:
        return
    for vi, rt, b, e in _recs(d, lo, hi):
        if rt == _SPGR:
            sub: list = []
            _walk_shapes(d, b, e, sub, depth + 1)
            out.extend(_sort_table_cells(sub) if _is_table_group(d, b, e) else sub)
        elif rt in (RT_PPDRAWING, _DG):
            _walk_shapes(d, b, e, out, depth + 1)
        elif rt == _SP:
            blocks: list = []
            pib = 0
            oleid = 0
            flags = 0
            ole = False
            hlinks: list = []
            anchor = None
            wordart: list = []
            for vi2, rt2, b2, e2 in _recs(d, b, e):
                if rt2 == _FSP and e2 - b2 >= 8:
                    flags = _u32(d, b2 + 4)
                elif rt2 in (_CHILD_ANCHOR, _CLIENT_ANCHOR):
                    anchor = _anchor(d, rt2, b2, e2)
                elif rt2 == _OPT:
                    props = _opt_props(d, b2, e2, vi2 >> 4)
                    if _PID_PIB in props and props[_PID_PIB][1] is None:
                        pib = props[_PID_PIB][0]
                    if _PID_OLE_ID in props and props[_PID_OLE_ID][1] is None:
                        oleid = props[_PID_OLE_ID][0]
                    # WordArt (Fontwork): the text lives in gtextUNICODE, enabled by fGtext (0x4000)
                    # of the text-path booleans (msdffimp.cxx ImportShape, bIsFontwork)
                    gt = props.get(_PID_GTEXT_UNICODE)
                    if gt and gt[1] and props.get(_PID_GTEXT_BOOLS, (0,))[0] & 0x4000:
                        wa = gt[1][: len(gt[1]) // 2 * 2].decode("utf-16-le", "replace").rstrip("\x00")
                        if wa.strip():
                            wordart.append([4, wa, None, [], -1, []])
                elif rt2 == _CLIENT_TEXTBOX:
                    for _v, rt3, b3, e3 in _recs(d, b2, e2):
                        _feed(blocks, d, rt3, b3, e3, _v >> 4)
                elif rt2 == _CLIENT_DATA:
                    for _v, rt3, b3, e3 in _recs(d, b2, e2):
                        if rt3 == RT_EXOBJREF:
                            ole = True
                        elif rt3 == RT_INTERACTIVE and len(hlinks) < _MAX_LINKS:
                            hid, action = _interactive(d, b3, e3)
                            if hid and action == _ACTION_HYPERLINK:
                                hlinks.append(hid)
            blocks.extend(wordart)
            out.append({"blocks": blocks, "pib": pib, "ole": ole, "oleid": oleid, "anchor": anchor, "hlinks": hlinks,
                        "skip": bool(flags & (0x01 | 0x08))})


def _comments(d: bytes, lo: int, hi: int, out: list, depth: int = 0) -> None:
    """Texts of the Comment10 containers (slide comments) below a slide container."""
    if depth > 8:
        return
    for vi, rt, b, e in _recs(d, lo, hi):
        if rt == RT_COMMENT10:
            for vi2, rt2, b2, e2 in _recs(d, b, e):
                if rt2 == RT_CSTRING and vi2 >> 4 == 1:
                    t = d[b2:e2]
                    t = t[: len(t) // 2 * 2].decode("utf-16-le", "replace").replace("\x00", "")
                    out.extend(p for p in t.replace("\r\n", "\r").split("\r") if p.strip())
        elif (vi & 0xF == 0xF or rt == RT_PROGBINARY_DATA) and rt not in (RT_PPDRAWING, _DG, _SPGR):
            _comments(d, b, e, out, depth + 1)


def _container_shapes(d: bytes, b: int, e: int, _cache: dict | None = None) -> list:
    """Shapes of the PPDrawing of a container; cached per container because several
    SlidePersistAtoms of a hostile file may point at the same one."""
    if _cache is not None and (b, e) in _cache:
        return _cache[(b, e)]
    shapes: list = []
    if _cache is not None:
        _cache[(b, e)] = shapes
    for vi, rt, b2, e2 in _recs(d, b, e):
        if rt == RT_PPDRAWING:
            _walk_shapes(d, b2, e2, shapes)
            break
    return shapes


# ---------------------------------------------------------------- pictures

def _bstore(group: bytes, pictures: bytes) -> list:
    """BLIPs of the drawing group, in BStore order (None when absent/undecodable)."""
    blips: list = []
    decoded: dict = {}      # (stream, offset) -> blip: FBSEs of a hostile file may share one offset

    def blip_at(src, tag, lo, hi):
        if (tag, lo) not in decoded:
            decoded[(tag, lo)] = decode_blip(src, lo, hi)
        return decoded[(tag, lo)]

    def walk(lo, hi, depth):
        if depth > 8:
            return
        for vi, rt, b, e in _recs(group, lo, hi):
            if rt == _DGG or rt == RT_PPDRAWINGGROUP:
                walk(b, e, depth + 1)
            elif rt == _BSTORE:
                for vi2, rt2, b2, e2 in _recs(group, b, e):
                    got = None
                    if rt2 == _FBSE and e2 - b2 >= 36:
                        cb_name = group[b2 + 33]
                        emb = b2 + 36 + cb_name
                        if emb + 8 <= e2:
                            got = blip_at(group, 0, emb, e2)
                        if got is None:
                            fo = _u32(group, b2 + 28)
                            if fo + 8 <= len(pictures):
                                ln = _u32(pictures, fo + 4)
                                got = blip_at(pictures, 1, fo, min(fo + 8 + ln, len(pictures)))
                    elif rt2 != _FBSE:
                        got = blip_at(group, 0, b2 - 8, e2)
                    blips.append(got)

    walk(0, len(group), 0)
    return blips


# ------------------------------------------------------------------ driver

def _read_document(ppt: bytes, doc_range):
    """(drawing group, slide entries, notes entries) of the Document container;
    an entry is [persist id, slide id, text blocks]."""
    group = b""
    slide_entries: list = []
    notes_entries: list = []
    if doc_range is None:
        return group, slide_entries, notes_entries
    for vi, rt, b, e in _recs(ppt, *doc_range):
        if rt == RT_PPDRAWINGGROUP:
            group = ppt[b:e]
        elif rt == RT_SLWT:
            inst = vi >> 4
            cur = None
            entries = []
            for vi2, rt2, b2, e2 in _recs(ppt, b, e):
                if rt2 == RT_SLIDE_PERSIST and e2 - b2 >= 16:
                    ref, _fl, _ct, sid = struct.unpack_from("<IIiI", ppt, b2)
                    cur = [ref, sid, []]
                    entries.append(cur)
                elif cur is not None:
                    _feed(cur[2], ppt, rt2, b2, e2, vi2 >> 4)
            if inst == 0:
                slide_entries.extend(entries)
            elif inst == 2:
                notes_entries.extend(entries)
    return group, slide_entries, notes_entries


def _utf16(d: bytes, b: int, e: int) -> str:
    t = d[b:e]
    return t[: len(t) // 2 * 2].decode("utf-16-le", "replace").split("\x00")[0].strip()


def _hyperlinks(ppt: bytes, doc_range) -> dict:
    """{ExHyperlinkAtom id: (friendly name, target)} of the ExObjList of the Document
    ([MS-PPT] ExHyperlinkContainer: CString instance 0 = friendly name, 1 = target, 3 = location,
    "slideId,slideNumber,title"). A target-less link to a slide becomes "#<title>"."""
    out: dict = {}
    if doc_range is None:
        return out
    for _vi, rt, b, e in _recs(ppt, *doc_range):
        if rt != RT_EXOBJLIST:
            continue
        for _vi2, rt2, b2, e2 in _recs(ppt, b, e):
            if rt2 != RT_EXHYPERLINK:
                continue
            hid, strs = None, {}
            for vi3, rt3, b3, e3 in _recs(ppt, b2, e2):
                if rt3 == RT_EXHYPERLINK_ATOM and e3 - b3 >= 4:
                    hid = _u32(ppt, b3)
                elif rt3 == RT_CSTRING:
                    strs.setdefault(vi3 >> 4, _utf16(ppt, b3, e3))
            url = strs.get(1, "")
            if not url and strs.get(3):
                parts = strs[3].split(",")
                url = "#" + (parts[2] if len(parts) >= 3 and parts[2] else strs[3])
            if hid and url and hid not in out and len(out) < _MAX_LINKS:
                out[hid] = (strs.get(0, ""), url)
    return out


# classes LibreOffice converts to its own objects (msdffimp.cxx CheckForConvertToSOObj: MSO_*_CLASSID,
# classids.hxx): Equation, Word, Excel 5/8 sheet and chart, PowerPoint. Such objects are native
# (Math, Writer, Calc, Impress) and the export carries no picture for them.
_CONVERTED_CLSIDS = ("00021700", "0002CE02", "00020906", "00020810", "00020820", "00020821", "64818D10", "64818D11")


def _drop_picture(sh: dict, ole_ids: dict) -> bool:
    """True when the picture of a shape is not a picture in LibreOffice but an OLE / media object.

    * pictureId (OPT 267) names an ExOleObjAtom whose ExOleObjStg decompresses to a compound file:
      ImportOLE builds an OLE object. Converted classes (see above) leave no picture; the other
      classes stay generic OLE objects and LibreOffice still shows their replacement graphic,
      which we only keep for charts (Excel-like workbook with a presentation stream).
    * an ExObjRefAtom without any pictureId refers to a media object (sound / video), which
      LibreOffice replaces by a media shape.
    * a pictureId whose storage cannot be read (ImportOLE fails) keeps the plain picture."""
    oid = sh["oleid"]
    if oid:
        info = ole_ids.get(oid)
        if info is None:
            return False
        return True  # an OLE object, not a picture (replacement graphics are LibreOffice's own rendering)
    return bool(sh["ole"])


def _root_clsid(d: bytes) -> str:
    try:
        ss = 1 << struct.unpack_from('<H', d, 30)[0]
        fs = struct.unpack_from('<I', d, 48)[0]
        o = (fs + 1) * ss + 80
        a, b, c = struct.unpack_from('<IHH', d, o)
        return '%08X-%04X-%04X-%s' % (a, b, c, d[o + 8:o + 16].hex().upper())
    except Exception:
        return ''


def _ole_storage_ok(ppt: bytes, off: int):
    """(stream names, root CLSID) when the ExOleObjStg record at `off` holds a compound file with a "\\1Ole" or "Contents"
    stream of at least 10 bytes: the condition under which LibreOffice turns the shape into an
    OLE object (svdfppt.cxx SdrPowerPointImport::ImportOLE: SdrPowerPointOLEDecompress, Storage,
    OpenSotStream "\\1Ole" / "Contents")."""
    if off < 0 or off + 12 > len(ppt):
        return False
    _vi, rt, ln = struct.unpack_from("<HHI", ppt, off)
    if rt != RT_EXOLEOBJSTG or ln <= 4:
        return False
    try:
        dec = zlib.decompressobj()
        data = dec.decompress(ppt[off + 12:min(off + 8 + ln, len(ppt))], 1 << 28)
        cf = CompoundFile(data)
        for name in ("\x01Ole", "Contents"):
            if cf.exists(name) and len(cf.open_stream(name)) >= 10:
                return (frozenset(x.lower() for x in cf.list_streams()), _root_clsid(data))
    except Exception:       # hostile data: not usable as an OLE object, the picture stays
        pass
    return None


def _ole_ids(ppt: bytes, persist: dict, doc_range) -> dict:
    """exObjIds of the embedded objects / controls LibreOffice can import as OLE objects: ExEmbed and
    ExControl of the Document's ExObjList with an ExOleObjAtom whose persist id leads to a usable
    ExOleObjStg (svdfppt.cxx SdrPowerPointImport::SeekOle, ImportOLE)."""
    ids: dict = {}
    if doc_range is None:
        return ids
    ok: dict = {}
    for _vi, rt, b, e in _recs(ppt, *doc_range):
        if rt != RT_EXOBJLIST:
            continue
        for _vi2, rt2, b2, e2 in _recs(ppt, b, e):
            if rt2 not in (RT_EXEMBED, RT_EXCONTROL):
                continue
            for _vi3, rt3, b3, e3 in _recs(ppt, b2, e2):
                if rt3 == RT_EXOLEOBJATOM and e3 - b3 >= 20 and len(ok) < _MAX_LINKS:
                    oid, pid = _u32(ppt, b3 + 8), _u32(ppt, b3 + 16)
                    off = persist.get(pid)
                    if pid and off is not None and oid not in ids:
                        if off not in ok:
                            ok[off] = _ole_storage_ok(ppt, off)
                        if ok[off]:
                            ids[oid] = ok[off]
                    break
    return ids


def _container_at(ppt: bytes, persist: dict, ref: int, want: int):
    off = persist.get(ref)
    if off is None or off < 0 or off + 8 > len(ppt):
        return None
    vi, rt, ln = struct.unpack_from("<HHI", ppt, off)
    if rt != want:
        return None
    return off + 8, min(off + 8 + ln, len(ppt))


def parse(ppt: bytes, pictures: bytes = b"", current_user: bytes = b"", images_enabled: bool = True) -> Document:
    if len(current_user) >= 16 and _u32(current_user, 12) == ENCRYPTED_TOKEN:
        raise EncryptedDocumentError("PowerPoint presentation is encrypted")
    if current_user and _u32(current_user, 12) != USER_TOKEN:
        raise UnsupportedFormatError("PowerPoint 95 presentation (only PowerPoint 97-2003 is supported)")
    doc = Document(format="ppt")

    persist: dict[int, int] = {}
    doc_range = None
    if len(current_user) >= 20:
        persist, doc_id = _persist_map(ppt, _u32(current_user, 16))
        off = persist.get(doc_id) if doc_id is not None else None
        if off is not None and off + 8 <= len(ppt):
            vi, rt, ln = struct.unpack_from("<HHI", ppt, off)
            if rt == RT_DOCUMENT:
                doc_range = (off + 8, min(off + 8 + ln, len(ppt)))
    group, slide_entries, notes_entries = _read_document(ppt, doc_range)

    def resolve():
        out = []
        for ref, sid, blocks in slide_entries[:_MAX_SLIDES]:
            rng = _container_at(ppt, persist, ref, RT_SLIDE)
            if rng:
                out.append((sid, blocks, rng))
        return out

    order = resolve() if doc_range else []
    scanned = None
    if not order:
        # damaged or missing edit chain: rebuild the persist directory by scanning the stream
        scanned = _scan_top_level(ppt)
        persist, scan_doc, scan_slides = scanned
        if scan_doc:
            doc_range = scan_doc
            group, slide_entries, notes_entries = _read_document(ppt, doc_range)
            order = resolve()
        if order:
            doc.warnings.append("PowerPoint edit chain not usable; persist directory rebuilt by scanning")
        elif scan_slides:
            doc.warnings.append("PowerPoint edit chain not usable; slides read in file order")
            order = [(None, slide_entries[k][2] if k < len(slide_entries) else [], rng)
                     for k, rng in enumerate(scan_slides)]
    if doc_range is None and not order:
        if ppt[4:8] == b"\xff\xff\x00\x00":
            raise UnsupportedFormatError("PowerPoint 95 presentation (only PowerPoint 97-2003 is supported)")
        raise CorruptDocumentError("PowerPoint Document stream has no document container")
    blips = _bstore(group, pictures) if group and images_enabled else []

    shape_cache: dict = {}
    # notes pages: by the slide's SlideAtom.notesIdRef (= the notes page's SlidePersistAtom id, the
    # link LibreOffice follows, svdfppt.cxx GetNotesPageId), else by NotesAtom.slideIdRef
    notes_by_sid: dict[int, list] = {}
    notes_of: dict[int, list] = {}
    for ref, nsid, _blocks in notes_entries:
        rng = _container_at(ppt, persist, ref, RT_NOTES)
        if not rng:
            continue
        for vi2, rt2, b2, e2 in _recs(ppt, *rng):
            if rt2 == RT_NOTES_ATOM and e2 - b2 >= 4:
                shapes = _container_shapes(ppt, *rng, shape_cache)
                notes_by_sid.setdefault(nsid, shapes)
                if _u32(ppt, b2):
                    notes_of.setdefault(_u32(ppt, b2), shapes)
                break

    def notes_shapes(sid, rng):
        for vi2, rt2, b2, e2 in _recs(ppt, *rng):
            if rt2 == RT_SLIDE_ATOM:
                nid = _u32(ppt, b2 + 16) if e2 - b2 >= 20 else 0
                if nid and nid in notes_by_sid:
                    return notes_by_sid[nid]
                break
        return notes_of.get(sid, []) if sid is not None else []

    media_of: dict[str, int] = {}
    pib_cache: dict[int, int | None] = {}

    def image_index(pib: int):
        if pib in pib_cache:
            return pib_cache[pib]
        idx = None
        if 0 < pib <= len(blips) and blips[pib - 1] is not None:
            data, mime = blips[pib - 1]
            key = hashlib.sha1(data).hexdigest()
            if key not in media_of:
                media_of[key] = len(doc.images)
                doc.images.append(Image(data=data, mime=mime, kind="floating", story="main"))
            idx = media_of[key]
        pib_cache[pib] = idx
        return idx

    hyper = _hyperlinks(ppt, doc_range)
    ole_ids = _ole_ids(ppt, persist, doc_range)
    budget = 16 * len(ppt) + 1_000_000   # characters of slide text we are willing to emit
    meta: list = []   # per slide: [(paragraph text, text type)] for the block builder
    for n, (sid, outline, rng) in enumerate(order, 1):
        if budget <= 0:
            doc.warnings.append("PowerPoint text limit reached; remaining slides skipped")
            break
        slide = Slide(index=n)
        items: list = []
        loose: list = []        # (text, url) of links that are not a run of a paragraph
        for sh in _container_shapes(ppt, *rng, shape_cache):
            first = len(items)
            for blk in sh["blocks"]:
                ttype, text, ref, fields = blk[:4]
                ranges = blk[5]
                if text is None and ref is not None:
                    hit = _outline_block(outline, ref)
                    if hit is not None:
                        ttype, text, _r, fields = hit[:4]
                        ranges = hit[5]
                if text:
                    for p, spans in _paragraphs_ranges(text, fields, n, ranges):
                        items.append((p, ttype, [(lo, hi, hyper[h][1]) for lo, hi, h in spans if h in hyper]))
            for h in sh["hlinks"]:
                if h in hyper and len(loose) < _MAX_LINKS:
                    name, url = hyper[h]
                    label = " ".join(it[0] for it in items[first:])[:200] or name or url
                    loose.append((label, url))
            if sh["pib"] and not _drop_picture(sh, ole_ids) and not sh["skip"]:
                i = image_index(sh["pib"])
                if i is not None and i not in slide.images:
                    slide.images.append(i)
        slide.texts = [it[0] for it in items]
        slide.links = _dedupe([(p[lo:hi], url) for p, _t, ls in items for lo, hi, url in ls] + loose)
        budget -= sum(len(p) + 1 for p in slide.texts) + 64
        _comments(ppt, *rng, slide.comments)
        for sh in notes_shapes(sid, rng):
            for ttype, text, _ref, fields, _inst, *_rest in sh["blocks"]:
                if text and ttype == TX_NOTES:
                    slide.notes.extend(_paragraphs(text, fields, n))
        doc.slides.append(slide)
        meta.append((items, [lk for lk in loose]))
    _fill_flat(doc)
    _build_blocks(doc, meta)
    return doc


def _dedupe(links: list) -> list:
    seen: set = set()
    return [lk for lk in links if not (lk in seen or seen.add(lk))]


def _fill_flat(doc: Document) -> None:
    main: list[str] = []
    for s in doc.slides:
        main.extend(s.texts)
        main.extend(s.comments)
        main.extend(s.notes)
    doc.stories["main"] = main


def _build_blocks(doc: Document, meta: list) -> None:
    blocks: list = []
    for slide, (items, loose) in zip(doc.slides, meta):
        blocks.append(Paragraph("Slide %d" % slide.index, heading=2))
        for text, ttype, spans in items:
            links = [Link(lo, hi, url) for lo, hi, url in spans]
            if ttype in _TITLE_TYPES:
                blocks.append(Paragraph(text, heading=3, links=links))
            elif ttype in _BODY_TYPES:
                blocks.append(Paragraph(text, list_level=0, links=links))
            else:
                blocks.append(Paragraph(text, links=links))
        if slide.images:
            blocks.append(Paragraph(IMAGE_MARK * len(slide.images), images=list(slide.images)))
        if loose:
            blocks.append(Paragraph("Links", heading=3))
            blocks.extend(Paragraph(t, list_level=0, links=[Link(0, len(t), u)]) for t, u in _dedupe(loose))
        if slide.comments:
            blocks.append(Paragraph("Comments", heading=3))
            blocks.extend(Paragraph(t) for t in slide.comments)
        if slide.notes:
            blocks.append(Paragraph("Notes", heading=3))
            blocks.extend(Paragraph(t) for t in slide.notes)
    doc.blocks = blocks
