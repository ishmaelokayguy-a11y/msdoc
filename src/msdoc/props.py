# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice sw/source/filter/ww8/ww8scan.cxx (MPL-2.0).
"""Character properties needed for extraction (not for formatting).

We only need a handful of sprms:
  sprmCFSpec      0x0855  character is special (0x01 picture, 0x08 drawn object, 0x02 note ref, ...)
  sprmCFData      0x0806  0x01 char is a field data holder, not a picture
  sprmCFOle2      0x080A  0x01 char is an embedded OLE object
  sprmCPicLocation 0x6A03 offset of PICFAndOfficeArtData in the Data stream
  sprmCFObj       0x0856
  sprmCFVanish    0x083C  hidden text (keep it; LibreOffice keeps hidden text too)

Spec: [MS-DOC] 2.9.62 ChpxFkp, 2.9.25 BxPap/Chpx, 2.8.? PlcBteChpx,
2.9.? Sprm (operand size from spra), 2.6.1 Character properties,
2.9.? Prl/grpprl in Prc (piece-level sprms, Pcd.prm, Prm0/Prm1).
LibreOffice reference: sw/source/filter/ww8/ww8scan.cxx — WW8PLCFx_Fc_FKP,
WW8PLCFx_Cp_FKP, WW8Fkp, wwSprmParser (GetSprmSize, the sprm size table),
WW8PLCFx_SEPX; ww8par6.cxx for what each sprm means.

Requirements:
  * iterate CHPX FKP runs over the whole document by FC, resolve to CP via
    the piece table; also apply piece-level sprms (Pcd.prm) on top
  * expose: char_sprms_at(cp) -> dict[int, bytes] (sprm id -> operand bytes)
    and an efficient iterator over runs [(cp_start, cp_end, sprms)]
  * unknown sprm ids must be skipped using the spra-derived operand size;
    sprmTDefTable / sprmPChgTabs have variable-length special cases
"""
from __future__ import annotations

import bisect
import struct

SPRM_CFSPEC = 0x0855
SPRM_CFDATA = 0x0806
SPRM_CFOLE2 = 0x080A
SPRM_CPICLOCATION = 0x6A03
SPRM_CFOBJ = 0x0856
SPRM_CFVANISH = 0x083C

# [MS-DOC] 2.6.1 Sprm / ww8scan.cxx wwSprmParser::GetSprmTailLen special cases.
_SPRM_TDEFTABLE10 = 0xD606
_SPRM_TDEFTABLE = 0xD608
_SPRM_PCHGTABS = 0xC615

# [MS-DOC] 2.6.1 Sprm: spra (top 3 bits of the 16-bit sprm id) -> fixed
# operand size. spra == 6 (variable length) is handled separately below.
_FIXED_OPERAND_SIZE = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}

# [MS-DOC] 2.9.215 Prm0: isprm (7-bit index, from Pcd.prm bits 1-7) -> Sprm id.
# Ported verbatim from the aSprmId[0x80] table in
# WW8PLCFx_PCDAttrs::GetSprms (ww8scan.cxx); 0 means "no such sprm" (noop).
_PRM0_ISPRM_TO_SPRM = [
    0x0000, 0x0000, 0x0000, 0x0000,
    0x2402, 0x2403, 0x2404, 0x2405,
    0x2406, 0x2407, 0x2408, 0x2409,
    0x260A, 0x0000, 0x240C, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
    0x2416, 0x2417, 0x0000, 0x0000,
    0x0000, 0x261B, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x2423, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
    0x242A, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x2430, 0x2431,
    0x0000, 0x2433, 0x2434, 0x2435,
    0x2436, 0x2437, 0x2438, 0x0000,
    0x0000, 0x243B, 0x0000, 0x0000,
    0x0000, 0x0800, 0x0801, 0x0802,
    0x0000, 0x0000, 0x0000, 0x0806,
    0x0000, 0x0000, 0x0000, 0x080A,
    0x0000, 0x2A0C, 0x0858, 0x2859,
    0x0000, 0x0000, 0x0000, 0x2A33,
    0x0000, 0x0835, 0x0836, 0x0837,
    0x0838, 0x0839, 0x083A, 0x083B,
    0x083C, 0x0000, 0x2A3E, 0x0000,
    0x0000, 0x0000, 0x2A42, 0x0000,
    0x2A44, 0x0000, 0x2A46, 0x0000,
    0x2A48, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x2A53,
    0x0854, 0x0855, 0x0856, 0x2E00,
    0x2640, 0x2441, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000,
]


def _sgc(sprm: int) -> int:
    """[MS-DOC] 2.6.1 Sprm.sgc: 2 means "modifies a character property"."""
    return (sprm >> 10) & 0x7


