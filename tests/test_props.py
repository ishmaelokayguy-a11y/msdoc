# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Synthetic-byte tests for msdoc.props (sprm sizing + CharProps)."""
from __future__ import annotations

import struct

from msdoc.fib import Fib
from msdoc.pieces import PieceTable
from msdoc.props import (
    SPRM_CFOLE2,
    SPRM_CFSPEC,
    SPRM_CFVANISH,
    SPRM_CPICLOCATION,
    CharProps,
    _iter_sprms,
    sprm_operand_size,
)

_FCOMPRESSED_FLAG = 0x40000000


# --------------------------------------------------------------------- sprm size


def test_fixed_operand_sizes_by_spra():
    # spra = top 3 bits of the 16-bit sprm id ([MS-DOC] 2.6.1 Sprm).
    assert sprm_operand_size(0x0000, b"", 0) == 1  # spra 0: ToggleOperand
    assert sprm_operand_size(0x2000, b"", 0) == 1  # spra 1
    assert sprm_operand_size(0x4000, b"\x00\x00", 0) == 2  # spra 2
    assert sprm_operand_size(0x6000, b"\x00" * 4, 0) == 4  # spra 3
    assert sprm_operand_size(0x8000, b"\x00\x00", 0) == 2  # spra 4
    assert sprm_operand_size(0xA000, b"\x00\x00", 0) == 2  # spra 5
    assert sprm_operand_size(0xE000, b"\x00" * 3, 0) == 3  # spra 7


def test_known_sprm_constants_have_expected_sizes():
    assert sprm_operand_size(SPRM_CFSPEC, b"\x01", 0) == 1
    assert sprm_operand_size(SPRM_CFOLE2, b"\x01", 0) == 1
    assert sprm_operand_size(SPRM_CPICLOCATION, b"\x00" * 4, 0) == 4


def test_generic_variable_length_sprm():
    data = bytes([3, 0xAA, 0xBB, 0xCC])  # length byte 3, then 3 data bytes
    assert sprm_operand_size(0xC001, data, 0) == 4  # 1 (len byte) + 3


def test_sprm_tdeftable_var2():
    # count stored is (actual length + 1); count=5 -> 4 data bytes + 2 count bytes.
    data = struct.pack("<H", 5) + b"\x00" * 4
    assert sprm_operand_size(0xD608, data, 0) == 6
    # count=0 -> no data at all, just the 2 count bytes.
    data0 = struct.pack("<H", 0)
    assert sprm_operand_size(0xD608, data0, 0) == 2
    # sprmTDefTable10 behaves the same way.
    assert sprm_operand_size(0xD606, data, 0) == 6


def test_sprm_pchgtabs_normal_and_complex():
    # Normal case: first byte is the length, like any other L_VAR sprm.
    data = bytes([3, 1, 2, 3])
    assert sprm_operand_size(0xC615, data, 0) == 4
    # Complex case: first byte 0xFF, then nDel, then 4 bytes per deletion,
    # then nIns, then 3 bytes per insertion.
    n_del, n_ins = 2, 1
    data = bytes([0xFF, n_del]) + b"\x00" * (4 * n_del) + bytes([n_ins]) + b"\x00" * (3 * n_ins)
    assert sprm_operand_size(0xC615, data, 0) == 1 + 2 + 4 * n_del + 3 * n_ins


def test_sprm_size_truncated_data_does_not_raise():
    assert sprm_operand_size(0xC001, b"", 0) == 1  # generic var, no length byte available
    assert sprm_operand_size(0xD608, b"\x01", 0) == 2  # TDefTable, truncated count


def test_iter_sprms_round_trip():
    grpprl = struct.pack("<H", SPRM_CFSPEC) + b"\x01" + struct.pack("<H", SPRM_CPICLOCATION) + struct.pack("<I", 1234)
    parsed = dict(_iter_sprms(grpprl))
    assert parsed[SPRM_CFSPEC] == b"\x01"
    assert parsed[SPRM_CPICLOCATION] == struct.pack("<I", 1234)


# --------------------------------------------------------------------- CharProps


def _chpx_fkp_page(runs: list[tuple[int, int, bytes]]) -> bytes:
    """Build a single 512-byte ChpxFkp page from (fc_start, fc_end, grpprl)."""
    crun = len(runs)
    fcs = [r[0] for r in runs] + [runs[-1][1]]
    page = bytearray(512)
    struct.pack_into(f"<{crun + 1}I", page, 0, *fcs)
    rgb_off = (crun + 1) * 4
    cursor = rgb_off + crun
    offsets = []
    for _, _, grpprl in runs:
        if cursor % 2:
            cursor += 1  # rgb[i]*2 must be able to reach this offset
        offsets.append(cursor)
        blob = bytes([len(grpprl)]) + grpprl
        page[cursor:cursor + len(blob)] = blob
        cursor += len(blob)
    assert cursor <= 511, "test page too small for synthetic Chpx data"
    for i, off in enumerate(offsets):
        page[rgb_off + i] = off // 2
    page[511] = crun
    return bytes(page)


