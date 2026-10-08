# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sw/source/filter/ww8/ww8scan.cxx (class WW8Fib) (MPL-2.0).
# That LibreOffice file incorporates work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""File Information Block.

Spec: [MS-DOC] 2.5.1 Fib, 2.5.2 FibBase, 2.5.4-2.5.5 FibRgLw97,
2.5.6-2.5.11 FibRgFcLcb97/2000/2002/2003/2007, 2.5.12 FibRgCswNew.
LibreOffice reference: sw/source/filter/ww8/ww8scan.cxx, class WW8Fib
(constructor parses the FIB from a stream; WW8Fib::GetFIBVersion).

Requirements:
  * wIdent must be 0xA5EC, otherwise UnsupportedFormatError
  * nFib < 0x00C1 (Word 6/95 and older) -> UnsupportedFormatError
  * fEncrypted and not fObfuscated -> EncryptedDocumentError (RC4/CryptoAPI)
  * fObfuscated (XOR) -> EncryptedDocumentError for now
  * table stream = "1Table" if fWhichTblStm else "0Table"
  * nFibNew from FibRgCswNew overrides nFib when present
  * every fc/lcb pair is exposed by its spec name WITHOUT the "fc"/"lcb"
    prefix, e.g. fib.fclcb["Clx"] == (fcClx, lcbClx)
  * be tolerant: cbRgFcLcb may be smaller than the version implies (older
    writers) -- missing pairs are (0, 0)

All offsets below are relative to the start of the FIB (= start of the
WordDocument stream) and apply only to nFib >= 0x6A ("Ver8", i.e. Word 97+;
everything we accept has nFib >= 0x00C1 which is always Ver8). The Ver6/7
("Ver67") layout used by Word 6/95 is out of scope (UnsupportedFormatError).

Byte layout was cross-checked against WW8Fib::WW8Fib's sequence of
rSt.ReadXxx()/Seek() calls rather than the stale hex-offset comments on the
struct's member declarations in ww8scan.hxx, which describe the older Ver67
layout and do not match Ver8 (there is no fcMin/fcMac-is-at-0x18 style
constant offset for anything after FibRgLw97 -- Ver8 inserts the FibRgW97
"shorts" block that shifts everything that follows).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError

FIB_WIDENT = 0xA5EC
FIB_MIN_NFIB = 0x00C1

# FibBase + FibRgW97 + FibRgLw97 (the fixed-size part every Ver8 FIB has,
# through cbRgFcLcb) ends at 0x9A: that is where the FibRgFcLcb array starts.
_RGFCLCB_START = 0x9A

# FibRgFcLcb97 (2.5.6), read back-to-back starting at _RGFCLCB_START: 62
# (fc, lcb) pairs with no padding between them. Names are the spec field
# names with the "fc"/"lcb" prefix stripped.
_SEQUENTIAL_PAIR_NAMES = (
    "StshfOrig", "Stshf", "PlcffndRef", "PlcffndTxt", "PlcfandRef", "PlcfandTxt",
    "PlcfSed", "PlcfPad", "PlcfPhe", "SttbfGlsy", "PlcfGlsy", "PlcfHdd",
    "PlcfBteChpx", "PlcfBtePapx", "PlcfSea", "Sttbfffn",
    "PlcfFldMom", "PlcfFldHdr", "PlcfFldFtn", "PlcfFldAtn", "PlcfFldMcr",
    "SttbfBkmk", "PlcfBkf", "PlcfBkl", "Cmds", "PlcfMcr", "SttbfMcr",
    "PrDrvr", "PrEnvPort", "PrEnvLand", "Wss", "Dop", "SttbfAssoc", "Clx",
    "PlcfPgdFtn", "AutosaveSource", "GrpXstAtnOwners", "SttbfAtnBkmk",
    # reserved in Ver8+ (used for drawn objects pre-Word97; always empty now)
    "PlcfDoaMom", "PlcfDoaHdr",
    "PlcSpaMom", "PlcSpaHdr", "PlcfAtnbkf", "PlcfAtnbkl", "Pms",
    "FormFieldSttbf", "PlcfendRef", "PlcfendTxt", "PlcfFldEdn", "PlcfPgdEdn",
    "DggInfo", "SttbfRMark", "SttbfCaption", "SttbAutoCaption", "PlcfWkb",
    "PlcfSpl", "PlcftxbxTxt", "PlcfFldTxbx", "PlcfHdrtxbxTxt", "PlcfFldHdrTxbx",
    "StwUser", "Sttbttmbd",
)

# FibRgFcLcb2000/2002/2003 (2.5.7-2.5.9): not contiguous with the block
# above -- WW8Fib::WW8Fib jumps to these absolute offsets (ww8scan.cxx,
# "rSt.Seek(0x02da)" and siblings) because real files leave reserved/unused
# space in between (e.g. 0x302-0x372 is documented as "== ignore").
_TRAILING_PAIR_OFFSETS = (
    ("SttbFnm", 0x2DA),
    ("PlcfLst", 0x2E2),
    ("PlfLfo", 0x2EA),
    ("PlcftxbxBkd", 0x2F2),
    ("PlcfHdrtxbxBkd", 0x2FA),
    ("SttbListNames", 0x372),
    ("PlcfTch", 0x382),
    ("AtrdExtra", 0x41A),
    ("PlcfBkfFactoid", 0x432),
    ("PlcfBklFactoid", 0x442),
    ("FactoidData", 0x44A),
)


def _u16(data: bytes, off: int) -> Optional[int]:
    if off < 0 or off + 2 > len(data):
        return None
    return data[off] | (data[off + 1] << 8)


def _i16(data: bytes, off: int) -> Optional[int]:
    v = _u16(data, off)
    if v is None:
        return None
    return v - 0x10000 if v >= 0x8000 else v


