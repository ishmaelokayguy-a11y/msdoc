# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Compound File Binary (OLE2) reader.

Spec: [MS-CFB] https://learn.microsoft.com/openspecs/windows_protocols/ms-cfb/
  2.2 Compound File Header, 2.3 Compound File FAT Sectors, 2.4 Compound File
  Mini FAT Sectors, 2.5 Compound File DIFAT Sectors, 2.6 Compound File
  Directory Sectors / Directory Entries, 2.6.3 Red-Black Tree.
LibreOffice reference: sot/source/sdstor/ (stg.cxx, stgio.cxx, stgstrms.cxx,
stgdir.cxx, stgcache.cxx) — consulted for edge-case behaviour (truncated
streams, bad directory entries); this implementation follows [MS-CFB]
directly rather than porting LibreOffice's StgCache/StgDirEntry classes,
which are structured very differently (object cache + AVL tree) from what
is needed here.

Every chain walk (FAT, Mini FAT, DIFAT, directory siblings) is bounded by
the number of elements in the structure it walks and guards against
revisiting a sector/entry id, so a cyclic or adversarial file can only
produce a truncated/partial result, never a hang. Nothing but
CorruptDocumentError/NotOleFileError escapes this module.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import CorruptDocumentError, NotOleFileError

CFB_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")

HEADER_SIZE = 512
DIR_ENTRY_SIZE = 128
DIFAT_HEADER_ENTRIES = 109

# [MS-CFB] 2.1 Compound File Sector Numbers and Types — reserved sector/
# chain-terminator values.
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
_TERMINAL = (FREESECT, ENDOFCHAIN, FATSECT, DIFSECT)

# [MS-CFB] 2.6.1 Compound File Directory Entry — Object Type values.
STGTY_INVALID = 0
STGTY_STORAGE = 1
STGTY_STREAM = 2
STGTY_ROOT = 5

NOSTREAM = 0xFFFFFFFF

MINI_SECTOR_SHIFT = 6  # [MS-CFB] 2.2 header: "MUST be set to 0x0006".
MINI_SECTOR_SIZE = 1 << MINI_SECTOR_SHIFT
DEFAULT_MINI_STREAM_CUTOFF = 0x1000


@dataclass
class _DirEntry:
    name: str
    obj_type: int
    left_id: int
    right_id: int
    child_id: int
    start_sector: int
    size: int


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _nb_sectors(size: int, sector_size: int) -> int:
    return (size + sector_size - 1) // sector_size if size > 0 else 0