def _piece_table_for(word_document: bytes, pieces: list[tuple[int, int, int, bool, int]]) -> PieceTable:
    """pieces: list of (cp_start, cp_end, fc_byte_offset, compressed, prm)."""
    aCP = [p[0] for p in pieces] + [pieces[-1][1]]
    pcds = b""
    for _, _, fc_off, compressed, prm in pieces:
        fc_raw = (fc_off * 2) | _FCOMPRESSED_FLAG if compressed else fc_off
        pcds += struct.pack("<HIH", 0, fc_raw, prm)
    plcpcd = struct.pack(f"<{len(aCP)}i", *aCP) + pcds
    clx = bytes([0x02]) + struct.pack("<I", len(plcpcd)) + plcpcd
    return PieceTable(clx, 0, len(clx), word_document)


def _fib_with_fkp(fc: int, lcb: int) -> Fib:
    return Fib(fclcb={"PlcfBteChpx": (fc, lcb)})


def test_charprops_runs_reads_chpx_sprms():
    word_document = bytearray(2048)
    page = _chpx_fkp_page([(0, 10, struct.pack("<H", SPRM_CFSPEC) + b"\x01")])
    word_document[0:512] = page

    # PlcfBteChpx: aFC=[0, 10], aPnBteChpx=[pn=0] in the table stream.
    table_stream = struct.pack("<2i", 0, 10) + struct.pack("<I", 0)
    fib = _fib_with_fkp(0, len(table_stream))

    pt = _piece_table_for(bytes(word_document), [(0, 10, 0, True, 0)])
    cp = CharProps(fib, bytes(word_document), table_stream, pt)

    runs = list(cp.runs())
    assert len(runs) == 1
    cp_lo, cp_hi, sprms = runs[0]
    assert (cp_lo, cp_hi) == (0, 10)
    assert sprms[SPRM_CFSPEC] == b"\x01"
    assert cp.sprms_at(3)[SPRM_CFSPEC] == b"\x01"
    assert not cp.warnings


def test_charprops_prm0_overrides_chpx():
    word_document = bytearray(2048)
    # CHPX says "not vanish" implicitly (sprm absent); Prm0 says vanish=1.
    page = _chpx_fkp_page([(0, 10, b"")])
    word_document[0:512] = page
    table_stream = struct.pack("<2i", 0, 10) + struct.pack("<I", 0)
    fib = _fib_with_fkp(0, len(table_stream))

    # Prm0: fComplex=0, isprm=92 (-> sprmCFVanish 0x083C), val=1.
    isprm = 92
    val = 1
    prm = (isprm << 1) | (val << 8)
    pt = _piece_table_for(bytes(word_document), [(0, 10, 0, False, prm)])
    cp = CharProps(fib, bytes(word_document), table_stream, pt)

    sprms = cp.sprms_at(2)
    assert sprms[SPRM_CFVANISH] == bytes([1])


def test_charprops_prm1_grpprl_from_prc():
    word_document = bytearray(2048)
    page = _chpx_fkp_page([(0, 10, b"")])
    word_document[0:512] = page
    table_stream = struct.pack("<2i", 0, 10) + struct.pack("<I", 0)
    fib = _fib_with_fkp(0, len(table_stream))

    # Piece-level grpprl (via Prc[0]) sets sprmCFSpec=1 (a character sprm).
    grpprl = struct.pack("<H", SPRM_CFSPEC) + b"\x01"
    aCP = [0, 10]
    prm = 1  # fComplex=1, igrpprl=0
    fc_raw = _FCOMPRESSED_FLAG  # compressed, actual fc = 0
    pcds = struct.pack("<HIH", 0, fc_raw, prm)
    plcpcd = struct.pack("<2i", *aCP) + pcds
    clx = (
        bytes([0x01]) + struct.pack("<h", len(grpprl)) + grpprl  # Prc[0]
        + bytes([0x02]) + struct.pack("<I", len(plcpcd)) + plcpcd  # Pcdt
    )
    pt = PieceTable(clx, 0, len(clx), bytes(word_document))
    cp = CharProps(fib, bytes(word_document), table_stream, pt)

    sprms = cp.sprms_at(5)
    assert sprms[SPRM_CFSPEC] == b"\x01"


def test_charprops_missing_plcfbtechpx_recovers():
    fib = Fib(fclcb={})
    pt = _piece_table_for(bytes(16), [(0, 5, 0, False, 0)])
    cp = CharProps(fib, bytes(16), b"", pt)
    assert cp.sprms_at(0) == {}
    assert list(cp.runs()) == []
    assert cp.warnings


def test_charprops_corrupt_fkp_page_does_not_crash():
    word_document = bytearray(1024)
    word_document[511] = 0xFF  # crun way above the 0x65 max
    table_stream = struct.pack("<2i", 0, 10) + struct.pack("<I", 0)
    fib = _fib_with_fkp(0, len(table_stream))
    pt = _piece_table_for(bytes(word_document), [(0, 10, 0, False, 0)])
    cp = CharProps(fib, bytes(word_document), table_stream, pt)
    assert list(cp.runs()) == []
    assert cp.warnings
