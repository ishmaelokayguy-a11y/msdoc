#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Build the OpenDocument (.odt / .ods / .odp) test corpus.

    python3 oracle/office_fetch.py --ext odf                 # all three (odt|ods|odp are aliases)
    python3 oracle/office_fetch.py --ext odf --extra ~/odfs  # + your own files
    python3 oracle/odf_corpus.py [--only libreoffice ...]    # same, directly

Resumable: each source is a shallow blob-less sparse clone under data_dir()/sources/; the
blobs of the wanted paths are fetched in chunks of 400 before `git sparse-checkout set`
(a plain sparse checkout of ~3000 binary files over the lazy-fetch path is very slow), so a
time-limited shell can simply re-run the command until it prints the corpus counts.

Sources: LibreOffice core (every *.odt/ods/odp/ott/ots/otp: sw/qa, sc/qa, sd/qa, xmloff, oox,
chart2, svx, vcl, writerperfect, xmlsecurity, ...; plus the ODF import/export reference sources
xmloff/source/{text,table,draw,style,core}, xmloff/inc, include/xmloff, s{w,c,d}/source/filter/xml,
package/source/{zippackage,manifest}), Apache Tika and Apache POI test documents, the ODF Toolkit
(tdf/odftoolkit), odfdo, odfpy, ezodf and pandoc's ODT reader tests.

Layout under data_dir()/odf:
    corpus/{odt,ods,odp}/{dev,holdout}/<id>.<ext>   id = sha256[:16]; 15% holdout by hash
    manifest.json   id -> {ext, source, path, kind, split, sha256, bytes, mimetype}
    README.md       layout, text definitions, LibreOffice quirks, reference-source paths

The stored extension follows the `mimetype` member, not the file name (a spreadsheet named
.odt is stored as .ods); templates (.ott/.ots/.otp, mimetype *-template) are stored under the
ext of their document type with kind "template".
kind: normal | template (scored like normal) | encrypted (META-INF/manifest.xml gives content.xml
      or encrypted-package a manifest:encryption-data: LibreOffice needs a password; expected
      EncryptedDocumentError. Files where only Basic macro libraries are encrypted are normal)
      | fuzz (crash/fuzz/ofz/poc/forcepoint/CVE names, LibreOffice's pass/ and indeterminate/
      import-test dirs: must not crash, LibreOffice's text is best effort)
      | notodf (not a zip, non-ODF or non-text/spreadsheet/presentation mimetype, no content.xml)
      | fail (sources' */fail/* dirs; make_golden also re-labels a normal/template/fuzz file
      as fail when LibreOffice cannot load it: manifest "lo_failed": true, "kind_fetch": old kind)
Files > 8 MB (per file) are skipped; duplicates (same sha256) are kept once (first source wins).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import re
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import data_dir, load_json, save_json  # noqa: E402

HOLDOUT_PERCENT = 15
MAX_BYTES = 8 * 1024 * 1024
ODF_EXTS = ("odt", "ods", "odp")
NAME_EXTS = {"odt": "odt", "ott": "odt", "ods": "ods", "ots": "ods", "odp": "odp", "otp": "odp"}
DOCS = [f"/**/*.{e}" for e in NAME_EXTS] + [f"/**/*.{e.upper()}" for e in NAME_EXTS]
LO_REFERENCE = [
    "/xmloff/source/text/", "/xmloff/source/table/", "/xmloff/source/draw/", "/xmloff/source/style/",
    "/xmloff/source/core/", "/xmloff/inc/", "/include/xmloff/",
    "/sw/source/filter/xml/", "/sc/source/filter/xml/", "/sd/source/filter/xml/",
    "/package/source/zippackage/", "/package/source/manifest/", "/COPYING.MPL",
]
# (name, url, directory under data_dir()/sources, sparse patterns)
SOURCES = [
    ("libreoffice", "https://github.com/LibreOffice/core", "libreoffice-odf", DOCS + LO_REFERENCE),
    ("tika", "https://github.com/apache/tika", "tika-odf", DOCS + ["/LICENSE.txt", "/NOTICE.txt"]),
    ("poi", "https://github.com/apache/poi", "poi-odf", ["/test-data/**/*.ods", "/test-data/**/*.odt",
                                                         "/test-data/**/*.odp", "/LICENSE", "/NOTICE"]),
    ("odftoolkit", "https://github.com/tdf/odftoolkit", "odftoolkit", DOCS + ["/LICENSE*", "/NOTICE*"]),
    ("odfdo", "https://github.com/jdum/odfdo", "odfdo", DOCS + ["/LICENSE*"]),
    ("odfpy", "https://github.com/eea/odfpy", "odfpy", DOCS + ["/LICENSE*", "/COPYING*"]),
    ("ezodf", "https://github.com/T0ha/ezodf", "ezodf", DOCS + ["/LICENSE*"]),
    ("pandoc", "https://github.com/jgm/pandoc", "pandoc-odt", ["/test/**/*.odt", "/COPYRIGHT"]),
]
FUZZ_RE = re.compile(r"/pass/|/indeterminate/|fuzz|forcepoint|ofz|crash|hang-|poc|CVE-|EDB-|minimized", re.I)
MIME_EXT = {"text": "odt", "text-template": "odt", "text-master": "odt", "text-master-template": "odt",
            "spreadsheet": "ods", "spreadsheet-template": "ods",
            "presentation": "odp", "presentation-template": "odp"}
CHUNK = 400


def _git(dest: Path, *args: str, timeout: float | None = None) -> str:
    return subprocess.run(["git", "-C", str(dest), *args], check=True, capture_output=True, text=True,
                          timeout=timeout).stdout


def _wanted(path: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p.endswith("/"):
            if ("/" + path).startswith(p):
                return True
        elif p.startswith("/**/"):
            if fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], p[4:]) or fnmatch.fnmatchcase(path, p[4:]):
                return True
        elif fnmatch.fnmatchcase("/" + path, p):
            return True
    return False


