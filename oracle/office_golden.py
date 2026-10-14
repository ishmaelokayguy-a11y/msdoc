#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Golden output for .xls / .ppt from LibreOffice (converted to .ods / .odp and parsed).

    python3 oracle/office_golden.py [--ext xls|ppt] [--split dev|holdout] [--budget 140]
                                    [--ids ID ...] [--force]

Resumable: ids that already have a golden file are skipped, so a time-limited
shell can simply be re-run. Golden files live in data_dir()/office/golden/<id>.json:

  xls: {"sheets": [{"name", "hidden", "cells": [[row, col, text]...],
                    "merges": [[row, col, rows, cols]...]}], "images": [fingerprint...]}
  ppt: {"slides": [{"name", "texts": [paragraph...], "notes": [paragraph...]}],
        "images": [fingerprint...]}
  or   {"error": "..."} when LibreOffice cannot open the file.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import data_dir, fingerprint, load_json, save_json  # noqa: E402

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def q(prefix: str, name: str) -> str:
    return "{%s}%s" % (NS[prefix], name)


MAX_REPEAT_EMPTY = 4096


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def para_text(p: ET.Element) -> str:
    out: list[str] = []

    def rec(e: ET.Element) -> None:
        if e.text:
            out.append(e.text)
        for ch in e:
            t = local(ch.tag)
            if t == "s":
                out.append(" " * int(ch.get(q("text", "c"), "1")))
            elif t == "tab":
                out.append("\t")
            elif t == "line-break":
                out.append("\n")
            elif t in ("annotation", "note", "tracked-changes", "bookmark", "bookmark-start", "bookmark-end"):
                pass
            else:
                rec(ch)
            if ch.tail:
                out.append(ch.tail)

    rec(p)
    return "".join(out)


def cell_text(cell: ET.Element) -> str:
    return "\n".join(para_text(p) for p in cell.findall(q("text", "p")))


def _rows(table: ET.Element):
    for ch in table:
        t = local(ch.tag)
        if t == "table-row":
            yield ch
        elif t in ("table-header-rows", "table-row-group", "table-rows"):
            yield from _rows(ch)


def hidden_tables(root: ET.Element) -> set[str]:
    hidden = set()
    for st in root.iter(q("style", "style")):
        tp = st.find(q("style", "table-properties"))
        if tp is not None and tp.get(q("table", "display")) == "false":
            hidden.add(st.get(q("style", "name")))
    return hidden


def fallback_images(root: ET.Element) -> set:
    """LibreOffice stores a PNG rendering after the metafile in a draw:frame; it is not a second picture."""
    skip = set()
    for fr in root.iter(q("draw", "frame")):
        imgs = [c for c in fr if c.tag == q("draw", "image")]
        skip.update(imgs[1:])
    return skip


def parse_ods(content: bytes, zf: zipfile.ZipFile) -> dict:
    root = ET.fromstring(content)
    skip_imgs = fallback_images(root)
    hid = hidden_tables(root)
    sheets = []
    images = []
    seen_href = set()
    for table in root.iter(q("table", "table")):
        if table.get(q("table", "name")) is None:
            continue
        # only top-level sheets (office:spreadsheet/table:table)
        cells, merges = [], []
        r = 0
        for row in _rows(table):
            rrep = int(row.get(q("table", "number-rows-repeated"), "1"))
            c = 0
            row_cells = []
            for cell in row:
                t = local(cell.tag)
                if t not in ("table-cell", "covered-table-cell"):
                    continue
                crep = int(cell.get(q("table", "number-columns-repeated"), "1"))
                txt = cell_text(cell) if t == "table-cell" else ""
                if txt:
                    for k in range(min(crep, MAX_REPEAT_EMPTY)):
                        row_cells.append((c + k, txt))
                    if t == "table-cell":
                        rs = int(cell.get(q("table", "number-rows-spanned"), "1"))
                        cs = int(cell.get(q("table", "number-columns-spanned"), "1"))
                        if rs > 1 or cs > 1:
                            merges.append([r, c, rs, cs])
                elif t == "table-cell":
                    rs = int(cell.get(q("table", "number-rows-spanned"), "1"))
                    cs = int(cell.get(q("table", "number-columns-spanned"), "1"))
                    if rs > 1 or cs > 1:
                        merges.append([r, c, rs, cs])
                c += crep
            if row_cells:
                for k in range(min(rrep, MAX_REPEAT_EMPTY)):
                    cells.extend([r + k, cc, tx] for cc, tx in row_cells)
            r += rrep
        sheets.append({"name": table.get(q("table", "name")),
                       "hidden": table.get(q("table", "style-name")) in hid,
                       "cells": cells, "merges": merges})
        for img in table.iter(q("draw", "image")):
            href = img.get(q("xlink", "href"))
            if img in skip_imgs:
                continue
            if href and href not in seen_href and not href.startswith("Object"):
                seen_href.add(href)
                try:
                    images.append(fingerprint(zf.read(href)))
                except KeyError:
                    pass
    return {"sheets": sheets, "images": images}


