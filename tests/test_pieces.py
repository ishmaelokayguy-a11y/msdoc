# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Synthetic-byte tests for msdoc.pieces.PieceTable."""
from __future__ import annotations

import struct

from msdoc.pieces import PieceTable

_FCOMPRESSED_FLAG = 0x40000000


def _pcd(fc_raw: int, prm: int = 0, flags: int = 0) -> bytes:
    return struct.pack("<HIH", flags, fc_raw, prm)


def _pcdt(aCP: list[int], pcds: bytes) -> bytes:
    plcpcd = struct.pack(f"<{len(aCP)}i", *aCP) + pcds
    return bytes([0x02]) + struct.pack("<I", len(plcpcd)) + plcpcd


def _prc(grpprl: bytes) -> bytes:
    return bytes([0x01]) + struct.pack("<h", len(grpprl)) + grpprl


def _make_doc(word_len: int = 256) -> bytearray:
    return bytearray(word_len)


def test_mixed_compressed_and_uncompressed_pieces_out_of_fc_order():
    # Piece A: cp [0,5) compressed ascii "Hello" stored late in the stream.
    # Piece B: cp [5,10) uncompressed unicode "World" stored *earlier* in the
    # stream than piece A, to exercise piece order (by CP) != FC order.
    wd = _make_doc()
    wd[100:105] = b"Hello"
    wd[10:20] = "World".encode("utf-16-le")

    fc_raw_a = (100 * 2) | _FCOMPRESSED_FLAG  # compressed: fc stored as byte*2
    fc_raw_b = 10  # uncompressed: fc stored directly

    pcds = _pcd(fc_raw_a) + _pcd(fc_raw_b)
    clx = _pcdt([0, 5, 10], pcds)
    table = bytes(clx)

    pt = PieceTable(table, 0, len(table), bytes(wd))

    assert pt.text(0, 10) == "HelloWorld"
    assert pt.text(0, 5) == "Hello"
    assert pt.text(5, 10) == "World"
    assert pt.text(2, 7) == "lloWo"

    assert pt.cp_to_fc(0) == (100, True)
    assert pt.cp_to_fc(4) == (104, True)
    assert pt.cp_to_fc(5) == (10, False)
    assert pt.cp_to_fc(9) == (18, False)
    assert not pt.warnings


def test_clx_with_leading_prc_blocks():
    wd = _make_doc()
    wd[0:10] = "ABCDE".encode("utf-16-le")
    pcds = _pcd(0)
    clx = _prc(b"\x01\x02\x03") + _prc(b"") + _pcdt([0, 5], pcds)
    table = bytes(clx)

    pt = PieceTable(table, 0, len(table), bytes(wd))
    assert pt.text(0, 5) == "ABCDE"
    assert pt.prc_grpprl(0) == b"\x01\x02\x03"
    assert pt.prc_grpprl(1) == b""
    assert pt.prc_grpprl(2) == b""  # out of range -> empty, no crash


def test_compressed_special_byte_overrides():
    # 0x93/0x94 are the MS-DOC 2.9.73 overrides for left/right double quotes.
    wd = _make_doc()
    wd[0:3] = bytes([0x93, 0x41, 0x94])  # “A”
    pcds = _pcd(0 | _FCOMPRESSED_FLAG)
    clx = _pcdt([0, 3], pcds)
    table = bytes(clx)

    pt = PieceTable(table, 0, len(table), bytes(wd))
    assert pt.text(0, 3) == "“A”"


def test_missing_clx_recovers_with_warning():
    pt = PieceTable(b"", -1, 0, b"")
    assert pt.text(0, 10) == ""
    assert pt.cp_to_fc(0) == (0, False)
    assert pt.warnings


def test_truncated_pcdt_recovers_with_warning():
    table = bytes([0x02]) + struct.pack("<I", 9999)  # lcb far beyond buffer
    pt = PieceTable(table, 0, len(table), b"")
    assert pt.text(0, 10) == ""
    assert pt.warnings


def test_text_out_of_range_cp_does_not_crash():
    wd = _make_doc()
    wd[0:10] = "ABCDE".encode("utf-16-le")
    pcds = _pcd(0)
    clx = _pcdt([0, 5], pcds)
    table = bytes(clx)
    pt = PieceTable(table, 0, len(table), bytes(wd))

    assert pt.text(100, 200) == ""
    assert pt.text(-5, 3) == "ABC"  # clamped to the start of the first piece
    assert pt.cp_to_fc(999) == (8, False)  # clamped to last valid cp in piece


def test_piece_text_beyond_word_document_end_is_clipped():
    wd = _make_doc(20)
    wd[10:20] = "World".encode("utf-16-le")
    # Piece claims 20 chars (40 bytes) starting at fc=10, but WordDocument
    # is only 20 bytes long -> must clip, not crash.
    pcds = _pcd(10)
    clx = _pcdt([0, 20], pcds)
    table = bytes(clx)
    pt = PieceTable(table, 0, len(table), bytes(wd))

    result = pt.text(0, 20)
    assert result.startswith("World")
    assert len(result) <= 20
