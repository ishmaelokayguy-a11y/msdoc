#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Produce reference ("golden") output with LibreOffice.

Each corpus file is converted to ODT by headless LibreOffice; the ODT XML is
then walked to collect text stories and images (original bytes, which LO
keeps for most formats). Output per file:

    golden/<id>.json   {"status": "ok"|"lo_failed", "stories": {...}, "images": [...]}
    golden/<id>/NN.ext image bytes (for debugging; the JSON has fingerprints)

    python3 oracle/make_golden.py [--jobs 4] [--force] [--ids ID ...]
    python3 oracle/make_golden.py --file some.doc --print   # inspect one file

RTF (--ext rtf): the corpus built by `oracle/office_fetch.py --ext rtf` is converted the
same way (RTF -> ODT) into the same representation, under data_dir()/rtf/golden/, for the
kinds normal and fuzz. RTF goldens also carry "links": [{"url", "text"}] (every text:a in
the body, notes, frames and headers/footers, in document order). --budget makes a run stop
starting new files after N seconds; files already written are skipped on the next run:

    python3 oracle/make_golden.py --ext rtf --jobs 3 --timeout 60 --budget 100   # repeat until "0 files"

Requires LibreOffice >= 7.x (soffice on PATH, $SOFFICE, or the default macOS
app location) and Pillow.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import data_dir, fingerprint, load_json, save_json  # noqa: E402

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
}