def sparse_fetch(url: str, dest: Path, patterns: list[str]) -> None:
    """Shallow blob-less sparse clone; prefetch the wanted blobs in chunks, then check them out."""
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, str(dest)], check=True)
    todo = []
    for line in _git(dest, "ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, typ, oid = meta.split()
        if typ == "blob" and _wanted(path, patterns) and not (dest / path).exists():
            todo.append(oid)
    for i in range(0, len(todo), CHUNK):
        _git(dest, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
             "--filter=blob:none", "origin", *todo[i:i + CHUNK])
        print(f"  {dest.name}: fetched {min(i + CHUNK, len(todo))}/{len(todo)} blobs", flush=True)
    _git(dest, "sparse-checkout", "set", "--no-cone", *patterns)


_ENTRY_RE = re.compile(rb"<manifest:file-entry\b[^>]*?full-path=\"([^\"]*)\"[^>]*?(?<!/)>(.*?)</manifest:file-entry>", re.S)


def _encrypted_content(manifest_xml: bytes) -> bool:
    """True if content.xml (or the whole package, ODF 1.3 "encrypted-package") carries
    manifest:encryption-data. Password-protected Basic libraries alone do not make a document
    unreadable: those files stay "normal"."""
    for path, body in _ENTRY_RE.findall(manifest_xml):
        if b"encryption-data" in body and path in (b"content.xml", b"encrypted-package"):
            return True
    return False


def classify(data: bytes, path: str) -> tuple[str, str, str | None]:
    """-> (ext to store under, kind, mimetype)."""
    p = "/" + path.replace("\\", "/")
    name_ext = NAME_EXTS.get(p.rsplit(".", 1)[-1].lower(), "odt")
    template = p.rsplit(".", 1)[-1].lower() in ("ott", "ots", "otp")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
    except Exception:
        return name_ext, "fail" if "/fail/" in p.lower() else "notodf", None
    mt = None
    ext = name_ext
    try:
        if "mimetype" in names:
            raw = z.read("mimetype")[:200].strip()
            mt = raw.decode("ascii") if all(32 < b < 127 for b in raw) else ("<binary>" if raw else None)
    except Exception:
        mt = None
    if mt:
        prefix = "application/vnd.oasis.opendocument."
        sub = mt[len(prefix):] if mt.startswith(prefix) else None
        if sub not in MIME_EXT:
            return name_ext, "fail" if "/fail/" in p.lower() else "notodf", mt
        ext, template = MIME_EXT[sub], sub.endswith("-template")
    encrypted = False
    try:
        if "META-INF/manifest.xml" in names:
            encrypted = _encrypted_content(z.read("META-INF/manifest.xml"))
    except Exception:
        pass
    if "/fail/" in p.lower():
        return ext, "fail", mt
    if encrypted:
        return ext, "encrypted", mt
    if "content.xml" not in names:
        return ext, "notodf", mt
    if FUZZ_RE.search(p):
        return ext, "fuzz", mt
    return ext, "template" if template else "normal", mt


def main(a=None) -> int:
    if a is None:
        ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("--extra", type=Path)
        ap.add_argument("--only", nargs="*", help="fetch only these sources (names in SOURCES)")
        ap.add_argument("--no-fetch", action="store_true", help="use the existing clones only")
        a = ap.parse_args()
    d = data_dir()
    src = d / "sources"
    only = getattr(a, "only", None)
    roots = []
    for name, url, sub, pats in SOURCES:
        dest = src / sub
        if not getattr(a, "no_fetch", False) and (not only or name in only):
            try:
                sparse_fetch(url, dest, pats)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                print(f"warning: {name} not fetched: {e}", file=sys.stderr)
        roots.append((name, dest))
    if getattr(a, "extra", None):
        roots.append(("extra", a.extra))
    out = d / "odf"
    man_path = out / "manifest.json"
    manifest = load_json(man_path, {})
    seen = {v["sha256"] for v in manifest.values()}
    added = skipped_big = 0
    for source, root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or ".git" in p.parts or p.suffix.lower().lstrip(".") not in NAME_EXTS:
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
            ext, kind, mt = classify(data, rel)
            split = "holdout" if int(sha[:6], 16) % 100 < HOLDOUT_PERCENT else "dev"
            fid = sha[:16]
            dest = out / "corpus" / ext / split / f"{fid}.{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            manifest[fid] = {"ext": ext, "source": source, "path": rel, "kind": kind, "split": split,
                             "sha256": sha, "bytes": len(data), "mimetype": mt}
            added += 1
    save_json(man_path, manifest)
    write_readme(d, manifest)
    c = Counter((v["ext"], v["split"], v["kind"]) for v in manifest.values())
    print(f"odf: added {added}; total {len(manifest)}; skipped >8MB: {skipped_big}")
    for k, n in sorted(c.items()):
        print(" ", *k, n)
    return 0


