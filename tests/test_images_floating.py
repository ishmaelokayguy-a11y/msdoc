# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Floating pictures (OfficeArt) and the shared BLIP decoder."""
from __future__ import annotations

import hashlib
import json
import os
import random
import struct
import zlib
from pathlib import Path

import pytest

from msdoc import images
from msdoc.images import decode_blip, extract_floating_images

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" + b"\xff\xd9"


def rec(rtype: int, body: bytes, inst: int = 0, ver: int = 0) -> bytes:
    return struct.pack("<HHI", (inst << 4) | ver, rtype, len(body)) + body


def cont(rtype: int, *children: bytes, inst: int = 0) -> bytes:
    return rec(rtype, b"".join(children), inst, 0xF)


def blip_png(data: bytes = PNG, two_uids: bool = False) -> bytes:
    inst = 0x6E1 if two_uids else 0x6E0
    return rec(0xF01E, b"U" * (32 if two_uids else 16) + b"\xff" + data, inst)


def blip_metafile(rtype: int, inst: int, data: bytes, compress: bool) -> bytes:
    payload = zlib.compress(data) if compress else data
    mh = struct.pack("<I4i2iI", len(data), 0, 0, 100, 100, 1000, 1000, len(payload))
    mh += bytes([0 if compress else 0xFE, 0xFE])
    return rec(rtype, b"U" * 16 + mh + payload, inst)


def fbse(size: int, fo_delay: int, embedded: bytes = b"", bt: int = 6) -> bytes:
    body = bytes([bt, bt]) + b"\0" * 16 + struct.pack("<HIII", 0xFF, size, 1, fo_delay) + b"\0\0\0\0"
    return rec(0xF007, body + embedded, bt)


def fsp(spid: int, flags: int = 0xA00, shape_type: int = 75) -> bytes:
    return rec(0xF00A, struct.pack("<II", spid, flags), shape_type, 2)


def opt(*props: tuple[int, int]) -> bytes:
    return rec(0xF00B, b"".join(struct.pack("<HI", p, v) for p, v in props), len(props), 3)


def picture(spid: int, pib: int, flags: int = 0xA00) -> bytes:
    return cont(0xF004, fsp(spid, flags), opt((0x4104, pib)))


def group(spid: int, *children: bytes) -> bytes:
    return cont(0xF003, cont(0xF004, rec(0xF009, b"\0" * 16), fsp(spid, 0x201, 0)), *children)


def drawing(*shapes: bytes) -> bytes:
    patriarch = cont(0xF004, rec(0xF009, b"\0" * 16), fsp(1024, 0x5, 0))
    return cont(0xF002, rec(0xF008, b"\0" * 8), cont(0xF003, patriarch, *shapes))


def plcspa(entries: list[tuple[int, int]]) -> bytes:
    cps = [cp for cp, _ in entries] + [entries[-1][0] + 1]
    out = b"".join(struct.pack("<i", c) for c in cps)
    for _, lid in entries:
        out += struct.pack("<i", lid) + b"\0" * 22
    return out


class FakeFib:
    def __init__(self, pairs: dict, ccp_text: int = 100, ccp_ftn: int = 0) -> None:
        self.fclcb = pairs
        self.ccpText = ccp_text
        self.ccpFtn = ccp_ftn

    def pair(self, name, table_len):
        got = self.fclcb.get(name)
        if got is None or got[0] + got[1] > table_len:
            return None
        return got


def build(bstore: list[bytes], drawings: list[tuple[int, bytes]], spa_mom=None, spa_hdr=None, **kw):
    dgg = cont(0xF000, rec(0xF006, b"\0" * 16), cont(0xF001, *bstore, inst=len(bstore)))
    art = dgg + b"".join(bytes([lbl]) + d for lbl, d in drawings)
    table = b"\0" * 10 + art
    pairs = {"DggInfo": (10, len(art))}
    for name, entries in (("PlcSpaMom", spa_mom), ("PlcSpaHdr", spa_hdr)):
        if entries:
            pairs[name] = (len(table), len(plcspa(entries)))
            table += plcspa(entries)
    return FakeFib(pairs, **kw), table


