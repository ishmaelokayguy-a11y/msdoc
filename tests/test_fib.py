# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Tests for msdoc.fib.

cfb.py is not implemented yet, so corpus-backed tests open the WordDocument
stream with olefile directly (test-only dependency, per CLAUDE.md).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

olefile = pytest.importorskip("olefile")

from msdoc.errors import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError
from msdoc.fib import Fib, parse_fib

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
import common  # noqa: E402

DATA = common.data_dir()
MANIFEST = DATA / "corpus" / "manifest.json"


def _manifest():
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _dev_ids(kind):
    return [k for k, v in _manifest().items() if v["kind"] == kind and v["split"] == "dev"]


def _word_document_stream(doc_id):
    """WordDocument stream bytes for a dev corpus id, or None if the file
    is not an OLE2 container or has no WordDocument stream at all (that is
    cfb.py's problem, not fib.py's -- those files raise before parse_fib is
    ever called)."""
    path = DATA / "corpus" / "dev" / f"{doc_id}.doc"
    try:
        ole = olefile.OleFileIO(str(path))
    except Exception:
        return None
    try:
        return ole.openstream("WordDocument").read()
    except Exception:
        return None


# --------------------------------------------------------------- synthetic


def _minimal_fib(nfib=0x00C1, wident=0xA5EC, aBits2=0x00, cfclcb=0, extra=b"", ccp_text=0):
    """Build the smallest-plausible Ver8 FIB prefix used by the tests
    below: FibBase (through fcMac) + FibRgW97 + FibRgLw97 up to and
    including cbRgFcLcb, followed by `cfclcb` zeroed (fc, lcb) pairs."""
    buf = bytearray(0x9A)
    struct.pack_into("<H", buf, 0x00, wident)
    struct.pack_into("<H", buf, 0x02, nfib)
    struct.pack_into("<B", buf, 0x0A, 0x00)
    struct.pack_into("<B", buf, 0x0B, aBits2)
    struct.pack_into("<i", buf, 0x4C, ccp_text)
    struct.pack_into("<H", buf, 0x98, cfclcb)
    buf += bytes(8 * cfclcb)
    buf += extra
    return bytes(buf)


def test_wident_mismatch_raises_unsupported():
    data = _minimal_fib(wident=0x1234)
    with pytest.raises(UnsupportedFormatError):
        parse_fib(data)


def test_old_nfib_raises_unsupported():
    data = _minimal_fib(nfib=0x0065)  # Word 6.0
    with pytest.raises(UnsupportedFormatError):
        parse_fib(data)


def test_nfib_just_below_minimum_raises_unsupported():
    data = _minimal_fib(nfib=0x00C0)
    with pytest.raises(UnsupportedFormatError):
        parse_fib(data)


def test_nfib_at_minimum_is_accepted():
    data = _minimal_fib(nfib=0x00C1)
    f = parse_fib(data)
    assert f.nfib == 0x00C1


def test_encrypted_raises():
    data = _minimal_fib(aBits2=0x01)  # fEncrypted
    with pytest.raises(EncryptedDocumentError):
        parse_fib(data)


def test_obfuscated_raises():
    data = _minimal_fib(aBits2=0x80)  # fObfuscated
    with pytest.raises(EncryptedDocumentError):
        parse_fib(data)


def test_which_table_stream_bit():
    data = _minimal_fib(aBits2=0x00)
    assert parse_fib(data).table_stream_name == "0Table"
    data = _minimal_fib(aBits2=0x02)
    assert parse_fib(data).table_stream_name == "1Table"


def test_too_short_for_header_raises_corrupt():
    with pytest.raises(CorruptDocumentError):
        parse_fib(b"\x01\x02")


def test_empty_input_raises_corrupt():
    with pytest.raises(CorruptDocumentError):
        parse_fib(b"")


def test_short_fib_tolerated_missing_pairs_are_zero():
    # cbRgFcLcb says 0 pairs -- every fc/lcb pair must come back (0, 0),
    # not an error, not an IndexError from reading past the buffer.
    data = _minimal_fib(cfclcb=0)
    f = parse_fib(data)
    assert f.fclcb["Clx"] == (0, 0)
    assert f.fclcb["DggInfo"] == (0, 0)
    assert f.fclcb["PlcfTch"] == (0, 0)


def test_partial_fib_fills_only_available_pairs():
    # cbRgFcLcb says 34 pairs (enough for Clx, the 34th), nothing beyond.
    data = _minimal_fib(cfclcb=34)
    f = parse_fib(data)
    assert f.fclcb["StshfOrig"] == (0, 0)
    assert f.fclcb["Clx"] == (0, 0)  # still zero: we wrote no real values
    assert f.fclcb["DggInfo"] == (0, 0)


