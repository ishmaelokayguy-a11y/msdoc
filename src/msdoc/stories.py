# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sw/source/filter/ww8/ww8par.cxx, ww8par2.cxx, ww8par5.cxx, ww8par6.cxx,
# ww8scan.cxx and filter/source/msfilter/msdffimp.cxx (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""Split the CP space into stories and turn raw text into clean paragraphs.

CP layout ([MS-DOC] 2.3 Document Parts): main [0, ccpText), then footnotes
(ccpFtn), headers (ccpHdd), comments (ccpAtn), endnotes (ccpEdn), main-story
textboxes (ccpTxbx), header textboxes (ccpHdrTxbx). The individual notes /
headers / textboxes are delimited by their PLCs: PlcffndTxt, PlcfHdd,
PlcfandTxt, PlcfendTxt, PlcftxbxTxt, PlcfHdrtxbxTxt.

LibreOffice reference: sw/source/filter/ww8/ww8par.cxx (SwWW8ImplReader,
ReadText, ReadChars, ReadChar, ReadPlainChars, HandlePageBreakChar),
ww8par5.cxx (fields: Read_Field, End_Field), ww8par2.cxx (tables),
ww8scan.cxx WW8PLCFx_SubDoc, WW8PLCFx_HdFt.

Special characters ([MS-DOC] 2.4.? and 2.8.25 PlcFld):
  0x0D  paragraph end -> new paragraph
  0x07  cell / row end mark -> new paragraph (row-end marks produce no paragraph)
  0x0B  line break -> "\n" inside the paragraph
  0x0C  page/section break -> new paragraph
  0x0E  column break -> new paragraph
  0x09  tab -> "\t"
  0x1E  non-breaking hyphen -> U+2011 ; 0x1F optional hyphen -> drop
  0x13 field begin, 0x14 separator, 0x15 end -> keep only the RESULT part
       (between 0x14 and 0x15); fields without a separator contribute nothing.
       Fields nest.
  0x01, 0x08, 0x02, 0x03, 0x04, 0x05, 0x28 with sprmCFSpec -> drop from text
       (0x01/0x08 are image anchors — report them to images.py by CP)
  The last paragraph mark of each story/note is not a separate empty paragraph.
