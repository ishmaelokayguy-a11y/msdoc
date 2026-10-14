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

RTF (--ext rtf) is a word-processor format and has its own layout, scored by the
.doc oracle (oracle/make_golden.py --ext rtf, oracle/compare.py --ext rtf):

    python3 oracle/office_fetch.py --ext rtf                 # clone LO + POI + Tika sparse, build corpus
    python3 oracle/office_fetch.py --ext rtf --extra ~/rtfs  # + your own .rtf files

    data_dir()/rtf/corpus/{dev,holdout}/<id>.rtf     id = sha256[:16], 15% holdout by hash
    data_dir()/rtf/manifest.json   id -> {ext, source, path, kind, split, sha256, bytes}
    data_dir()/rtf/README.md       layout + where the LibreOffice RTF reader sources are
    data_dir()/sources/libreoffice-rtf/   sw/qa .rtf files + sw/source/writerfilter/{rtftok,dmapper}
    data_dir()/sources/poi-rtf/, sources/tika/   Apache POI / Apache Tika .rtf test files
rtf kind: normal | fuzz (LibreOffice pass/indeterminate crash-test files, CVE/fuzzer
          samples: LO must not crash, its text is best effort) | fail (LO rejects them:
          */fail/*) | notrtf (named .rtf but does not start with "{\\rtf")
Files > 8 MB are skipped; duplicates (same sha256) are kept once (first source wins).
"""
from __future__ import annotations

import argparse
import hashlib
import re
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


# ------------------------------------------------------------------ RTF

RTF_LO_SPARSE = [
    "/sw/qa/**/*.rtf", "/sw/qa/**/*.RTF", "/writerfilter/qa/**/*.rtf",
    "/sw/qa/extras/rtfimport/*.cxx", "/sw/qa/extras/rtfexport/*.cxx",  # what LO asserts per test file
    # reference sources for the RTF parser (read / port under MPL-2.0). writerfilter moved
    # into sw/source/writerfilter in LibreOffice 24.8; the old top-level path is kept for older checkouts.
    "/sw/source/writerfilter/rtftok/", "/sw/source/writerfilter/dmapper/", "/sw/source/writerfilter/inc/",
    "/writerfilter/source/rtftok/", "/writerfilter/source/dmapper/", "/writerfilter/inc/",
    "/COPYING.MPL",
]
RTF_POI_SPARSE = ["/test-data/**/*.rtf", "/test-data/**/*.RTF", "/LICENSE", "/NOTICE"]
RTF_TIKA_SPARSE = ["/**/test-documents/**/*.rtf", "/**/test-documents/**/*.RTF", "/LICENSE.txt", "/NOTICE.txt"]
RTF_FUZZ_RE = re.compile(r"/pass/|/indeterminate/|fuzz|forcepoint|ofz|crash|hang-|poc|CVE-|EDB-|minimized", re.I)


def classify_rtf(data: bytes, path: str) -> str:
    p = "/" + path.replace("\\", "/")
    if "/fail/" in p.lower():
        return "fail"
    head = data[:64]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if not head.lstrip(b" \t\r\n").startswith(b"{\\rtf"):
        return "notrtf"
    if RTF_FUZZ_RE.search(p):
        return "fuzz"
    return "normal"


def rtf_main(a) -> int:
    d = data_dir()
    src = d / "sources"
    roots = []
    for name, url, pats, override in (
            ("libreoffice", "https://github.com/LibreOffice/core", RTF_LO_SPARSE, a.lo),
            ("poi", "https://github.com/apache/poi", RTF_POI_SPARSE, a.poi),
            ("tika", "https://github.com/apache/tika", RTF_TIKA_SPARSE, None)):
        dest = override or src / {"libreoffice": "libreoffice-rtf", "poi": "poi-rtf", "tika": "tika"}[name]
        if not override:
            try:
                sparse_clone(url, dest, pats)
            except (subprocess.CalledProcessError, OSError) as e:
                print(f"warning: {name} not fetched: {e}", file=sys.stderr)
        roots.append((name, dest))
    if a.extra:
        roots.append(("extra", a.extra))
    out = d / "rtf"
    man_path = out / "manifest.json"
    manifest = load_json(man_path, {})
    seen = {v["sha256"] for v in manifest.values()}
    added = skipped_big = 0
    for source, root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or ".git" in p.parts or p.suffix.lower() != ".rtf":
                continue
            size = p.stat().st_size
            if size == 0 or size > MAX_BYTES:
                skipped_big += size > MAX_BYTES
                continue
            data = p.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if sha in seen:
                continue
            seen.add(sha)
            rel = p.relative_to(root).as_posix()
            split = "holdout" if int(sha[:6], 16) % 100 < HOLDOUT_PERCENT else "dev"
            fid = sha[:16]
            dest = out / "corpus" / split / f"{fid}.rtf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            manifest[fid] = {"ext": "rtf", "source": source, "path": rel, "kind": classify_rtf(data, rel),
                             "split": split, "sha256": sha, "bytes": len(data)}
            added += 1
    save_json(man_path, manifest)
    lo_src = src / "libreoffice-rtf"
    rtftok = next((x for x in (lo_src / "sw/source/writerfilter/rtftok", lo_src / "writerfilter/source/rtftok") if x.exists()),
                  lo_src / "sw/source/writerfilter/rtftok")
    rel = lambda x: "$DATA/" + x.relative_to(d).as_posix() if x.is_relative_to(d) else str(x)  # noqa: E731
    (out / "README.md").write_text(RTF_README.format(rtftok=rel(rtftok), dmapper=rel(rtftok.parent / "dmapper"),
                                                     lo=rel(lo_src), data=d),
                                   encoding="utf-8")
    from collections import Counter
    c = Counter((v["split"], v["kind"]) for v in manifest.values())
    print(f"rtf: added {added}; total {len(manifest)}; skipped >8MB: {skipped_big}")
    for k, n in sorted(c.items()):
        print(" ", k, n)
    return 0


RTF_README = """# RTF test corpus (generated by oracle/office_fetch.py --ext rtf)

- corpus/dev/<id>.rtf, corpus/holdout/<id>.rtf (holdout = final exam: do not read)
- manifest.json: id -> {{ext, source, path, kind, split, sha256, bytes}}
  kind: normal | fuzz | fail | notrtf (see oracle/office_fetch.py docstring)
- golden/<id>.json (+ golden/<id>/NN.ext pictures): python3 oracle/make_golden.py --ext rtf
- score: python3 oracle/compare.py --ext rtf [--check | --diff ID | --update-baseline]
- baseline.json / struct_baseline.json: ratchet baselines (integrator updates them)

LibreOffice RTF reader sources (MPL-2.0, may be read and ported):
- tokenizer / RTF state machine: {rtftok}
- domain mapper (paragraphs, tables, fields, headers, notes): {dmapper}
- whole sparse checkout (also sw/qa .rtf tests, e.g. sw/qa/extras/rtfimport/rtfimport.cxx): {lo}

$DATA is the shared data dir (here: {data}; see oracle/common.py data_dir()).
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", choices=["office", "rtf"], default="office",
                    help="office (default): .xls/.ppt corpus; rtf: the .rtf corpus under data/rtf")
    ap.add_argument("--lo", type=Path)
    ap.add_argument("--poi", type=Path)
    ap.add_argument("--extra", type=Path)
    a = ap.parse_args()
    if a.ext == "rtf":
        return rtf_main(a)
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
