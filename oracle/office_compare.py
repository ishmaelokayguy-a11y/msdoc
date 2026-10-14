#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Score msdoc on .xls / .ppt against the LibreOffice golden, with a no-regression ratchet.

    python3 oracle/office_compare.py --ext xls                 # dev split, writes reports/office-xls.md
    python3 oracle/office_compare.py --ext ppt --ids ID ...    # subset
    python3 oracle/office_compare.py --ext xls --diff ID       # sheet/slide diff for one file
    python3 oracle/office_compare.py --ext xls --check         # fail if any file is below the baseline
    python3 oracle/office_compare.py --ext xls --update-baseline   # integrator only (on the Mac!)
    python3 oracle/office_compare.py --ext xls --split holdout     # final evaluation

Score = 0.7 * text + 0.3 * images.
  xls text  = similarity of the workbook as text: per sheet "name" + one line per
              row of the non-empty cells (tab separated), sheets in order
  ppt text  = per slide: paragraphs of the slide, then of its notes
  images    = F1 of matched pictures (sha256, then perceptual hash, then vector order)
kind != normal: 1 if the expected MsDocError is raised (legacy -> UnsupportedFormatError,
encrypted -> EncryptedDocumentError, notole -> NotOleFileError) or, for fail, if nothing
crashes or hangs. Crash / timeout / non-MsDocError exception -> 0.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from common import data_dir, fingerprint, load_json, norm_par, save_json  # noqa: E402
from compare import _str_sim, image_score  # noqa: E402

TEXT_W, IMG_W = 0.7, 0.3
EPS = 1e-4
EXPECTED = {"legacy": "UnsupportedFormatError", "encrypted": "EncryptedDocumentError", "notole": "NotOleFileError"}
TIMEOUT = 60
import re  # noqa: E402
FUZZ_RE = re.compile(r"fuzz|crash|ofz|hang-|poc|clusterfuzz|/fail/|forcepoint", re.I)

CHILD = r'''
import json, sys, traceback
sys.path.insert(0, %(src)r); sys.path.insert(0, %(oracle)r)
from common import fingerprint
import msdoc
try:
    d = msdoc.extract(sys.argv[1])
except msdoc.MsDocError as e:
    print(json.dumps({"status": "error", "error_type": type(e).__name__, "error": str(e)[:200]})); sys.exit(0)
except BaseException as e:
    print(json.dumps({"status": "crash", "error_type": type(e).__name__, "error": traceback.format_exc()[-400:]})); sys.exit(0)
out = {"status": "ok", "warnings": d.warnings[:10], "images": [fingerprint(i.data, i.mime) for i in d.images]}
if d.format == "xls":
    out["sheets"] = [{"name": s.name, "hidden": s.hidden,
                      "cells": [[r, c, t] for (r, c), t in sorted(s.cells.items())],
                      "merges": [list(m) for m in s.merges]} for s in d.sheets]
else:
    out["slides"] = [{"name": None, "texts": s.texts + s.comments, "notes": s.notes} for s in d.slides]
print(json.dumps(out))
'''


def run_one(path: Path) -> dict:
    t0 = time.time()
    code = CHILD % {"src": str(REPO / "src"), "oracle": str(HERE)}
    try:
        p = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "seconds": TIMEOUT}
    try:
        r = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"status": "crash", "error_type": "NoOutput", "error": (p.stderr or p.stdout)[-300:]}
    r["seconds"] = round(time.time() - t0, 2)
    return r


# ------------------------------------------------------------------ scoring

def xls_lines(sheets: list[dict]) -> str:
    out = []
    for s in sheets:
        out.append(norm_par(s["name"] or ""))
        rows: dict[int, list] = {}
        for r, c, t in s["cells"]:
            rows.setdefault(r, []).append((c, norm_par(t)))
        for r in sorted(rows):
            line = "\t".join(t for _, t in sorted(rows[r]) if t)
            if line:
                out.append(line)
    return "\n".join(out)


def ppt_lines(slides: list[dict]) -> str:
    out = []
    for i, s in enumerate(slides, 1):
        out.append("--- slide %d" % i)
        out += [x for x in (norm_par(t) for t in s["texts"]) if x]
        out += [x for x in ("notes: " + norm_par(t) if norm_par(t) else "" for t in s["notes"]) if x]
    return "\n".join(out)


def text_score(ext: str, g: dict, r: dict) -> tuple[float, dict]:
    if ext == "xls":
        ga, ra = xls_lines(g["sheets"]), xls_lines(r["sheets"])
    else:
        ga, ra = ppt_lines(g["slides"]), ppt_lines(r["slides"])
    if not ga and not ra:
        return 1.0, {}
    return _str_sim(ga, ra), {"golden_chars": len(ga), "ours_chars": len(ra)}