"""
from __future__ import annotations

import bisect
import struct
from typing import NamedTuple

from .props import _PRM0_ISPRM_TO_SPRM, SPRM_CFDATA, SPRM_CFSPEC, SPRM_CPICLOCATION, _iter_sprms

SPRM_CFRMARKDEL = 0x0800
SPRM_CSYMBOL = 0x6A09

_PAR_BREAKS = frozenset("\r\x07\x0c\x0e")
_FIELD_BEGIN, _FIELD_SEP, _FIELD_END = "\x13", "\x14", "\x15"
# Characters that only carry meaning together with sprmCFSpec (pictures,
# note references, annotation marks, ...). [MS-DOC] 2.6.1 sprmCFSpec.
_SPEC_CHARS = frozenset("\x01\x02\x03\x04\x05\x06\x08\x28")
_ANCHOR_CHARS = frozenset("\x01\x08")
# 0x1E non-breaking hyphen: LibreOffice inserts U+2011; 0x1F optional
# hyphen: a soft hyphen, invisible in the extracted text.
_CHAR_MAP = {0x1E: "‑", 0x1F: None}

# Headers: PlcfHdd starts with 6 separator stories (footnote/endnote
# separators and continuation notices), then 6 stories per section
# ([MS-DOC] 2.9.? Plcfhdd). LibreOffice does not show the separators.
_HDD_SEPARATORS = 6
# FTXBXS (PlcftxbxTxt / PlcfHdrtxbxTxt data element) is 22 bytes.
_FTXBXS_SIZE = 22


def _plc_cps(fib, table: bytes, name: str, cb_data: int) -> list[int]:
    """Return the aCP array of a PLC in the table stream ([MS-DOC] 2.2.2 Plc)."""
    got = fib.pair(name, len(table)) if hasattr(fib, "pair") else None
    if not got:
        return []
    fc, lcb = got
    if lcb < 4:
        return []
    n = (lcb - 4) // (4 + cb_data)
    if n <= 0:
        return []
    return list(struct.unpack_from(f"<{n + 1}i", table, fc))


def _ranges(cps: list[int], base: int, limit: int, skip: int = 0) -> list[tuple[int, int]]:
    """Consecutive [cp_i, cp_i+1) ranges of a story PLC, made absolute and
    clamped to [base, base+limit)."""
    out = []
    end_abs = base + limit
    for i in range(skip, len(cps) - 1):
        lo = base + max(0, cps[i])
        hi = min(base + cps[i + 1], end_abs)
        if hi > lo:
            out.append((lo, hi))
    return out


def _toggle(op) -> bool:
    """[MS-DOC] 2.9.? ToggleOperand: 0 off, 1 on, 0x80 same as the style
    (off for these flags, which styles do not set), 0x81 inverse of the
    style (on)."""
    return bool(op) and op[0] in (1, 0x81)


class _RunFlags:
    """Per-CP character flags that change how a character is extracted,
    built once from the CHPX runs (props.runs(), sorted by CP)."""

    SPEC = 1     # sprmCFSpec: special character (picture, note ref, ...)
    DELETED = 2  # sprmCFRMarkDel: tracked deletion
    SYMBOL_SHIFT = 8  # bits 8+: sprmCSymbol character, 0 if none

    def __init__(self, props) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.flags: list[int] = []
        try:
            runs = props.runs()
            for lo, hi, sprms in runs:
                fl = 0
                if _toggle(sprms.get(SPRM_CFSPEC)):
                    fl |= self.SPEC
                if _toggle(sprms.get(SPRM_CFRMARKDEL)):
                    fl |= self.DELETED
                op = sprms.get(SPRM_CSYMBOL)
                if op and len(op) >= 4:
                    # [MS-DOC] sprmCSymbol: ftc (2 bytes) + xchar (2 bytes);
                    # LibreOffice (ww8par6.cxx Read_Symbol) shows xchar.
                    xchar = struct.unpack_from("<H", op, len(op) - 2)[0]
                    if xchar >= 0x20:
                        fl |= xchar << self.SYMBOL_SHIFT
                if fl and hi > lo:
                    if self.starts and lo < self.ends[-1]:
                        # overlapping / unsorted runs: keep the list sorted
                        continue
                    self.starts.append(lo)
                    self.ends.append(hi)
                    self.flags.append(fl)
        except Exception:  # pragma: no cover - props are defensive already
            pass

    def segments(self, lo: int, hi: int):
        """Yield (seg_lo, seg_hi, flags) covering [lo, hi) exactly."""
        i = max(0, bisect.bisect_right(self.starts, lo) - 1)
        cur = lo
        n = len(self.starts)
        while cur < hi:
            while i < n and self.ends[i] <= cur:
                i += 1
            if i >= n or self.starts[i] >= hi:
                yield cur, hi, 0
                return
            s = self.starts[i]
            if s > cur:
                yield cur, s, 0
                cur = s
            e = min(self.ends[i], hi)
            yield cur, e, self.flags[i]
            cur = e
            i += 1


# Paragraph / table sprms used to decide where a paragraph's text lives.
# [MS-DOC] 2.6.2 (paragraph) and 2.6.3 (table) sprm tables.
SPRM_PFINTABLE = 0x2416
SPRM_PFTTP = 0x2417
SPRM_PFINNERTTP = 0x244C
SPRM_PDCS = 0x442C
SPRM_PITAP = 0x6649
SPRM_PPC = 0x261B
SPRM_PWR = 0x2423
SPRM_PDXAABS = 0x8418
SPRM_PDYAABS = 0x8419
SPRM_PDXAWIDTH = 0x841A
SPRM_PWHEIGHTABS = 0x442B
SPRM_PDYAFROMTEXT = 0x842E
SPRM_PDXAFROMTEXT = 0x842F
SPRM_TPC = 0x360D
SPRM_TDXAABS = 0x940E
SPRM_TDYAABS = 0x940F
SPRM_TDXAFROMTEXT = 0x9410
SPRM_TDYAFROMTEXT = 0x9411
SPRM_TDYAFROMTEXTBOTTOM = 0x941E
SPRM_TDXAFROMTEXTRIGHT = 0x941F
SPRM_POUTLVL = 0x2640

_FKP_PAGE = 512
_MAX_FKP_PAGES = 1 << 20


def _i16(op: bytes) -> int:
    if len(op) < 2:
        return 0
    return struct.unpack_from("<h", op, 0)[0]


def _u8(op: bytes) -> int:
    return op[0] if op else 0


class _Styles:
    """Paragraph sprms of each style, with istdBase inheritance resolved.

    [MS-DOC] 2.9.271 STSH, 2.9.272 STSHI (cbStshi, Stshif.cstd,
    cbSTDBaseInFile), 2.9.260 STD (StdfBase: stk, istdBase, cupx), 2.9.? Xstz
    name, 2.9.? LPUpxPapx (istd + grpprl). LibreOffice: ww8scan.cxx
    WW8Style, ww8par2.cxx WW8RStyle.
    """

    def __init__(self, fib, table: bytes) -> None:
        self._raw: list[tuple[int, bytes]] = []  # (istdBase, papx grpprl)
        self._sti: list[int] = []  # STD.StdfBase.sti, invariant style identifier
        self._resolved: dict[int, dict[int, bytes]] = {}
        try:
            self._parse(fib, table)
        except (struct.error, IndexError, ValueError):
            pass

    def _parse(self, fib, table: bytes) -> None:
        got = fib.pair("Stshf", len(table)) if hasattr(fib, "pair") else None
        if not got or got[1] < 2 + 18:
            return
        fc, lcb = got
        end = fc + lcb
        cb_stshi = struct.unpack_from("<H", table, fc)[0]
        stshi = fc + 2
        cstd = struct.unpack_from("<H", table, stshi)[0]
        cb_base = struct.unpack_from("<H", table, stshi + 2)[0]
        pos = stshi + cb_stshi
        for _ in range(min(cstd, 0x0FFE)):
            if pos + 2 > end:
                break
            cb = struct.unpack_from("<H", table, pos)[0]
            std = pos + 2
            pos = std + cb
            if cb == 0 or pos > end or cb < max(cb_base, 10):
                self._raw.append((0x0FFF, b""))
                self._sti.append(0)
                continue
            # StdfBase first WORD: sti:12, fScratch/fInvalHeight/fHasUpe/fMassCopy:4
            # (ww8struc.hxx WW8_STD). sti 1..9 are the built-in Heading 1..9 styles,
            # independent of the (possibly localized) style name.
            sti = struct.unpack_from("<H", table, std)[0] & 0x0FFF
            self._sti.append(sti)
            stk_base = struct.unpack_from("<H", table, std + 2)[0]
            stk, istd_base = stk_base & 0xF, stk_base >> 4
            cupx = struct.unpack_from("<H", table, std + 4)[0] & 0xF
            p = std + cb_base
            cch = struct.unpack_from("<H", table, p)[0] if p + 2 <= pos else 0
            p += 2 + 2 * cch + 2  # Xstz: cch, UTF-16 chars, terminating 0
            papx = b""
            if stk == 1 and cupx >= 1:
                if (p - std) & 1:
                    p += 1
                if p + 2 <= pos:
                    cbu = struct.unpack_from("<H", table, p)[0]
                    if p + 2 + cbu <= pos and cbu >= 2:
                        papx = table[p + 4:p + 2 + cbu]  # skip UpxPapx.istd
            self._raw.append((istd_base, papx))

    def para_sprms(self, istd: int) -> dict[int, bytes]:
        got = self._resolved.get(istd)
        if got is not None:
            return got
        chain = []
        cur = istd
        while 0 <= cur < len(self._raw) and cur not in chain and len(chain) < 64:
            chain.append(cur)
            if cur in self._resolved:
                break
            cur = self._raw[cur][0]
        out: dict[int, bytes] = {}
        for i in reversed(chain):
            if i in self._resolved:
                out = dict(self._resolved[i])
                continue
            own = dict(_iter_sprms(self._raw[i][1]))
            if SPRM_POUTLVL not in own:
                # ww8par.hxx SwWW8StyInf::SetOrgWWIdent: a built-in heading
                # style defaults to outline level sti-1 unless its own grpprl
                # carries an explicit sprmPOutLvl (handled above); inherited
                # by styles based on it like any other paragraph property.
                sti = self._sti[i] if i < len(self._sti) else 0
                if 1 <= sti <= 9:
                    own[SPRM_POUTLVL] = bytes([sti - 1])
            out.update(own)
            self._resolved[i] = dict(out)
        self._resolved.setdefault(istd, out)
        return self._resolved[istd]


class _ParaProps:
    """Paragraph properties (style + PAPX) by CP of the paragraph mark.

    [MS-DOC] 2.8.? PlcBtePapx, 2.9.175 PapxFkp, 2.9.174 PapxInFkp,
    2.9.111 GrpPrlAndIstd; piece-level Pcd.Prm sprms with sgc == 1 are
    applied on top ([MS-DOC] 2.4.6.1 Direct Paragraph Formatting).
    LibreOffice: ww8scan.cxx WW8PLCFx_Cp_FKP / WW8Fkp (ePAP).
    """

    def __init__(self, fib, word_document: bytes, table_stream: bytes, pieces) -> None:
        self._pieces = pieces
        self._runs: list[tuple[int, int, bytes]] = []
        self._starts: list[int] = []
        self._cache: dict[bytes, dict[int, bytes]] = {}
        self._prm_cache: dict[bytes, dict[int, bytes]] = {}
        self.warnings: list[str] = []
        self._styles = _Styles(fib, table_stream)
        got = fib.pair("PlcfBtePapx", len(table_stream)) if hasattr(fib, "pair") else None
        if not got or got[1] < 12:
            return
        fc, lcb = got
        n = min((lcb - 4) // 8, _MAX_FKP_PAGES)
        seen: set[int] = set()
        runs: list[tuple[int, int, bytes]] = []
        for i in range(n):
            pn = struct.unpack_from("<I", table_stream, fc + (n + 1) * 4 + i * 4)[0] & 0x3FFFFF
            off = pn * _FKP_PAGE
            if off in seen:
                continue
            seen.add(off)
            if off + _FKP_PAGE > len(word_document):
                self.warnings.append(f"stories: PapxFkp page pn={pn} out of range")
                continue
            runs.extend(self._parse_page(word_document[off:off + _FKP_PAGE]))
        runs.sort(key=lambda r: r[0])
        self._runs = runs
        self._starts = [r[0] for r in runs]

    @staticmethod
    def _parse_page(page: bytes):
        cpara = page[_FKP_PAGE - 1]
        if cpara < 1 or (cpara + 1) * 4 + cpara * 13 > _FKP_PAGE - 1:
            return
        fcs = struct.unpack_from(f"<{cpara + 1}I", page, 0)
        bx = (cpara + 1) * 4
        for i in range(cpara):
            lo, hi = fcs[i], fcs[i + 1]
            if hi <= lo:
                continue
            b = page[bx + i * 13]
            grpprl = b""
            if b:
                pos = b * 2
                cb = page[pos] if pos < _FKP_PAGE else 0
                if cb:
                    size, start = 2 * cb - 1, pos + 1
                else:
                    size = 2 * page[pos + 1] if pos + 1 < _FKP_PAGE else 0
                    start = pos + 2
                # GrpPrlAndIstd: istd (2 bytes) then the grpprl.
                grpprl = page[start:min(start + size, _FKP_PAGE - 1)]
            yield lo, hi, grpprl

    def _prm_sprms(self, piece) -> dict[int, bytes]:
        prm_b = getattr(piece, "prm", b"")
        if len(prm_b) != 2:
            return {}
        got = self._prm_cache.get(prm_b)
        if got is not None:
            return got
        prm = struct.unpack_from("<H", prm_b, 0)[0]
        out: dict[int, bytes] = {}
        if prm & 1:
            for sid, op in _iter_sprms(self._pieces.prc_grpprl(prm >> 1)):
                if (sid >> 10) & 7 in (1, 5):  # paragraph / table sprms
                    out[sid] = op
        else:
            isprm = (prm & 0xFE) >> 1
            sid = _PRM0_ISPRM_TO_SPRM[isprm] if isprm < len(_PRM0_ISPRM_TO_SPRM) else 0
            if sid and (sid >> 10) & 7 == 1:
                out[sid] = bytes([(prm >> 8) & 0xFF])
        self._prm_cache[prm_b] = out
        return out

    def sprms_at(self, cp: int) -> dict[int, bytes]:
        if cp < 0 or not self._runs:
            return {}
        fc, _ = self._pieces.cp_to_fc(cp)
        i = bisect.bisect_right(self._starts, fc) - 1
        grpprl = b""
        if i >= 0 and self._runs[i][0] <= fc < self._runs[i][1]:
            grpprl = self._runs[i][2]
        sprms = self._cache.get(grpprl)
        if sprms is None:
            istd = struct.unpack_from("<H", grpprl, 0)[0] if len(grpprl) >= 2 else 0
            sprms = dict(self._styles.para_sprms(istd))
            sprms.update(_iter_sprms(grpprl[2:]))
            self._cache[grpprl] = sprms
        piece = self._pieces.piece_at_cp(cp)
        prm = self._prm_sprms(piece) if piece is not None else {}
        if prm:
            sprms = dict(sprms)
            sprms.update(prm)
        return sprms


def _is_frame(sp: dict[int, bytes]) -> bool:
    """Does this paragraph start an APO (absolutely positioned object) that
    LibreOffice imports as a text frame?

    LibreOffice ww8par2.cxx SwWW8ImplReader::TestApo (frame if sprmPWr or
    sprmPPc is present), ww8par6.cxx WW8FlyPara::Read and WW8FlyPara::IsEmpty
    (a frame whose position/size/wrap all have default values is ignored).
    """
    if SPRM_PPC not in sp and SPRM_PWR not in sp:
        return False
    wr = _u8(sp.get(SPRM_PWR, b"\x02"))
    return not (
        _i16(sp.get(SPRM_PDXAABS, b"")) == 0
        and _i16(sp.get(SPRM_PDYAABS, b"")) == 0
        and _i16(sp.get(SPRM_PWHEIGHTABS, b"")) & 0x7FFF == 0
        and _i16(sp.get(SPRM_PDXAWIDTH, b"")) == 0
        and _i16(sp.get(SPRM_PDXAFROMTEXT, b"")) == 0
        and _i16(sp.get(SPRM_PDYAFROMTEXT, b"")) == 0
        and _u8(sp.get(SPRM_PPC, b"")) == 0
        and wr in (0, 2)
    )


def _is_floating_table(sp: dict[int, bytes]) -> bool:
    """Row-end TAP with a table position that LibreOffice turns into a
    frame. ww8par2.cxx SwWW8ImplReader::ParseTabPos +
    WW8FlyPara::ApplyTabPos / IsEmpty."""
    keys = (SPRM_TPC, SPRM_TDXAABS, SPRM_TDYAABS, SPRM_TDXAFROMTEXT, SPRM_TDYAFROMTEXT,
            SPRM_TDYAFROMTEXTBOTTOM, SPRM_TDXAFROMTEXTRIGHT)
    if not any(k in sp for k in keys):
        return False
    return not (
        _u8(sp.get(SPRM_TPC, b"")) == 0
        and _i16(sp.get(SPRM_TDXAABS, b"")) == 0
        and _i16(sp.get(SPRM_TDYAABS, b"")) == 0
        and _i16(sp.get(SPRM_TDXAFROMTEXT, b"")) == 0
        and _i16(sp.get(SPRM_TDXAFROMTEXTRIGHT, b"")) == 0
        # ApplyTabPos copies only the left/right margins and dyaFromText
        # into the upper margin; the bottom one is compared too.
        and _i16(sp.get(SPRM_TDYAFROMTEXT, b"")) == 0
        and _i16(sp.get(SPRM_TDYAFROMTEXTBOTTOM, b"")) == 0
    )


def _table_depth(sp: dict[int, bytes]) -> int:
    """[MS-DOC] sprmPItap (table depth), falling back to sprmPFInTable."""
    op = sp.get(SPRM_PITAP)
    if op is not None and len(op) >= 4:
        return max(0, min(struct.unpack_from("<i", op, 0)[0], 64))
    return 1 if _u8(sp.get(SPRM_PFINTABLE, b"")) else 0


def heading_level(sp: dict[int, bytes]) -> int:
    """Final Word outline level of a paragraph (0 = body text, 1..9 =
    heading level), given its merged style+direct sprms (as returned by
    _ParaProps.sprms_at).

    LibreOffice ww8par6.cxx Read_POutLvl and ww8par.hxx
    SwWW8StyInf::WW8OutlineLevelToOutlinelevel: the sprmPOutLvl byte 0..8
    is heading level+1, 9 (or anything >= WW8ListManager::nMaxLevel==10)
    means body text. The built-in heading styles' implicit default is
    injected by _Styles.para_sprms, so a plain style lookup is enough here.
    """
    op = sp.get(SPRM_POUTLVL)
    if not op:
        return 0
    v = op[0]
    return 0 if v >= 9 else v + 1


def _flatten_cells(pars: list[tuple[str, int]], pieces, papx: _ParaProps) -> list[tuple[str, int]]:
    """Writer has no tables in foot/endnotes: LibreOffice imports a table
    there as plain paragraphs, the cells of a row running together. A
    paragraph closed by a cell mark (0x07 that is not a row end) is joined
    with the next one."""
    out: list[tuple[str, int]] = []
    carry = ""
    for text, mark in pars:
        text = carry + text
        carry = ""
        if mark >= 0 and pieces.text(mark, mark + 1) == "\x07":
            sp = papx.sprms_at(mark)
            if not (_u8(sp.get(SPRM_PFTTP, b"")) or _u8(sp.get(SPRM_PFINNERTTP, b""))):
                carry = text
                continue
        out.append((text, mark))
    if carry:
        out.append((carry, -1))
    return out


def _route(pars: list[tuple[str, int]], papx: _ParaProps) -> tuple[list[str], list[str]]:
    """Split a story's paragraphs into (in-flow, frame) paragraphs.

    Frames (APO paragraphs and floating tables) are reported by
    LibreOffice as text frames, i.e. in the "textboxes" story. A table row
    is only known to float at its row-end mark (the TAP lives there), so
    table paragraphs are held per nesting level until their row ends
    (ww8par.cxx ProcessSpecial: SearchRowEnd + ParseTabPos per cell level).
    """
    flow: list[str] = []
    frames: list[str] = []
    pending: list[list[tuple[str, bool]]] = [[]]  # index = table depth
    dropcap = ""

    def flush_to(level: int) -> None:
        # Close every table level deeper than `level` without a row end
        # (broken table): its paragraphs move one level out unchanged.
        while len(pending) - 1 > level:
            items = pending.pop()
            pending[-1].extend(items)
        if level == 0:
            for t, fr in pending[0]:
                (frames if fr else flow).append(t)
            pending[0].clear()

    prev_depth = 0
    table_in_frame = False
    for text, mark in pars:
        sp = papx.sprms_at(mark)
        if dropcap:
            # ww8par6.cxx: a drop-cap frame (sprmPDcs.fdct != 0) becomes a
            # drop-cap attribute of the following paragraph.
            text, dropcap = dropcap + text, ""
        dcs = sp.get(SPRM_PDCS, b"")
        if dcs and dcs[0] & 7 and _is_frame(sp):
            dropcap = text
            continue
        depth = _table_depth(sp)
        apo = _is_frame(sp)
        if depth == 0:
            table_in_frame = False
        elif prev_depth == 0:
            # ww8par.cxx ProcessSpecial: the frame decision is taken when
            # the table starts; a table that starts inside an APO stays in
            # that frame for all its rows.
            table_in_frame = apo
        prev_depth = depth
        flush_to(depth)
        while len(pending) - 1 < depth:
            pending.append([])
        pending[depth].append((text, apo or table_in_frame))
        if depth:
            row_end = _u8(sp.get(SPRM_PFTTP, b"")) if depth == 1 else _u8(sp.get(SPRM_PFINNERTTP, b""))
            if row_end:
                items = pending.pop()
                floating = _is_floating_table(sp)
                if floating and depth == 1:
                    # A positioned table's rows each carry their own TAP
                    # position; a following unpositioned row starts a new,
                    # in-flow table (ww8par2.cxx WW8TabDesc splits tables
                    # whose rows differ in position).
                    table_in_frame = False
                pending[-1].extend((t, fr or floating) for t, fr in items)
                if depth == 1:
                    flush_to(0)
    if dropcap:
        pending[-1].append((dropcap, False))
    flush_to(0)
    return flow, frames


class ParaInfo(NamedTuple):
    """One main-story flow paragraph with the metadata msdoc.blocks needs to
    rebuild structure. `text` and the paragraph order exactly match
    stories["main"]; frames (APO paragraphs, floating tables) are excluded,
    same as in build_stories's flow."""
    text: str
    mark: int   # CP of the paragraph/cell/row-end mark, or -1 for the last paragraph
    sprms: dict  # merged style+direct PAPX sprms (_ParaProps.sprms_at(mark))
    depth: int   # table nesting depth, 0 = not in a table
    row_end: bool  # True if `mark` ends a table row at `depth`
    start: int = -1  # CP of the paragraph's first character, -1 if unknown
    links: tuple = ()  # (start, end, url) spans into `text`, from HYPERLINK fields