def _collect_paragraphs(el: ET.Element, out: list[str]) -> None:
    for ch in el:
        t = local(ch.tag)
        if t in ("p", "h") and ch.tag.startswith("{%s}" % NS["text"]):
            s = para_text(ch)
            if s.strip():
                out.append(s)
        elif ch.tag == q("presentation", "notes"):
            continue
        else:
            _collect_paragraphs(ch, out)


def parse_odp(content: bytes, zf: zipfile.ZipFile) -> dict:
    root = ET.fromstring(content)
    skip_imgs = fallback_images(root)
    slides = []
    images = []
    seen_href = set()
    for page in root.iter(q("draw", "page")):
        texts: list[str] = []
        _collect_paragraphs(page, texts)
        notes: list[str] = []
        for n in page.iter(q("presentation", "notes")):
            _collect_paragraphs(n, notes)
        slides.append({"name": page.get(q("draw", "name")), "texts": texts, "notes": notes})
        for img in page.iter(q("draw", "image")):
            href = img.get(q("xlink", "href"))
            if img in skip_imgs:
                continue
            # "TablePreview*.svm" is LibreOffice's own rendering of a table, not a picture of the file
            if href and href not in seen_href and not href.startswith("Object") and "TablePreview" not in href:
                seen_href.add(href)
                try:
                    images.append(fingerprint(zf.read(href)))
                except KeyError:
                    pass
    return {"slides": slides, "images": images}


def convert(paths: list[Path], target: str, outdir: Path, timeout: int) -> bool:
    cmd = ["soffice", "--headless", "--norestore", "--nolockcheck", "--nodefault",
           "-env:UserInstallation=file:///tmp/msdoc_office_lo_profile",
           "--convert-to", target, "--outdir", str(outdir), *map(str, paths)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-f", "soffice"], check=False)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", choices=["xls", "ppt"])
    ap.add_argument("--split", default="dev")
    ap.add_argument("--budget", type=float, default=140)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--batch", type=int, default=8)
    a = ap.parse_args()
    d = data_dir() / "office"
    manifest = load_json(d / "manifest.json", {})
    gold = d / "golden"
    gold.mkdir(parents=True, exist_ok=True)
    todo = []
    for fid, m in manifest.items():
        if m["kind"] != "normal":
            continue
        if a.ext and m["ext"] != a.ext:
            continue
        if a.ids is not None and a.ids and fid not in a.ids:
            continue
        if not a.ids and m["split"] != a.split:
            continue
        if (gold / f"{fid}.json").exists() and not a.force:
            continue
        todo.append(fid)
    t0 = time.time()
    done = 0
    tmp = Path(tempfile.mkdtemp(prefix="msdoc_office_", dir="/dev/shm" if Path("/dev/shm").exists() else None))
    try:
        while todo and time.time() - t0 < a.budget:
            ext = manifest[todo[0]]["ext"]
            batch = [f for f in todo if manifest[f]["ext"] == ext][: a.batch]
            todo = [f for f in todo if f not in batch]
            target = "ods" if ext == "xls" else "odp"
            out = tmp / "out"
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir()
            paths = [d / "corpus" / ext / manifest[f]["split"] / f"{f}.{ext}" for f in batch]
            left = a.budget - (time.time() - t0)
            ok = convert(paths, target, out, timeout=max(20, min(75, int(left))))
            missing = [f for f in batch if not (out / f"{f}.{target}").exists()]
            if missing and not ok:
                # one hung file killed the batch: retry the rest one by one
                for f in missing:
                    if time.time() - t0 > a.budget:
                        todo.append(f)
                        continue
                    convert([d / "corpus" / ext / manifest[f]["split"] / f"{f}.{ext}"], target, out, timeout=25)
            for f in batch:
                if f in todo:
                    continue
                o = out / f"{f}.{target}"
                if not o.exists():
                    save_json(gold / f"{f}.json", {"error": "libreoffice produced no output"})
                else:
                    try:
                        with zipfile.ZipFile(o) as zf:
                            g = (parse_ods if ext == "xls" else parse_odp)(zf.read("content.xml"), zf)
                    except Exception as e:  # corrupt output
                        g = {"error": f"{type(e).__name__}: {e}"}
                    save_json(gold / f"{f}.json", g)
                done += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"golden: {done} written, {len(todo)} left")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
