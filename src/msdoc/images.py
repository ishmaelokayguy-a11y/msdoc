# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sw/source/filter/ww8/ww8graf.cxx, ww8graf2.cxx,
# filter/source/msfilter/msdffimp.cxx, include/filter/msfilter/msdffimp.hxx
# and escherex.hxx (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""Image extraction.

INLINE pictures ([MS-DOC] 2.9.192 PICFAndOfficeArtData, 2.9.193 PICF,
2.9.194 PICMID):
  0x01 special char with sprmCPicLocation (and without sprmCFData/sprmCFOle2)
  -> operand = offset into the "Data" stream -> PICF (lcb, cbHeader, mfpf.mm)
  -> if mm == 0x0066 (MM_SHAPEFILE) a cchPicName + name follows
  -> OfficeArtInlineSpContainer: an OfficeArtSpContainer followed by
     OfficeArtBStoreContainerFileBlock(s) (BLIPs) — [MS-ODRAW] 2.2.32, 2.2.23
  Older files may store a raw metafile/bitmap after the PICF (mm 0x0064 /
  MM_ANISOTROPIC etc.) — handle the documented variants.

FLOATING pictures ([MS-DOC] 2.9.? PlcfSpa, 2.9.? OfficeArtContent, 2.9.? Spa):
  0x08 special char; PlcSpaMom / PlcSpaHdr map CP -> Spa.lid (shape id)
  -> OfficeArtContent at fcDggInfo/lcbDggInfo in the table stream:
     OfficeArtDggContainer (contains the BStore) + one OfficeArtDgContainer
     per drawing (main / header)
  -> find the shape (OfficeArtFSP.spid == lid), read its pib property
     (OfficeArtFOPT, opid 0x0104 pib) = 1-based index into the BStore
  -> OfficeArtFBSE: the BLIP is either inline in the BStore, or at
     foDelay in the WordDocument stream (the "delay stream")
  Group shapes, and shapes with a blip fill (fillBlip 0x0186) also count.

BLIPs ([MS-ODRAW] 2.2.23–2.2.29): recType 0xF01A EMF, 0xF01B WMF, 0xF01C PICT
(compressed metafiles: OfficeArtMetafileHeader then zlib if compression==0x00),
0xF01D JPEG (RGB or CMYK, told apart by recInstance), 0xF01E PNG, 0xF01F DIB
(prepend a 14-byte BITMAPFILEHEADER -> image/bmp), 0xF029 TIFF, and 0xF02A,
a second JPEG record type (2.2.23 OfficeArtBlip, product note <5>; 2.2.27
itself only names 0xF01D, but Word writes 0xF02A for CMYK JPEGs). recInstance decides whether the
header has 1 or 2 UIDs (16 or 32 bytes before the data).

LibreOffice reference: sw/source/filter/ww8/ww8graf.cxx, ww8graf2.cxx
(ImportGraf, ImportGraf1, WW8PicDesc), filter/source/msfilter/msdffimp.cxx
(SvxMSDffManager::GetBLIP, GetBLIPDirect, GetShape, ProcessObj,
SvxMSDffManager::Init / CheckForConvertToSOObj),
include/filter/msfilter/msdffimp.hxx, escherex.hxx (record type constants).

