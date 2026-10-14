# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sc/source/filter/excel/xistream.cxx, xistring.cxx, xistyle.cxx, impop.cxx,
# excimp8.cxx and xiescher.cxx (MPL-2.0).
"""Excel 97-2003 (BIFF8) workbooks: [MS-XLS] -> Document with sheets, tables and pictures.

Read: workbook globals (SST, XF, FORMAT, BOUNDSHEET8, DATEMODE), the cell
records of every sheet (LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK, FORMULA +
STRING, BOOLERR), MERGECELLS, HLINK and the OfficeArt pictures
(MSODRAWINGGROUP / MSODRAWING / OBJ). Everything read from the file is
untrusted: loops are bounded by the stream length.
"""
from __future__ import annotations

import struct
from functools import lru_cache

from . import numfmt
from .errors import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError
from .images import _dib_to_bmp, blip_looks_valid, decode_blip, dib_complete, wmf_looks_valid
from .model import IMAGE_MARK, Cell, Document, Image, Link, Paragraph, Row, Sheet, Table

R_BOF, R_EOF, R_CONTINUE = 0x0809, 0x000A, 0x003C
R_BOUNDSHEET, R_SST, R_XF, R_FORMAT, R_DATEMODE = 0x0085, 0x00FC, 0x00E0, 0x041E, 0x0022
R_FILEPASS, R_CODEPAGE = 0x002F, 0x0042
R_LABELSST, R_LABEL, R_RSTRING, R_NUMBER, R_RK, R_MULRK = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD
R_FORMULA, R_STRING, R_BOOLERR = 0x0006, 0x0207, 0x0205
R_MERGECELLS, R_HLINK = 0x00E5, 0x01B8
R_MSODRAWINGGROUP, R_MSODRAWING, R_OBJ = 0x00EB, 0x00EC, 0x005D
R_NOTE, R_TXO, R_MSODRAWINGSEL, R_IMGDATA = 0x001C, 0x01B6, 0x00ED, 0x007F
_MAX_CELLS = 8_000_000


# ------------------------------------------------------------------ records

def _records(wb: bytes, pos: int):
    """Yield (id, start, end) of the records from `pos` (CONTINUE included)."""
    n = len(wb)
    while pos + 4 <= n:
        rid, ln = struct.unpack_from("<HH", wb, pos)
        start = pos + 4
        end = min(start + ln, n)
        yield rid, start, end
        pos = start + ln


