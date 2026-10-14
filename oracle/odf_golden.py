#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Golden output for .odt / .ods / .odp from LibreOffice's interpretation of the file.

    python3 oracle/make_golden.py --ext odt [--jobs 3] [--budget 150] [--ids ID ...] [--force]
    python3 oracle/make_golden.py --ext ods --file x.ods --print       # inspect one file
    python3 oracle/odf_golden.py --ext odp ...                         # same, directly

Resumable: ids that already have a golden file are skipped; --budget stops starting new batches
after N seconds, so a time-limited shell simply re-runs the command until "0 left".
All kinds are attempted (normal, template, fuzz, fail, encrypted, notodf). A normal/template file
that LibreOffice cannot load is re-labelled kind "fail" in the manifest ("lo_failed": true,
"kind_fetch": <old kind>).

Why not parse the original content.xml? The parser under test reads exactly that XML, so a golden
derived from it would be a tautology (it would agree with any parser that shares the walker's
mistakes and never check LibreOffice's semantics). Instead every file is LOADED by headless
LibreOffice and:
  (a) round-tripped by LibreOffice into the same format (odt->odt, ods->ods, odp->odp). The output
      is LibreOffice's own model written back: styles resolved and normalised, list/heading
      structure as LO understood it, spreadsheet display text re-formatted by LO's number
      formatter (with LO's recalculated formula results), repeat counts and hidden state as LO
      sees them, damaged parts dropped or repaired. That ODF is parsed by the existing walkers:
      make_golden.Walker (odt; fallback pictures skipped, see OdfWalker), office_golden.parse_ods,
      office_golden.parse_odp, plus links / comments / merges collected here;
  (b) exported by LibreOffice as plain text, an independent cross-check through a different
      export filter: odt -> "txt:Text (encoded):UTF8", ods -> CSV of every sheet with
      "save cell contents as shown" (filter options 44,34,76,1,,0,false,true,true,false,false,-1);
      odp has no text export filter and gets none. Stored as golden "plain"; "plain_agreement"
      is the similarity of (a) and (b) under the same text definition.
The SCORE is driven by (a): it carries everything that is scored (stories, notes, headers,
comments, pictures, structure, sheets, merges, slides, notes pages) while (b) only has the body
text (txt export adds list labels and field results, CSV has no names, hidden state or pictures).
compare.py reports the similarity of the parser's text to (b) as "plain", never in the score.

Golden JSON (data_dir()/odf/golden/<id>.json, pictures in golden/<id>/NN.ext):
  all:  {"status": "ok" | "lo_failed" | "too_large" (ods with > MAX_CELLS expanded non-empty cells:
          scored for robustness only), "ext", "images": [fingerprint + {kind, story, name, file}]}
  odt:  + "stories" {main, footnotes, endnotes, headers, comments, textboxes}, "struct" (see
          structscore.py), "links" [{url, text}], "plain" [line...], "plain_agreement"
  ods:  + "sheets" [{name, hidden, cells [[row, col, text]], merges [[row, col, rows, cols]],
          links [[row, col, url, text]]}], "plain" {"sheets": [{name, lines}]}, "plain_agreement"
  odp:  + "slides" [{name, texts, notes, comments, links [[text, url]]}]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import data_dir, fingerprint, load_json, norm_par, save_json  # noqa: E402
from make_golden import D, EXT, O, T, TB, Walker, collect_links, find_soffice, q  # noqa: E402
from office_golden import _rows, local, para_text, parse_odp, parse_ods  # noqa: E402

ODF_EXTS = ("odt", "ods", "odp")
MAX_CELLS = 1_000_000  # ods goldens with more expanded non-empty cells get status "too_large"
PLAIN_FILTER = {"odt": "txt:Text (encoded):UTF8",
                "ods": "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true,false,false,-1"}
OOO = "{http://openoffice.org/2009/office}"
PRES_NOTES = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}notes"

README_TEXT_DEFS = """## What is scored (exact text definitions; oracle/odf_score.py)

Golden = LibreOffice's round trip of the file (odt->odt, ods->ods, odp->odp), parsed; see
oracle/odf_golden.py for why the original content.xml is not used. Score per normal/template file =
0.7 * text + 0.3 * picture F1. Pictures: every draw:image of a draw:frame that is not an OLE object
(draw:object / draw:object-ole) and not a fallback (2nd+ draw:image in the same frame, LibreOffice's
PNG rendering of an SVG/metafile), fingerprinted like oracle/common.fingerprint (sha256, then 8x8
average hash for rasters, then vector-to-vector in order). odt: one per frame occurrence; ods/odp: one
per distinct Pictures/ href (TablePreview*.svm and Object* replacement images excluded).

- odt: exactly the .doc/.rtf definition (oracle/compare.py text_score): Document.stories
  main/footnotes/endnotes/headers/comments/textboxes, one string per text:p/text:h; text:s -> spaces,
  text:tab -> \\t, text:line-break -> \\n; list labels (text:number), note citations, bookmarks,
  reference marks, change marks and soft page breaks are dropped; field elements contribute their
  displayed content; table cells contribute their paragraphs row by row; text:tracked-changes,
  sequence/variable decls, forms and index *source* templates are skipped (index bodies are text).
  Headers/footers = every style:header/footer(-left/-first) of every master page in styles.xml.
  Text-box frames and shape text -> "textboxes". Main counts double; headers/comments/textboxes are
  compared as sets of unique normalised paragraphs. Structure (struct, reported + own ratchet):
  headings (outline level), list items (level, ordered by the list style's level type), tables
  (rows x cols, spans), picture anchors, link and note counts, from Document.blocks.
  Links: F1 of the URL multiset of text:a (body, notes, frames, headers/footers), reported.
- ods: Document.sheets, in order: Sheet.name, then one line per row = the non-empty cells of the
  row, column order, tab separated (oracle/office_compare.xls_lines). A cell's text = its text:p
  paragraphs joined by "\\n" (normalised to a space) = LibreOffice's DISPLAYED text (number
  formats, dates, booleans, percentages, currency and recalculated formula results such as
  Err:502 / #NAME? as LO shows them). Covered (merged-away) cells are empty. Repeats are expanded
  (number-columns/rows-repeated, at most 4096 repeats of a non-empty cell/row). A workbook whose
  expansion exceeds 1,000,000 non-empty cells gets golden status "too_large" (robustness only). Hidden sheets ARE
  included (Sheet.hidden must say so). Reported only: hidden-flag accuracy, merges F1 over
  (sheet, row, col, rows, cols), cell-link F1 over (sheet, row, col, url).
- odp: per slide (draw:page of office:presentation, in order) a line "--- slide N", the slide's
  paragraphs (every non-empty text:p/text:h of all shapes and frames, document order, tables
  cell by cell) followed by the comment paragraphs (office:annotation / officeooo:annotation;
  LibreOffice writes them after presentation:notes, so Slide.texts + Slide.comments matches the
  golden order), then "notes: <paragraph>" for each paragraph of presentation:notes
  (oracle/office_compare.ppt_lines). Master-page text (styles.xml) is NOT included. Reported only:
  comment similarity, link URL F1 (text:a and draw:a of the slide, not its notes).
- plain (cross-check, reported only): odt main story vs LibreOffice's txt export lines; ods
  workbook text vs LibreOffice's CSV export (same line definition, names from the golden).

### LibreOffice quirks parser authors must know
- ods display text is LibreOffice's: it re-formats every value with the cell's number style
  (office:value + style:data-style-name), so a file written by another producer with no or a stale
  text:p still gets formatted text in the golden. Formulas are LO's cached or recalculated values;
  error cells show Err:5xx / #DIV/0! etc. Booleans are TRUE/FALSE (locale en-US).
- ods: external-reference caches (table:table holding table:table-source mode="copy-results-only")
  are not sheets. Volatile functions (RAND, NOW, TODAY, ...) are recalculated on load, so their cells
  can never match (they are also the main reason for plain_agreement < 1).
- ods: LO trims trailing empty repeated rows/columns and expands nothing it does not need; huge
  repeats of empty cells never reach the golden. Cell annotations (comments) are not part of the text.
- Fields showing the file name/path (text:file-name, odp too) show the temporary path LibreOffice
  loaded the file from (.../rt/<id>.src); relative links are kept as written (the round trip is
  saved in the folder of the loaded copy).
- odt fields (page number/count, date/time not fixed, word count, file name, author) may be
  recomputed by LO on load; the stored element content of the original can differ.
- odt: LO may renumber/merge lists and drop empty text:list wrappers; text:number labels are never scored.
- odp: LO writes comments as officeooo:annotation (originals may use office:annotation); notes
  pages contain a draw:page-thumbnail and an (often empty) notes frame; an empty presentation
  becomes one empty slide.
- Templates (.ott/.ots/.otp, kind template) are loaded as documents; same golden format.
"""


# ------------------------------------------------------------------ parsing of LibreOffice's ODF

class OdfWalker(Walker):
    """make_golden.Walker, but the 2nd+ draw:image of a frame (LibreOffice's fallback rendering) is
    not a picture of its own (as office_golden.fallback_images does for ods/odp)."""

    def drawing(self, el, story: str) -> None:
        if el.tag == D("frame") and el.find(D("object")) is None and el.find(D("object-ole")) is None:
            anchor = el.get(q("text", "anchor-type"), "paragraph")
            kind = "inline" if anchor == "as-char" else "floating"
            first = True
            for ch in el:
                if ch.tag == D("image"):
                    if first:
                        self.image(ch, story, kind, el.get(D("name")))
                    first = False
                elif ch.tag == D("text-box"):
                    for b in ch:
                        self.block(b, "textboxes")
            return
        super().drawing(el, story)

    def _scan(self, el, c: dict) -> None:
        for ch in el:
            if ch.tag == D("frame") and ch.find(D("object")) is None and ch.find(D("object-ole")) is None:
                c["img"] += min(1, sum(1 for x in ch if x.tag == D("image")))
            else:
                super()._scan([ch], c)


def _sheet_tables(root):
    return [t for t in root.iter(TB("table")) if t.get(TB("name")) is not None]  # same set as parse_ods


def ods_links(root) -> list[list]:
    """Per sheet (parse_ods order): [[row, col, url, text]] of every text:a in a cell."""
    out = []
    for table in _sheet_tables(root):
        links = []
        r = 0
        for row in _rows(table):
            rrep = int(row.get(TB("number-rows-repeated"), "1"))
            c = 0
            for cell in row:
                t = local(cell.tag)
                if t not in ("table-cell", "covered-table-cell"):
                    continue
                crep = int(cell.get(TB("number-columns-repeated"), "1"))
                for a in cell.iter(T("a")):
                    href = a.get(q("xlink", "href"))
                    if href:
                        links.append([r, c, href, "".join(a.itertext())])
                c += crep
            r += rrep
        out.append(links)
    return out


def _is_annotation(el) -> bool:
    return el.tag in (O("annotation"), OOO + "annotation")


def odp_extras(root) -> list[dict]:
    """Per draw:page: comment paragraphs and links (text:a / draw:a outside presentation:notes)."""
    out = []
    for page in root.iter(D("page")):
        comments, links = [], []

        def walk(el, in_comment=False):
            for ch in el:
                if ch.tag == PRES_NOTES:
                    continue
                if _is_annotation(ch):
                    for p in ch.iter():
                        if p.tag in (T("p"), T("h")):
                            s = para_text(p)
                            if s.strip():
                                comments.append(s)
                    continue
                if ch.tag in (T("a"), D("a")) and ch.get(q("xlink", "href")):
                    links.append(["".join(ch.itertext()) if ch.tag == T("a") else "", ch.get(q("xlink", "href"))])
                walk(ch)
        walk(page)
        out.append({"comments": comments, "links": links})
    return out


def _blobs_for(z: zipfile.ZipFile, images: list[dict]) -> list[bytes | None]:
    by_sha = {}
    for n in z.namelist():
        if n.endswith("/") or n in ("content.xml", "styles.xml", "meta.xml", "settings.xml", "mimetype"):
            continue
        try:
            b = z.read(n)
        except Exception:
            continue
        import hashlib
        by_sha.setdefault(hashlib.sha256(b).hexdigest(), b)
    return [by_sha.get(im["sha256"]) for im in images]


def parse_roundtrip(ext: str, path: Path) -> tuple[dict, list]:
    with zipfile.ZipFile(path) as z:
        if ext == "odt":
            w = OdfWalker(z)
            w.run()
            return {"stories": w.stories, "images": w.images, "struct": w.struct, "links": collect_links(z)}, w.blobs
        content = z.read("content.xml")
        root = ET.fromstring(content)
        if ext == "ods":
            g = parse_ods(content, z)
            for s, lk in zip(g["sheets"], ods_links(root)):
                s["links"] = lk
            # LibreOffice's cache of external references ('file:///...'#Sheet1) is written as a hidden
            # table:table with <table:table-source table:mode="copy-results-only">: not a sheet of the file
            ext_cache = [any(c.tag == TB("table-source") and c.get(TB("mode")) == "copy-results-only" for c in t)
                         for t in _sheet_tables(root)]
            g["sheets"] = [s for s, drop in zip(g["sheets"], ext_cache) if not drop]
        else:
            g = parse_odp(content, z)
            for s, ex in zip(g["slides"], odp_extras(root)):
                s.update(ex)
        g = {k: g[k] for k in (("sheets" if ext == "ods" else "slides"), "images")}
        for im in g["images"]:
            im.setdefault("kind", "floating")
            im.setdefault("story", "main")
        return g, _blobs_for(z, g["images"])


def plain_lines(ext: str, g: dict, txt: Path | None, csvs: list[tuple[str, Path]]):
    """-> (plain golden field, agreement with the round-trip golden under the same text definition)."""
    from compare import _str_sim
    if ext == "odt" and txt is not None and txt.exists():
        raw = txt.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        plain = [ln for ln in raw]
        a = "\n".join(x for x in (norm_par(p) for p in g["stories"]["main"]) if x)
        b = "\n".join(x for x in (norm_par(p) for p in plain) if x)
        return plain, (round(_str_sim(a, b), 4) if a or b else 1.0)
    if ext == "ods" and csvs:
        import csv
        from office_compare import xls_lines
        by_name = dict(csvs)
        sheets = []
        for s in g["sheets"]:
            p = by_name.get(s["name"])
            lines = None
            if p is not None and p.exists():
                with open(p, encoding="utf-8-sig", errors="replace", newline="") as f:
                    lines = ["\t".join(x for x in (norm_par(c) for c in row) if x) for row in csv.reader(f)]
                lines = [x for x in lines if x]
            sheets.append({"name": s["name"], "lines": lines})
        a = xls_lines(g["sheets"])
        b = plain_text_ods(sheets)
        return {"sheets": sheets}, (round(_str_sim(a, b), 4) if a or b else 1.0)
    return None, None


def plain_text_ods(sheets: list[dict]) -> str:
    out = []
    for s in sheets:
        out.append(norm_par(s["name"] or ""))
        out += s["lines"] or []
    return "\n".join(out)


# ------------------------------------------------------------------ LibreOffice

SOFFICE = find_soffice()


def soffice(paths: list[Path], target: str, outdir: Path, profile: Path, timeout: float) -> tuple[bool, str]:
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
           f"-env:UserInstallation={profile.as_uri()}", "--convert-to", target, "--outdir", str(outdir), *map(str, paths)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, text=True,
                         errors="replace")
    try:
        out, _ = p.communicate(timeout=timeout)
        return True, out
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        return False, out or ""