Deduplication: report each anchored picture once per anchor (two anchors
pointing at the same BLIP -> two Image entries with equal data). Do not
report BLIPs that are not referenced by any anchor (unused BStore entries).
"""
from __future__ import annotations

import struct
import zlib

from .model import STORY_KEYS, Image
from .props import SPRM_CFDATA, SPRM_CFOBJ, SPRM_CFOLE2, SPRM_CFVANISH, SPRM_CPICLOCATION


def extract_images(fib, word_document: bytes, table_stream: bytes, data_stream: bytes | None, props, anchors) -> list:
    """Return list[msdoc.model.Image] in story/CP order."""
    images: list[Image] = []
    try:
        images.extend(extract_inline_images(fib, data_stream, props, anchors))
    except (struct.error, IndexError, ValueError, KeyError, RecursionError, zlib.error):
        pass
    try:
        images.extend(extract_floating_images(fib, word_document, table_stream, anchors))
    except (struct.error, IndexError, ValueError, KeyError, RecursionError, zlib.error):
        pass
    order = {k: i for i, k in enumerate(STORY_KEYS)}
    images.sort(key=lambda im: (order.get(im.story, len(order)), im.cp if im.cp is not None else -1))
    return images



# ---------------------------------------------------------------- BLIPs
# [MS-ODRAW] 2.2.23-2.2.31. recType -> (mime, is_metafile). recInstance
# values with a single rgbUid are listed in _BLIP_ONE_UID; the odd value
# next to each adds rgbUid2 (msdffimp.cxx GetBLIPDirect: nSkip = 16 or 32).
_BLIP_TYPES = {
    0xF01A: ("image/x-emf", True),
    0xF01B: ("image/x-wmf", True),
    0xF01C: ("image/x-pict", True),
    0xF01D: ("image/jpeg", False),
    0xF01E: ("image/png", False),
    0xF01F: ("image/bmp", False),
    0xF029: ("image/tiff", False),
    0xF02A: ("image/jpeg", False),  # [MS-ODRAW] 2.2.23: also OfficeArtBlipJPEG
}
_BLIP_ONE_UID = {0x3D4, 0x216, 0x542, 0x46A, 0x6E2, 0x6E0, 0x7A8, 0x6E4}
_METAFILE_HEADER_SIZE = 34  # OfficeArtMetafileHeader, [MS-ODRAW] 2.2.31
_MAX_METAFILE = 64 << 20


def decode_blip(buf: bytes, pos: int, end: int | None = None) -> tuple[bytes, str] | None:
    """Decode the OfficeArtBlip record whose header starts at buf[pos].

    Returns (image bytes, mime) or None if the record is not a BLIP or is
    truncated. Metafiles are inflated; DIBs get a BITMAPFILEHEADER."""
    if end is None or end > len(buf):
        end = len(buf)
    if pos < 0 or pos + 8 > end:
        return None
    ver_inst, rtype, rlen = struct.unpack_from("<HHI", buf, pos)
    info = _BLIP_TYPES.get(rtype)
    if info is None:
        return None
    mime, metafile = info
    inst = ver_inst >> 4
    body = pos + 8
    rend = min(body + rlen, end)
    hdr = 16 if inst in _BLIP_ONE_UID else 32
    if metafile:
        mh = body + hdr
        if mh + _METAFILE_HEADER_SIZE > rend:
            return None
        # The metafile is returned as stored (a WMF without a placeable
        # header), as LibreOffice keeps it.
        cb_size = struct.unpack_from("<I", buf, mh)[0]
        cb_save = struct.unpack_from("<I", buf, mh + 28)[0]
        compression = buf[mh + 32]
        raw = buf[mh + _METAFILE_HEADER_SIZE:min(rend, mh + _METAFILE_HEADER_SIZE + cb_save)]
        if compression == 0:
            try:
                d = zlib.decompressobj()
                data = d.decompress(raw, min(max(cb_size, 1), _MAX_METAFILE))
            except zlib.error:
                return None
        else:
            data = bytes(raw)
        if not data:
            return None
        return data, mime
    start = body + hdr + 1  # tag byte
    if start >= rend:
        return None
    data = bytes(buf[start:rend])
    if rtype == 0xF01F:
        data = _dib_to_bmp(data)
        if data is None:
            return None
    return data, mime


def _dib_to_bmp(dib: bytes) -> bytes | None:
    """Prepend a BITMAPFILEHEADER to a packed DIB (BITMAPINFOHEADER or
    BITMAPCOREHEADER, colour table, bits)."""
    if len(dib) < 12:
        return None
    hsize = struct.unpack_from("<I", dib, 0)[0]
    if hsize == 12:
        bits = struct.unpack_from("<H", dib, 10)[0]
        colors = (1 << bits) * 3 if bits <= 8 else 0
    elif 40 <= hsize <= len(dib) - 0 and len(dib) >= 40:
        bits, compression = struct.unpack_from("<HI", dib, 14)
        used = struct.unpack_from("<I", dib, 32)[0]
        if used == 0 and bits <= 8:
            used = 1 << bits
        colors = min(used, 1 << 16) * 4
        if compression == 3 and hsize == 40:  # BI_BITFIELDS masks
            colors += 12
    else:
        return None
    off = 14 + hsize + colors
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, off) + dib


# ------------------------------------------------------------- OfficeArt
# Record types, [MS-ODRAW] 2.2.x.
_RT_DGG_CONTAINER = 0xF000
_RT_BSTORE_CONTAINER = 0xF001
_RT_DG_CONTAINER = 0xF002
_RT_SPGR_CONTAINER = 0xF003
_RT_SP_CONTAINER = 0xF004
_RT_FBSE = 0xF007
_RT_FSP = 0xF00A
_RT_OPT = 0xF00B
_RT_TERTIARY_OPT = 0xF122
_PID_PIB = 0x0104
_FSP_GROUP = 0x0001
_FSP_PATRIARCH = 0x0004
_FSP_DELETED = 0x0008
_FSP_OLESHAPE = 0x0010
_SPA_SIZE = 26  # [MS-DOC] 2.9.? Spa
_FBSE_SIZE = 36
_MAX_DEPTH = 64


def _officeart_records(data: bytes, lo: int, hi: int):
    """Yield (rec_ver_inst, rec_type, body_lo, body_hi) of the OfficeArt
    records in data[lo:hi] ([MS-ODRAW] 2.1.1 OfficeArtRecordHeader). A
    record overrunning its parent is clipped to it."""
    pos = lo
    hi = min(hi, len(data))
    while pos + 8 <= hi:
        ver_inst, rtype, rlen = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        yield ver_inst, rtype, body, min(body + rlen, hi)
        pos = body + rlen


class _OfficeArt:
    """OfficeArtContent ([MS-DOC] 2.9.? OfficeArtContent): an
    OfficeArtDggContainer (holding the BStore), then for each drawing a
    dgglbl byte and an OfficeArtDgContainer. Ported in spirit from
    msdffimp.cxx SvxMSDffManager::GetDrawingGroupContainerData /
    GetDrawingContainerData."""

    def __init__(self, table: bytes, fc: int, lcb: int) -> None:
        self.table = table
        self.bstore: list[tuple[int, int, int]] = []  # (ver_inst, rtype, body_lo, body_hi)
        self.shapes: dict[int, tuple[bool, int, int]] = {}  # spid -> (is_group, lo, hi)
        self._budget = max(1000, lcb // 4)
        pos, hi = fc, min(fc + lcb, len(table))
        while pos + 8 <= hi:
            ver_inst, rtype, rlen = struct.unpack_from("<HHI", table, pos)
            if rtype not in (_RT_DGG_CONTAINER, _RT_DG_CONTAINER):
                pos += 1  # dgglbl byte before each drawing
                if pos + 8 > hi:
                    break
                ver_inst, rtype, rlen = struct.unpack_from("<HHI", table, pos)
                if rtype != _RT_DG_CONTAINER:
                    break
            body, end = pos + 8, min(pos + 8 + rlen, hi)
            if rtype == _RT_DGG_CONTAINER:
                self._read_dgg(body, end)
            else:
                self._read_dg(body, end)
            pos = body + rlen

    def _tick(self) -> bool:
        self._budget -= 1
        return self._budget >= 0

    def _read_dgg(self, lo: int, hi: int) -> None:
        for _, rtype, b, e in _officeart_records(self.table, lo, hi):
            if not self._tick():
                return
            if rtype == _RT_BSTORE_CONTAINER:
                for vi, t2, b2, e2 in _officeart_records(self.table, b, e):
                    if not self._tick():
                        return
                    self.bstore.append((vi, t2, b2, e2))

    def _read_dg(self, lo: int, hi: int) -> None:
        for _, rtype, b, e in _officeart_records(self.table, lo, hi):
            if rtype == _RT_SPGR_CONTAINER:
                self._index_group(b, e, 0)
            elif rtype == _RT_SP_CONTAINER:
                self._index_shape(b, e, b, e, False)

    def _index_shape(self, lo: int, hi: int, reg_lo: int, reg_hi: int, group: bool) -> None:
        spid = self._spid(lo, hi)
        if spid is not None and spid not in self.shapes:
            self.shapes[spid] = (group, reg_lo, reg_hi)

    def _index_group(self, lo: int, hi: int, depth: int) -> None:
        """Register every shape of an OfficeArtSpgrContainer. The group's
        own id is that of its first OfficeArtSpContainer."""
        if depth > _MAX_DEPTH:
            return
        first = True
        for _, rtype, b, e in _officeart_records(self.table, lo, hi):
            if not self._tick():
                return
            if rtype == _RT_SP_CONTAINER:
                if first:
                    self._index_shape(b, e, lo, hi, True)
                else:
                    self._index_shape(b, e, b, e, False)
            elif rtype == _RT_SPGR_CONTAINER:
                self._index_group(b, e, depth + 1)
            first = False

    def _spid(self, lo: int, hi: int):
        fsp = self._fsp(lo, hi)
        return fsp[0] if fsp else None

    def _fsp(self, lo: int, hi: int):
        for _, rtype, b, e in _officeart_records(self.table, lo, hi):
            if rtype == _RT_FSP and e - b >= 8:
                return struct.unpack_from("<II", self.table, b)
        return None

    def _pib(self, lo: int, hi: int) -> int:
        """pib ([MS-ODRAW] 2.3.23.? pib, opid 0x0104) of a shape, 0 if none."""
        for vi, rtype, b, e in _officeart_records(self.table, lo, hi):
            if rtype not in (_RT_OPT, _RT_TERTIARY_OPT):
                continue
            n = vi >> 4
            for i in range(min(n, (e - b) // 6)):
                opid, op = struct.unpack_from("<HI", self.table, b + 6 * i)
                if opid & 0x3FFF == _PID_PIB and not opid & 0x8000:
                    return op
        return 0

    def picture_pibs(self, spid: int) -> list[int]:
        """pib of every picture shape the anchored shape `spid` stands for:
        the shape itself, or every picture inside a group (in drawing
        order). OLE shapes are skipped (their image is a preview)."""
        got = self.shapes.get(spid)
        if got is None:
            return []
        group, lo, hi = got
        out: list[int] = []
        if group:
            self._group_pibs(lo, hi, 0, out)
        else:
            self._shape_pib(lo, hi, out)
        return out

    def _shape_pib(self, lo: int, hi: int, out: list[int]) -> None:
        fsp = self._fsp(lo, hi)
        if fsp is None or fsp[1] & (_FSP_OLESHAPE | _FSP_DELETED | _FSP_GROUP | _FSP_PATRIARCH):
            return
        pib = self._pib(lo, hi)
        if pib > 0:
            out.append(pib)

    def _group_pibs(self, lo: int, hi: int, depth: int, out: list[int]) -> None:
        if depth > _MAX_DEPTH:
            return
        first = True
        for _, rtype, b, e in _officeart_records(self.table, lo, hi):
            if not self._tick():
                return
            if rtype == _RT_SP_CONTAINER and not first:
                self._shape_pib(b, e, out)
            elif rtype == _RT_SPGR_CONTAINER:
                self._group_pibs(b, e, depth + 1, out)
            first = False

    def blip(self, pib: int, delay: bytes) -> tuple[bytes, str] | None:
        """BLIP for a 1-based BStore index: an OfficeArtFBSE whose BLIP is
        embedded after it or lives at foDelay in the delay stream (the
        WordDocument stream), or a bare BLIP record ([MS-ODRAW] 2.2.20
        OfficeArtBStoreContainerFileBlock, 2.2.32 OfficeArtFBSE)."""
        if not 1 <= pib <= len(self.bstore):
            return None
        _, rtype, b, e = self.bstore[pib - 1]
        if rtype != _RT_FBSE:
            return decode_blip(self.table, b - 8, e)
        if e - b < _FBSE_SIZE:
            return None
        size, _cref, fo_delay = struct.unpack_from("<III", self.table, b + 20)
        cb_name = self.table[b + 33]
        emb = b + _FBSE_SIZE + cb_name
        if emb + 8 <= e:
            got = decode_blip(self.table, emb, e)
            if got:
                return got
        if fo_delay < len(delay) and size >= 8:
            return decode_blip(delay, fo_delay, min(len(delay), fo_delay + size))
        return None


def _officeart_spa(fib, table: bytes, name: str) -> dict[int, int]:
    """aCP -> Spa.lid from PlcSpaMom / PlcSpaHdr ([MS-DOC] 2.8.? PlcfSpa:
    n+1 CPs then n 26-byte Spa whose first field is lid)."""
    got = fib.pair(name, len(table)) if hasattr(fib, "pair") else None
    out: dict[int, int] = {}
    if not got or got[1] < 4 + 4 + _SPA_SIZE:
        return out
    fc, lcb = got
    n = (lcb - 4) // (4 + _SPA_SIZE)
    for i in range(n):
        cp = struct.unpack_from("<i", table, fc + 4 * i)[0]
        lid = struct.unpack_from("<i", table, fc + 4 * (n + 1) + _SPA_SIZE * i)[0]
        out.setdefault(cp, lid)
    return out


def extract_floating_images(fib, word_document: bytes, table: bytes, anchors) -> list[Image]:
    """Pictures of OfficeArt shapes anchored by 0x08 characters.

    As in ww8graf.cxx SwWW8ImplReader::Read_GrafLayer, only the main story
    (PlcSpaMom) and the header story (PlcSpaHdr, CPs relative to the start
    of the header document) have drawn objects; the Spa must sit exactly at
    the anchor CP. Its lid names the shape (msdffimp.cxx GetShape)."""
    got = fib.pair("DggInfo", len(table)) if hasattr(fib, "pair") else None
    if not got or got[1] < 8 or not anchors:
        return []
    art = _OfficeArt(table, got[0], got[1])
    if not art.shapes or not art.bstore:
        return []
    hdr_start = max(0, int(getattr(fib, "ccpText", 0) or 0)) + max(0, int(getattr(fib, "ccpFtn", 0) or 0))
    out: list[Image] = []
    for story, plc, base in (("main", "PlcSpaMom", 0), ("headers", "PlcSpaHdr", hdr_start)):
        cps = anchors.get(story) or []
        if not cps:
            continue
        spa = _officeart_spa(fib, table, plc)
        if not spa:
            continue
        # A header story inherited by several sections is emitted (and its
        # anchors listed) once per section; its shapes exist only once.
        for cp in sorted(set(cps)):
            lid = spa.get(cp - base)
            if lid is None:
                continue
            for pib in art.picture_pibs(lid & 0xFFFFFFFF):
                got_blip = art.blip(pib, word_document)
                if got_blip:
                    data, mime = got_blip
                    out.append(Image(data=data, mime=mime, kind="floating", story=story, cp=cp))
    return out


# ------------------------------------------------------------- inline
_RT_BLIP_FIRST, _RT_BLIP_LAST = 0xF01A, 0xF02A
_PICF_SIZE = 68
_MM_SHAPE, _MM_SHAPEFILE = 0x0064, 0x0066
_MAX_RECORDS = 4096


def _blips_in(data: bytes, pos: int, end: int) -> list[tuple[bytes, str]]:
    """BLIPs found in the record sequence data[pos:end]: FBSE records with an
    embedded BLIP, and bare BLIP records (OfficeArtInlineSpContainer.rgfb)."""
    out: list[tuple[bytes, str]] = []
    for _ in range(_MAX_RECORDS):
        if pos + 8 > end:
            break
        _, rtype, ln = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        rend = min(end, body + ln)
        b = None
        if rtype == _RT_FBSE:
            if body + _FBSE_SIZE <= rend:
                sub = body + _FBSE_SIZE + data[body + 33]
                b = decode_blip(data, sub, rend)
        elif _RT_BLIP_FIRST <= rtype <= _RT_BLIP_LAST:
            b = decode_blip(data, pos, rend)
        if b:
            out.append(b)
        if ln > end - body:
            break
        pos = body + ln
    return out


def _inline_picture(data: bytes, off: int) -> Image | None:
    """Decode one PICFAndOfficeArtData at data[off:] ([MS-DOC] 2.9.192)."""
    if off < 0 or off + _PICF_SIZE > len(data):
        return None
    lcb, cb_header, mm = struct.unpack_from("<iHH", data, off)
    end = min(off + lcb, len(data))
    pos = off + cb_header
    if lcb <= cb_header or cb_header < _PICF_SIZE or pos >= end:
        return None
    if mm == _MM_SHAPEFILE:
        pos += 1 + data[pos]
    if mm in (_MM_SHAPE, _MM_SHAPEFILE):
        if pos + 8 > end:
            return None
        rtype, ln = struct.unpack_from("<HI", data, pos + 2)
        if rtype == _RT_SP_CONTAINER:
            pos += 8 + ln
        blips = _blips_in(data, pos, end)
        if blips:
            return Image(data=blips[0][0], mime=blips[0][1], kind="inline")
        return None
    return None


_OLE_FIELDS = ("EMBED", "LINK", "CONTROL")


def _ole_field_cps(fib, props, anchors) -> set[int]:
    """CPs of the anchors that sit inside an EMBED/LINK field: their picture
    is the preview of an OLE object, which is not reported."""
    wanted = {cp for cps in anchors.values() for cp in cps}
    if not wanted:
        return set()
    total = sum(max(0, int(getattr(fib, a, 0) or 0)) for a in (
        "ccpText", "ccpFtn", "ccpHdd", "ccpAtn", "ccpEdn", "ccpTxbx", "ccpHdrTxbx"))
    last = min(total, max(wanted) + 1)
    found: set[int] = set()
    stack: list[list] = []  # [code chars, in_code, is_ole]
    step = 1 << 16
    for lo in range(0, last, step):
        text = props._pieces.text(lo, min(last, lo + step))
        for i, ch in enumerate(text):
            if ch == "\x13":
                if len(stack) < 64:
                    stack.append([[], True, False])
            elif ch == "\x14":
                if stack and stack[-1][1]:
                    top = stack[-1]
                    top[1] = False
                    code = "".join(top[0]).lstrip().upper()
                    top[2] = code.startswith(_OLE_FIELDS)
            elif ch == "\x15":
                if stack:
                    stack.pop()
            elif stack and stack[-1][1]:
                if len(stack[-1][0]) < 32:
                    stack[-1][0].append(ch)
            elif ch == "\x01" and lo + i in wanted and any(e[2] for e in stack):
                found.add(lo + i)
    return found


def extract_inline_images(fib, data_stream: bytes | None, props, anchors) -> list:
    """Inline pictures: 0x01 anchors with sprmCPicLocation, not sprmCFData /
    sprmCFOle2 (ww8par.cxx ImportGraf, ww8graf2.cxx ImportGraf1), and not
    the preview of an EMBED/LINK field."""
    if not data_stream:
        return []
    out: list = []
    try:
        ole = _ole_field_cps(fib, props, anchors)
    except (struct.error, IndexError, ValueError):
        ole = set()
    for story, cps in anchors.items():
        for cp in cps:
            try:
                if cp in ole or _char_at(props, cp) != "\x01":
                    continue
                sp = props.sprms_at(cp)
                op = sp.get(SPRM_CPICLOCATION)
                if not op or len(op) < 4:
                    continue
                if any(sp.get(k, b"\0")[:1] not in (b"", b"\0") for k in (SPRM_CFDATA, SPRM_CFOLE2, SPRM_CFOBJ)):
                    continue
                if sp.get(SPRM_CFVANISH, b"\0")[:1] in (b"\x01", b"\x81"):
                    continue  # hidden text: LibreOffice drops the picture
                img = _inline_picture(data_stream, struct.unpack_from("<i", op, 0)[0])
            except (struct.error, IndexError, ValueError):
                continue
            if img:
                img.story = story
                img.cp = cp
                out.append(img)
    return out


def _char_at(props, cp: int) -> str:
    """The character at `cp` (anchor lists contain both 0x01 and 0x08)."""
    return props._pieces.text(cp, cp + 1)