def _route_structured(pars: list[tuple[str, int]], papx: "_ParaProps",
                      links: list[tuple[int, int, int, str]] = ()) -> list[ParaInfo]:
    """Like _route, but keeps each flow paragraph's sprms and table nesting
    instead of reducing it to plain text. Kept as a separate walk (rather
    than changing _route itself) so a bug here cannot regress the
    oracle-checked flat stories; msdoc.blocks cross-checks its output
    against stories["main"] before using it (see build_blocks).

    `links` (from stories._paragraphs, by index into `pars`) is carried
    along by that same original index so it survives frame exclusion,
    table-row buffering and drop-cap merging; a drop-cap prefix shifts its
    paragraph's link offsets by the prefix length.
    """
    links_by_idx: dict[int, list[tuple[int, int, str]]] = {}
    for idx, s, e, url in links:
        links_by_idx.setdefault(idx, []).append((s, e, url))
    flow: list[ParaInfo] = []
    # index = depth; items: (text, mark, sp, depth, row_end, frame, start, orig_idx)
    pending: list[list[tuple]] = [[]]
    dropcap = ""
    dropcap_start = -1

    def flush_to(level: int) -> None:
        while len(pending) - 1 > level:
            items = pending.pop()
            pending[-1].extend(items)
        if level == 0:
            for t, mark, sp, depth, row_end, fr, start, orig_idx in pending[0]:
                if not fr:
                    flow.append(ParaInfo(t, mark, sp, depth, row_end, start,
                                          tuple(links_by_idx.get(orig_idx, ()))))
            pending[0].clear()

    prev_depth = 0
    table_in_frame = False
    prev_mark = -1
    for orig_idx, (text, mark) in enumerate(pars):
        start = prev_mark + 1
        prev_mark = mark if mark >= 0 else prev_mark
        sp = papx.sprms_at(mark)
        if dropcap:
            prefix = len(dropcap)
            text, dropcap = dropcap + text, ""
            start = dropcap_start
            if prefix and orig_idx in links_by_idx:
                links_by_idx[orig_idx] = [(s + prefix, e + prefix, u) for s, e, u in links_by_idx[orig_idx]]
        dcs = sp.get(SPRM_PDCS, b"")
        if dcs and dcs[0] & 7 and _is_frame(sp):
            dropcap, dropcap_start = text, start
            continue
        depth = _table_depth(sp)
        apo = _is_frame(sp)
        if depth == 0:
            table_in_frame = False
        elif prev_depth == 0:
            table_in_frame = apo
        prev_depth = depth
        flush_to(depth)
        while len(pending) - 1 < depth:
            pending.append([])
        row_end = False
        if depth:
            row_end = bool(_u8(sp.get(SPRM_PFTTP, b"")) if depth == 1 else _u8(sp.get(SPRM_PFINNERTTP, b"")))
        pending[depth].append((text, mark, sp, depth, row_end, apo or table_in_frame, start, orig_idx))
        if depth and row_end:
            items = pending.pop()
            floating = _is_floating_table(sp)
            if floating and depth == 1:
                table_in_frame = False
            pending[-1].extend((t, m, s, d, r, fr or floating, st, oi) for t, m, s, d, r, fr, st, oi in items)
            if depth == 1:
                flush_to(0)
    if dropcap:
        pending[-1].append((dropcap, -1, {}, 0, False, False, dropcap_start, -1))
    flush_to(0)
    return flow