# ------------------------------------------------------------ decode_blip

def test_png_one_and_two_uids():
    assert decode_blip(blip_png(), 0) == (PNG, "image/png")
    assert decode_blip(blip_png(two_uids=True), 0) == (PNG, "image/png")


def test_jpeg_and_cmyk_jpeg():
    assert decode_blip(rec(0xF01D, b"U" * 16 + b"\xff" + JPEG, 0x46A), 0) == (JPEG, "image/jpeg")
    # [MS-ODRAW] 2.2.27: CMYK JPEG is still recType 0xF01D (recInstance 0x6E2/0x6E3).
    assert decode_blip(rec(0xF01D, b"U" * 16 + b"\xff" + JPEG, 0x6E2), 0) == (JPEG, "image/jpeg")
    assert decode_blip(rec(0xF01D, b"U" * 32 + b"\xff" + JPEG, 0x6E3), 0) == (JPEG, "image/jpeg")
    # [MS-ODRAW] 2.2.23 also maps recType 0xF02A to OfficeArtBlipJPEG; Word
    # writes it for CMYK JPEGs (seen with recInstance 0x6E2 in real files).
    assert decode_blip(rec(0xF02A, b"U" * 16 + b"\xff" + JPEG, 0x6E2), 0) == (JPEG, "image/jpeg")
    assert decode_blip(rec(0xF02A, b"U" * 32 + b"\xff" + JPEG, 0x6E3), 0) == (JPEG, "image/jpeg")
    # Neighbouring record types are not BLIPs.
    assert decode_blip(rec(0xF02B, b"U" * 16 + b"\xff" + JPEG, 0x6E2), 0) is None


def test_compressed_and_raw_metafiles():
    emf = b"\x01\0\0\0" + b"emf" * 50
    assert decode_blip(blip_metafile(0xF01A, 0x3D4, emf, True), 0) == (emf, "image/x-emf")
    wmf = b"\x01\0\x09\0" + b"wmf" * 10
    assert decode_blip(blip_metafile(0xF01B, 0x216, wmf, False), 0) == (wmf, "image/x-wmf")


def test_dib_gets_bitmapfileheader():
    hdr = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 8, 0, 0, 0, 0, 0, 0)
    dib = hdr + b"\0" * (256 * 4) + b"\0" * 8
    data, mime = decode_blip(rec(0xF01F, b"U" * 16 + b"\xff" + dib, 0x7A8), 0)
    assert mime == "image/bmp"
    assert data[:2] == b"BM" and data[14:] == dib
    assert struct.unpack_from("<I", data, 2)[0] == len(data)
    assert struct.unpack_from("<I", data, 10)[0] == 14 + 40 + 1024


def test_bad_blips_return_none():
    assert decode_blip(b"", 0) is None
    assert decode_blip(rec(0xF00B, b"x" * 30), 0) is None
    assert decode_blip(blip_png()[:20], 0) is None
    broken = bytearray(blip_metafile(0xF01A, 0x3D4, b"x" * 100, True))
    broken[8 + 16 + 34:] = b"\x00" * (len(broken) - 58)
    assert decode_blip(bytes(broken), 0) is None
    assert decode_blip(blip_png(), -5) is None


# ------------------------------------------------------- floating pictures

def test_embedded_and_delayed_blips():
    delay_png = b"\x89PNG\r\n\x1a\n" + b"delayed"
    delayed = blip_png(delay_png)
    word = b"\0" * 64 + delayed
    fib, table = build([fbse(len(blip_png()), 0, blip_png()), fbse(len(delayed), 64)],
                       [(0, drawing(picture(1025, 1), picture(1026, 2)))],
                       spa_mom=[(3, 1025), (7, 1026)])
    got = extract_floating_images(fib, word, table, {"main": [3, 5, 7]})
    assert [(i.data, i.cp, i.kind, i.story) for i in got] == [
        (PNG, 3, "floating", "main"), (delay_png, 7, "floating", "main")]