class CompoundFile:
    """Read-only view of an OLE2/CFB container.

    Raises NotOleFileError if ``data`` is not a CFB container at all, and
    CorruptDocumentError if the signature matches but the structure cannot
    be parsed far enough to locate a root directory. Beyond that, malformed
    structures (broken chains, out-of-range sector ids, oversized declared
    stream lengths, directory cycles) are tolerated: the affected chain is
    simply truncated/stopped, matching LibreOffice's tolerant reader rather
    than failing the whole document.
    """

    def __init__(self, data: bytes) -> None:
        if data[:8] != CFB_SIGNATURE:
            raise NotOleFileError("missing CFB signature")
        if len(data) < HEADER_SIZE:
            raise CorruptDocumentError("file shorter than CFB header")
        self._data = data
        self._parse_header()
        self._build_fat()
        self._build_directory()
        self._build_minifat()
        self._build_ministream()
        self._build_paths()

    # ------------------------------------------------------------ header

    def _parse_header(self) -> None:
        hdr = self._data
        major_version = _u16(hdr, 26)
        if major_version == 3:
            sector_shift = 9
        elif major_version == 4:
            sector_shift = 12
        else:
            raise CorruptDocumentError(f"unsupported CFB major version {major_version}")
        self._major_version = major_version
        self._sector_size = 1 << sector_shift

        if len(self._data) < self._sector_size:
            raise CorruptDocumentError("file shorter than its own header sector")

        first_dir_sector = _u32(hdr, 48)
        mini_cutoff = _u32(hdr, 56)
        # A few real-world files carry garbage here; fall back to the spec
        # default rather than letting a bogus cutoff push every stream to
        # the wrong storage.
        self._mini_cutoff = mini_cutoff if 0 < mini_cutoff <= 0x7FFFFFFF else DEFAULT_MINI_STREAM_CUTOFF
        first_minifat_sector = _u32(hdr, 60)
        first_difat_sector = _u32(hdr, 68)

        self._first_dir_sector = first_dir_sector
        self._first_minifat_sector = first_minifat_sector

        difat_header = list(struct.unpack_from(f"<{DIFAT_HEADER_ENTRIES}I", hdr, 76))
        self._fat_sector_ids = [sid for sid in difat_header if sid not in _TERMINAL]

        total_sectors_guess = max(1, (len(self._data) - self._sector_size) // self._sector_size)
        # DIFAT sectors are chained via the last 4 bytes of each DIFAT
        # sector itself (not through the FAT, which doesn't exist yet).
        limit = total_sectors_guess + 2
        visited: set[int] = set()
        sid = first_difat_sector
        count = 0
        entries_per_sector = self._sector_size // 4
        while sid not in _TERMINAL and sid not in visited and count < limit:
            sec = self._read_sector_raw(sid)
            if sec is None:
                break
            visited.add(sid)
            count += 1
            entries = struct.unpack_from(f"<{entries_per_sector}I", sec, 0)
            self._fat_sector_ids.extend(e for e in entries[:-1] if e not in _TERMINAL)
            sid = entries[-1]

    def _read_sector_raw(self, sid: int) -> bytes | None:
        """Full sector_size bytes for sid, or None if it doesn't start in the file.

        A file's last sector routinely runs short of a full sector_size on
        disk (real-world truncation, or simply an odd file size); such a
        sector is still valid and is zero-padded to length, matching
        LibreOffice's StgPage (stgcache.cxx, zero-fills before reading).
        Only a sector that doesn't even *start* inside the file is missing.
        """
        if sid < 0:
            return None
        off = self._sector_size + sid * self._sector_size
        if off < 0 or off >= len(self._data):
            return None
        chunk = self._data[off : off + self._sector_size]
        if len(chunk) < self._sector_size:
            chunk = chunk + b"\x00" * (self._sector_size - len(chunk))
        return chunk

    # --------------------------------------------------------------- FAT

    def _build_fat(self) -> None:
        entries_per_sector = self._sector_size // 4
        fat: list[int] = []
        for sid in self._fat_sector_ids:
            sec = self._read_sector_raw(sid)
            if sec is None:
                continue
            fat.extend(struct.unpack_from(f"<{entries_per_sector}I", sec, 0))
        self._fat = fat

    def _follow_chain(self, start: int, fat: list[int], check_physical: bool = True) -> list[int]:
        """Follow a FAT-style chain, bounded and cycle-safe.

        Any break in the chain (out-of-range id, revisit, sector beyond the
        physical file) simply ends the chain early rather than raising —
        callers treat the result as "however much data is actually there".

        ``check_physical`` validates the id against the file's regular
        sectors and must be disabled for Mini FAT chains, whose ids index
        into the mini stream (a logical array of 64-byte sectors), not the
        file's own sectors — a valid mini sector id is routinely larger
        than the file's regular sector count.
        """
        sids: list[int] = []
        visited: set[int] = set()
        sid = start
        limit = len(fat) + 1
        while sid not in _TERMINAL:
            if sid < 0 or sid >= len(fat) or sid in visited:
                break
            if check_physical and self._read_sector_raw(sid) is None:
                break
            visited.add(sid)
            sids.append(sid)
            if len(sids) > limit:
                break
            sid = fat[sid]
        return sids

    def _follow_sized_chain(
        self, start: int, fat: list[int], nb_sectors: int, check_physical: bool
    ) -> list[int]:
        """Follow a chain for exactly as many steps as a declared byte size
        implies, the way LibreOffice/olefile read stream content: a fixed
        iteration count bounds the walk instead of cycle detection.

        This matters because a stream's chain is allowed to revisit a
        sector — seen in dev-corpus regression files with deliberately
        overlapping Mini FAT chains — as long as the sectors visited add up
        to the declared size; a visited-set cycle guard like
        ``_follow_chain`` uses would cut such a chain short and silently
        drop real data. ``nb_sectors`` is still capped by ``len(fat)`` so a
        corrupt/huge declared size can't turn this into a long-running loop.
        """
        sids: list[int] = []
        sid = start
        for _ in range(min(nb_sectors, len(fat) + 1)):
            if sid in _TERMINAL or sid < 0 or sid >= len(fat):
                break
            if check_physical and self._read_sector_raw(sid) is None:
                break
            sids.append(sid)
            sid = fat[sid]
        return sids

    def _gather(self, sids: list[int], size: int) -> bytes:
        if size < 0:
            size = 0
        cap = len(sids) * self._sector_size
        remaining = min(size, cap)
        parts = []
        for sid in sids:
            if remaining <= 0:
                break
            off = self._sector_size + sid * self._sector_size
            take = min(self._sector_size, remaining)
            parts.append(self._data[off : off + take])
            remaining -= take
        return b"".join(parts)

    # --------------------------------------------------------- directory

    def _build_directory(self) -> None:
        dir_sids = self._follow_chain(self._first_dir_sector, self._fat)
        # Directory size isn't bounded by a declared length (unlike regular
        # streams) — it's simply "all sectors in the chain". Unlike
        # _read_sector_raw, do NOT zero-pad a short final sector: a
        # genuinely truncated file can leave a trailing directory entry
        # with only part of its 128 bytes physically present, and that
        # entry must be dropped rather than completed with zeroes (padding
        # it would fabricate — not recover — the rest of the entry, e.g. a
        # plausible-looking but invented stream). LibreOffice/olefile treat
        # the directory stream the same way: its usable size is whatever
        # byte count the chain actually yields, floor-divided by 128.
        parts = []
        for sid in dir_sids:
            off = self._sector_size + sid * self._sector_size
            parts.append(self._data[off : off + self._sector_size])
        raw = b"".join(parts)
        entries: list[_DirEntry] = []
        for off in range(0, len(raw) - DIR_ENTRY_SIZE + 1, DIR_ENTRY_SIZE):
            entries.append(self._parse_dir_entry(raw, off))
        self._entries = entries

        root_id = next((i for i, e in enumerate(entries) if e.obj_type == STGTY_ROOT), None)
        if root_id is None:
            raise CorruptDocumentError("no root directory entry")
        self._root_id = root_id

    def _parse_dir_entry(self, raw: bytes, off: int) -> _DirEntry:
        name_raw = raw[off : off + 64]
        name_len = _u16(raw, off + 64)
        if name_len > 64:
            name_len = 64
        # name_len is the byte count including the trailing null character,
        # so the name itself is name_raw[: name_len - 2]. A real file is
        # expected to keep this >= 2 for any used entry, but LibreOffice
        # and olefile both decode whatever this slice comes out to even
        # when it's not (name_len 0 or 1 makes this a negative Python slice
        # index, e.g. -2, which keeps "all but the last two bytes" of the
        # 64-byte field rather than an empty name) — matched here so an
        # unallocated/corrupt slot round-trips identically to the oracle.
        name = name_raw[: name_len - 2].decode("utf-16-le", errors="replace")
        obj_type = raw[off + 66]
        left_id, right_id, child_id = struct.unpack_from("<3I", raw, off + 68)
        start_sector = _u32(raw, off + 116)
        size_lo, size_hi = struct.unpack_from("<II", raw, off + 120)
        if self._major_version == 3:
            # [MS-CFB] 2.6.1: for v3 files the high 32 bits "MUST be zero";
            # some real files leave garbage there instead, which would
            # otherwise make every stream look multi-gigabyte.
            size = size_lo
        else:
            size = size_lo | (size_hi << 32)
        return _DirEntry(name, obj_type, left_id, right_id, child_id, start_sector, size)

    def _siblings(self, child_id: int) -> list[int]:
        """All directory entry ids reachable from a storage's child pointer.

        This is the sibling red-black tree [MS-CFB] 2.6.3 flattened to a
        plain set of nodes — order doesn't matter for path-building, only
        reachability, so left/right are walked identically.
        """
        out: list[int] = []
        seen: set[int] = set()
        stack = [child_id]
        limit = len(self._entries) + 2
        while stack:
            eid = stack.pop()
            if eid == NOSTREAM or eid < 0 or eid >= len(self._entries):
                continue
            if eid in seen:
                continue
            seen.add(eid)
            if len(seen) > limit:
                break
            entry = self._entries[eid]
            out.append(eid)
            if entry.obj_type == STGTY_INVALID:
                # An unallocated directory slot. Real red-black tree edges
                # never route through one of these; a fuzzed file can still
                # leave garbage in its sibling pointers, so they must not be
                # trusted/followed (seen in dev corpus: a garbage "right"
                # pointer on a free slot resurrected an unrelated stream).
                continue
            stack.append(entry.left_id)
            stack.append(entry.right_id)
        return out

    def _build_paths(self) -> None:
        self._streams: dict[str, int] = {}
        self._stream_names: list[str] = []
        visited_storages: set[int] = set()
        stack = [(self._root_id, "")]
        limit = len(self._entries) + 2
        while stack:
            sid, prefix = stack.pop()
            if sid in visited_storages:
                continue
            visited_storages.add(sid)
            if len(visited_storages) > limit:
                break
            entry = self._entries[sid]
            for cid in self._siblings(entry.child_id):
                child = self._entries[cid]
                path = f"{prefix}/{child.name}" if prefix else child.name
                if child.obj_type == STGTY_STORAGE:
                    stack.append((cid, path))
                elif child.obj_type == STGTY_STREAM:
                    self._streams[self._normalize(path)] = cid
                    self._stream_names.append(path)
                # STGTY_INVALID (unallocated/corrupt) entries are skipped.

    # ------------------------------------------------------------- mini

    def _build_minifat(self) -> None:
        minifat_sids = self._follow_chain(self._first_minifat_sector, self._fat)
        entries_per_sector = self._sector_size // 4
        minifat: list[int] = []
        for sid in minifat_sids:
            sec = self._read_sector_raw(sid)
            if sec is None:
                continue
            minifat.extend(struct.unpack_from(f"<{entries_per_sector}I", sec, 0))
        self._minifat = minifat

    def _build_ministream(self) -> None:
        root = self._entries[self._root_id]
        nb = _nb_sectors(root.size, self._sector_size)
        sids = self._follow_sized_chain(root.start_sector, self._fat, nb, check_physical=True)
        self._ministream = self._gather(sids, root.size)

    def _gather_mini(self, msids: list[int], size: int) -> bytes:
        if size < 0:
            size = 0
        ms = self._ministream
        remaining = min(size, len(msids) * MINI_SECTOR_SIZE)
        parts = []
        for sid in msids:
            if remaining <= 0:
                break
            off = sid * MINI_SECTOR_SIZE
            if off >= len(ms):
                break
            take = min(MINI_SECTOR_SIZE, remaining, len(ms) - off)
            parts.append(ms[off : off + take])
            remaining -= take
        return b"".join(parts)

    # -------------------------------------------------------------- read

    def _read_entry_data(self, entry: _DirEntry) -> bytes:
        if entry.obj_type == STGTY_ROOT:
            return self._ministream[: entry.size] if entry.size >= 0 else b""
        if entry.size < self._mini_cutoff:
            nb = _nb_sectors(entry.size, MINI_SECTOR_SIZE)
            msids = self._follow_sized_chain(entry.start_sector, self._minifat, nb, check_physical=False)
            return self._gather_mini(msids, entry.size)
        nb = _nb_sectors(entry.size, self._sector_size)
        sids = self._follow_sized_chain(entry.start_sector, self._fat, nb, check_physical=True)
        return self._gather(sids, entry.size)

    @staticmethod
    def _normalize(path: str) -> str:
        return "/".join(part.lower() for part in path.split("/"))

    # ------------------------------------------------------------- API

    def list_streams(self) -> list[str]:
        """All stream paths, '/'-separated, storage names included."""
        return list(self._stream_names)

    def exists(self, path: str) -> bool:
        return self._normalize(path) in self._streams

    def open_stream(self, path: str) -> bytes:
        """Full stream content. Raises KeyError-free CorruptDocumentError if missing."""
        eid = self._streams.get(self._normalize(path))
        if eid is None:
            raise CorruptDocumentError(f"stream not found: {path}")
        return self._read_entry_data(self._entries[eid])