def build_main_structure(fib, word_document: bytes, table_stream: bytes, pieces, props) -> list[ParaInfo]:
    """Structured view of the main story's flow paragraphs for msdoc.blocks:
    same paragraphs, in the same order, as stories["main"] (frames excluded).
    Returns [] on any structural problem (never raises)."""
    try:
        flags = _RunFlags(props)
        papx = _ParaProps(fib, word_document, table_stream, pieces)
        n = max(0, int(getattr(fib, "ccpText", 0) or 0))
        pars, links = _paragraphs(pieces, 0, n, flags, [], note=False)
        return _route_structured(pars, papx, links)
    except (struct.error, IndexError, ValueError):
        return []


_CODE, _RESULT, _SUPPRESSED = 0, 1, 2
_MAX_FIELD_DEPTH = 1000
_MAX_CODE_CHARS = 4096


def _macrobutton_text(code: list[str]):
    """MACROBUTTON fields display the text after the macro name instead of
    their result (LibreOffice ww8par5.cxx Read_F_Macro); None for other
    field types."""
    words = "".join(code).split(None, 2)
    if not words or words[0].upper() != "MACROBUTTON":
        return None
    return words[2] if len(words) > 2 else ""


def _field_param_tokens(data: str):
    """Tokenize a field instruction's parameters: quoted/bare-word text
    tokens (yielded as (-2, text)) and "\\<letter>" switches (yielded as
    (letter, None)). Port of filter/source/msfilter/util.cxx
    WW8ReadFieldParams::SkipToNextToken / FindNextStringPiece (MPL-2.0),
    simplified to the cases HYPERLINK's switches actually use. `i` strictly
    increases every iteration, so this is bounded by len(data)."""
    n = len(data)
    i = 0
    while i < n:
        while i < n and data[i] == " ":
            i += 1
        if i >= n:
            return
        start = i
        if data[start] in "\"“":
            i += 1
            qstart = i
            while i < n and data[i] not in "\"”":
                i += 1
            text = data[qstart:i]
            if i < n:
                i += 1
            yield -2, text
            continue
        if data[start] == "\\" and (start + 1 >= n or data[start + 1] != "\\"):
            ch = data[start + 1] if start + 1 < n else " "
            i = start + 2
            yield ch, None
            continue
        j = start
        while j < n and data[j] != " ":
            if data[j] == "\\":
                if j + 1 < n and data[j + 1] == "\\":
                    j += 2
                    continue
                break
            j += 1
        i = j + 1 if j < n else j
        yield -2, data[start:j]