def sprm_operand_size(sprm: int, data: bytes, pos: int) -> int:
    """Size in bytes of the operand of `sprm` whose operand starts at data[pos].

    This does not include the 2-byte sprm id itself. Bounded against `data`
    so a truncated/corrupt grpprl never reads out of range.
    """
    spra = (sprm >> 13) & 0x7
    fixed = _FIXED_OPERAND_SIZE.get(spra)
    if fixed is not None:
        return fixed
    # spra == 6: variable length.
    if sprm in (_SPRM_TDEFTABLE, _SPRM_TDEFTABLE10):
        # ww8scan.cxx GetSprmTailLen, L_VAR2 case: 2-byte count (count-1 is
        # the actual data length), plus the 2 count bytes themselves.
        if pos + 2 > len(data):
            return 2
        count = struct.unpack_from("<H", data, pos)[0]
        data_len = count - 1 if count >= 1 else 0
        return 2 + data_len
    if sprm == _SPRM_PCHGTABS:
        # ww8scan.cxx GetSprmTailLen, sprmPChgTabs special case.
        if pos >= len(data):
            return 1
        lenbyte = data[pos]
        if lenbyte != 0xFF:
            return 1 + lenbyte
        n_del = data[pos + 1] if pos + 1 < len(data) else 0
        ins_idx = pos + 2 + 4 * n_del
        n_ins = data[ins_idx] if ins_idx < len(data) else 0
        return 1 + 2 + 4 * n_del + 3 * n_ins
    # Generic variable-length sprm: 1-byte length prefix + that many bytes.
    if pos >= len(data):
        return 1
    return 1 + data[pos]


def _iter_sprms(data: bytes):
    """Yield (sprm_id, operand_bytes) for a grpprl, bounded by len(data)."""
    pos = 0
    n = len(data)
    while pos + 2 <= n:
        sprm_id = struct.unpack_from("<H", data, pos)[0]
        op_start = pos + 2
        op_size = sprm_operand_size(sprm_id, data, op_start)
        op_end = op_start + op_size
        if op_end > n:
            yield sprm_id, data[op_start:n]
            break
        yield sprm_id, data[op_start:op_end]
        pos = op_end


# [MS-DOC] 2.9.33 ChpxFkp: 512-byte page, crun in the last byte.
_FKP_PAGE_SIZE = 512
_MAX_CRUN = 0x65
_MAX_FKP_PAGES = 1 << 20