def score_file(ext: str, m: dict, g: dict | None, r: dict) -> dict:
    st = r["status"]
    out = {"status": st, "kind": m["kind"]}
    if st in ("crash", "timeout"):
        out["score"] = 0.0
        out["note"] = (r.get("error_type", "") + ": " + r.get("error", "")).strip(": ")[:200]
        return out
    kind = m["kind"]
    if g is not None and "error" in g:
        kind = "fail"  # LibreOffice cannot open it either
    if kind == "fail":
        out["score"] = 1.0
        return out
    if st == "error" and FUZZ_RE.search(m["path"]):
        out["score"] = 1.0  # fuzzer-origin file: a clean MsDocError is an acceptable answer
        out["note"] = "fuzz: " + r.get("error_type", "")
        return out
    if kind in EXPECTED:
        out["score"] = 1.0 if (st == "error" and r.get("error_type") == EXPECTED[kind]) else 0.0
        if not out["score"]:
            out["note"] = "expected %s, got %s" % (EXPECTED[kind], r.get("error_type") or st)
        return out
    if st != "ok":
        out["score"] = 0.0
        out["note"] = "%s: %s" % (r.get("error_type"), r.get("error"))
        return out
    ts, tdet = text_score(ext, g, r)
    is_, idet = image_score(g["images"], r["images"])
    out.update({"text": round(ts, 4), "images": round(is_, 4), "score": round(TEXT_W * ts + IMG_W * is_, 4), "detail": {**tdet, "img": idet}})
    return out


def load_golden(d: Path, fid: str):
    return load_json(d / "golden" / f"{fid}.json")


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", required=True, choices=["xls", "ppt"])
    ap.add_argument("--split", default="dev", choices=["dev", "holdout"])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--diff")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--worst", type=int, default=15)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    d = data_dir() / "office"
    manifest = load_json(d / "manifest.json", {})
    if a.diff:
        m = manifest[a.diff]
        r = run_one(d / "corpus" / a.ext / m["split"] / f"{a.diff}.{a.ext}")
        g = load_golden(d, a.diff)
        return diff(a.ext, a.diff, m, g, r)
    ids = [f for f, m in manifest.items() if m["ext"] == a.ext and m["split"] == a.split]
    if a.ids:
        ids = [i for i in ids if i in a.ids]
    ids.sort()
    t0 = time.time()

    def work(fid):
        m = manifest[fid]
        r = run_one(d / "corpus" / a.ext / m["split"] / f"{fid}.{a.ext}")
        return fid, score_file(a.ext, m, load_golden(d, fid), r)

    with ThreadPoolExecutor(a.jobs) as ex:
        results = dict(ex.map(work, ids))
    scores = [v["score"] for v in results.values()]
    normal = [v["score"] for v in results.values() if v["kind"] == "normal" and "text" in v]
    mean = sum(scores) / len(scores) if scores else 0.0
    mean_n = sum(normal) / len(normal) if normal else 0.0
    base_p = d / f"baseline-{a.ext}.json"
    base = load_json(base_p, {})
    regress = [(f, base[f], v["score"]) for f, v in results.items() if f in base and v["score"] < base[f] - EPS]
    crashes = [f for f, v in results.items() if v["status"] in ("crash", "timeout")]
    save_json(REPO / "reports" / f"office-{a.ext}.json", results)
    write_report(a, results, manifest, mean, mean_n, t0)
    if not a.quiet:
        print(f"{a.ext} {a.split}: files={len(ids)} mean={mean:.4f} normal_mean={mean_n:.4f} crashes={len(crashes)} "
              f"regressions={len(regress)} ({time.time() - t0:.0f}s)")
        worst = sorted(results.items(), key=lambda kv: kv[1]["score"])[: a.worst]
        for f, v in worst:
            print(f"  {v['score']:.3f} {f} {manifest[f]['path'][-48:]} {v.get('note', '')[:80]} text={v.get('text')} img={v.get('images')}")
    if a.update_baseline:
        save_json(base_p, {f: v["score"] for f, v in results.items()})
        print("baseline updated:", base_p)
    if a.check and (regress or crashes):
        for f, b, s in regress:
            print(f"REGRESSION {f}: {b:.4f} -> {s:.4f}")
        for f in crashes:
            print("CRASH", f, results[f].get("note"))
        return 1
    return 0


def write_report(a, results, manifest, mean, mean_n, t0):
    lines = [f"# {a.ext} {a.split}: mean {mean:.4f}, normal {mean_n:.4f}", ""]
    for f, v in sorted(results.items(), key=lambda kv: kv[1]["score"])[:60]:
        lines.append(f"- {v['score']:.3f} `{f}` {manifest[f]['path']} {v.get('note', '')} text={v.get('text')} img={v.get('images')}")
    p = REPO / "reports" / f"office-{a.ext}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def diff(ext, fid, m, g, r) -> int:
    import difflib
    print(fid, m["path"], "kind", m["kind"], "status", r["status"], r.get("error", "")[:200])
    if g is None or "error" in (g or {}) or r["status"] != "ok":
        print("golden:", None if g is None else g.get("error"))
        return 0
    ga = (xls_lines(g["sheets"]) if ext == "xls" else ppt_lines(g["slides"])).splitlines()
    ra = (xls_lines(r["sheets"]) if ext == "xls" else ppt_lines(r["slides"])).splitlines()
    n = 0
    for line in difflib.unified_diff(ga, ra, "golden(LibreOffice)", "ours", lineterm="", n=1):
        print(line[:200])
        n += 1
        if n > 120:
            print("..."); break
    print("images:", image_score(g["images"], r["images"]))
    if r.get("warnings"):
        print("warnings:", r["warnings"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