_SHEET_RE = re.compile(r"^Writing sheet (.*) -> (.*\.csv)\s*$", re.M)


def golden_batch(ext: str, items: list[tuple[str, Path]], work: Path, profile: Path, timeout: float):
    """-> {fid: (golden, blobs)} for every item."""
    rt, pl = work / "rt", work / "plain"
    shutil.rmtree(work, ignore_errors=True)
    # Each input is copied as rt/<id>.src and LibreOffice writes rt/<id>.<ext> into the SAME directory
    # (type detection is by content): relative hyperlinks / linked images ("../x.png") are stored
    # relative to the saved file, so they only survive unchanged when input and output share a folder.
    rt.mkdir(parents=True)
    staged = []
    for f, p in items:
        shutil.copyfile(p, rt / f"{f}.src")
        staged.append((f, rt / f"{f}.src"))
    items = staged
    paths = [p for _, p in items]
    ok, _ = soffice(paths, ext, rt, profile, timeout)
    missing = [(f, p) for f, p in items if not (rt / f"{f}.{ext}").exists()]
    for f, p in missing:  # one bad file can stop or hang a batch: retry alone
        soffice([p], ext, rt, profile, min(timeout, 30))
    csvs: dict[str, list] = {}
    if ext in PLAIN_FILTER:
        have = [p for f, p in items if (rt / f"{f}.{ext}").exists()]
        if have:
            ok2, log = soffice(have, PLAIN_FILTER[ext], pl, profile, timeout)
            for name, path in _SHEET_RE.findall(log):
                stem = Path(path).name
                for f, _ in items:
                    if stem.startswith(f + "-"):
                        csvs.setdefault(f, []).append((name, Path(path)))
                        break
    res = {}
    for f, p in items:
        o = rt / f"{f}.{ext}"
        if not o.exists() or o.stat().st_size == 0:
            res[f] = ({"status": "lo_failed", "ext": ext, "error": "LibreOffice produced no output", "images": []}, [])
            continue
        try:
            g, blobs = parse_roundtrip(ext, o)
        except Exception as e:  # LO wrote something we cannot parse
            res[f] = ({"status": "lo_failed", "ext": ext, "error": f"{type(e).__name__}: {e}"[:300], "images": []}, [])
            continue
        g = {"status": "ok", "ext": ext, **g}
        ncells = sum(len(sh["cells"]) for sh in g.get("sheets", []))
        if ncells > MAX_CELLS:  # e.g. a non-empty row repeated to row 1048576: robustness-only file
            res[f] = ({"status": "too_large", "ext": ext, "error": f"{ncells} non-empty cells > {MAX_CELLS}",
                       "images": []}, [])
            continue
        try:
            plain, agree = plain_lines(ext, g, pl / f"{f}.txt", csvs.get(f, []))
        except Exception as e:
            plain, agree = None, None
            g["plain_error"] = f"{type(e).__name__}: {e}"[:200]
        if ext in PLAIN_FILTER:
            g["plain"], g["plain_agreement"] = plain, agree
        res[f] = (g, blobs)
    return res