def _u32(data: bytes, off: int) -> Optional[int]:
    if off < 0 or off + 4 > len(data):
        return None
    return data[off] | (data[off + 1] << 8) | (data[off + 2] << 16) | (data[off + 3] << 24)


def _i32(data: bytes, off: int) -> Optional[int]:
    v = _u32(data, off)
    if v is None:
        return None
    return v - 0x100000000 if v >= 0x80000000 else v


@dataclass
class Fib:
    nfib: int = 0
    fWhichTblStm: bool = False
    fEncrypted: bool = False
    fObfuscated: bool = False
    fComplex: bool = False
    lid: int = 0
    fExtChar: bool = False
    # FibRgLw97
    ccpText: int = 0
    ccpFtn: int = 0
    ccpHdd: int = 0
    ccpAtn: int = 0
    ccpEdn: int = 0
    ccpTxbx: int = 0
    ccpHdrTxbx: int = 0
    # FibRgFcLcb*: name -> (fc, lcb)
    fclcb: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def table_stream_name(self) -> str:
        return "1Table" if self.fWhichTblStm else "0Table"

    def pair(self, name: str, table_len: int) -> Optional[tuple[int, int]]:
        """(fc, lcb) for `name`, or None if absent or out of range of a table
        stream of length `table_len` (nearly every fc/lcb pair is an offset
        into the table stream, not the WordDocument stream)."""
        got = self.fclcb.get(name)
        if got is None:
            return None
        fc, lcb = got
        if fc < 0 or lcb < 0 or fc > table_len or fc + lcb > table_len:
            return None
        return (fc, lcb)


def parse_fib(word_document: bytes) -> Fib:
    """Parse the FIB at offset 0 of the WordDocument stream."""
    n = len(word_document)

    wIdent = _u16(word_document, 0x00)
    nFib = _u16(word_document, 0x02)
    if wIdent is None or nFib is None:
        raise CorruptDocumentError("WordDocument stream too short for a FIB header")

    if wIdent != FIB_WIDENT:
        raise UnsupportedFormatError(
            f"unrecognised FIB signature 0x{wIdent:04x} (expected 0x{FIB_WIDENT:04x})"
        )
    if nFib < FIB_MIN_NFIB:
        raise UnsupportedFormatError(f"nFib 0x{nFib:04x} predates Word 97 (out of scope)")

    lid = _u16(word_document, 0x06) or 0

    aBits1 = word_document[0x0A] if n > 0x0A else 0
    aBits2 = word_document[0x0B] if n > 0x0B else 0
    fComplex = bool(aBits1 & 0x04)
    fEncrypted = bool(aBits2 & 0x01)
    fWhichTblStm = bool(aBits2 & 0x02)
    fObfuscated = bool(aBits2 & 0x80)

    if fEncrypted or fObfuscated:
        # RC4/CryptoAPI (fEncrypted) or XOR obfuscation (fObfuscated); we do
        # not attempt to decrypt either.
        raise EncryptedDocumentError("document is password-protected")

    # FibRgLw97 (2.5.4/2.5.5), Ver8 layout: csw at 0x20 .. cbRgFcLcb at 0x98.
    ccpText = _i32(word_document, 0x4C) or 0
    ccpFtn = _i32(word_document, 0x50) or 0
    ccpHdd = _i32(word_document, 0x54) or 0
    ccpAtn = _i32(word_document, 0x5C) or 0
    ccpEdn = _i32(word_document, 0x60) or 0
    ccpTxbx = _i32(word_document, 0x64) or 0
    ccpHdrTxbx = _i32(word_document, 0x68) or 0

    cfclcb = _u16(word_document, 0x98) or 0
    rgfclcb_end = _RGFCLCB_START + cfclcb * 8

    fclcb: dict[str, tuple[int, int]] = {}

    def read_pair(off: int) -> tuple[int, int]:
        # Tolerate short FIBs (cbRgFcLcb smaller than the version implies,
        # or a stream truncated before this point): missing pairs are (0, 0)
        # rather than an error.
        if off + 8 > rgfclcb_end or off + 8 > n:
            return (0, 0)
        fc = _u32(word_document, off)
        lcb = _u32(word_document, off + 4)
        if fc is None or lcb is None:
            return (0, 0)
        return (fc, lcb)

    off = _RGFCLCB_START
    for name in _SEQUENTIAL_PAIR_NAMES:
        fclcb[name] = read_pair(off)
        off += 8
    for name, foff in _TRAILING_PAIR_OFFSETS:
        fclcb[name] = read_pair(foff)

    # FibRgCswNew (2.5.12): an optional array of 16-bit values immediately
    # after the FibRgFcLcb array (not at a fixed offset -- it depends on the
    # actual cbRgFcLcb, which varies by writer/version). When present,
    # rgCswNew[0] is nFibNew, the true sub-version, and overrides nFib.
    cswNew = _u16(word_document, rgfclcb_end) or 0
    nFibNew = (_u16(word_document, rgfclcb_end + 2) or 0) if cswNew >= 1 else 0
    effective_nfib = nFibNew if nFibNew else nFib

    return Fib(
        nfib=effective_nfib,
        fWhichTblStm=fWhichTblStm,
        fEncrypted=fEncrypted,
        fObfuscated=fObfuscated,
        fComplex=fComplex,
        lid=lid,
        fExtChar=bool(aBits2 & 0x10),
        ccpText=ccpText,
        ccpFtn=ccpFtn,
        ccpHdd=ccpHdd,
        ccpAtn=ccpAtn,
        ccpEdn=ccpEdn,
        ccpTxbx=ccpTxbx,
        ccpHdrTxbx=ccpHdrTxbx,
        fclcb=fclcb,
    )