def test_clx_pair_round_trip():
    cfclcb = 34  # enough pairs to cover StshfOrig .. Clx
    data = bytearray(_minimal_fib(cfclcb=cfclcb))
    clx_index = 33  # 0-based index of "Clx" in the sequential block
    off = 0x9A + clx_index * 8
    struct.pack_into("<ii", data, off, 1234, 56)
    f = parse_fib(bytes(data))
    assert f.fclcb["Clx"] == (1234, 56)


def test_ccp_text_round_trip():
    data = _minimal_fib(ccp_text=4321)
    assert parse_fib(data).ccpText == 4321


def test_truncated_stream_after_nfib_does_not_crash():
    # Only wIdent + nFib are present; everything else is truncated away.
    # That must degrade gracefully (zeroed fields), not raise or crash --
    # cbRgFcLcb itself is part of the truncated tail, so every fc/lcb pair
    # is "missing" per the short-FIB tolerance rule.
    f = parse_fib(b"\xec\xa5\xc1\x00")
    assert f.nfib == 0x00C1
    assert f.ccpText == 0
    assert f.fclcb["Clx"] == (0, 0)


def test_fib_new_overrides_nfib():
    cfclcb = 0
    data = bytearray(_minimal_fib(nfib=0x00C1, cfclcb=cfclcb))
    rgfclcb_end = 0x9A + cfclcb * 8
    data += bytes(4)
    struct.pack_into("<H", data, rgfclcb_end, 1)  # cswNew >= 1
    struct.pack_into("<H", data, rgfclcb_end + 2, 0x0112)  # nFibNew = Word 2007
    f = parse_fib(bytes(data))
    assert f.nfib == 0x0112


def test_fib_new_zero_does_not_override():
    cfclcb = 0
    data = bytearray(_minimal_fib(nfib=0x00C1, cfclcb=cfclcb))
    rgfclcb_end = 0x9A + cfclcb * 8
    data += bytes(4)
    struct.pack_into("<H", data, rgfclcb_end, 1)  # cswNew >= 1
    struct.pack_into("<H", data, rgfclcb_end + 2, 0)  # nFibNew = 0 -> ignored
    f = parse_fib(bytes(data))
    assert f.nfib == 0x00C1


def test_pair_out_of_range_returns_none():
    f = Fib(fclcb={"Clx": (100, 50)})
    assert f.pair("Clx", table_len=120) is None  # 100+50 > 120
    assert f.pair("Clx", table_len=200) == (100, 50)
    assert f.pair("Missing", table_len=200) is None


def test_pair_negative_fc_returns_none():
    f = Fib(fclcb={"Clx": (-1, 50)})
    assert f.pair("Clx", table_len=200) is None


# ---------------------------------------------------------------- corpus


_REQUIRED_KEYS = (
    "Clx", "PlcfBteChpx", "PlcfBtePapx", "PlcffndRef", "PlcffndTxt",
    "PlcfandTxt", "PlcfHdd", "PlcfendTxt", "PlcftxbxTxt", "PlcfHdrtxbxTxt",
    "PlcSpaMom", "PlcSpaHdr", "DggInfo", "PlcfFldMom",
)


@pytest.mark.parametrize("doc_id", _dev_ids("normal"))
def test_normal_corpus_files_parse(doc_id):
    wd = _word_document_stream(doc_id)
    if wd is None:
        pytest.skip("not an OLE2 file or no WordDocument stream (cfb.py's concern)")
    f = parse_fib(wd)
    assert f.nfib >= 0x00C1
    assert f.ccpText >= 0
    for key in _REQUIRED_KEYS:
        assert key in f.fclcb


@pytest.mark.parametrize("doc_id", _dev_ids("legacy"))
def test_legacy_corpus_files_raise_unsupported_or_are_not_ole(doc_id):
    wd = _word_document_stream(doc_id)
    if wd is None:
        pytest.skip("not an OLE2 file or no WordDocument stream (cfb.py's concern)")
    with pytest.raises(UnsupportedFormatError):
        parse_fib(wd)


@pytest.mark.parametrize("doc_id", _dev_ids("encrypted"))
def test_encrypted_corpus_files_raise(doc_id):
    wd = _word_document_stream(doc_id)
    if wd is None:
        pytest.skip("not an OLE2 file or no WordDocument stream (cfb.py's concern)")
    with pytest.raises(EncryptedDocumentError):
        parse_fib(wd)


@pytest.mark.parametrize("doc_id", _dev_ids("normal"))
def test_normal_corpus_pair_helper_never_crashes(doc_id):
    # Some "normal" dev files are oss-fuzz regression documents (garbage
    # fc/lcb values by design) -- pair() must just reject them, not raise.
    wd = _word_document_stream(doc_id)
    if wd is None:
        pytest.skip("not an OLE2 file or no WordDocument stream (cfb.py's concern)")
    f = parse_fib(wd)
    got = f.pair("Clx", table_len=len(wd))
    if got is not None:
        fc, lcb = got
        assert 0 <= fc and 0 <= lcb and fc + lcb <= len(wd)
