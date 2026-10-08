# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice sw/source/filter/ww8/ww8scan.cxx (MPL-2.0).
"""Piece table: CP (character position) -> text.

Spec: [MS-DOC] 2.9.38 Clx, 2.9.177 Pcdt, 2.9.178 PlcPcd, 2.9.172 Pcd,
2.9.73 FcCompressed (fCompressed: 8-bit cp1252 at fc/2, with the special
byte mapping table in 2.9.73), 2.4.1 "Retrieving Text".
LibreOffice reference: sw/source/filter/ww8/ww8scan.cxx — WW8ScannerBase
(OpenPieceTable), WW8PLCFpcd, WW8PLCFx_PCD, WW8ScannerBase::WW8Cp2Fc,
WW8ScannerBase::WW8ReadString, and the compressed-char mapping
(search for "0x82" / "Translate").

Requirements:
  * Clx may start with any number of Prc (clxt 0x01) blocks before the Pcdt (0x02)
  * compressed pieces: byte -> cp1252 with the MS-DOC 2.9.73 overrides
    (0x82..0x9F map to specific Unicode code points)
  * uncompressed pieces: UTF-16LE
  * text(cp_start, cp_end) spans multiple pieces; clamp out-of-range pieces
    and record a warning instead of raising
  * cp_to_fc(cp) -> (fc, compressed) — needed to look up character
    properties (CHPX FKPs are indexed by FC)
"""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass

# [MS-DOC] 2.9.73 FcCompressed: overrides to the plain cp1252 mapping for
# the 0x80-0x9F block. Everything else in that block keeps its cp1252 value
# (which is why this is "cp1252 with overrides", not a standalone table).
_FC_COMPRESSED_OVERRIDES = {
    0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039,
    0x8C: 0x0152, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D,
    0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122,
    0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153, 0x9F: 0x0178,
}


def _build_translate_table() -> dict:
    byte_to_codepoint = list(range(256))
    for b in range(0x80, 0xA0):
        try:
            byte_to_codepoint[b] = ord(bytes([b]).decode("cp1252"))
        except UnicodeDecodeError:
            pass  # undefined in cp1252; MS-DOC doesn't override it either, keep latin-1 passthrough
    for b, cp in _FC_COMPRESSED_OVERRIDES.items():
        byte_to_codepoint[b] = cp
    return {i: byte_to_codepoint[i] for i in range(256) if byte_to_codepoint[i] != i}


_TRANSLATE_TABLE = _build_translate_table()


def _decode_compressed(raw: bytes) -> str:
    return raw.decode("latin-1").translate(_TRANSLATE_TABLE)


_CLXT_PRC = 0x01
_CLXT_PCDT = 0x02
_PCD_SIZE = 8  # [MS-DOC] 2.9.172 Pcd: 2 bytes flags + 4 bytes FcCompressed + 2 bytes Prm
_FCOMPRESSED_FLAG = 0x40000000
_FCOMPRESSED_FC_MASK = 0x3FFFFFFF

# Defensive bounds: real documents have at most a handful of Prc blocks and
# pieces; these just stop a corrupt/fuzzed file from spinning forever.
_MAX_PRC_BLOCKS = 1 << 16
_MAX_PIECES = 1 << 22


@dataclass
class _Piece:
    cp_start: int
    cp_end: int
    fc: int  # already resolved to an actual WordDocument byte offset
    compressed: bool
    prm: bytes  # raw 2-byte Pcd.prm


