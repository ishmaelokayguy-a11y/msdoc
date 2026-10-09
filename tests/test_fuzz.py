# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Deterministic mutation fuzzing: extract() returns a Document or raises MsDocError.

Streams are mutated in memory (open_stream is patched) so that edits reach
the FIB, Clx/PLCFs (table stream), FKPs (WordDocument), the Data stream and
OfficeArt records (table stream / WordDocument / Data) without rebuilding a
CFB container. Set MSDOC_FUZZ_ITERATIONS for a longer run.
"""
import json
import os
import random
import time

import pytest

import common
import msdoc
from msdoc import cfb
from msdoc.errors import MsDocError

ITER = int(os.environ.get("MSDOC_FUZZ_ITERATIONS", "6"))
NFILES = 20
TIME_LIMIT = 5.0


def _normal_files():
    data = common.data_dir()
    manifest = json.loads((data / "corpus" / "manifest.json").read_text())
    out = []
    for fid, m in manifest.items():
        if m.get("kind") != "normal" or m.get("split") != "dev":
            continue
        p = data / "corpus" / "dev" / f"{fid}.doc"
        if p.exists():
            out.append((p.stat().st_size, fid, p))
    out.sort()
    return out


_ALL = _normal_files()


def _has_data(path):
    try:
        return cfb.CompoundFile(path.read_bytes()).exists("Data")
    except MsDocError:
        return False


# Smallest files overall, smallest with a Data stream (pictures, so Data /
# OfficeArt mutations have something to hit) and some mid-sized files that
# carry real structure (many pieces, FKPs, tables).
_picked = _ALL[:6]
_picked += [e for e in _ALL[6:] if _has_data(e[2])][:7]
_mid = len(_ALL) * 3 // 4
_picked += [e for e in _ALL[_mid:_mid + 20] if e not in _picked][:7]
FILES = [(fid, p) for _, fid, p in _picked]
pytestmark = pytest.mark.skipif(not FILES, reason="no dev corpus")


def _streams(raw):
    cf = cfb.CompoundFile(raw)
    return {n: cf.open_stream(n) for n in ("WordDocument", "0Table", "1Table", "Data") if cf.exists(n)}


def _mutate(rng, streams, mode):
    s = {k: bytearray(v) for k, v in streams.items()}
    tables = [k for k in s if k.endswith("Table")]
    w = s["WordDocument"]
    if mode == "truncate":
        k = rng.choice(list(s))
        del s[k][rng.randrange(len(s[k]) + 1):]
    elif mode == "fib":
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(min(len(w), 0x2A0))
            w[i] ^= 1 << rng.randrange(8)
    elif mode == "fib_ff" and len(w) > 4:
        i = rng.randrange(0, min(len(w), 0x2A0) - 3)
        w[i:i + 4] = b"\xff\xff\xff\xff"
    elif mode == "table" and tables:
        t = s[rng.choice(tables)]
        for _ in range(rng.randint(1, 16)):
            if t:
                t[rng.randrange(len(t))] ^= 1 << rng.randrange(8)
    elif mode == "fkp":
        for _ in range(rng.randint(1, 16)):
            if w:
                w[rng.randrange(len(w))] = rng.randrange(256)
    elif mode == "data" and "Data" in s:
        d = s["Data"]
        for _ in range(rng.randint(1, 16)):
            if d:
                d[rng.randrange(len(d))] = rng.randrange(256)
    elif mode == "officeart":
        # Smash length/instance bytes of OfficeArt-looking record headers
        # (0xF000-0xF1FF record types), then add a few random flips.
        for k in s:
            b = s[k]
            hits = [i for i in range(0, max(0, len(b) - 8))
                    if 0xF0 <= b[i + 3] <= 0xF1 and b[i + 1] in (0x00, 0x08, 0x0F)]
            for i in rng.sample(hits, min(len(hits), 4)):
                b[i + 4 + rng.randrange(4)] = rng.choice((0x00, 0xFF, 0x7F))
            for _ in range(4):
                if b:
                    b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
    elif w:
        w[rng.randrange(len(w))] ^= 0xFF
    return {k: bytes(v) for k, v in s.items()}


MODES = ["truncate", "fib", "fib_ff", "table", "fkp", "data", "officeart"]


@pytest.mark.parametrize("fid,path", FILES, ids=[f for f, _ in FILES])
def test_mutations(fid, path, monkeypatch):
    raw = path.read_bytes()
    streams = _streams(raw)
    rng = random.Random(f"msdoc-fuzz-{fid}")
    for it in range(ITER * len(MODES)):
        mode = MODES[it % len(MODES)]
        mutated = _mutate(rng, streams, mode)

        def open_stream(self, name, _m=mutated):
            if name in _m:
                return _m[name]
            raise MsDocError(f"stream not found: {name}")

        monkeypatch.setattr(cfb.CompoundFile, "open_stream", open_stream)
        monkeypatch.setattr(cfb.CompoundFile, "exists", lambda self, n, _m=mutated: n in _m)
        t0 = time.monotonic()
        try:
            assert msdoc.extract(raw) is not None
        except MsDocError:
            pass
        dt = time.monotonic() - t0
        assert dt < TIME_LIMIT, f"{fid} iter {it} ({mode}) took {dt:.1f}s"


def test_raw_file_truncation_and_flips():
    _, path = FILES[0]
    raw = path.read_bytes()
    rng = random.Random("raw")
    for _ in range(ITER * 4):
        b = bytearray(raw[:rng.randrange(len(raw) + 1)])
        for _ in range(rng.randint(0, 20)):
            if b:
                b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
        t0 = time.monotonic()
        try:
            msdoc.extract(bytes(b))
        except MsDocError:
            pass
        assert time.monotonic() - t0 < TIME_LIMIT


@pytest.mark.parametrize("size,fid,path", _ALL[-5:], ids=[f for _, f, _ in _ALL[-5:]])
def test_largest_files_fast(size, fid, path):
    t0 = time.monotonic()
    msdoc.extract(path)
    assert time.monotonic() - t0 < 2.0, f"{fid} ({size} bytes) too slow"
