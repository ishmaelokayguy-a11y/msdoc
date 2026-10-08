#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Build the test corpus and fetch LibreOffice reference sources.

    python3 oracle/fetch_corpus.py                 # LibreOffice + Apache POI test files
    python3 oracle/fetch_corpus.py --govdocs 3     # + .doc from Govdocs1 zips 000..002
    python3 oracle/fetch_corpus.py --extra ~/docs  # + your own .doc files

Layout (under data_dir(), shared by all worktrees):
    sources/libreoffice-core/   sparse checkout: test docs + ww8/msfilter/sot sources
    sources/poi/                sparse checkout: Apache POI test-data/document
    corpus/dev/<id>.doc         visible to agents
    corpus/holdout/<id>.doc     NOT visible to agents (final evaluation only)
    corpus/manifest.json        id -> {source, path, kind, split, nfib}

kind: normal | encrypted | legacy (Word 6/95/2, non-Word OLE) | fail (fuzzer
crashers LibreOffice rejects) | notole
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import data_dir, load_json, save_json  # noqa: E402

LO_SPARSE = [
    "/sw/qa/**/*.doc",
    "/sw/qa/**/*.DOC",
    # reference sources for agents (read / port under MPL-2.0)
    "/sw/source/filter/ww8/",
    "/filter/source/msfilter/",
    "/include/filter/msfilter/",
    "/sot/source/sdstor/",
    "/sw/qa/extras/ww8import/",
    "/COPYING.MPL",
]
POI_SPARSE = ["/test-data/document/*.doc", "/test-data/document/*.DOC", "/LICENSE", "/NOTICE"]
HOLDOUT_PERCENT = 15
GOVDOCS_URL = "https://digitalcorpora.s3.amazonaws.com/corpora/files/govdocs1/zipfiles/{:03d}.zip"


def sparse_clone(url: str, dest: Path, patterns: list[str]) -> None:
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, str(dest)], check=True)
    else:
        subprocess.run(["git", "-C", str(dest), "pull", "-q", "--depth", "1"], check=False)
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", *patterns], check=True)


def classify(data: bytes, path: str) -> tuple[str, int | None]:
    p = path.replace("\\", "/").lower()
    if "/fail/" in p:
        return "fail", None
    if data[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
        return ("legacy" if data[:2] in (b"\xdb\xa5", b"\x9b\xa5") else "notole"), None
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        if not ole.exists("WordDocument"):
            return "legacy", None
        wd = ole.openstream("WordDocument").read(0x0C)
        wident, nfib = struct.unpack_from("<HH", wd, 0)
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if wident != 0xA5EC or nfib < 0xC1:
            return "legacy", nfib
        if flags & 0x0100:  # fEncrypted
            return "encrypted", nfib
        return "normal", nfib
    except Exception:
        return "fail", None


def add_file(manifest: dict, corpus: Path, data: bytes, source: str, path: str) -> bool:
    h = hashlib.sha256(data).hexdigest()
    fid = h[:16]
    if fid in manifest:
        return False
    kind, nfib = classify(data, path)
    split = "holdout" if kind == "normal" and int(h[:8], 16) % 100 < HOLDOUT_PERCENT else "dev"
    out = corpus / split / f"{fid}.doc"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    manifest[fid] = {"source": source, "path": path, "kind": kind, "split": split, "nfib": nfib}
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-lo", action="store_true")
    ap.add_argument("--no-poi", action="store_true")
    ap.add_argument("--govdocs", type=int, default=0, metavar="N", help="download Govdocs1 zips 000..N-1 (~300MB each)")
    ap.add_argument("--extra", type=Path, action="append", default=[], help="directory with your own .doc files")
    args = ap.parse_args()

    d = data_dir()
    src, corpus = d / "sources", d / "corpus"
    manifest = load_json(corpus / "manifest.json", {})
    before = len(manifest)

    roots: list[tuple[str, Path]] = []
    if not args.no_lo:
        print("cloning LibreOffice core (sparse)...")
        sparse_clone("https://github.com/LibreOffice/core.git", src / "libreoffice-core", LO_SPARSE)
        roots.append(("libreoffice", src / "libreoffice-core"))
    if not args.no_poi:
        print("cloning Apache POI test-data (sparse)...")
        sparse_clone("https://github.com/apache/poi.git", src / "poi", POI_SPARSE)
        roots.append(("poi", src / "poi"))
    for e in args.extra:
        roots.append((f"extra:{e.name}", e.expanduser().resolve()))

    for name, root in roots:
        n = 0
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() == ".doc" and ".git" not in p.parts:
                n += add_file(manifest, corpus, p.read_bytes(), name, str(p.relative_to(root)))
        print(f"  {name}: +{n}")

    for i in range(args.govdocs):
        url = GOVDOCS_URL.format(i)
        cache = src / "govdocs" / f"{i:03d}.zip"
        if not cache.exists():
            print(f"downloading {url} ...")
            cache.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url) as r, open(cache, "wb") as f:
                shutil.copyfileobj(r, f)
        n = 0
        with zipfile.ZipFile(cache) as z:
            for info in z.infolist():
                if info.filename.lower().endswith(".doc"):
                    n += add_file(manifest, corpus, z.read(info), "govdocs1", info.filename)
        print(f"  govdocs {i:03d}: +{n}")

    save_json(corpus / "manifest.json", manifest)
    kinds: dict[str, int] = {}
    for m in manifest.values():
        key = f"{m['split']}/{m['kind']}"
        kinds[key] = kinds.get(key, 0) + 1
    print(f"corpus: {len(manifest)} files (+{len(manifest) - before})  " + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    print(f"data dir: {d}")


if __name__ == "__main__":
    main()