def write_readme(d: Path, manifest: dict) -> None:
    lo = d / "sources" / "libreoffice-odf"
    c = Counter((v["ext"], v["kind"], v["split"]) for v in manifest.values())
    rows = []
    for ext in ODF_EXTS:
        kinds = sorted({k for e, k, _ in c if e == ext})
        for k in kinds:
            rows.append(f"| {ext} | {k} | {c[(ext, k, 'dev')]} | {c[(ext, k, 'holdout')]} |")
    from odf_golden import README_TEXT_DEFS  # single source of the text definitions
    (d / "odf" / "README.md").write_text(README.format(
        lo=f"$DATA/sources/{lo.name}", data=d, counts="\n".join(rows), defs=README_TEXT_DEFS), encoding="utf-8")


README = """# OpenDocument test corpus (oracle/office_fetch.py --ext odf)

- corpus/{{odt,ods,odp}}/{{dev,holdout}}/<id>.<ext> (holdout = final exam: do not read it or its goldens)
- manifest.json: id -> {{ext, source, path, kind, split, sha256, bytes, mimetype}}
  (+ lo_failed / kind_fetch once make_golden found that LibreOffice cannot load it)
- golden/<id>.json (+ golden/<id>/NN.ext pictures): python3 oracle/make_golden.py --ext odt|ods|odp
- score: python3 oracle/compare.py --ext odt|ods|odp [--kind normal] [--diff ID] [--check] [--update-baseline]
- baseline-<ext>.json, struct_baseline-odt.json: ratchet baselines; NOT seeded until a parser exists.
  Seed once the parser lands: python3 oracle/compare.py --ext odt --update-baseline (same for ods, odp).

| ext | kind | dev | holdout |
|---|---|---|---|
{counts}

kind: normal | template (.ott/.ots/.otp; scored like normal) | encrypted (expect EncryptedDocumentError:
1.0, other MsDocError 0.5) | fuzz (clean MsDocError = 1.0, else scored against the golden) |
notodf (anything but a crash/hang = 1.0) | fail (LibreOffice cannot load it: anything but a crash/hang = 1.0).
A crash, timeout or NotImplementedError scores 0 for every kind.

{defs}

LibreOffice ODF import/export sources (MPL-2.0, may be read and ported), sparse checkout {lo}:
- xmloff/source/text (paragraphs, lists, fields, notes, sections, tracked changes), xmloff/source/table,
  xmloff/source/draw (frames, shapes, pictures, presentation pages, notes, annotations),
  xmloff/source/style (number formats: xmlnumfi.cxx, page/master styles), xmloff/source/core, xmloff/inc, include/xmloff
- sw/source/filter/xml (Writer: xmlimp.cxx, xmltbli.cxx tables), sc/source/filter/xml (Calc: xmlcelli.cxx cells,
  xmlimprt.cxx, number-columns-repeated limits), sd/source/filter/xml (Impress glue)
- package/source/zippackage, package/source/manifest (zip storage, manifest.xml, encryption-data)
- the corpus sources themselves: sw/qa, sc/qa, sd/qa ... (the .cxx unit tests next to them say what LO asserts)

$DATA is the shared data dir (here: {data}; see oracle/common.py data_dir()).
"""


if __name__ == "__main__":
    raise SystemExit(main())