def test_group_ole_and_header_anchors():
    fib, table = build([fbse(0, 0, blip_png())],
                       [(1, drawing(picture(2049, 1))),
                        (0, drawing(group(1030, picture(1031, 1), group(1032, picture(1033, 1))),
                                    picture(1040, 1, flags=0xA10)))],
                       spa_mom=[(0, 1030), (4, 1040)], spa_hdr=[(2, 2049)], ccp_text=10, ccp_ftn=5)
    got = extract_floating_images(fib, b"", table, {"main": [0, 4], "headers": [17, 17]})
    assert [(i.story, i.cp) for i in got] == [("main", 0), ("main", 0), ("headers", 17)]


def test_anchor_must_match_spa_cp_and_unused_blips_ignored():
    fib, table = build([fbse(0, 0, blip_png()), fbse(0, 0, blip_png())],
                       [(0, drawing(picture(1025, 2)))], spa_mom=[(3, 1025)])
    assert extract_floating_images(fib, b"", table, {"main": [2, 4]}) == []
    assert len(extract_floating_images(fib, b"", table, {"main": [3]})) == 1


def test_bad_pib_and_bad_delay_offset():
    fib, table = build([fbse(100, 0xFFFFFFFF)],
                       [(0, drawing(picture(1025, 1), picture(1026, 7)))], spa_mom=[(0, 1025), (1, 1026)])
    assert extract_floating_images(fib, b"\0" * 10, table, {"main": [0, 1]}) == []


def test_bstore_bare_blip_entry():
    fib, table = build([blip_png()], [(0, drawing(picture(1025, 1)))], spa_mom=[(0, 1025)])
    assert [i.data for i in extract_floating_images(fib, b"", table, {"main": [0]})] == [PNG]


def test_fuzzed_officeart_never_raises():
    fib, table = build([fbse(0, 0, blip_png()), fbse(30, 70)],
                       [(0, drawing(group(1030, picture(1031, 1), picture(1032, 2))))],
                       spa_mom=[(0, 1030)])
    rng = random.Random(1234)
    art_lo, art_len = fib.fclcb["DggInfo"]
    for _ in range(2000):
        t = bytearray(table)
        for _ in range(rng.randint(1, 8)):
            t[rng.randrange(art_lo, art_lo + art_len)] = rng.randrange(256)
        t = bytes(t[:rng.randint(art_lo, len(t))])
        extract_floating_images(fib, b"\0" * 80, t, {"main": [0]})
        images.extract_images(fib, b"\0" * 80, t, None, None, {"main": [0]})


def test_self_nesting_groups_bounded():
    inner = picture(1031, 1)
    for i in range(200):
        inner = group(2000 + i, inner)
    fib, table = build([fbse(0, 0, blip_png())], [(0, drawing(inner))], spa_mom=[(0, 2199)])
    extract_floating_images(fib, b"", table, {"main": [0]})


# ------------------------------------------------------------ corpus

def _data_dir():
    try:
        import common
        return Path(common.data_dir())
    except Exception:
        return None


@pytest.mark.parametrize("fid", ["259aa01f88c17bb4", "42a0da1e4589e275", "300a4b129fa8d4bb"])
def test_corpus_floating_png_jpeg(fid):
    d = _data_dir()
    if d is None or not (d / "corpus" / "dev" / f"{fid}.doc").exists():
        pytest.skip("corpus not available")
    import msdoc
    doc = msdoc.extract(d / "corpus" / "dev" / f"{fid}.doc")
    golden = json.loads((d / "golden" / f"{fid}.json").read_text())
    floating = [i for i in doc.images if i.kind == "floating"]
    assert len(floating) == len([g for g in golden["images"] if g["kind"] == "floating"])
    ours = {hashlib.sha256(i.data).hexdigest() for i in floating}
    if fid == "259aa01f88c17bb4":
        assert ours == {g["sha256"] for g in golden["images"]}