class _Chunks:
    """Reader over the data of a record and of the CONTINUE records after it."""

    def __init__(self, chunks: list[bytes]):
        self.ch = chunks
        self.i = 0
        self.o = 0

    def _norm(self) -> bool:
        while self.i < len(self.ch) and self.o >= len(self.ch[self.i]):
            self.i += 1
            self.o = 0
        return self.i < len(self.ch)

    def at_end(self) -> bool:
        return not self._norm()

    def in_chunk_left(self) -> int:
        return len(self.ch[self.i]) - self.o if self.i < len(self.ch) else 0

    def at_chunk_end(self) -> bool:
        return self.i < len(self.ch) and self.o >= len(self.ch[self.i])

    def next_chunk(self) -> bool:
        if self.i + 1 < len(self.ch):
            self.i += 1
            self.o = 0
            return True
        self.i = len(self.ch)
        return False

    def read(self, n: int) -> bytes:
        out = bytearray()
        while n > 0 and self._norm():
            take = min(n, len(self.ch[self.i]) - self.o)
            out += self.ch[self.i][self.o:self.o + take]
            self.o += take
            n -= take
        return bytes(out)

    def skip(self, n: int) -> None:
        while n > 0 and self._norm():
            take = min(n, len(self.ch[self.i]) - self.o)
            self.o += take
            n -= take

    def u8(self) -> int:
        b = self.read(1)
        return b[0] if b else 0

    def u16(self) -> int:
        b = self.read(2)
        return struct.unpack("<H", b)[0] if len(b) == 2 else 0

    def u32(self) -> int:
        b = self.read(4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else 0


def _read_xlstring(r: _Chunks, cch_bytes: int = 2) -> str:
    """XLUnicodeString / XLUnicodeRichExtendedString ([MS-XLS] 2.5.293, 2.5.294),
    continued across CONTINUE records (a flag byte restarts each continuation)."""
    cch = r.u8() if cch_bytes == 1 else r.u16()
    flags = r.u8()
    high = flags & 1
    runs = r.u16() if flags & 8 else 0
    ext = r.u32() if flags & 4 else 0
    out: list[str] = []
    remaining = cch
    while remaining > 0:
        if r.at_chunk_end() or r.in_chunk_left() == 0:
            if not r.next_chunk():
                break
            high = r.u8() & 1
            continue
        per = 2 if high else 1
        avail = r.in_chunk_left()
        take = min(remaining, avail // per)
        if take <= 0:
            if not r.next_chunk():
                break
            high = r.u8() & 1
            continue
        raw = r.read(take * per)
        out.append(raw.decode("utf-16-le", "replace") if high else raw.decode("latin-1"))
        remaining -= take
    r.skip(runs * 4 + ext)
    return "".join(out)


def _chain(wb: bytes, recs: list, k: int):
    """Chunks of record k plus the CONTINUE records right after it; returns (chunks, next k)."""
    _, s, e = recs[k]
    chunks = [wb[s:e]]
    k += 1
    while k < len(recs) and recs[k][0] == R_CONTINUE:
        chunks.append(wb[recs[k][1]:recs[k][2]])
        k += 1
    return chunks, k


# --------------------------------------------------------------- cell values

def _rk(v: int) -> float:
    if v & 2:
        x = float(struct.unpack("<i", struct.pack("<I", v & 0xFFFFFFFC))[0] >> 2)
    else:
        x = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return x / 100.0 if v & 1 else x


class _Workbook:
    def __init__(self) -> None:
        self.sst: list[str] = []
        self.xf_ifmt: list[int] = []
        self.formats: dict[int, str] = {}
        self.date1904 = False
        self.sheets: list[dict] = []  # name, pos, state, type
        self.warnings: list[str] = []

    @lru_cache(maxsize=65536)
    def _fmt(self, value: float, ifmt: int) -> str:
        return numfmt.format_number(value, ifmt, self.formats.get(ifmt), self.date1904)

    def string_text(self, text: str, ixfe: int) -> str:
        ifmt = self.xf_ifmt[ixfe] if 0 <= ixfe < len(self.xf_ifmt) else 0
        if ifmt == 0 or text == "":
            return text
        return numfmt.format_text(text, ifmt, self.formats.get(ifmt))

    def number_text(self, value: float, ixfe: int) -> str:
        ifmt = self.xf_ifmt[ixfe] if 0 <= ixfe < len(self.xf_ifmt) else 0
        return self._fmt(value, ifmt)

    __hash__ = object.__hash__


def _parse_globals(wb: bytes, w: _Workbook) -> None:
    recs = list(_records(wb, 0))
    if not recs or recs[0][0] != R_BOF:
        raise CorruptDocumentError("Workbook stream does not start with BOF")
    s = recs[0][1]
    vers, dt = struct.unpack_from("<HH", wb, s) if recs[0][2] - s >= 4 else (0, 0)
    if vers != 0x0600:
        raise UnsupportedFormatError("BIFF%s workbook (only BIFF8 / Excel 97-2003 is supported)" % {0x0500: "5/7"}.get(vers, "?"))
    k = 1
    depth = 0
    while k < len(recs):
        rid, s, e = recs[k]
        if rid == R_BOF:
            depth += 1
        elif rid == R_EOF:
            if depth == 0:
                break
            depth -= 1
        elif depth:
            pass
        elif rid == R_FILEPASS:
            raise EncryptedDocumentError("Excel workbook is password-protected (FILEPASS)")
        elif rid == R_DATEMODE and e - s >= 2:
            w.date1904 = bool(struct.unpack_from("<H", wb, s)[0])
        elif rid == R_XF and e - s >= 4:
            w.xf_ifmt.append(struct.unpack_from("<H", wb, s + 2)[0])
        elif rid == R_FORMAT and e - s >= 4:
            ch, k2 = _chain(wb, recs, k)
            r = _Chunks(ch)
            ifmt = r.u16()
            w.formats[ifmt] = _read_xlstring(r)
            k = k2
            continue
        elif rid == R_BOUNDSHEET and e - s >= 8:
            pos, state, typ = struct.unpack_from("<IBB", wb, s)
            r = _Chunks([wb[s + 6:e]])
            name = _read_xlstring(r, 1)
            w.sheets.append({"name": name, "pos": pos, "state": state, "type": typ & 0xFF})
        elif rid == R_SST:
            ch, k2 = _chain(wb, recs, k)
            w.sst = _parse_sst(ch)
            k = k2
            continue
        k += 1


def _parse_sst(chunks: list[bytes]) -> list[str]:
    r = _Chunks(chunks)
    r.skip(4)
    unique = r.u32()
    total = sum(len(c) for c in chunks)
    out: list[str] = []
    for _ in range(min(unique, total // 3 + 1)):
        if r.at_end():
            break
        out.append(_read_xlstring(r))
    return out


# --------------------------------------------------------------- one sheet

class _SheetData:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], tuple] = {}  # (r, c) -> (kind, value, ixfe)
        self.merges: list[tuple[int, int, int, int]] = []
        self.links: dict[tuple[int, int], str] = {}
        self.drawing: list[bytes] = []
        self.obj_types: list[int] = []
        self.obj_ole: list[int] = []  # storage id of an embedded OLE picture object, else 0
        self.old_pics: list[tuple[tuple[int, int], bytes, str]] = []  # BIFF5-layout OBJ + IMGDATA
        self.warnings: list[str] = []


def _parse_sheet(wb: bytes, pos: int, w: _Workbook) -> _SheetData:
    sd = _SheetData()
    if pos < 0 or pos + 4 > len(wb) or struct.unpack_from("<H", wb, pos)[0] != R_BOF:
        sd.warnings.append("sheet substream does not start with BOF")
        return sd
    recs = list(_records(wb, pos))
    pending_string: tuple[int, int, int] | None = None
    in_draw = False  # inside a run of drawing records (XclImpDrawing::ReadMsoDrawing loop)
    k = 1
    depth = 0
    while k < len(recs):
        rid, s, e = recs[k]
        n = e - s
        if rid not in _DRAW_RUN:
            in_draw = False
        if rid == R_BOF:
            depth += 1
        elif rid == R_EOF:
            if depth == 0:
                break
            depth -= 1
        elif depth:
            pass
        elif rid == R_LABELSST and n >= 10:
            row, col, ixfe, isst = struct.unpack_from("<HHHI", wb, s)
            if isst < len(w.sst):
                sd.cells[(row, col)] = ("s", w.sst[isst], ixfe)
        elif rid == R_NUMBER and n >= 14:
            row, col, ixfe = struct.unpack_from("<HHH", wb, s)
            sd.cells[(row, col)] = ("n", struct.unpack_from("<d", wb, s + 6)[0], ixfe)
        elif rid == R_RK and n >= 10:
            row, col, ixfe, rk = struct.unpack_from("<HHHI", wb, s)
            sd.cells[(row, col)] = ("n", _rk(rk), ixfe)
        elif rid == R_MULRK and n >= 6:
            row, col0 = struct.unpack_from("<HH", wb, s)
            cnt = (n - 6) // 6
            for i in range(cnt):
                ixfe, rk = struct.unpack_from("<HI", wb, s + 4 + 6 * i)
                sd.cells[(row, col0 + i)] = ("n", _rk(rk), ixfe)
        elif rid in (R_LABEL, R_RSTRING) and n >= 8:
            ch, k2 = _chain(wb, recs, k)
            r = _Chunks(ch)
            row, col, ixfe = r.u16(), r.u16(), r.u16()
            sd.cells[(row, col)] = ("s", _read_xlstring(r), ixfe)
            k = k2
            continue
        elif rid == R_BOOLERR and n >= 8:
            row, col, ixfe = struct.unpack_from("<HHH", wb, s)
            val, is_err = wb[s + 6], wb[s + 7]
            sd.cells[(row, col)] = ("e", numfmt.ERRORS.get(val, "#VALUE!"), ixfe) if is_err else ("b", bool(val), -1)  # LibreOffice gives BOOLERR cells the Standard format
        elif rid == R_FORMULA and n >= 20:
            row, col, ixfe = struct.unpack_from("<HHH", wb, s)
            v = wb[s + 6:s + 14]
            if v[6:8] == b"\xff\xff":
                t = v[0]
                if t == 0:
                    pending_string = (row, col, ixfe)
                elif t == 1:
                    sd.cells[(row, col)] = ("b", bool(v[2]), ixfe)
                elif t == 2:
                    sd.cells[(row, col)] = ("e", numfmt.ERRORS.get(v[2], "#VALUE!"), ixfe)
                elif t == 3:
                    sd.cells[(row, col)] = ("s", "", ixfe)
            else:
                sd.cells[(row, col)] = ("n", struct.unpack("<d", v)[0], ixfe)
        elif rid == R_STRING and pending_string is not None:
            ch, k2 = _chain(wb, recs, k)
            row, col, ixfe = pending_string
            sd.cells[(row, col)] = ("s", _read_xlstring(_Chunks(ch)), ixfe)
            pending_string = None
            k = k2
            continue
        elif rid == R_MERGECELLS and n >= 2:
            cnt = min(struct.unpack_from("<H", wb, s)[0], (n - 2) // 8)
            for i in range(cnt):
                r1, r2, c1, c2 = struct.unpack_from("<HHHH", wb, s + 2 + 8 * i)
                if r2 >= r1 and c2 >= c1 and (r2 > r1 or c2 > c1):
                    sd.merges.append((r1, c1, r2 - r1 + 1, c2 - c1 + 1))
        elif rid == R_HLINK and n >= 32:
            _parse_hlink(wb[s:e], sd)
        elif rid in (R_MSODRAWING, R_MSODRAWINGSEL):
            ch, k2 = _chain(wb, recs, k)
            sd.drawing.append(b"".join(ch))
            in_draw = True
            k = k2
            continue
        elif rid == R_CONTINUE and in_draw and depth == 0:
            # xiescher.cxx XclImpDrawing::ReadMsoDrawing: while reading the run of drawing
            # records, every CONTINUE (also one behind an OBJ) is more DFF stream data.
            sd.drawing.append(wb[s:e])
        elif rid == R_OBJ:
            if sd.drawing:
                sd.obj_types.append(_obj_type(wb[s:e]))
                sd.obj_ole.append(_obj_ole_storage(wb[s:e]))
            else:
                # xiescher.cxx XclImpDrawing::ReadObj: an OBJ with no DFF data before it is read
                # in BIFF5 layout (ReadObj5), the picture follows in an IMGDATA record.
                k = _read_old_obj(wb, recs, k, sd)
                continue
        elif rid == R_TXO and n >= 14:
            # TXO is followed by CONTINUE records with its text and its formatting runs;
            # any further CONTINUE records carry the Escher data of the next shape
            # (Excel puts a large shape there instead of starting a MSODRAWING record).
            cch, cb_runs = struct.unpack_from("<HH", wb, s + 10)
            k += 1
            text_left, runs_left = cch, cb_runs
            stray: list[bytes] = []
            while k < len(recs) and recs[k][0] == R_CONTINUE:
                cs, ce = recs[k][1], recs[k][2]
                if text_left > 0 and ce > cs:
                    per = 2 if wb[cs] & 1 else 1
                    text_left -= max(1, (ce - cs - 1) // per)
                elif runs_left > 0:
                    runs_left -= ce - cs
                else:
                    stray.append(wb[cs:ce])
                k += 1
            if stray:
                sd.drawing.append(b"".join(stray))
            continue
        if len(sd.cells) > _MAX_CELLS:
            sd.warnings.append("cell limit reached")
            break
        k += 1
    return sd


_DRAW_RUN = frozenset((R_MSODRAWING, R_MSODRAWINGSEL, R_CONTINUE, R_OBJ, R_TXO))


def _obj_type(d: bytes) -> int:
    """Kind of an OBJ record ([MS-XLS] 2.4.181): the ObjectType of ftCmo, or -1 for objects that
    are not plain pictures (ActiveX controls, embedded OLE objects), whose image is only a
    preview LibreOffice does not report as a picture."""
    if len(d) < 8 or struct.unpack_from("<H", d, 0)[0] != 0x0015:
        return -1
    ot = struct.unpack_from("<H", d, 4)[0]
    p = 0
    while p + 4 <= len(d):
        ft, cb = struct.unpack_from("<HH", d, p)
        if ft == 0x0008 and cb >= 2 and p + 6 <= len(d):  # ftPioGrbit
            grbit = struct.unpack_from("<H", d, p + 4)[0]
            if grbit & (0x0008 | 0x0020):  # fCtl (ActiveX control), fPrstm (OLE storage)
                return -1
        if ft == 0 and cb == 0:
            break
        p += 4 + cb
    return ot


def _obj_ole_storage(d: bytes) -> int:
    """Storage id of an embedded OLE object described by an OBJ record, else 0.

    xiescher.cxx XclImpPictureObj::ReadPictFmla / GetOleStorageName: the ftPictFmla
    subrecord ([MS-XLS] 2.5.153) holds a link formula; when it is a ptgTbl token the object is
    embedded and the 32-bit storage id behind the formula names the "MBD%08X" storage of the
    workbook. Picture controls (fCtl) are not OLE storages."""
    if len(d) < 8 or struct.unpack_from("<H", d, 0)[0] != 0x0015:
        return 0
    p = 0
    control = False
    pict = None
    while p + 4 <= len(d):
        ft, cb = struct.unpack_from("<HH", d, p)
        if ft == 0x0008 and cb >= 2 and p + 6 <= len(d):  # ftPioGrbit
            control = bool(struct.unpack_from("<H", d, p + 4)[0] & 0x0010)  # EXC_OBJ_PIC_CONTROL
        elif ft == 0x0009 and pict is None:  # ftPictFmla
            pict = (p + 4, min(p + 4 + cb, len(d)))
        if ft == 0 and cb == 0:
            break
        p += 4 + cb
    if pict is None or control:
        return 0
    b, e = pict
    if b + 2 > e:
        return 0
    link = struct.unpack_from("<H", d, b)[0]
    b += 2
    if link < 6 or b + 7 > e:
        return 0
    fmla = struct.unpack_from("<H", d, b)[0]
    if fmla == 0 or d[b + 6] != 0x02:  # EXC_TOKID_TBL: embedded (0x39 = linked, not handled)
        return 0
    end = b + link
    if end + 4 > e:
        return 0
    return struct.unpack_from("<I", d, end)[0]


def _read_old_obj(wb: bytes, recs: list, k: int, sd: "_SheetData") -> int:
    """BIFF5-layout OBJ (xiescher.cxx XclImpDrawObjBase::ReadObj5 / XclImpPictureObj::DoReadObj5)
    followed by its IMGDATA record (XclImpDrawing::ReadImgData). Returns the next record index."""
    _, s, e = recs[k]
    d = wb[s:e]
    k += 1
    if len(d) < 34 or struct.unpack_from("<H", d, 4)[0] != 0x0008:  # ObjectType 8 = picture
        return k
    flags = struct.unpack_from("<H", d, 8)[0]
    c1, dx1, r1, dy1, c2, dx2, r2, dy2 = struct.unpack_from("<8H", d, 10)
    if not (k < len(recs) and recs[k][0] == R_IMGDATA):
        return k
    ch, k = _chain(wb, recs, k)
    img = b"".join(ch)
    # ReadPictFmla + ReadName5: the page background is a hidden picture named "__BkgndObj"
    name_len = struct.unpack_from("<H", d, 30)[0]
    if flags & 0x0100 and name_len and len(d) > 60 and d[61:61 + d[60]] == b"__BkgndObj":
        return k
    # XclImpDrawObjBase::IsValidSize (see _shapes): zero-extent anchors are dropped
    if (c2 - c1) * 1024 + dx2 + 1 - dx1 <= 0 or (r2 - r1) * 256 + dy2 - dy1 <= 0:
        return k
    if len(img) < 8:
        return k
    fmt, _env, size = struct.unpack_from("<HHI", img, 0)
    if size > len(img) - 8:
        return k
    body = img[8:]  # ReadBmp/ReadWmf copy the rest of the record
    if fmt == 9:  # EXC_IMGDATA_BMP: DIB without file header (ReadBmp)
        if not dib_complete(body):
            return k
        bmp = _dib_to_bmp(body)
        if bmp is not None:
            sd.old_pics.append(((r1, c1), bmp, "image/bmp"))
    elif fmt == 2:  # EXC_IMGDATA_WMF: ReadWmf skips 8 more bytes
        wmf = body[8:]
        if wmf_looks_valid(wmf):
            sd.old_pics.append(((r1, c1), wmf, "image/x-wmf"))
    return k


_URL_MONIKER = bytes.fromhex("e0c9ea79f9bace118c8200aa004ba90b")


def _parse_hlink(d: bytes, sd: _SheetData) -> None:
    """HLink ([MS-XLS] 2.4.140): URL monikers and in-workbook locations."""
    try:
        r1, r2, c1, c2 = struct.unpack_from("<HHHH", d, 0)
        flags = struct.unpack_from("<I", d, 28)[0]
        p = 32
        if flags & 0x10:  # display name
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4 + 2 * ln
        if flags & 0x80:  # target frame
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4 + 2 * ln
        url = ""
        if flags & 0x01:  # has moniker
            if flags & 0x100:  # saved as string
                ln = struct.unpack_from("<I", d, p)[0]
                p += 4
                url = d[p:p + 2 * ln].decode("utf-16-le", "replace").rstrip("\0")
                p += 2 * ln
            else:
                clsid = d[p:p + 16]
                p += 16
                if clsid == _URL_MONIKER:
                    ln = struct.unpack_from("<I", d, p)[0]
                    p += 4
                    url = d[p:p + ln].decode("utf-16-le", "replace").split("\0", 1)[0]
        if not url and flags & 0x08:  # location (same workbook)
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4
            loc = d[p:p + 2 * ln].decode("utf-16-le", "replace").rstrip("\0")
            url = "#" + loc if loc else ""
        if url and r2 - r1 < 2000 and c2 - c1 < 256:
            for rr in range(r1, r2 + 1):
                for cc in range(c1, c2 + 1):
                    sd.links[(rr, cc)] = url
    except (struct.error, IndexError):
        pass


# ---------------------------------------------------------------- pictures

_RT_DGG, _RT_BSTORE, _RT_FBSE = 0xF000, 0xF001, 0xF007
_RT_DG, _RT_SPGR, _RT_SP = 0xF002, 0xF003, 0xF004
_RT_FSP, _RT_OPT, _RT_ANCHOR, _RT_CLIENT_DATA = 0xF00A, 0xF00B, 0xF010, 0xF011
_FBSE_SIZE = 36
_PID_PIB = 0x0104


def _escher(data: bytes, lo: int, hi: int):
    pos = lo
    hi = min(hi, len(data))
    while pos + 8 <= hi:
        vi, rt, ln = struct.unpack_from("<HHI", data, pos)
        yield vi, rt, pos + 8, min(pos + 8 + ln, hi)
        pos += 8 + ln


def _bstore(group: bytes) -> list:
    """BLIPs of the drawing group in BStore order (None when empty/undecodable)."""
    blips: list = []

    def walk(lo, hi, depth):
        if depth > 8:
            return
        for vi, rt, b, e in _escher(group, lo, hi):
            if rt == _RT_DGG:
                walk(b, e, depth + 1)
            elif rt == _RT_BSTORE:
                for vi2, rt2, b2, e2 in _escher(group, b, e):
                    got = None
                    if rt2 == _RT_FBSE and e2 - b2 >= _FBSE_SIZE:
                        cb_name = group[b2 + 33]
                        emb = b2 + _FBSE_SIZE + cb_name
                        if emb + 8 <= e2:
                            got = decode_blip(group, emb, e2)
                    elif rt2 != _RT_FBSE:
                        got = decode_blip(group, b2 - 8, e2)
                    blips.append(got)

    walk(0, len(group), 0)
    return blips


def _shapes(draw: bytes):
    """[(pib, (row1, col1) or None, has_client_data, is_group_or_deleted)] in file order."""
    out: list = []

    def sp_container(b, e):
        pib = 0
        anchor = None
        client = False
        flags = 0
        empty_anchor = False
        for vi, rt, b2, e2 in _escher(draw, b, e):
            if rt == _RT_FSP and e2 - b2 >= 8:
                flags = struct.unpack_from("<I", draw, b2 + 4)[0]
            elif rt == _RT_OPT:
                for i in range(min(vi >> 4, (e2 - b2) // 6)):
                    opid, op = struct.unpack_from("<HI", draw, b2 + 6 * i)
                    if opid & 0x3FFF == _PID_PIB and not opid & 0x8000:
                        pib = op
            elif rt == _RT_ANCHOR and e2 - b2 >= 18:
                _f, c1, dx1, r1, dy1, c2, dx2, r2, dy2 = struct.unpack_from("<9H", draw, b2)
                anchor = (r1, c1)
                # XclImpDrawObjBase::IsValidSize: a picture (area object) needs width > 3 and
                # height > 1 (1/100 mm); the width is rounded up, so only a zero-extent anchor
                # (right edge not right of the left one / bottom not below the top) is dropped.
                empty_anchor = ((c2 - c1) * 1024 + dx2 + 1 - dx1 <= 0) or ((r2 - r1) * 256 + dy2 - dy1 <= 0)
            elif rt == _RT_CLIENT_DATA:
                client = True
        out.append((pib, anchor, client, bool(flags & (0x01 | 0x08 | 0x80)) or empty_anchor))  # group / deleted / patriarch

    def walk(lo, hi, depth):
        if depth > 12:
            return
        for vi, rt, b, e in _escher(draw, lo, hi):
            if rt == _RT_SPGR:
                walk(b, e, depth + 1)
            elif rt == _RT_SP:
                sp_container(b, e)

    # xiescher.cxx XclImpDffConverter::ProcessDrawing/ProcessDgContainer/ProcessShGrContainer:
    # only the FIRST top-level drawing container of the sheet is imported, and inside it only
    # shape-group and shape containers (a nested DG container or other record is skipped).
    for vi, rt, b, e in _escher(draw, 0, len(draw)):
        if rt == _RT_DG:
            walk(b, e, 1)
        break
    return out


# ----------------------------------------------------------------- driver

def parse(wb: bytes, storages: frozenset | set | None = None) -> Document:
    w = _Workbook()
    _parse_globals(wb, w)
    doc = Document(format="xls")
    # drawing group: the BStore (pictures) lives in the globals substream
    group = b""
    recs = list(_records(wb, 0))
    k = 0
    while k < len(recs):
        if recs[k][0] == R_MSODRAWINGGROUP:
            ch, k = _chain(wb, recs, k)
            group += b"".join(ch)
            continue
        if recs[k][0] == R_EOF:
            break
        k += 1
    blips = _bstore(group) if group else []
    media_of: dict[int, int] = {}
    for info in w.sheets:
        sheet = Sheet(name=info["name"], hidden=info["state"] != 0)
        if info["type"] in (0, 1):
            sd = _parse_sheet(wb, info["pos"], w)
            doc.warnings.extend(sd.warnings)
            for (r, c), (kind, val, ixfe) in sd.cells.items():
                if kind == "s":
                    text = w.string_text(val, ixfe)
                elif kind == "n":
                    text = w.number_text(val, ixfe)
                elif kind == "b":
                    text = w.number_text(1.0 if val else 0.0, ixfe)  # LibreOffice shows booleans as numbers
                else:
                    text = val
                if text != "":
                    sheet.cells[(r, c)] = text
            sheet.merges = sd.merges
            sheet.links = {rc: u for rc, u in sd.links.items() if rc in sheet.cells}
            if sd.drawing and blips:
                shapes = _shapes(b"".join(sd.drawing))
                # every shape with ClientData is described by one OBJ record, in order
                client_idx = [i for i, s in enumerate(shapes) if s[2]]
                obj_type = dict(zip(client_idx, sd.obj_types)) if len(client_idx) == len(sd.obj_types) else {}
                obj_ole = dict(zip(client_idx, sd.obj_ole)) if len(client_idx) == len(sd.obj_ole) else {}
                for idx, (pib, anchor, client, skip) in enumerate(shapes):
                    if skip or pib <= 0 or pib > len(blips) or blips[pib - 1] is None:
                        continue
                    if obj_type.get(idx, 0x08) != 0x08:  # only picture objects (ot 8), not charts/OLE/comments
                        continue
                    # XclImpDffConverter::CreateSdrObject(XclImpPictureObj): a picture whose OLE
                    # storage "MBD%08X" exists in the file becomes an OLE object, not a picture.
                    if storages and obj_ole.get(idx) and ("MBD%08X" % obj_ole[idx]).upper() in storages:
                        continue
                    if not blip_looks_valid(*blips[pib - 1]):
                        continue  # LibreOffice drops pictures it cannot decode
                    if pib not in media_of:
                        data, mime = blips[pib - 1]
                        media_of[pib] = len(doc.images)
                        doc.images.append(Image(data=data, mime=mime, kind="floating", story="main"))
                    sheet.images.append((anchor[0] if anchor else 0, anchor[1] if anchor else 0, media_of[pib]))
            for (row, col), data, mime in sd.old_pics:
                doc.images.append(Image(data=data, mime=mime, kind="floating", story="main"))
                sheet.images.append((row, col, len(doc.images) - 1))
        doc.sheets.append(sheet)
    _fill_flat(doc)
    _build_blocks(doc)
    return doc


def col_letters(c: int) -> str:
    s = ""
    c += 1
    while c > 0:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s


def _sparse_table(sh: Sheet, rows: list, img_at: dict):
    """Huge, mostly empty sheets: one row per used row, cells addressed by column letter."""
    by_row: dict[int, list] = {}
    for (r, c), t in sh.cells.items():
        by_row.setdefault(r, []).append((c, t))
    for (r, c), imgs in img_at.items():
        by_row.setdefault(r, [])
    table = Table()
    for r in rows:
        items = sorted(by_row.get(r, []))
        text = "; ".join("%s: %s" % (col_letters(c), t.replace("\n", " ")) for c, t in items)
        imgs = [n for (rr, _), ns in sorted(img_at.items()) if rr == r for n in ns] if img_at else []
        p = Paragraph(IMAGE_MARK * len(imgs) + text, images=imgs)
        table.rows.append(Row(cells=[Cell(blocks=[Paragraph("Row %d" % (r + 1))]), Cell(blocks=[p])]))
    return table


def _sheet_table(sh: Sheet):
    """One HTML-style table for a sheet: empty rows/columns dropped, merges kept."""
    img_at: dict[tuple[int, int], list[int]] = {}
    for r, c, n in sh.images:
        img_at.setdefault((r, c), []).append(n)
    keyed = set(sh.cells) | set(img_at)
    if not keyed:
        return None
    rows = sorted({r for r, _ in keyed})
    cols = sorted({c for _, c in keyed})
    if len(rows) * len(cols) > max(20000, 4 * len(keyed)):
        return _sparse_table(sh, rows, img_at)
    span: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    import bisect
    for r, c, rs, cs in sh.merges:
        if (r, c) not in keyed:
            continue
        nr = bisect.bisect_left(rows, r + rs) - bisect.bisect_left(rows, r)
        nc = bisect.bisect_left(cols, c + cs) - bisect.bisect_left(cols, c)
        span[(r, c)] = (max(nr, 1), max(nc, 1))
        for rr in rows[bisect.bisect_left(rows, r):bisect.bisect_left(rows, r + rs)]:
            for cc in cols[bisect.bisect_left(cols, c):bisect.bisect_left(cols, c + cs)]:
                if (rr, cc) != (r, c):
                    covered.add((rr, cc))
    table = Table()
    for r in rows:
        row = Row()
        for c in cols:
            if (r, c) in covered:
                continue
            cell = Cell()
            text = sh.cells.get((r, c), "")
            imgs = img_at.get((r, c), [])
            lines = text.split("\n") if text else []
            if imgs or lines:
                first = lines[0] if lines else ""
                p = Paragraph(IMAGE_MARK * len(imgs) + first, images=list(imgs))
                url = sh.links.get((r, c))
                if url and first:
                    p.links.append(Link(len(imgs), len(imgs) + len(first), url))
                cell.blocks.append(p)
                cell.blocks.extend(Paragraph(x) for x in lines[1:])
            if (r, c) in span:
                cell.rowspan, cell.colspan = span[(r, c)]
            row.cells.append(cell)
        table.rows.append(row)
    return table


def _build_blocks(doc: Document) -> None:
    blocks: list = []
    for sh in doc.sheets:
        if sh.hidden:
            continue
        blocks.append(Paragraph(sh.name, heading=2))
        t = _sheet_table(sh)
        if t is not None:
            blocks.append(t)
    doc.blocks = blocks


def _fill_flat(doc: Document) -> None:
    main: list[str] = []
    for sh in doc.sheets:
        main.append(sh.name)
        rows: dict[int, list[tuple[int, str]]] = {}
        for (r, c), t in sh.cells.items():
            rows.setdefault(r, []).append((c, t))
        for r in sorted(rows):
            main.append("\t".join(t for _, t in sorted(rows[r])))
    doc.stories["main"] = main