def _hyperlink_url(code: str):
    """HYPERLINK field instruction -> target URL ("#bookmark" for a \\l
    switch with no external file), or None if this is not a HYPERLINK
    field or has neither. Port of ww8par5.cxx
    SwWW8ImplReader::Read_F_Hyperlink (MPL-2.0): the first bare/quoted
    token before any switch is the file name; \\l takes the next token as
    the bookmark; \\t (target) is read and discarded; the others
    (\\n \\h \\m \\s) take no extra argument."""
    head, _, rest = code.strip().partition(" ")
    if head.upper() != "HYPERLINK":
        return None
    url = mark = ""
    seen_switch = False
    it = _field_param_tokens(rest)
    for kind, text in it:
        if kind == -2:
            if not url and not seen_switch:
                url = text
        elif kind == "l":
            seen_switch = True
            nxt = next(it, None)
            if nxt is not None and nxt[0] == -2:
                mark = nxt[1][:-1] if nxt[1].endswith('"') else nxt[1]
        elif kind == "t":
            seen_switch = True
            next(it, None)
        elif kind in ("n", "h", "m", "s"):
            seen_switch = True
    if mark:
        url = url + "#" + mark if url else "#" + mark
    return url or None


class _StoryBuilder:
    """Turns raw characters into paragraphs. Field state is kept per
    instance; one instance per story range."""

    def __init__(self, anchors: list[int]) -> None:
        # CP of the first character of a foot/endnote, -2 outside notes
        self.note_start = -2
        # (paragraph text, CP of the character that ended it or -1)
        self.pars: list[tuple[str, int]] = []
        self.cur: list[str] = []
        # Field stack: True while inside the field code (before 0x14),
        # False in the result part. Text is visible when no enclosing
        # field is in its code part.
        self.fields: list[list] = []  # [state, code chars, link info or None]
        self.in_code = 0  # fields whose text is currently hidden
        self.anchors = anchors
        # (paragraph index in self.pars once closed, start, end, url) for
        # every HYPERLINK field's result text. Only tracked for a field
        # that is not nested in another one (self.fields has just the one
        # entry when its separator is seen) -- HYPERLINK fields are not
        # meaningfully nested in real documents, and a result range cannot
        # be attributed to an enclosing field's paragraph/offset space.
        self.links: list[tuple[int, int, int, str]] = []

    def feed(self, text: str, cp0: int, flags: int) -> None:
        if flags & _RunFlags.DELETED:
            # Tracked deletion: LibreOffice's golden shows the document with
            # changes accepted, so deleted text (paragraph marks included)
            # disappears.
            return
        spec = bool(flags & _RunFlags.SPEC)
        symbol = chr(flags >> _RunFlags.SYMBOL_SHIFT) if flags >> _RunFlags.SYMBOL_SHIFT else ""
        cur = self.cur
        for i, ch in enumerate(text):
            if ch == _FIELD_BEGIN:
                if len(self.fields) < _MAX_FIELD_DEPTH:
                    self.fields.append([_CODE, [], None])
                    self.in_code += 1
                continue
            if ch == _FIELD_SEP:
                top = self.fields[-1] if self.fields else None
                if top and top[0] == _CODE:
                    display = _macrobutton_text(top[1])
                    if display is None:
                        top[0] = _RESULT
                        self.in_code -= 1
                        if len(self.fields) == 1:
                            url = _hyperlink_url("".join(top[1]))
                            if url:
                                top[2] = (len(cur), url, len(self.pars))
                    else:
                        if self.in_code == 1:
                            cur.append(display)
                        top[0] = _SUPPRESSED
                continue
            if ch == _FIELD_END:
                if self.fields:
                    state, code, link = self.fields.pop()
                    if state == _CODE and self.in_code == 1:
                        display = _macrobutton_text(code)
                        if display:
                            cur.append(display)
                    if state != _RESULT:
                        self.in_code -= 1
                    elif link is not None:
                        start, url, par_idx = link
                        if par_idx == len(self.pars) and len(cur) > start:
                            self.links.append((par_idx, start, len(cur), url))
                continue
            if self.in_code:
                top = self.fields[-1]
                if top[0] == _CODE and len(top[1]) < _MAX_CODE_CHARS:
                    top[1].append(ch)
                continue
            if ch in _PAR_BREAKS:
                self.pars.append(("".join(cur), cp0 + i))
                cur = self.cur = []
                continue
            if symbol and (ch >= " " or ch == "\x28"):
                cur.append(symbol)
                continue
            if spec and ch in _SPEC_CHARS:
                if ch in _ANCHOR_CHARS:
                    self.anchors.append(cp0 + i)
                elif ch == "\x02" and self.note_start != -2 and cp0 + i != self.note_start:
                    # Auto-numbered note mark inside note text: LibreOffice
                    # keeps a "?" placeholder for it; only the mark that
                    # opens the note is removed (ww8par.cxx End_Footnote).
                    cur.append("?")
                continue
            if ch == "\x0b":
                cur.append("\n")
                continue
            o = ord(ch)
            if o in _CHAR_MAP:
                rep = _CHAR_MAP[o]
                if rep:
                    cur.append(rep)
                continue
            if o < 0x20 and ch != "\t":
                continue
            cur.append(ch)

    def finish(self) -> tuple[list[tuple[str, int]], list[tuple[int, int, int, str]]]:
        if self.cur:
            self.pars.append(("".join(self.cur), -1))
            self.cur = []
        return self.pars, self.links


def _paragraphs(pieces, lo: int, hi: int, flags: _RunFlags, anchors: list[int],
                note: bool = False) -> tuple[list[tuple[str, int]], list[tuple[int, int, int, str]]]:
    """(paragraphs, links): links are (par_index, start, end, url) spans
    into the paragraph at that 0-based index of the returned list, for
    every HYPERLINK field result found ([MS-DOC] 2.8.25 PlcFld)."""
    b = _StoryBuilder(anchors)
    if note:
        b.note_start = lo
    for s, e, fl in flags.segments(lo, hi):
        b.feed(pieces.text(s, e), s, fl)
    return b.finish()


# PlcfHdd story order within a section ([MS-DOC] 2.9.? Plcfhdd).
_HDD_EVEN_HEADER, _HDD_ODD_HEADER, _HDD_EVEN_FOOTER, _HDD_ODD_FOOTER, _HDD_FIRST_HEADER, _HDD_FIRST_FOOTER = range(6)
SPRM_SFTITLEPAGE = 0x300A
_SED_SIZE = 12


