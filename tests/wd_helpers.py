# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Test-only FIB reading, independent of msdoc.fib (which may still be a stub
in this worktree — see prompts/12-text.md). Reads just enough of the FIB
(FibBase, FibRgLw97, FibRgFcLcb97) with olefile to exercise pieces.py and
props.py against real corpus documents.
"""
from __future__ import annotations

import struct
from pathlib import Path

import olefile

# [MS-DOC] 2.5.6 FibRgFcLcb97: field order, 0-based index of each fc/lcb pair.
_FCLCB_NAMES = [
    "StshfOrig", "Stshf", "PlcffndRef", "PlcffndTxt", "PlcfandRef", "PlcfandTxt",
    "PlcfSed", "PlcPad", "PlcfPhe", "SttbfGlsy", "PlcfGlsy", "PlcfHdd",
    "PlcfBteChpx", "PlcfBtePapx", "PlcfSea", "SttbfFfn", "PlcfFldMom", "PlcfFldHdr",
    "PlcfFldFtn", "PlcfFldAtn", "PlcfFldMcr", "SttbfBkmk", "PlcfBkf", "PlcfBkl",
    "Cmds", "Unused1", "SttbfMcr", "PrDrvr", "PrEnvPort", "PrEnvLand", "Wss", "Dop",
    "SttbfAssoc", "Clx",
]
_FIB_RG_LW97_OFFSET = 64
_CCP_TEXT_INDEX = 3  # cbMac, reserved1, reserved2, ccpText, ...
_FIB_RG_FCLCB_COUNT_OFFSET = 152
_FIB_RG_FCLCB_OFFSET = 154


class SimpleFib:
    def __init__(self, fclcb: dict, ccp_text: int, table_stream_name: str) -> None:
        self.fclcb = fclcb
        self.ccp_text = ccp_text
        self.table_stream_name = table_stream_name


def read_simple_fib(word_document: bytes) -> SimpleFib:
    # FibBase: wIdent(2) nFib(2) unused(2) lid(2) pnNext(2) flags1(2) ...
    flags1 = struct.unpack_from("<H", word_document, 10)[0]
    table_stream_name = "1Table" if (flags1 & 0x0200) else "0Table"
    n_pairs = struct.unpack_from("<H", word_document, _FIB_RG_FCLCB_COUNT_OFFSET)[0]
    fclcb = {}
    for i, name in enumerate(_FCLCB_NAMES):
        if i >= n_pairs:
            break
        off = _FIB_RG_FCLCB_OFFSET + i * 8
        fc, lcb = struct.unpack_from("<II", word_document, off)
        fclcb[name] = (fc, lcb)
    ccp_text = struct.unpack_from("<i", word_document, _FIB_RG_LW97_OFFSET + _CCP_TEXT_INDEX * 4)[0]
    return SimpleFib(fclcb, ccp_text, table_stream_name)


def open_doc_streams(path: Path):
    """Return (word_document, table_stream) bytes for a real .doc corpus file."""
    ole = olefile.OleFileIO(str(path))
    try:
        word_document = ole.openstream("WordDocument").read()
        fib = read_simple_fib(word_document)
        table_stream = ole.openstream(fib.table_stream_name).read()
        return word_document, table_stream, fib
    finally:
        ole.close()