class CharProps:
    def __init__(self, fib, word_document: bytes, table_stream: bytes, pieces) -> None:
        self._word_document = word_document
        self._pieces = pieces
        self.warnings: list[str] = []
        # Sorted, non-overlapping (fc_start, fc_end, grpprl_bytes) runs
        # gathered from every ChpxFkp page referenced by PlcfBteChpx.
        self._fc_runs: list[tuple[int, int, bytes]] = []
        self._fc_run_starts: list[int] = []
        self._parse_plcfbtechpx(fib, table_stream)

    # ------------------------------------------------------------ PlcfBteChpx

    def _parse_plcfbtechpx(self, fib, table_stream: bytes) -> None:
        fc, lcb = fib.fclcb.get("PlcfBteChpx", (0, 0))
        if lcb <= 0 or fc < 0 or fc + lcb > len(table_stream):
            self.warnings.append("props: missing or out-of-range PlcfBteChpx")
            return
        plc = table_stream[fc:fc + lcb]
        if len(plc) < 4:
            return
        n = (len(plc) - 4) // 8
        n = min(n, _MAX_FKP_PAGES)
        if n <= 0:
            return
        pn_base = (n + 1) * 4
        seen_pages: set[int] = set()
        runs: list[tuple[int, int, bytes]] = []
        for i in range(n):
            off = pn_base + i * 4
            if off + 4 > len(plc):
                self.warnings.append("props: PlcfBteChpx truncated")
                break
            pn_raw = struct.unpack_from("<I", plc, off)[0]
            pn = pn_raw & 0x3FFFFF  # [MS-DOC] 2.9.? PnFkpChpx: pn is 22 bits
            page_off = pn * _FKP_PAGE_SIZE
            if page_off in seen_pages:
                continue
            seen_pages.add(page_off)
            if page_off < 0 or page_off + _FKP_PAGE_SIZE > len(self._word_document):
                self.warnings.append(f"props: ChpxFkp page at pn={pn} out of range")
                continue
            page = self._word_document[page_off:page_off + _FKP_PAGE_SIZE]
            runs.extend(self._parse_fkp_page(page))
        runs.sort(key=lambda r: r[0])
        self._fc_runs = runs
        self._fc_run_starts = [r[0] for r in runs]

    def _parse_fkp_page(self, page: bytes):
        crun = page[_FKP_PAGE_SIZE - 1]
        if crun < 1 or crun > _MAX_CRUN:
            self.warnings.append("props: ChpxFkp page has invalid crun")
            return
        rgfc_count = crun + 1
        rgfc_bytes = rgfc_count * 4
        if rgfc_bytes + crun > _FKP_PAGE_SIZE - 1:
            self.warnings.append("props: ChpxFkp page crun too large")
            return
        fcs = struct.unpack_from(f"<{rgfc_count}I", page, 0)
        rgb_off = rgfc_bytes
        for i in range(crun):
            fc_start, fc_end = fcs[i], fcs[i + 1]
            if fc_end <= fc_start:
                continue
            b = page[rgb_off + i]
            grpprl = b""
            if b:
                n_ofs = b * 2
                if n_ofs < _FKP_PAGE_SIZE - 1:
                    cb = page[n_ofs]
                    data_start = n_ofs + 1
                    if cb:
                        data_end = min(data_start + cb, _FKP_PAGE_SIZE)
                        grpprl = page[data_start:data_end]
            yield fc_start, fc_end, grpprl

    # ------------------------------------------------------------ Pcd.Prm

    def _piece_prm_sprms(self, piece) -> dict[int, bytes]:
        """[MS-DOC] 2.4.6.2 Direct Character Formatting step 6: Pcd.Prm,
        filtered to the sprms that modify character properties (sgc == 2)."""
        if len(piece.prm) != 2:
            return {}
        prm = struct.unpack_from("<H", piece.prm, 0)[0]
        out: dict[int, bytes] = {}
        if prm & 1:
            # Prm1: igrpprl indexes Clx.RgPrc.
            igrpprl = prm >> 1
            grpprl = self._pieces.prc_grpprl(igrpprl)
            if grpprl:
                for sprm_id, operand in _iter_sprms(grpprl):
                    if _sgc(sprm_id) == 2:
                        out[sprm_id] = operand
        else:
            # Prm0: a single (isprm, val) pair.
            isprm = (prm & 0xFE) >> 1
            if 0 <= isprm < len(_PRM0_ISPRM_TO_SPRM):
                sprm_id = _PRM0_ISPRM_TO_SPRM[isprm]
                if sprm_id and _sgc(sprm_id) == 2:
                    val = (prm >> 8) & 0xFF
                    out[sprm_id] = bytes([val])
        return out

    # --------------------------------------------------------------- lookup

    def _overlapping_fc_runs(self, fc_lo: int, fc_hi: int):
        starts = self._fc_run_starts
        idx = bisect.bisect_right(starts, fc_lo) - 1
        if idx < 0:
            idx = 0
        n = len(self._fc_runs)
        i = idx
        while i < n:
            run = self._fc_runs[i]
            if run[0] >= fc_hi:
                break
            if run[1] > fc_lo:
                yield run
            i += 1

    def _piece_runs(self, piece):
        cp_lo, cp_hi = piece.cp_start, piece.cp_end
        if cp_hi <= cp_lo:
            return
        scale = 1 if piece.compressed else 2
        fc_lo = piece.fc
        fc_hi = piece.fc + (cp_hi - cp_lo) * scale
        prm_sprms = self._piece_prm_sprms(piece)
        cursor = fc_lo
        for rfc_lo, rfc_hi, grpprl in self._overlapping_fc_runs(fc_lo, fc_hi):
            ov_lo = max(cursor, rfc_lo)
            ov_hi = min(fc_hi, rfc_hi)
            if ov_hi <= ov_lo:
                continue
            if ov_lo > cursor:
                gap_lo = cp_lo + (cursor - fc_lo) // scale
                gap_hi = cp_lo + (ov_lo - fc_lo) // scale
                if gap_hi > gap_lo and prm_sprms:
                    yield gap_lo, gap_hi, dict(prm_sprms)
            run_lo = cp_lo + (ov_lo - fc_lo) // scale
            run_hi = cp_lo + (ov_hi - fc_lo) // scale
            if run_hi > run_lo:
                sprms = dict(_iter_sprms(grpprl))
                sprms.update(prm_sprms)
                yield run_lo, run_hi, sprms
            cursor = ov_hi
        if cursor < fc_hi:
            gap_lo = cp_lo + (cursor - fc_lo) // scale
            if gap_lo < cp_hi and prm_sprms:
                yield gap_lo, cp_hi, dict(prm_sprms)

    def runs(self):
        """Yield (cp_start, cp_end, sprms: dict[int, bytes])."""
        for piece in self._pieces.iter_pieces():
            yield from self._piece_runs(piece)

    def sprms_at(self, cp: int) -> dict[int, bytes]:
        piece = self._pieces.piece_at_cp(cp)
        if piece is None or piece.cp_end <= piece.cp_start:
            return {}
        cp_c = min(max(cp, piece.cp_start), piece.cp_end - 1)
        scale = 1 if piece.compressed else 2
        fc = piece.fc + (cp_c - piece.cp_start) * scale
        sprms: dict[int, bytes] = {}
        for rfc_lo, rfc_hi, grpprl in self._overlapping_fc_runs(fc, fc + 1):
            if rfc_lo <= fc < rfc_hi:
                sprms = dict(_iter_sprms(grpprl))
                break
        sprms.update(self._piece_prm_sprms(piece))
        return sprms