def _section_title_pages(fib, word_document: bytes, table: bytes) -> list[bool]:
    """fTitlePage of every section: PlcfSed -> Sed.fcSepx -> Sepx grpprl
    ([MS-DOC] 2.8.? PlcfSed, 2.9.? Sed, 2.9.? Sepx, sprmSFTitlePage)."""
    got = fib.pair("PlcfSed", len(table)) if hasattr(fib, "pair") else None
    if not got or got[1] < 4 + 4 + _SED_SIZE:
        return []
    fc, lcb = got
    n = (lcb - 4) // (4 + _SED_SIZE)
    out = []
    for i in range(n):
        off = fc + (n + 1) * 4 + i * _SED_SIZE + 2
        fc_sepx = struct.unpack_from("<i", table, off)[0]
        title = False
        if 0 <= fc_sepx and fc_sepx + 2 <= len(word_document):
            cb = struct.unpack_from("<H", word_document, fc_sepx)[0]
            grpprl = word_document[fc_sepx + 2:fc_sepx + 2 + cb]
            for sid, op in _iter_sprms(grpprl):
                if sid == SPRM_SFTITLEPAGE:
                    title = bool(_u8(op))
        out.append(title)
    return out


def _facing_pages(fib, table: bytes) -> bool:
    """Dop.fFacingPages ([MS-DOC] 2.7.? DopBase, bit 0 of the first byte)."""
    got = fib.pair("Dop", len(table)) if hasattr(fib, "pair") else None
    if not got or got[1] < 1:
        return False
    return bool(table[got[0]] & 1)


def _header_ranges(cps: list[int], base: int, limit: int, title_pages: list[bool], facing: bool) -> list[tuple[int, int]]:
    """Header/footer stories LibreOffice actually shows.

    After the 6 separator stories, PlcfHdd has 6 stories per section; the
    final aCP entry only closes the list. Even-page stories are used only
    with Dop.fFacingPages, first-page ones only when the section has
    sprmSFTitlePage. An empty story inherits the previous section's story
    of the same kind ([MS-DOC] 2.3.? Header and footer; LibreOffice
    ww8par6.cxx SwWW8ImplReader::SetHdFt / WW8PLCFx_HdFt::GetTextPosExact).
    """
    raw = []
    for i in range(_HDD_SEPARATORS, len(cps) - 2):
        lo = base + max(0, cps[i])
        hi = min(base + cps[i + 1], base + limit)
        raw.append((lo, hi))
    nsec = len(raw) // 6
    if not title_pages:
        title_pages = [False] * nsec
    out: list[tuple[int, int]] = []
    last: dict[int, tuple[int, int]] = {}
    for sec in range(nsec):
        title = title_pages[sec] if sec < len(title_pages) else False
        for k in range(6):
            lo, hi = raw[sec * 6 + k]
            if hi > lo:
                last[k] = (lo, hi)
            used = (k in (_HDD_ODD_HEADER, _HDD_ODD_FOOTER)
                    or (facing and k in (_HDD_EVEN_HEADER, _HDD_EVEN_FOOTER))
                    or (title and k in (_HDD_FIRST_HEADER, _HDD_FIRST_FOOTER)))
            if used and k in last:
                out.append(last[k])
    return out


# OfficeArt record types ([MS-ODRAW] 2.2.x).
_RT_DGG_CONTAINER = 0xF000
_RT_DG_CONTAINER = 0xF002
_RT_SPGR_CONTAINER = 0xF003
_RT_SP_CONTAINER = 0xF004
_RT_FSP = 0xF00A
_SPA_SIZE = 26


def _records(data: bytes, lo: int, hi: int):
    """Yield (rec_type, is_container, body_lo, body_hi) of the OfficeArt
    records in data[lo:hi] ([MS-ODRAW] 2.1.1 OfficeArtRecordHeader)."""
    pos = lo
    while pos + 8 <= hi:
        ver_inst, rtype, rlen = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        end = min(body + rlen, hi)
        yield rtype, (ver_inst & 0xF) == 0xF, body, end
        pos = body + rlen


def _first_spid(data: bytes, lo: int, hi: int) -> int:
    """spid of the first OfficeArtFSP in an SpContainer body."""
    for rtype, _, b, e in _records(data, lo, hi):
        if rtype == _RT_FSP and e - b >= 4:
            return struct.unpack_from("<I", data, b)[0]
    return -1


def _drawings(fib, table: bytes):
    """Yield (body_lo, body_hi) of every OfficeArtDgContainer.
    [MS-DOC] 2.9.? OfficeArtContent: an OfficeArtDggContainer, then per
    drawing a 1-byte dgglbl followed by an OfficeArtDgContainer."""
    got = fib.pair("DggInfo", len(table)) if hasattr(fib, "pair") else None
    if not got or got[1] < 8:
        return
    pos, hi = got[0], got[0] + got[1]
    while pos + 8 <= hi:
        rtype = struct.unpack_from("<H", table, pos + 2)[0]
        if rtype not in (_RT_DGG_CONTAINER, _RT_DG_CONTAINER):
            pos += 1  # the dgglbl byte before each drawing
            if pos + 8 > hi:
                break
            rtype = struct.unpack_from("<H", table, pos + 2)[0]
            if rtype != _RT_DG_CONTAINER:
                break
        rlen = struct.unpack_from("<I", table, pos + 4)[0]
        body, end = pos + 8, min(pos + 8 + rlen, hi)
        if rtype == _RT_DG_CONTAINER:
            yield body, end
        pos = body + rlen


def _shape_groups(fib, table: bytes) -> dict[int, int]:
    """Map every shape id inside a top-level group to the group's shape
    id (the id a Spa anchor refers to). [MS-ODRAW] 2.2.13
    OfficeArtDgContainer, 2.2.16 OfficeArtSpgrContainer."""
    out: dict[int, int] = {}
    budget = [100000]

    def collect(b: int, e: int, top: int, depth: int) -> None:
        for rtype, cont, rb, re_ in _records(table, b, e):
            budget[0] -= 1
            if budget[0] < 0 or depth > 64:
                return
            if rtype == _RT_FSP and re_ - rb >= 4:
                out[struct.unpack_from("<I", table, rb)[0]] = top
            elif cont:
                collect(rb, re_, top, depth + 1)

    for body, end in _drawings(fib, table):
        for t1, _, b1, e1 in _records(table, body, end):
            if t1 != _RT_SPGR_CONTAINER:
                continue
            # patriarch: its child groups are the top-level shapes
            for t2, _, b2, e2 in _records(table, b1, e1):
                if t2 == _RT_SPGR_CONTAINER:
                    first = next(iter(_records(table, b2, e2)), None)
                    if first and first[0] == _RT_SP_CONTAINER:
                        top = _first_spid(table, first[2], first[3])
                        if top >= 0:
                            collect(b2, e2, top, 0)
    return out


_RT_OPT = 0xF00B
_RT_TERTIARY_OPT = 0xF122
_PID_GTEXT_UNICODE = 0x00C0


def _gtext(data: bytes, lo: int, hi: int) -> str:
    """gtextUNICODE ([MS-ODRAW] 2.3.22.? Geometry Text) of an OfficeArtFOPT
    body: n 6-byte properties, then the complex data in property order."""
    nprops = struct.unpack_from("<H", data, lo - 8)[0] >> 4
    cpos = lo + 6 * nprops
    if cpos > hi:
        return ""
    for i in range(nprops):
        pid, op = struct.unpack_from("<HI", data, lo + 6 * i)
        if not pid & 0x8000:
            continue
        if pid & 0x3FFF == _PID_GTEXT_UNICODE:
            raw = data[cpos:min(cpos + op, hi)]
            return raw[:len(raw) - len(raw) % 2].decode("utf-16-le", "replace").split("\0", 1)[0]
        cpos += op
        if cpos > hi:
            break
    return ""


