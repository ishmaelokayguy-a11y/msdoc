#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Build the .xls / .ppt test corpus (LibreOffice qa files + Apache POI test-data).

    python3 oracle/office_fetch.py                       # clone sources, build corpus
    python3 oracle/office_fetch.py --lo DIR --poi DIR    # use existing sparse clones
    python3 oracle/office_fetch.py --extra ~/files       # + your own .xls/.ppt

Layout under data_dir()/office:
    corpus/{xls,ppt}/{dev,holdout}/<id>.<ext>
    manifest.json   id -> {ext, source, path, kind, split}
kind: normal | encrypted | legacy (BIFF2-5, PowerPoint 95) | notole | fail
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from common import data_dir, load_json, save_json  # noqa: E402
from msdoc import cfb  # noqa: E402

LO_SPARSE = [
    "/sc/qa/**/*.xls", "/sc/qa/**/*.XLS", "/sd/qa/**/*.ppt", "/sd/qa/**/*.PPT",
    # reference sources (read / port under MPL-2.0)
    "/sc/source/filter/excel/", "/sd/source/filter/ppt/",
    "/filter/source/msfilter/", "/include/filter/msfilter/", "/COPYING.MPL",
]
POI_SPARSE = ["/test-data/spreadsheet/*.xls", "/test-data/slideshow/*.ppt", "/LICENSE", "/NOTICE"]
HOLDOUT_PERCENT = 15
MAX_BYTES = 8 * 1024 * 1024


def sparse_clone(url: str, dest: Path, patterns: list[str]) -> None:
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", *patterns], check=True)


def classify(data: bytes, path: str, ext: str) -> str:
    p = path.replace("\\", "/").lower()
    if "/fail/" in p:
        return "fail"
    if data[:8] != cfb.CFB_SIGNATURE:
        # BIFF2-4 flat worksheets / PowerPoint 4 are not OLE
        if ext == "xls" and data[:2] in (b"\x09\x00", b"\x09\x02", b"\x09\x04"):
            return "legacy"
        return "notole"
    try:
        c = cfb.CompoundFile(data)
    except Exception:
        return "fail"
    try:
        if ext == "xls":
            if c.exists("Workbook"):
                s = c.open_stream("Workbook")
                return "encrypted" if _biff_has_filepass(bytes(s)) else "normal"
            if c.exists("Book"):
                return "legacy"
            return "legacy"
        pre = "PP97_DUALSTORAGE/" if c.exists("PP97_DUALSTORAGE/PowerPoint Document") else ""
        if c.exists(pre + "PowerPoint Document"):
            cu = c.open_stream(pre + "Current User") if c.exists(pre + "Current User") else b""
            if cu[12:16] == b"\xdf\xc4\xd1\xf3":  # headerToken 0xF3D1C4DF of an encrypted file
                return "encrypted"
            if cu and cu[12:16] != b"\x5f\xc0\x91\xe3":  # PowerPoint 95: no 97 headerToken
                return "legacy"
            return "normal"
        return "legacy"
    except Exception:
        return "fail"


def _biff_has_filepass(wb: bytes) -> bool:
    pos = 0
    for _ in range(200):
        if pos + 4 > len(wb):
            return False
        rid, ln = struct.unpack_from("<HH", wb, pos)
        if rid == 0x002F:
            return True
        if rid == 0x000A:  # EOF of globals
            return False
        pos += 4 + ln
    return False


def collect(roots: list[tuple[str, Path]]):
    for source, root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or ".git" in p.parts:
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext in ("xls", "ppt"):
                yield source, root, p, ext


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=Path)
    ap.add_argument("--poi", type=Path)
    ap.add_argument("--extra", type=Path)
    a = ap.parse_args()
    d = data_dir()
    lo = a.lo or d / "sources" / "libreoffice-office"
    poi = a.poi or d / "sources" / "poi-office"
    if not a.lo:
        sparse_clone("https://github.com/LibreOffice/core", lo, LO_SPARSE)
    if not a.poi:
        sparse_clone("https://github.com/apache/poi", poi, POI_SPARSE)
    roots = [("libreoffice", lo), ("poi", poi)]
    if a.extra:
        roots.append(("extra", a.extra))
    out = d / "office"
    man_path = out / "manifest.json"
    manifest = load_json(man_path, {})
    seen = {v["sha1"] for v in manifest.values()}
    added = 0
    for source, root, p, ext in collect(roots):
        data = p.read_bytes()
        if not data or len(data) > MAX_BYTES:
            continue
        sha = hashlib.sha1(data).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)
        rel = p.relative_to(root).as_posix()
        kind = classify(data, rel, ext)
        split = "holdout" if int(sha[:6], 16) % 100 < HOLDOUT_PERCENT else "dev"
        fid = sha[:10]
        dest = out / "corpus" / ext / split / f"{fid}.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest[fid] = {"ext": ext, "source": source, "path": rel, "kind": kind, "split": split, "sha1": sha}
        added += 1
    save_json(man_path, manifest)
    from collections import Counter
    c = Counter((v["ext"], v["split"], v["kind"]) for v in manifest.values())
    print(f"added {added}; total {len(manifest)}")
    for k, n in sorted(c.items()):
        print(" ", k, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
