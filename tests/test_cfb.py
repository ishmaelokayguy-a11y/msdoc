# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Tests for msdoc.cfb.CompoundFile.

Two kinds of coverage:
  * synthetic byte-level tests for the header, FAT/mini-FAT, directory, and
    deliberately corrupted/cyclic/out-of-range structures (never hang, never
    raise anything but CorruptDocumentError/NotOleFileError);
  * a cross-check against `olefile` (test-only dependency) over every OLE
    file in the dev corpus: same stream list, identical bytes for the
    streams msdoc actually reads.
"""
from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from msdoc import cfb
from msdoc.errors import CorruptDocumentError, NotOleFileError

olefile = pytest.importorskip("olefile")

DATA_DIR = None
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
    import common as _common

    DATA_DIR = _common.data_dir()
except Exception:  # pragma: no cover - oracle helpers are optional for this file
    DATA_DIR = None


ENDOFCHAIN = cfb.ENDOFCHAIN
FREESECT = cfb.FREESECT
NOSTREAM = cfb.NOSTREAM
STGTY_STORAGE = cfb.STGTY_STORAGE
STGTY_STREAM = cfb.STGTY_STREAM
STGTY_ROOT = cfb.STGTY_ROOT


# --------------------------------------------------------------- builder


class CfbBuilder:
    """Minimal hand-rolled CFB (v3, 512-byte sector) writer for tests.

    Not a general-purpose writer: just enough to place streams through the
    regular FAT and/or the Mini FAT, and to let tests reach in and corrupt
    specific fields (FAT entries, directory entries, header bytes) before
    calling .build().
    """

    SECTOR_SIZE = 512
    MINI_SECTOR_SIZE = 64
    MINI_CUTOFF = 4096

    def __init__(self) -> None:
        self._sectors: list[bytes] = []  # sector index -> exactly SECTOR_SIZE bytes
        self._fat: list[int] = []  # parallel to _sectors
        self._entries: list[dict] = []  # directory entries, index 0 is root
        self._add_entry("Root Entry", STGTY_ROOT, NOSTREAM, NOSTREAM, NOSTREAM, ENDOFCHAIN, 0)
        self._mini_sectors: list[bytes] = []
        self._minifat: list[int] = []

    # ---- low-level sector/FAT bookkeeping -----------------------------

    def _alloc_chain(self, data: bytes) -> tuple[int, int]:
        """Write data (padded) as a chain of regular sectors; return (first_sid, size)."""
        size = len(data)
        if size == 0:
            return ENDOFCHAIN, 0
        n = (size + self.SECTOR_SIZE - 1) // self.SECTOR_SIZE
        padded = data + b"\x00" * (n * self.SECTOR_SIZE - size)
        first = len(self._sectors)
        for i in range(n):
            self._sectors.append(padded[i * self.SECTOR_SIZE : (i + 1) * self.SECTOR_SIZE])
            self._fat.append(first + i + 1 if i + 1 < n else ENDOFCHAIN)
        return first, size

    def _alloc_mini_chain(self, data: bytes) -> tuple[int, int]:
        size = len(data)
        if size == 0:
            return ENDOFCHAIN, 0
        n = (size + self.MINI_SECTOR_SIZE - 1) // self.MINI_SECTOR_SIZE
        padded = data + b"\x00" * (n * self.MINI_SECTOR_SIZE - size)
        first = len(self._mini_sectors)
        for i in range(n):
            self._mini_sectors.append(padded[i * self.MINI_SECTOR_SIZE : (i + 1) * self.MINI_SECTOR_SIZE])
            self._minifat.append(first + i + 1 if i + 1 < n else ENDOFCHAIN)
        return first, size

    def _add_entry(self, name, obj_type, left, right, child, start, size) -> int:
        self._entries.append(
            dict(name=name, obj_type=obj_type, left=left, right=right, child=child, start=start, size=size)
        )
        return len(self._entries) - 1

    # ---- public API used by tests --------------------------------------

    def add_stream(self, name: str, data: bytes) -> int:
        """Add a stream as a direct child of the root; returns its entry id."""
        if len(data) < self.MINI_CUTOFF:
            start, size = self._alloc_mini_chain(data)
        else:
            start, size = self._alloc_chain(data)
        eid = self._add_entry(name, STGTY_STREAM, NOSTREAM, NOSTREAM, NOSTREAM, start, size)
        self._link_under_root(eid)
        return eid

    def _link_under_root(self, eid: int) -> None:
        root = self._entries[0]
        if root["child"] == NOSTREAM:
            root["child"] = eid
        else:
            # simple right-chain of siblings; balance doesn't matter for a BST walk
            cur = root["child"]
            while self._entries[cur]["right"] != NOSTREAM:
                cur = self._entries[cur]["right"]
            self._entries[cur]["right"] = eid

    def entry(self, eid: int) -> dict:
        return self._entries[eid]

    def raw_fat(self) -> list[int]:
        return self._fat

    def set_fat(self, sid: int, value: int) -> None:
        self._fat[sid] = value

    def build(self) -> bytes:
        # Mini stream (root's own regular-FAT-backed content) + Mini FAT table.
        mini_blob = b"".join(self._mini_sectors)
        root_start, root_size = self._alloc_chain(mini_blob)
        self._entries[0]["start"] = root_start
        self._entries[0]["size"] = root_size

        minifat_blob = b"".join(struct.pack("<I", v) for v in self._minifat)
        minifat_start, _ = self._alloc_chain(minifat_blob)

        # Directory sectors (4 entries per 512-byte sector for v3).
        dir_blob = b"".join(self._pack_entry(e) for e in self._entries)
        dir_start, _ = self._alloc_chain(dir_blob)

        # FAT sector(s) themselves need a sector slot too.
        fat_sector_start = len(self._sectors)
        n_fat_sectors = (len(self._fat) + 1 + 127) // 128  # +1 to also cover the FAT sector(s)
        # Reserve space first (values filled below), then serialize.
        fat_sids = list(range(fat_sector_start, fat_sector_start + n_fat_sectors))
        self.fat_sector_ids = fat_sids
        for sid in fat_sids:
            self._sectors.append(b"\x00" * self.SECTOR_SIZE)  # placeholder
            self._fat.append(cfb.FATSECT)

        fat_entries_per_sector = self.SECTOR_SIZE // 4
        total_fat_entries = len(fat_sids) * fat_entries_per_sector
        fat_padded = list(self._fat) + [FREESECT] * (total_fat_entries - len(self._fat))
        for i, sid in enumerate(fat_sids):
            chunk = fat_padded[i * fat_entries_per_sector : (i + 1) * fat_entries_per_sector]
            self._sectors[sid] = struct.pack(f"<{fat_entries_per_sector}I", *chunk)

        header = self._pack_header(
            first_dir_sector=dir_start,
            first_minifat_sector=minifat_start,
            first_difat_sector=ENDOFCHAIN,
            num_fat_sectors=len(fat_sids),
            fat_sector_ids=fat_sids,
        )
        if self.SECTOR_SIZE > len(header):
            # The header occupies one full sector; for v4 (4096-byte
            # sectors) only the first 512 bytes carry fields, the rest is
            # zero-padding, same as a real file.
            header = header + b"\x00" * (self.SECTOR_SIZE - len(header))
        body = b"".join(self._sectors)
        return header + body

    def _pack_entry(self, e: dict) -> bytes:
        name_utf16 = e["name"].encode("utf-16-le")
        name_len = len(name_utf16) + 2  # include null terminator
        name_field = (name_utf16 + b"\x00\x00").ljust(64, b"\x00")
        return struct.pack(
            "<64sHBB3I16sI8s8sIQ",
            name_field,
            name_len,
            e["obj_type"],
            1,  # color flag, unused by the reader
            e["left"],
            e["right"],
            e["child"],
            b"\x00" * 16,
            0,
            b"\x00" * 8,
            b"\x00" * 8,
            e["start"],
            e["size"],
        )

    def _pack_header(self, first_dir_sector, first_minifat_sector, first_difat_sector,
                      num_fat_sectors, fat_sector_ids) -> bytes:
        difat = fat_sector_ids[:109] + [FREESECT] * (109 - min(len(fat_sector_ids), 109))
        hdr = bytearray(512)
        hdr[0:8] = cfb.CFB_SIGNATURE
        struct.pack_into("<H", hdr, 24, 0x003E)  # minor version
        struct.pack_into("<H", hdr, 26, 3)  # major version
        struct.pack_into("<H", hdr, 28, 0xFFFE)  # byte order
        struct.pack_into("<H", hdr, 30, 9)  # sector shift
        struct.pack_into("<H", hdr, 32, 6)  # mini sector shift
        struct.pack_into("<I", hdr, 40, 0)  # num dir sectors (0 for v3)
        struct.pack_into("<I", hdr, 44, num_fat_sectors)
        struct.pack_into("<I", hdr, 48, first_dir_sector)
        struct.pack_into("<I", hdr, 56, self.MINI_CUTOFF)
        struct.pack_into("<I", hdr, 60, first_minifat_sector)
        struct.pack_into("<I", hdr, 64, 1 if first_minifat_sector != ENDOFCHAIN else 0)
        struct.pack_into("<I", hdr, 68, first_difat_sector)
        struct.pack_into("<I", hdr, 72, 0)
        struct.pack_into(f"<{len(difat)}I", hdr, 76, *difat)
        return bytes(hdr)


def minimal_cfb(**streams: bytes) -> bytes:
    b = CfbBuilder()
    for name, data in streams.items():
        b.add_stream(name, data)
    return b.build()


# -------------------------------------------------------------- basic IO


def test_missing_signature_raises_not_ole():
    with pytest.raises(NotOleFileError):
        cfb.CompoundFile(b"not an ole file at all")


def test_empty_input_raises_not_ole():
    with pytest.raises(NotOleFileError):
        cfb.CompoundFile(b"")


def test_short_header_raises_corrupt():
    data = cfb.CFB_SIGNATURE + b"\x00" * 100
    with pytest.raises(CorruptDocumentError):
        cfb.CompoundFile(data)


def test_unsupported_major_version_raises_corrupt():
    hdr = bytearray(512)
    hdr[0:8] = cfb.CFB_SIGNATURE
    struct.pack_into("<H", hdr, 26, 7)  # bogus major version
    with pytest.raises(CorruptDocumentError):
        cfb.CompoundFile(bytes(hdr))


def test_roundtrip_regular_and_mini_streams():
    big = b"X" * 9000  # >= mini cutoff, goes through the regular FAT
    small = b"hello mini stream"  # < mini cutoff, goes through the Mini FAT
    data = minimal_cfb(WordDocument=big, Table=small)
    c = cfb.CompoundFile(data)
    assert set(c.list_streams()) == {"WordDocument", "Table"}
    assert c.open_stream("WordDocument") == big
    assert c.open_stream("Table") == small


def test_case_insensitive_lookup():
    data = minimal_cfb(WordDocument=b"abc")
    c = cfb.CompoundFile(data)
    assert c.exists("worddocument")
    assert c.exists("WORDDOCUMENT")
    assert c.open_stream("wordDOCUMENT") == b"abc"


def test_missing_stream_raises_corrupt_not_keyerror():
    data = minimal_cfb(WordDocument=b"abc")
    c = cfb.CompoundFile(data)
    assert not c.exists("NoSuchStream")
    with pytest.raises(CorruptDocumentError):
        c.open_stream("NoSuchStream")


def test_empty_stream_roundtrips():
    data = minimal_cfb(Empty=b"")
    c = cfb.CompoundFile(data)
    assert c.open_stream("Empty") == b""


# ------------------------------------------------------------ robustness


def test_follow_chain_cycle_detection_terminates():
    """_follow_chain (directory / FAT-sector-list / Mini FAT table) must stop
    on a revisit rather than loop forever."""
    c = cfb.CompoundFile(minimal_cfb(WordDocument=b"x"))
    fat = [1, 2, 0]  # 0 -> 1 -> 2 -> 0 (cycle)
    sids = c._follow_chain(0, fat, check_physical=False)
    assert sids == [0, 1, 2]


def test_follow_sized_chain_allows_revisit_but_stays_bounded():
    """_follow_sized_chain (stream content) is bounded by the declared size's
    sector count, not by cycle detection — see its docstring for why a real
    dev-corpus file relies on this to read a stream with an overlapping
    chain correctly."""
    c = cfb.CompoundFile(minimal_cfb(WordDocument=b"x"))
    fat = [1, 2, 3, 1]  # 0 -> 1 -> 2 -> 3 -> 1 -> 2 -> 3 -> 1 -> ...
    # Revisiting sector 1 is tolerated (unlike _follow_chain)...
    sids = c._follow_sized_chain(0, fat, nb_sectors=5, check_physical=False)
    assert sids == [0, 1, 2, 3, 1]
    # ...but the walk is still capped independently of nb_sectors (here by
    # len(fat) + 1), so a bogus/huge declared size can't run away.
    sids = c._follow_sized_chain(0, fat, nb_sectors=1_000_000, check_physical=False)
    assert len(sids) == len(fat) + 1


def test_directory_sibling_cycle_does_not_hang():
    data = minimal_cfb(A=b"1", B=b"2")
    c = cfb.CompoundFile(data)
    # Force a cycle in the sibling tree: make B's right pointer point back to A.
    entries = c._entries
    a_id = next(i for i, e in enumerate(entries) if e.name == "A")
    b_id = next(i for i, e in enumerate(entries) if e.name == "B")
    entries[b_id] = entries[b_id].__class__(
        entries[b_id].name, entries[b_id].obj_type, a_id, a_id,
        entries[b_id].child_id, entries[b_id].start_sector, entries[b_id].size,
    )
    out = c._siblings(entries[0].child_id)
    assert isinstance(out, list)  # terminates instead of hanging


def test_out_of_range_start_sector_returns_empty_not_crash():
    data = bytearray(minimal_cfb(WordDocument=b"Z" * 20))
    c = cfb.CompoundFile(bytes(data))
    entries = c._entries
    idx = next(i for i, e in enumerate(entries) if e.name == "WordDocument")
    bad = entries[idx]
    entries[idx] = bad.__class__(bad.name, bad.obj_type, bad.left_id, bad.right_id,
                                  bad.child_id, 999_999, bad.size)
    assert c.open_stream("WordDocument") == b""


def test_truncated_final_sector_is_tolerated():
    data = minimal_cfb(WordDocument=b"hello world, this is more than one mini sector of data!!")
    # Chop the file short mid-way through the last physical sector, like a
    # file that got cut off mid-write; must not raise or hang.
    truncated = data[: len(data) - 7]
    c = cfb.CompoundFile(truncated)
    assert "WordDocument" in c.list_streams()
    content = c.open_stream("WordDocument")
    assert isinstance(content, bytes)


def test_name_length_zero_does_not_crash():
    b = CfbBuilder()
    b.add_stream("WordDocument", b"abc")
    data = bytearray(b.build())
    c = cfb.CompoundFile(bytes(data))
    # Directly exercise the parser on a hand-built 128-byte entry with
    # nameLen=0 and a non-trivial raw name field, as seen in fuzzed files.
    raw = bytearray(128)
    raw[0:10] = b"\x01Ole\x00\x00\x00\x00\x00\x00"
    struct.pack_into("<H", raw, 64, 0)
    raw[66] = STGTY_STREAM
    entry = c._parse_dir_entry(bytes(raw), 0)
    assert isinstance(entry.name, str)  # decodes without raising


def test_major_version_4_sector_size():
    b = CfbBuilder()
    b.SECTOR_SIZE = 4096
    data = bytearray(b.build())
    struct.pack_into("<H", data, 26, 4)
    struct.pack_into("<H", data, 30, 12)
    c = cfb.CompoundFile(bytes(data))
    assert c._sector_size == 4096


def test_difat_chain_is_followed():
    """A file needing more than 109 FAT sectors lists the rest through a
    chained DIFAT sector ([MS-CFB] 2.5) instead of the header's 109 direct
    slots. Force exactly that shape with 2 FAT sectors, move the 2nd out of
    the header into a hand-built DIFAT sector, and check that the stream
    whose data only the 2nd FAT sector describes still reads back correctly
    — i.e. the DIFAT chain, not just the header's direct 109 entries, was
    actually followed.
    """
    b = CfbBuilder()
    big = b"R" * (127 * CfbBuilder.SECTOR_SIZE)  # enough to need 2 FAT sectors
    b.add_stream("WordDocument", big)
    built = bytearray(b.build())
    assert len(b.fat_sector_ids) == 2, b.fat_sector_ids
    _, second_fat_sid = b.fat_sector_ids

    sector_size = CfbBuilder.SECTOR_SIZE
    entries_per_sector = sector_size // 4

    # Remove the 2nd FAT sector id from the header's direct 109-slot array.
    struct.pack_into("<I", built, 76 + 4 * 1, FREESECT)

    # Hand-build one DIFAT sector: its first slot lists the 2nd FAT sector,
    # the rest are FREESECT padding, and the last slot is the "next DIFAT
    # sector" pointer (ENDOFCHAIN, since one DIFAT sector is all we need).
    difat_sector_sid = (len(built) - CfbBuilder.SECTOR_SIZE) // sector_size
    difat_values = [second_fat_sid] + [FREESECT] * (entries_per_sector - 2) + [ENDOFCHAIN]
    built += struct.pack(f"<{entries_per_sector}I", *difat_values)

    struct.pack_into("<I", built, 68, difat_sector_sid)  # first DIFAT sector
    struct.pack_into("<I", built, 72, 1)  # number of DIFAT sectors

    c = cfb.CompoundFile(bytes(built))
    assert c.open_stream("WordDocument") == big


def test_large_file_opens_quickly():
    import time

    big = b"Q" * (2 * 1024 * 1024)
    data = minimal_cfb(WordDocument=big, Table=b"small")
    t0 = time.monotonic()
    c = cfb.CompoundFile(data)
    assert c.open_stream("WordDocument") == big
    assert time.monotonic() - t0 < 2.0


# -------------------------------------------------------- olefile cross-check


def _dev_corpus_manifest() -> dict:
    if DATA_DIR is None:
        return {}
    manifest_path = DATA_DIR / "corpus" / "manifest.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {k: v for k, v in manifest.items() if v.get("split") == "dev"}


_DEV_MANIFEST = _dev_corpus_manifest()
_DEV_IDS = list(_DEV_MANIFEST)


@pytest.mark.skipif(not _DEV_IDS, reason="dev corpus not available")
@pytest.mark.parametrize("doc_id", _DEV_IDS)
def test_matches_olefile_on_dev_corpus(doc_id):
    path = DATA_DIR / "corpus" / "dev" / f"{doc_id}.doc"
    if not path.exists():
        pytest.skip("file not present")
    data = path.read_bytes()
    if not olefile.isOleFile(data=data):
        pytest.skip("not an OLE file")

    try:
        mine = cfb.CompoundFile(data)
    except CorruptDocumentError:
        # msdoc is intentionally more tolerant than olefile (see module
        # docstring); a file msdoc still manages to open is fine even if
        # olefile can't, but if msdoc *also* refuses, there's nothing to
        # compare.
        pytest.skip("msdoc also rejected this file")

    if _DEV_MANIFEST.get(doc_id, {}).get("kind") == "fail":
        # These are deliberately-fuzzed files where even LibreOffice itself
        # is expected to fail (golden status "lo_failed"); msdoc's job here
        # is only to not crash/hang while reading whatever it can, not to
        # byte-match olefile's own best-effort recovery from equally
        # corrupt header fields (e.g. a bogus Mini FAT sector size that
        # olefile trusts and msdoc deliberately does not — see
        # CompoundFile's MINI_SECTOR_SHIFT comment).
        for name in mine.list_streams():
            mine.open_stream(name)
        return

    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception:
        # olefile is stricter than LibreOffice on some fuzzed/corrupt dev
        # files (see docs/lo_disagreements.md candidates); nothing to
        # compare against when it refuses to open at all.
        pytest.skip("olefile rejected this file")

    try:
        my_streams = {s.lower() for s in mine.list_streams()}
        ole_streams = {("/".join(p)).lower() for p in ole.listdir(streams=True, storages=False)}
        assert my_streams == ole_streams

        for name in ("WordDocument", "0Table", "1Table", "Data"):
            has_mine = mine.exists(name)
            assert has_mine == (name.lower() in ole_streams)
            if has_mine:
                assert mine.open_stream(name) == ole.openstream(name).read()
    finally:
        ole.close()