def _wordart(fib, table: bytes) -> list[tuple[int, str]]:
    """(spid, text) of every WordArt shape (one with gtextUNICODE).
    LibreOffice imports these as shapes with text (msdffimp.cxx
    ImportObj / ImportFontWork), which the golden lists as textboxes."""
    out: list[tuple[int, str]] = []
    budget = [100000]

    def walk(lo: int, hi: int, depth: int) -> None:
        for rtype, cont, b, e in _records(table, lo, hi):
            budget[0] -= 1
            if budget[0] < 0 or depth > 64:
                return
            if rtype == _RT_SP_CONTAINER:
                spid, text = -1, ""
                for t2, _, b2, e2 in _records(table, b, e):
                    if t2 == _RT_FSP and e2 - b2 >= 4:
                        spid = struct.unpack_from("<I", table, b2)[0]
                    elif t2 in (_RT_OPT, _RT_TERTIARY_OPT) and not text:
                        text = _gtext(table, b2, e2)
                if spid >= 0 and text.strip():
                    out.append((spid, text))
            elif cont:
                walk(b, e, depth + 1)

    for body, end in _drawings(fib, table):
        walk(body, end, 0)
    return out


def _spa_anchors(fib, table: bytes, name: str, base: int) -> dict[int, list[int]]:
    """Spa.lid -> absolute anchor CPs from PlcSpaMom / PlcSpaHdr (whose
    aCP are relative to the start of their story)."""
    spa = fib.pair(name, len(table)) if hasattr(fib, "pair") else None
    anchors: dict[int, list[int]] = {}
    if spa and spa[1] >= 4 + 4 + _SPA_SIZE:
        m = (spa[1] - 4) // (4 + _SPA_SIZE)
        for i in range(m):
            cp = struct.unpack_from("<i", table, spa[0] + 4 * i)[0]
            lid = struct.unpack_from("<i", table, spa[0] + (m + 1) * 4 + _SPA_SIZE * i)[0]
            anchors.setdefault(lid, []).append(base + cp)
    return anchors


