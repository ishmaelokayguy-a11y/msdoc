# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Inline picture extraction: synthetic Data-stream structures."""
import struct
import zlib
from types import SimpleNamespace

from msdoc import images
from msdoc.props import SPRM_CFDATA, SPRM_CFVANISH, SPRM_CPICLOCATION

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


def rec(ver, inst, rtype, body):
    return struct.pack("<HHI", ver | (inst << 4), rtype, len(body)) + body


def blip(rtype, inst, payload):
    return rec(0, inst, rtype, payload)


def bse(blip_rec, btype=6):
    head = struct.pack("<BB16sHIIIBBBB", btype, btype, b"u" * 16, 0, len(blip_rec), 1, 0, 0, 0, 0, 0)
    return rec(2, btype, 0xF007, head + blip_rec)


def picf(tail, mm=0x64):
    return struct.pack("<iHH", 68 + len(tail), 68, mm) + b"\0" * 60 + tail


SP = rec(15, 0, 0xF004, rec(2, 0xA, 0xF00A, b"\0" * 8))


def inline_at(data, off=0):
    return images._inline_picture(data, off)


def test_png_in_bse():
    d = picf(SP + bse(blip(0xF01E, 0x6E0, b"u" * 16 + b"\xff" + PNG)))
    img = inline_at(d)
    assert (img.data, img.mime, img.kind) == (PNG, "image/png", "inline")


def test_two_uid_variant_and_shapefile_name():
    d = picf(b"\x03abc" + SP + bse(blip(0xF01E, 0x6E1, b"u" * 32 + b"\xff" + PNG)), mm=0x66)
    assert inline_at(d).data == PNG


def test_compressed_wmf_is_inflated():
    wmf = b"\xd7\xcd\xc6\x9a" + b"w" * 40
    comp = zlib.compress(wmf)
    meta = struct.pack("<I16s8sIBB", len(wmf), b"", b"", len(comp), 0, 0xFE)
    meta = meta[:32] + b"\0\xfe"  # compression 0 (deflate), filter 0xFE
    img = inline_at(picf(SP + bse(blip(0xF01B, 0x216, b"u" * 16 + meta + comp), 3)))
    assert (img.data, img.mime) == (wmf, "image/x-wmf")


def test_dib_gets_bitmapfileheader():
    dib = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 8, 0, 16, 0, 0, 2, 0) + b"\0" * 8 + b"\0" * 16
    img = inline_at(picf(SP + bse(blip(0xF01F, 0x7A8, b"u" * 16 + b"\xff" + dib), 7)))
    assert img.mime == "image/bmp"
    assert img.data[:2] == b"BM"
    assert struct.unpack_from("<I", img.data, 10)[0] == 14 + 40 + 8
    assert img.data[14:] == dib


def test_garbage_is_none():
    assert inline_at(b"") is None
    assert inline_at(b"\xff" * 200) is None
    assert inline_at(picf(SP + b"\xff" * 30)) is None
    assert inline_at(picf(b"")) is None


class FakeProps:
    def __init__(self, text, sprms):
        self._pieces = SimpleNamespace(text=lambda lo, hi: text[lo:hi])
        self._sprms = sprms

    def sprms_at(self, cp):
        return self._sprms.get(cp, {})


def run(text, sprms, data):
    fib = SimpleNamespace(ccpText=len(text))
    cps = [i for i, c in enumerate(text) if c in "\x01\x08"]
    return images.extract_inline_images(fib, data, FakeProps(text, sprms), {"main": cps})


def test_anchor_filters():
    good = picf(SP + bse(blip(0xF01E, 0x6E0, b"u" * 16 + b"\xff" + PNG)))
    loc = {SPRM_CPICLOCATION: struct.pack("<i", 0)}
    text = "a\x01b\x01c\x01d\x13EMBED Foo\x14\x01\x15\x08\x01"
    n = len(text)
    sprms = {
        1: loc,
        3: {**loc, SPRM_CFDATA: b"\x01"},
        5: {**loc, SPRM_CFVANISH: b"\x01"},
        n - 3: loc,  # inside an EMBED field
        n - 2: loc,  # 0x08: not an inline picture
        n - 1: loc,
    }
    out = run(text, sprms, good)
    assert [(i.cp, i.story) for i in out] == [(1, "main"), (n - 1, "main")]
    assert run(text, sprms, None) == []