def q(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


T = lambda local: q("text", local)  # noqa: E731
D = lambda local: q("draw", local)  # noqa: E731
TB = lambda local: q("table", local)  # noqa: E731
O = lambda local: q("office", local)  # noqa: E731

SKIP_BLOCK = {T("tracked-changes"), T("sequence-decls"), T("variable-decls"), T("user-field-decls"),
              O("forms"), T("table-of-content-source"), T("alphabetical-index-source"),
              T("illustration-index-source"), T("user-index-source"), T("bibliography-source"),
              T("index-title-template"), TB("covered-table-cell"), TB("table-columns"), TB("table-column")}
SKIP_INLINE = {T("note-citation"), T("number"), T("bookmark"), T("bookmark-start"), T("bookmark-end"),
               T("reference-mark"), T("reference-mark-start"), T("reference-mark-end"),
               T("soft-page-break"), T("change"), T("change-start"), T("change-end"),
               O("annotation-end"), T("toc-mark"), T("alphabetical-index-mark"), T("user-index-mark")}
SHAPES_WITH_TEXT = {D("custom-shape"), D("rect"), D("ellipse"), D("polygon"), D("polyline"), D("path"),
                    D("line"), D("connector"), D("circle"), D("regular-polygon"), D("caption"), D("measure")}


class _Wrap(list):
    """Iterable holding one element, so that _scan(el) can be applied to the element itself."""
    def __init__(self, el):
        super().__init__([el])


class Walker:
    def __init__(self, odt: zipfile.ZipFile):
        self.z = odt
        self.stories: dict[str, list[str]] = {k: [] for k in ("main", "footnotes", "endnotes", "headers", "comments", "textboxes")}
        self.images: list[dict] = []
        self.blobs: list[bytes] = []
        self.struct: list[dict] = []
        self.list_styles: dict[str, dict[int, bool]] = {}

    # ---- block level
    def block(self, el, story: str) -> None:
        tag = el.tag
        if tag in SKIP_BLOCK:
            return
        if tag in (T("p"), T("h")):
            buf: list[str] = []
            self.inline(el, story, buf)
            self.stories[story].append("".join(buf))
            return
        if tag == D("frame") or tag in SHAPES_WITH_TEXT or tag == D("g"):
            self.drawing(el, story)
            return
        for ch in el:
            self.block(ch, story)

    # ---- inline level
    def inline(self, el, story: str, buf: list[str]) -> None:
        if el.text:
            buf.append(el.text)
        for ch in el:
            tag = ch.tag
            if tag == T("s"):
                buf.append(" " * int(ch.get(q("text", "c"), "1")))
            elif tag == T("tab"):
                buf.append("\t")
            elif tag == T("line-break"):
                buf.append("\n")
            elif tag == T("note"):
                cls = ch.get(q("text", "note-class"), "footnote")
                body = ch.find(T("note-body"))
                if body is not None:
                    for b in body:
                        self.block(b, "endnotes" if cls == "endnote" else "footnotes")
            elif tag == O("annotation"):
                for b in ch:
                    if b.tag in (T("p"), T("h"), T("list")):
                        self.block(b, "comments")
            elif tag == D("frame") or tag in SHAPES_WITH_TEXT or tag == D("g"):
                self.drawing(ch, story)
            elif tag in SKIP_INLINE:
                pass
            else:
                self.inline(ch, story, buf)
            if ch.tail:
                buf.append(ch.tail)

    # ---- frames / shapes
    def drawing(self, el, story: str) -> None:
        tag = el.tag
        if tag == D("g"):
            for ch in el:
                self.drawing(ch, story)
            return
        if tag == D("frame"):
            if el.find(D("object")) is not None or el.find(D("object-ole")) is not None:
                return  # OLE objects (and their preview images) are out of scope
            anchor = el.get(q("text", "anchor-type"), "paragraph")
            kind = "inline" if anchor == "as-char" else "floating"
            for ch in el:
                if ch.tag == D("image"):
                    self.image(ch, story, kind, el.get(D("name")))
                elif ch.tag == D("text-box"):
                    tb_story = "textboxes"
                    for b in ch:
                        self.block(b, tb_story)
            return
        # custom shape / rect with text
        for ch in el:
            if ch.tag in (T("p"), T("h"), T("list")):
                self.block(ch, "textboxes")

    def image(self, el, story: str, kind: str, name) -> None:
        href = el.get(q("xlink", "href"))
        data = None
        if href and not href.startswith(("http:", "https:", "file:")):
            try:
                data = self.z.read(href.lstrip("./"))
            except KeyError:
                data = None
        if data is None:
            bin_el = el.find(O("binary-data"))
            if bin_el is not None and bin_el.text:
                import base64
                data = base64.b64decode(bin_el.text)
        if not data:
            return
        fp = fingerprint(data)
        fp.update({"kind": kind, "story": story, "name": name})
        self.images.append(fp)
        self.blobs.append(data)

    # ---- structure of the main story (see oracle/structscore.py for the format)
    def _collect_list_styles(self, root) -> None:
        for ls in root.iter(T("list-style")):
            name = ls.get(q("style", "name"))
            if not name:
                continue
            lv = {}
            for ch in ls:
                try:
                    n = int(ch.get(T("level"), "0"))
                except ValueError:
                    continue
                lv[n] = ch.tag == T("list-level-style-number")
            self.list_styles[name] = lv

    def _scan(self, el, c: dict) -> None:
        for ch in el:
            tag = ch.tag
            if tag == T("note"):
                c["notes"] += 1
            elif tag == O("annotation"):
                pass
            elif tag == D("frame"):
                if ch.find(D("object")) is None and ch.find(D("object-ole")) is None:
                    c["img"] += sum(1 for x in ch if x.tag == D("image"))
            elif tag == D("g"):
                self._scan(ch, c)
            elif tag in SHAPES_WITH_TEXT:
                pass
            else:
                if tag == T("a") and ch.get(q("xlink", "href")):
                    c["links"] += 1
                self._scan(ch, c)

    def sblock(self, el, out: list, lvl, lstyle) -> None:
        tag = el.tag
        if tag in SKIP_BLOCK:
            return
        if tag in (T("p"), T("h")):
            c = {"img": 0, "notes": 0, "links": 0}
            self._scan(el, c)
            h = 0
            if tag == T("h"):
                try:
                    h = max(1, min(9, int(el.get(T("outline-level"), "1"))))
                except ValueError:
                    h = 1
            lst = None
            if lvl is not None and not h:
                lst = [lvl, bool(self.list_styles.get(lstyle, {}).get(lvl + 1, False))]
            out.append({"t": "p", "h": h, "l": lst, **c})
        elif tag == T("list"):
            st = el.get(T("style-name")) or lstyle
            nl = 0 if lvl is None else lvl + 1
            for item in el:
                for ch in item:
                    self.sblock(ch, out, nl, st)
        elif tag == TB("table"):
            out.append(self.stable(el))
        elif tag == D("frame") or tag == D("g"):
            c = {"img": 0, "notes": 0, "links": 0}
            self._scan(_Wrap(el), c)
            if c["img"]:
                out.append({"t": "p", "h": 0, "l": None, **c})
        elif tag in SHAPES_WITH_TEXT:
            return
        else:
            for ch in el:
                self.sblock(ch, out, lvl, lstyle)

    def stable(self, tbl) -> dict:
        rows: list[list[dict]] = []

        def rep(el, key):
            try:
                return max(1, min(int(el.get(TB(key), "1")), 200))
            except ValueError:
                return 1

        def walk(el):
            for ch in el:
                if ch.tag == TB("table-row"):
                    cells = []
                    for cell in ch:
                        if cell.tag != TB("table-cell"):
                            continue
                        b: list[dict] = []
                        for x in cell:
                            self.sblock(x, b, None, None)
                        try:
                            cs = int(cell.get(TB("number-columns-spanned"), "1"))
                            rs = int(cell.get(TB("number-rows-spanned"), "1"))
                        except ValueError:
                            cs = rs = 1
                        cells.extend([{"cs": cs, "rs": rs, "b": b}] * rep(cell, "number-columns-repeated"))
                    rows.extend([cells] * rep(ch, "number-rows-repeated"))
                elif ch.tag in (TB("table-header-rows"), TB("table-rows"), TB("table-row-group")):
                    walk(ch)
        walk(tbl)
        return {"t": "tbl", "rows": rows}

    def run(self) -> None:
        content = ET.fromstring(self.z.read("content.xml"))
        self._collect_list_styles(content)
        if "styles.xml" in self.z.namelist():
            self._collect_list_styles(ET.fromstring(self.z.read("styles.xml")))
        _b = content.find(O("body"))
        _t = _b.find(O("text")) if _b is not None else None
        if _t is not None:
            for ch in _t:
                self.sblock(ch, self.struct, None, None)
        body = content.find(O("body"))
        text = body.find(O("text")) if body is not None else None
        if text is not None:
            for ch in text:
                self.block(ch, "main")
        if "styles.xml" in self.z.namelist():
            styles = ET.fromstring(self.z.read("styles.xml"))
            master = styles.find(O("master-styles"))
            if master is not None:
                for page in master:
                    for hf in page:
                        if hf.tag.split("}")[1] in ("header", "footer", "header-left", "footer-left",
                                                     "header-first", "footer-first"):
                            for b in hf:
                                self.block(b, "headers")


def find_soffice() -> str | None:
    """$SOFFICE, soffice/libreoffice on PATH, or the standard macOS/Windows locations."""
    cands = [os.environ.get("SOFFICE"), shutil.which("soffice"), shutil.which("libreoffice"),
             "/Applications/LibreOffice.app/Contents/MacOS/soffice",
             r"C:\Program Files\LibreOffice\program\soffice.exe"]
    return next((c for c in cands if c and Path(c).exists()), None)


SOFFICE = find_soffice()


def convert(doc: Path, outdir: Path, profile: Path, timeout: int) -> Path | None:
    cmd = [SOFFICE, "--headless", "--norestore", "--nologo", "--nodefault",
           f"-env:UserInstallation={profile.as_uri()}", "--convert-to", "odt", "--outdir", str(outdir), str(doc)]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        return None
    out = outdir / (doc.stem + ".odt")
    return out if out.exists() and out.stat().st_size > 0 else None


def collect_links(z: zipfile.ZipFile) -> list[dict]:
    """Hyperlinks (text:a with an xlink:href) of the body, then of the headers/footers."""
    out = []
    content = ET.fromstring(z.read("content.xml"))
    roots = [content.find(O("body"))]
    if "styles.xml" in z.namelist():
        roots.append(ET.fromstring(z.read("styles.xml")).find(O("master-styles")))
    for r in roots:
        if r is None:
            continue
        for a in r.iter(T("a")):
            href = a.get(q("xlink", "href"))
            if href:
                out.append({"url": href, "text": "".join(a.itertext())})
    return out


def golden_for(doc: Path, profile: Path, timeout: int, links: bool = False) -> tuple[dict, list[bytes]]:
    with tempfile.TemporaryDirectory() as td:
        odt = convert(doc, Path(td), profile, timeout)
        if odt is None:
            return {"status": "lo_failed", "stories": {}, "images": []}, []
        try:
            with zipfile.ZipFile(odt) as z:
                w = Walker(z)
                w.run()
                found = collect_links(z) if links else None
        except Exception as e:  # malformed ODT from LO: treat as failure
            return {"status": "lo_failed", "error": repr(e), "stories": {}, "images": []}, []
    g = {"status": "ok", "stories": w.stories, "images": w.images, "struct": w.struct}
    if links:
        g["links"] = found
    return g, w.blobs


EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tif",
       "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct", "image/x-svm": "svm", "image/svg+xml": "svg"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--missing-struct", action="store_true", help="only files whose golden has no 'struct' yet (resume)")
    ap.add_argument("--file", type=Path, help="process a single arbitrary .doc and print the result")
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--ext", choices=["doc", "rtf"], default="doc", help="corpus: doc (default) or rtf (data_dir()/rtf)")
    ap.add_argument("--budget", type=float, help="stop starting new conversions after this many seconds")
    args = ap.parse_args()
    rtf = args.ext == "rtf"
    if SOFFICE is None:
        sys.exit("LibreOffice not found: put soffice on PATH or set SOFFICE=/path/to/soffice")

    d = data_dir()
    profiles = Path(tempfile.mkdtemp(prefix="msdoc-lo-profiles-"))

    if args.file:
        g, _ = golden_for(args.file.resolve(), profiles / "p0", args.timeout, links=rtf)
        import json
        print(json.dumps(g, ensure_ascii=False, indent=1))
        return

    if rtf:
        root, gold = d / "rtf", d / "rtf" / "golden"
        manifest = load_json(root / "manifest.json", {})
    else:
        root, gold = d, d / "golden"
        manifest = load_json(d / "corpus" / "manifest.json", {})
    todo = []
    for fid, m in manifest.items():
        if args.ids and fid not in args.ids:
            continue
        if rtf and m["kind"] not in ("normal", "fuzz"):
            continue  # fail / notrtf are scored for robustness only
        gp = gold / f"{fid}.json"
        if args.missing_struct:
            old = load_json(gp, None)
            if old is not None and ("struct" in old or old.get("status") != "ok"):
                continue
        elif not args.force and gp.exists():
            continue
        todo.append((fid, root / "corpus" / m["split"] / f"{fid}.{'rtf' if rtf else 'doc'}"))
    print(f"{len(todo)} files to convert with {args.jobs} LibreOffice workers")

    import threading
    slots = list(range(args.jobs))
    lock = threading.Lock()
    done = [0]
    import time
    t0 = time.monotonic()

    def work(item):
        fid, path = item
        if args.budget is not None and time.monotonic() - t0 > args.budget:
            return
        with lock:
            slot = slots.pop()
        try:
            g, blobs = golden_for(path, profiles / f"p{slot}", args.timeout, links=rtf)
        finally:
            with lock:
                slots.append(slot)
        gdir = gold / fid
        shutil.rmtree(gdir, ignore_errors=True)
        for i, (meta, blob) in enumerate(zip(g["images"], blobs)):
            gdir.mkdir(parents=True, exist_ok=True)
            fn = f"{i:02d}.{EXT.get(meta['mime'], 'bin')}"
            (gdir / fn).write_bytes(blob)
            meta["file"] = f"{fid}/{fn}"
        save_json(gold / f"{fid}.json", g)
        with lock:
            done[0] += 1
            if done[0] % 20 == 0 or done[0] == len(todo):
                print(f"  {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(work, todo))
    shutil.rmtree(profiles, ignore_errors=True)
    if args.budget is not None:
        print(f"written {done[0]}, left {len(todo) - done[0]} (re-run to continue)")


if __name__ == "__main__":
    main()