def _visible_header_textboxes(fib, table: bytes, cps: list[int], start: int, n: int,
                              hdr_start: int, used_headers: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Header textbox stories, minus those whose shape is anchored only in
    header stories LibreOffice does not use (see _header_ranges): those
    shapes are never imported. FTXBXS.lid ([MS-DOC] 2.9.? FTXBXS) names
    the textbox shape; PlcSpaHdr maps anchor CPs (relative to the header
    document) to Spa.lid, the top-level shape id."""
    got = fib.pair("PlcfHdrtxbxTxt", len(table))
    if not got:
        return _ranges(cps, start, n)
    anchors = _spa_anchors(fib, table, "PlcSpaHdr", hdr_start)
    groups = _shape_groups(fib, table)

    def visible(cp: int) -> bool:
        return any(lo <= cp < hi for lo, hi in used_headers)

    out = []
    k = len(cps) - 1
    for i in range(k):
        lo = start + max(0, cps[i])
        hi = min(start + cps[i + 1], start + n)
        if hi <= lo:
            continue
        off = got[0] + k * 4 + 4 + i * _FTXBXS_SIZE + 14
        if off + 4 <= got[0] + got[1]:
            lid = struct.unpack_from("<i", table, off)[0]
            where = anchors.get(groups.get(lid, lid))
            if where and not any(visible(cp) for cp in where):
                continue
        out.append((lo, hi))
    return out


def _cp_layout(fib) -> dict[str, tuple[int, int]]:
    """key -> (base CP, ccp) for every story/sub-story in document CP order
    ([MS-DOC] 2.3 Document Parts): the same accumulation build_stories
    uses for its own `layout` list, factored out so msdoc.blocks can get a
    single story's range without reimplementing the running offset."""
    out: dict[str, tuple[int, int]] = {}
    base = 0
    for key, attr in (("main", "ccpText"), ("footnotes", "ccpFtn"), ("headers", "ccpHdd"),
                      ("comments", "ccpAtn"), ("endnotes", "ccpEdn"), ("textboxes", "ccpTxbx"),
                      ("hdrtextboxes", "ccpHdrTxbx")):
        n = max(0, int(getattr(fib, attr, 0) or 0))
        out[key] = (base, n)
        base += n
    return out


def note_ranges(fib, table_stream: bytes, kind: str) -> list[tuple[int, int]]:
    """Per-note (lo, hi) CP ranges for kind in ("footnotes", "endnotes")
    ([MS-DOC] 2.9.? PlcffndTxt/PlcfendTxt): one pair per real footnote or
    endnote, unlike build_stories's flat, all-notes-concatenated story
    text."""
    base, n = _cp_layout(fib).get(kind, (0, 0))
    if n <= 0:
        return []
    name = "PlcffndTxt" if kind == "footnotes" else "PlcfendTxt"
    cps = _plc_cps(fib, table_stream, name, 0)
    if len(cps) < 2:
        return [(base, base + n)]
    return _ranges(cps, base, n)


def header_ranges(fib, word_document: bytes, table_stream: bytes) -> list[tuple[int, int]]:
    """Header/footer (lo, hi) CP ranges LibreOffice actually shows, in the
    same order as stories["headers"] (see _header_ranges)."""
    base, n = _cp_layout(fib).get("headers", (0, 0))
    if n <= 0:
        return []
    cps = _plc_cps(fib, table_stream, "PlcfHdd", 0)
    if len(cps) < 2:
        return []
    return _header_ranges(cps, base, n, _section_title_pages(fib, word_document, table_stream),
                          _facing_pages(fib, table_stream))


def textbox_ranges(fib, table_stream: bytes) -> list[tuple[int, int]]:
    """Main-story text-box shape (lo, hi) CP ranges ([MS-DOC] PlcftxbxTxt),
    in the same order as stories["textboxes"]'s own-shape portion
    (build_stories's emit("textboxes", ...))."""
    base, n = _cp_layout(fib).get("textboxes", (0, 0))
    if n <= 0:
        return []
    cps = _plc_cps(fib, table_stream, "PlcftxbxTxt", _FTXBXS_SIZE)
    if len(cps) < 2:
        return [(base, base + n)]
    return _ranges(cps, base, n)


def header_textbox_ranges(fib, word_document: bytes, table_stream: bytes) -> list[tuple[int, int]]:
    """Header/footer text-box shape (lo, hi) CP ranges LibreOffice shows
    ([MS-DOC] PlcfHdrtxbxTxt), mirroring build_stories's own selection
    (_visible_header_textboxes needs the already-selected header ranges
    and the headers story's own base CP, same as build_stories's
    hdr_start)."""
    base, n = _cp_layout(fib).get("hdrtextboxes", (0, 0))
    if n <= 0:
        return []
    cps = _plc_cps(fib, table_stream, "PlcfHdrtxbxTxt", _FTXBXS_SIZE)
    if len(cps) < 2:
        return []
    used_headers = header_ranges(fib, word_document, table_stream)
    if not used_headers:
        return []
    hdr_start = _cp_layout(fib).get("headers", (0, 0))[0]
    return _visible_header_textboxes(fib, table_stream, cps, base, n, hdr_start, used_headers)


def build_structured_ranges(fib, word_document: bytes, table_stream: bytes, pieces, props,
                           ranges: list[tuple[int, int]], note: bool = False) -> list[list[ParaInfo]]:
    """One ParaInfo list per (lo, hi) in `ranges`, sharing a single
    _RunFlags/_ParaProps pass over the whole document: both scan the
    entire CHPX/PapxFkp tables regardless of range, so building them once
    and reusing them (e.g. one range per footnote, or per header/footer
    story) avoids redundant full-document rescans."""
    try:
        flags = _RunFlags(props)
        papx = _ParaProps(fib, word_document, table_stream, pieces)
    except (struct.error, IndexError, ValueError):
        return [[] for _ in ranges]
    out: list[list[ParaInfo]] = []
    for lo, hi in ranges:
        try:
            pars, links = _paragraphs(pieces, lo, hi, flags, [], note=note)
            out.append(_route_structured(pars, papx, links))
        except (struct.error, IndexError, ValueError):
            out.append([])
    return out


def build_note_structure(fib, word_document: bytes, table_stream: bytes, pieces, props,
                         kind: str) -> list[tuple[list[str], list[ParaInfo]]]:
    """Per-note (expected flat paragraph text, structured ParaInfo list)
    pairs for kind in ("footnotes", "endnotes").

    The flat text is exactly what build_stories puts in stories[kind] for
    this one note (_flatten_cells then _route -- LibreOffice imports a
    table inside a footnote/endnote as plain paragraphs, the cells of a
    row run together, see _flatten_cells); the structured list is the same
    note's raw, un-flattened paragraphs with heading/list/table metadata.
    When the two don't match 1:1 (a table was flattened away), the caller
    should fall back to the flat text for that one note rather than
    mis-attribute structure to the wrong paragraph."""
    out: list[tuple[list[str], list[ParaInfo]]] = []
    try:
        flags = _RunFlags(props)
        papx = _ParaProps(fib, word_document, table_stream, pieces)
    except (struct.error, IndexError, ValueError):
        return out
    for lo, hi in note_ranges(fib, table_stream, kind):
        try:
            pars, _links = _paragraphs(pieces, lo, hi, flags, [], note=True)
            flat, _frames = _route(_flatten_cells(pars, pieces, papx), papx)
            structured = _route_structured(pars, papx)
        except (struct.error, IndexError, ValueError):
            flat, structured = [], []
        out.append((flat, structured))
    return out


def build_stories(fib, word_document: bytes, table_stream: bytes, pieces, props,
                  data_stream: bytes | None = None) -> tuple[dict[str, list[str]], dict[str, list[int]]]:
    """Return (stories, anchors) where anchors maps story -> list of CPs of
    0x01 (inline picture) and 0x08 (drawn object) special characters.
    `data_stream` (optional) is used for the text of inline WordArt."""
    stories: dict[str, list[str]] = {k: [] for k in ("main", "footnotes", "endnotes", "headers", "comments", "textboxes")}
    anchors: dict[str, list[int]] = {k: [] for k in stories}
    flags = _RunFlags(props)
    papx = _ParaProps(fib, word_document, table_stream, pieces)

    def ccp(name: str) -> int:
        return max(0, int(getattr(fib, name, 0) or 0))

    base = 0
    layout = []
    for key, attr in (("main", "ccpText"), ("footnotes", "ccpFtn"), ("headers", "ccpHdd"),
                      ("comments", "ccpAtn"), ("endnotes", "ccpEdn"), ("textboxes", "ccpTxbx"),
                      ("hdrtextboxes", "ccpHdrTxbx")):
        n = ccp(attr)
        layout.append((key, base, n))
        base += n

    def emit(key: str, ranges) -> None:
        story = "textboxes" if key == "hdrtextboxes" else key
        for lo, hi in ranges:
            pars, _links = _paragraphs(pieces, lo, hi, flags, anchors[story], note=story in ("footnotes", "endnotes"))
            if story == "textboxes":
                stories[story].extend(t for t, _ in pars)
                continue
            if story in ("footnotes", "endnotes"):
                pars = _flatten_cells(pars, pieces, papx)
            flow, frames = _route(pars, papx)
            if story == "comments":
                # LibreOffice stores comment text as a plain string where
                # line breaks are newlines; the golden lists them as
                # separate paragraphs.
                flow = [line for t in flow for line in t.split("\n")]
            stories[story].extend(flow)
            stories["textboxes"].extend(frames)

    used_headers = None
    hdr_start = layout[2][1]
    plcs = {
        "footnotes": ("PlcffndTxt", 0, 0),
        "headers": ("PlcfHdd", 0, _HDD_SEPARATORS),
        "comments": ("PlcfandTxt", 0, 0),
        "endnotes": ("PlcfendTxt", 0, 0),
        "textboxes": ("PlcftxbxTxt", _FTXBXS_SIZE, 0),
        "hdrtextboxes": ("PlcfHdrtxbxTxt", _FTXBXS_SIZE, 0),
    }
    for key, start, n in layout:
        if n <= 0:
            continue
        if key == "main":
            emit(key, [(start, start + n)])
            continue
        name, cb, skip = plcs[key]
        cps = _plc_cps(fib, table_stream, name, cb)
        if key == "headers":
            if len(cps) >= 2:
                used_headers = _header_ranges(cps, start, n, _section_title_pages(fib, word_document, table_stream),
                                              _facing_pages(fib, table_stream))
                emit(key, used_headers)
        elif key == "hdrtextboxes" and len(cps) >= 2 and used_headers is not None:
            emit(key, _visible_header_textboxes(fib, table_stream, cps, start, n, hdr_start, used_headers))
        elif len(cps) >= 2:
            emit(key, _ranges(cps, start, n, skip))
        else:
            emit(key, [(start, start + n)])
    stories["textboxes"].extend(_visible_wordart(fib, table_stream, hdr_start, used_headers))
    if data_stream:
        for key in stories:
            for cp in anchors[key]:
                stories["textboxes"].extend(_inline_wordart(props, cp, data_stream))
    return stories, anchors


_PICF_MM_SHAPEFILE = 0x0066


def _inline_wordart(props, cp: int, data: bytes) -> list[str]:
    """WordArt text of an inline shape: sprmCPicLocation -> PICFAndOfficeArtData
    in the Data stream ([MS-DOC] 2.9.192/2.9.193: PICF.lcb, cbHeader,
    mfpf.mm; MM_SHAPEFILE adds a cchPicName-prefixed name), then an
    OfficeArtInlineSpContainer whose OPT may carry gtextUNICODE."""
    try:
        sprms = props.sprms_at(cp)
        op = sprms.get(SPRM_CPICLOCATION)
        if not op or len(op) < 4 or sprms.get(SPRM_CFDATA, b"\0")[:1] not in (b"", b"\0"):
            return []
        off = struct.unpack_from("<i", op, 0)[0]
        if off < 0 or off + 8 > len(data):
            return []
        lcb, cb_header, _, mm = struct.unpack_from("<iHHH", data, off)
        end = min(off + lcb, len(data))
        pos = off + cb_header
        if mm == _PICF_MM_SHAPEFILE and pos < end:
            pos += 1 + data[pos]
        if lcb <= cb_header or pos + 8 > end:
            return []
        rtype = struct.unpack_from("<H", data, pos + 2)[0]
        if rtype != _RT_SP_CONTAINER:
            return []
        for t2, _, b2, e2 in _records(data, pos + 8, min(pos + 8 + struct.unpack_from("<I", data, pos + 4)[0], end)):
            if t2 in (_RT_OPT, _RT_TERTIARY_OPT):
                text = _gtext(data, b2, e2)
                if text.strip():
                    return text.split("\n")
    except (struct.error, IndexError):
        pass
    return []


def _visible_wordart(fib, table: bytes, hdr_start: int, used_headers) -> list[str]:
    """Text of WordArt shapes anchored in the main story or in a header
    story LibreOffice shows."""
    try:
        shapes = _wordart(fib, table)
        if not shapes:
            return []
        groups = _shape_groups(fib, table)
        main = _spa_anchors(fib, table, "PlcSpaMom", 0)
        hdr = _spa_anchors(fib, table, "PlcSpaHdr", hdr_start)
    except (struct.error, IndexError):
        return []
    out = []
    for spid, text in shapes:
        top = groups.get(spid, spid)
        if top in main or any(lo <= cp < hi for cp in hdr.get(top, ()) for lo, hi in (used_headers or ())):
            out.extend(text.split("\n"))
    return out