def write_golden(gold: Path, fid: str, g: dict, blobs: list) -> None:
    gdir = gold / fid
    shutil.rmtree(gdir, ignore_errors=True)
    for i, (meta, blob) in enumerate(zip(g["images"], blobs)):
        if not blob:
            continue
        gdir.mkdir(parents=True, exist_ok=True)
        fn = f"{i:02d}.{EXT.get(meta['mime'], 'bin')}"
        (gdir / fn).write_bytes(blob)
        meta["file"] = f"{fid}/{fn}"
    save_json(gold / f"{fid}.json", g)


def relabel_failures(d: Path, ext: str) -> int:
    """kind normal/template + golden lo_failed -> kind fail (kind_fetch keeps the old kind); undone
    when a later golden (--force) is ok. Idempotent; derived from the golden files on disk."""
    man_path = d / "manifest.json"
    manifest = load_json(man_path, {})
    changed = n = 0
    for fid, m in manifest.items():
        if m["ext"] != ext:
            continue
        g = load_json(d / "golden" / f"{fid}.json")
        if g is None:
            continue
        if g["status"] == "lo_failed" and m["kind"] in ("normal", "template"):
            m.update(kind="fail", lo_failed=True, kind_fetch=m["kind"])
            changed += 1
        elif g["status"] != "lo_failed" and m.get("lo_failed"):
            m["kind"] = m.pop("kind_fetch", "normal")
            m.pop("lo_failed", None)
            changed += 1
        n += bool(m.get("lo_failed"))
    if changed:
        save_json(man_path, manifest)
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ext", required=True, choices=ODF_EXTS)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=45, help="seconds per LibreOffice batch (a single-file retry gets min(this, 30))")
    ap.add_argument("--budget", type=float, default=150, help="stop starting new batches after N seconds")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--kind", nargs="*", help="only these manifest kinds (default: all)")
    ap.add_argument("--file", type=Path, help="golden for one arbitrary file, printed")
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args(argv)
    if SOFFICE is None:
        sys.exit("LibreOffice not found: put soffice on PATH or set SOFFICE=/path/to/soffice")
    profiles = Path(tempfile.gettempdir()) / "msdoc-odf-lo-profiles"
    scratch = Path(tempfile.mkdtemp(prefix="msdoc-odf-"))
    try:
        if a.file:
            tmp = scratch / "in" / f"one.{a.ext}"
            tmp.parent.mkdir(parents=True)
            shutil.copyfile(a.file, tmp)
            g, _ = golden_batch(a.ext, [("one", tmp)], scratch / "w", profiles / "p0", a.timeout)["one"]
            print(json.dumps(g, ensure_ascii=False, indent=1))
            return 0
        d = data_dir() / "odf"
        man_path = d / "manifest.json"
        manifest = load_json(man_path, {})
        gold = d / "golden"
        gold.mkdir(parents=True, exist_ok=True)
        todo = []
        for fid, m in sorted(manifest.items()):
            if m["ext"] != a.ext or (a.ids and fid not in a.ids) or (a.kind and m["kind"] not in a.kind):
                continue
            if not a.force and (gold / f"{fid}.json").exists():
                continue
            todo.append((fid, d / "corpus" / a.ext / m["split"] / f"{fid}.{a.ext}"))
        batches = [todo[i:i + a.batch] for i in range(0, len(todo), a.batch)]
        print(f"{a.ext}: {len(todo)} files to convert, {len(batches)} batches, {a.jobs} LibreOffice workers", flush=True)
        t0 = time.monotonic()
        lock = threading.Lock()
        slots = list(range(a.jobs))
        stats = {"ok": 0, "lo_failed": 0}

        def work(batch):
            if time.monotonic() - t0 > a.budget:
                return
            with lock:
                slot = slots.pop()
            try:
                res = golden_batch(a.ext, batch, scratch / f"w{slot}", profiles / f"p{slot}", a.timeout)
            finally:
                with lock:
                    slots.append(slot)
            for fid, (g, blobs) in res.items():
                write_golden(gold, fid, g, blobs)
                with lock:
                    stats[g["status"]] = stats.get(g["status"], 0) + 1

        with ThreadPoolExecutor(a.jobs) as ex:
            list(ex.map(work, batches))
        relabelled = relabel_failures(d, a.ext)
        from odf_corpus import write_readme
        write_readme(data_dir(), load_json(man_path, {}))
        left = sum(1 for f, _ in todo if not (gold / f"{f}.json").exists())
        print(f"{a.ext}: written " + " ".join(f"{k}={v}" for k, v in stats.items()) + f" (manifest kind fail: {relabelled} LibreOffice-unreadable normal/template files); "
              f"{left} left ({time.monotonic() - t0:.0f}s)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