class PieceTable:
    def __init__(self, table_stream: bytes, fc_clx: int, lcb_clx: int, word_document: bytes) -> None:
        self._word_document = word_document
        self.warnings: list[str] = []
        self._prcs: list[bytes] = []
        self._pieces: list[_Piece] = []
        self._piece_starts: list[int] = []
        self._parse_clx(table_stream, fc_clx, lcb_clx)

    # ------------------------------------------------------------------ Clx

    def _parse_clx(self, data: bytes, fc_clx: int, lcb_clx: int) -> None:
        if fc_clx < 0 or lcb_clx <= 0 or fc_clx + lcb_clx > len(data):
            self.warnings.append("pieces: missing or out-of-range Clx; no text available")
            return
        pos = fc_clx
        end = fc_clx + lcb_clx
        pcdt_bytes = None
        blocks = 0
        while pos < end:
            blocks += 1
            if blocks > _MAX_PRC_BLOCKS:
                self.warnings.append("pieces: too many Clx blocks, giving up")
                return
            clxt = data[pos]
            pos += 1
            if clxt == _CLXT_PCDT:
                if pos + 4 > end:
                    self.warnings.append("pieces: truncated Pcdt")
                    return
                lcb = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if pos + lcb > end:
                    self.warnings.append("pieces: Pcdt.lcb out of range, clamped")
                    lcb = end - pos
                pcdt_bytes = data[pos:pos + lcb]
                pos += lcb
                break
            elif clxt == _CLXT_PRC:
                if pos + 2 > end:
                    self.warnings.append("pieces: truncated Prc")
                    return
                cb = struct.unpack_from("<h", data, pos)[0]
                pos += 2
                if cb < 0 or pos + cb > end:
                    self.warnings.append("pieces: Prc grpprl out of range, truncated")
                    cb = max(0, end - pos)
                self._prcs.append(data[pos:pos + cb])
                pos += cb
            else:
                self.warnings.append(f"pieces: unknown clxt 0x{clxt:02x}, stopping")
                return
        if pcdt_bytes is None:
            self.warnings.append("pieces: Clx has no Pcdt")
            return
        self._parse_plcpcd(pcdt_bytes)

    def _parse_plcpcd(self, plc: bytes) -> None:
        if len(plc) < 4:
            self.warnings.append("pieces: PlcPcd too short")
            return
        n = (len(plc) - 4) // (4 + _PCD_SIZE)
        n = min(n, _MAX_PIECES)
        if n <= 0:
            self.warnings.append("pieces: PlcPcd has no pieces")
            return
        cps = struct.unpack_from(f"<{n + 1}i", plc, 0)
        pcd_base = (n + 1) * 4
        pieces: list[_Piece] = []
        for i in range(n):
            off = pcd_base + i * _PCD_SIZE
            if off + _PCD_SIZE > len(plc):
                self.warnings.append(f"pieces: PlcPcd truncated at piece {i}")
                break
            cp_start, cp_end = cps[i], cps[i + 1]
            if cp_end <= cp_start:
                continue
            fc_raw = struct.unpack_from("<I", plc, off + 2)[0]
            prm = plc[off + 6:off + 8]
            compressed = bool(fc_raw & _FCOMPRESSED_FLAG)
            fc = fc_raw & _FCOMPRESSED_FC_MASK
            if compressed:
                fc //= 2
            pieces.append(_Piece(cp_start, cp_end, fc, compressed, prm))
        if not pieces:
            self.warnings.append("pieces: no usable pieces in PlcPcd")
            return
        # [MS-DOC] 2.8 Plc: aCp MUST be sorted ascending, but be defensive.
        pieces.sort(key=lambda p: p.cp_start)
        self._pieces = pieces
        self._piece_starts = [p.cp_start for p in pieces]

    # ------------------------------------------------------------- lookups

    def _piece_index_at_cp(self, cp: int) -> int:
        if not self._pieces:
            return -1
        idx = bisect.bisect_right(self._piece_starts, cp) - 1
        if idx < 0:
            idx = 0
        return idx

    def piece_at_cp(self, cp: int) -> _Piece | None:
        idx = self._piece_index_at_cp(cp)
        if idx < 0:
            return None
        return self._pieces[idx]

    def iter_pieces(self):
        yield from self._pieces

    def prc_grpprl(self, index: int) -> bytes:
        if 0 <= index < len(self._prcs):
            return self._prcs[index]
        return b""

    def cp_to_fc(self, cp: int) -> tuple[int, bool]:
        piece = self.piece_at_cp(cp)
        if piece is None:
            return 0, False
        cp_clamped = min(max(cp, piece.cp_start), piece.cp_end - 1)
        delta = cp_clamped - piece.cp_start
        fc = piece.fc + (delta if piece.compressed else delta * 2)
        return fc, piece.compressed

    # ----------------------------------------------------------------- text

    def text(self, cp_start: int, cp_end: int) -> str:
        if cp_end <= cp_start or not self._pieces:
            return ""
        idx = self._piece_index_at_cp(cp_start)
        if idx < 0:
            return ""
        out = []
        n = len(self._pieces)
        i = idx
        while i < n:
            piece = self._pieces[i]
            if piece.cp_start >= cp_end:
                break
            lo = max(cp_start, piece.cp_start)
            hi = min(cp_end, piece.cp_end)
            if hi > lo:
                out.append(self._piece_text(piece, lo, hi))
            i += 1
        return "".join(out)

    def _piece_text(self, piece: _Piece, cp_lo: int, cp_hi: int) -> str:
        n_chars = cp_hi - cp_lo
        delta = cp_lo - piece.cp_start
        if piece.compressed:
            fc = piece.fc + delta
            n_bytes = n_chars
        else:
            fc = piece.fc + delta * 2
            n_bytes = n_chars * 2
        doc_len = len(self._word_document)
        if fc < 0 or fc > doc_len:
            self.warnings.append(f"pieces: piece fc {fc} out of range, skipped")
            return ""
        if fc + n_bytes > doc_len:
            self.warnings.append("pieces: piece text runs past end of WordDocument, clipped")
            n_bytes = doc_len - fc
            if not piece.compressed:
                n_bytes -= n_bytes % 2
        if n_bytes <= 0:
            return ""
        raw = self._word_document[fc:fc + n_bytes]
        if piece.compressed:
            return _decode_compressed(raw)
        return raw.decode("utf-16-le", errors="replace")
