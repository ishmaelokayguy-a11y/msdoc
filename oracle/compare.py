#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Score msdoc against the LibreOffice golden output, with a no-regression ratchet.

    python3 oracle/compare.py                    # score dev split, write reports/latest.{md,json}
    python3 oracle/compare.py --check            # fail (exit 1) if any file scores below baseline
    python3 oracle/compare.py --diff ID          # detailed story diff + image table for one file
    python3 oracle/compare.py --ids ID ID ...    # subset
    python3 oracle/compare.py --update-baseline  # integrator only: raise baseline to current scores
    python3 oracle/compare.py --split holdout    # integrator only: final evaluation

Per-file score = 0.7 * text + 0.3 * images (normal files).
  text   = length-weighted similarity over stories (main counts double);
           headers/comments/textboxes compared as sets of unique paragraphs
  images = F1 of matched images; match by sha256, else by perceptual hash
           (rasters), else pair remaining vector images in order (LibreOffice
           re-encodes most WMF into its own SVM format, so bytes can't match)
Special kinds: fail -> 1 if no crash/hang; legacy -> 1 for UnsupportedFormatError;
encrypted -> 1 for EncryptedDocumentError; notole -> 1 for NotOleFileError.
Crash (non-MsDocError exception), timeout, NotImplementedError -> 0.

RTF (--ext rtf; corpus from `office_fetch.py --ext rtf`, golden from `make_golden.py --ext rtf`)
uses the same scoring on data_dir()/rtf: corpus/<split>/<id>.rtf, golden/<id>.json,
baseline.json, struct_baseline.json; reports go to reports/rtf.{md,json}.

    python3 oracle/compare.py --ext rtf                  # dev split
    python3 oracle/compare.py --ext rtf --kind normal    # only normal files
    python3 oracle/compare.py --ext rtf --diff ID        # story / structure / image diff
    python3 oracle/compare.py --ext rtf --check          # ratchet vs data/rtf/baseline.json
    python3 oracle/compare.py --ext rtf --update-baseline   # integrator only

Extra RTF kinds: notrtf (named .rtf, not RTF) -> 1 unless crash/hang; fuzz (LibreOffice
crash-test / CVE samples) -> 1 for a clean MsDocError, else scored against the golden.
Hyperlinks: "links" = F1 of the link URLs (multiset) vs LibreOffice's text:a, reported
only (the link count is already part of the structure score).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from structscore import struct_score  # noqa: E402
from common import REPO, STORY_KEYS, UNORDERED_STORIES, data_dir, load_json, norm_story, save_json  # noqa: E402

TEXT_W, IMG_W = 0.7, 0.3
VECTOR = {"image/x-wmf", "image/x-emf", "image/x-pict", "image/x-svm", "image/svg+xml", "application/octet-stream"}
EPS = 1e-4

try:
    from rapidfuzz.distance import Levenshtein as _lev

    def _str_sim(a: str, b: str) -> float:
        return _lev.normalized_similarity(a, b)
except ImportError:  # pragma: no cover
    def _str_sim(a: str, b: str) -> float:
        if len(a) + len(b) > 40000:
            al, bl = a.split("\n"), b.split("\n")
            return difflib.SequenceMatcher(None, al, bl, autojunk=False).ratio()
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# ------------------------------------------------------------------ scoring

def text_score(g: dict, r: dict) -> tuple[float, dict]:
    num = den = 0.0
    per = {}
    for k in STORY_KEYS:
        gl = norm_story(k, g.get(k, []))
        rl = norm_story(k, r.get(k, []))
        ga, ra = "\n".join(gl), "\n".join(rl)
        if not ga and not ra:
            continue
        s = _str_sim(ga, ra)
        w = (len(ga) + len(ra)) * (2 if k == "main" else 1)
        per[k] = round(s, 4)
        num += s * w
        den += w
    return (num / den if den else 1.0), per


def _ahash_dist(a, b) -> int:
    return bin(a ^ b).count("1")


def image_score(gimgs: list[dict], rimgs: list[dict]) -> tuple[float, dict]:
    if not gimgs and not rimgs:
        return 1.0, {"golden": 0, "ours": 0}
    used = set()
    credit = 0.0
    matched = {"exact": 0, "perceptual": 0, "vector": 0}
    remaining = []
    for gi in gimgs:  # pass 1: exact bytes
        j = next((j for j, ri in enumerate(rimgs) if j not in used and ri["sha256"] == gi["sha256"]), None)
        if j is None:
            remaining.append(gi)
        else:
            used.add(j); credit += 1; matched["exact"] += 1
    rem2 = []
    for gi in remaining:  # pass 2: perceptual
        j = None
        if gi.get("ahash") is not None:
            best = None
            for jj, ri in enumerate(rimgs):
                if jj in used or ri.get("ahash") is None:
                    continue
                if gi["w"] and ri["w"] and abs(gi["w"] / gi["h"] - ri["w"] / ri["h"]) > 0.05 * (gi["w"] / gi["h"]):
                    continue
                d = _ahash_dist(gi["ahash"], ri["ahash"])
                if d <= 6 and (best is None or d < best[0]):
                    best = (d, jj)
            j = best[1] if best else None
        if j is None:
            rem2.append(gi)
        else:
            used.add(j); credit += 1; matched["perceptual"] += 1
    for gi in rem2:  # pass 3: vector <-> vector (LO may re-encode metafiles)
        if gi["mime"] not in VECTOR:
            continue
        j = next((j for j, ri in enumerate(rimgs) if j not in used and ri["mime"] in VECTOR), None)
        if j is not None:
            used.add(j); credit += 1; matched["vector"] += 1
    p = credit / len(rimgs) if rimgs else 0.0
    rcl = credit / len(gimgs) if gimgs else 0.0
    f1 = 2 * p * rcl / (p + rcl) if p + rcl else 0.0
    return f1, {"golden": len(gimgs), "ours": len(rimgs), "precision": round(p, 3), "recall": round(rcl, 3), **matched}


FUZZ_RE = re.compile(r"/ww8/pass/|ofz|hang-|forcepoint|crash|fuzz|poc|clusterfuzz", re.I)
EXPECTED_ERROR = {"legacy": "UnsupportedFormatError", "encrypted": "EncryptedDocumentError", "notole": "NotOleFileError"}


def score_file(m: dict, g: dict | None, r: dict) -> dict:
    st = r["status"]
    out = {"status": st, "kind": m["kind"]}
    if st in ("crash", "timeout", "not_implemented"):
        out["score"] = 0.0
        out["note"] = (r.get("error_type", "") + ": " + r.get("error", "")).strip(": ")[:200]
        return out
    kind = m["kind"]
    if kind == "fail":
        out["score"] = 1.0
        return out
    if kind == "notrtf" or (kind == "fuzz" and st == "error"):
        out["score"] = 1.0
        if st == "error":
            out["note"] = f"{r.get('error_type')}: {r.get('error', '')[:120]}"
        return out
    if kind in EXPECTED_ERROR and st == "error":
        out["score"] = 1.0 if r.get("error_type") == EXPECTED_ERROR[kind] else 0.5
        out["note"] = f"{r.get('error_type')}: {r.get('error', '')[:120]}"
        return out
    if g is None:
        out["score"] = None
        out["note"] = "no golden (run make_golden.py)"
        return out
    if g["status"] != "ok":
        out["score"] = 1.0 if st in ("ok", "error") else 0.0
        out["note"] = "LibreOffice could not open this file: robustness-only scoring"
        return out
    if st == "error":
        out["score"] = 0.0
        out["note"] = f"{r.get('error_type')}: {r.get('error', '')[:160]}"
        return out
    ts, per = text_score(g["stories"], r["stories"])
    im, idet = image_score(g["images"], r["images"])
    out.update(score=round(TEXT_W * ts + IMG_W * im, 4), text=round(ts, 4), stories=per, images=idet, img_f1=round(im, 4))
    # Structure (headings/lists/tables/picture anchors) is tracked separately: own baseline, not part of `score`.
    if g.get("struct") is not None and r.get("struct") is not None:
        out["struct"] = round(struct_score(g["struct"], r["struct"]), 4)
    if g.get("links") is not None and r.get("links") is not None:  # RTF goldens only
        out["links"] = round(link_f1(g["links"], r["links"]), 4)
    return out


def link_f1(g: list, r: list) -> float:
    from collections import Counter
    gc, rc = Counter(x["url"] for x in g), Counter(x["url"] for x in r)
    if not gc and not rc:
        return 1.0
    hit = sum((gc & rc).values())
    return 2 * hit / (sum(gc.values()) + sum(rc.values()))


class Layout:
    """Where a corpus lives: doc (data_dir()/corpus, golden, baseline.json) or rtf (data_dir()/rtf/...)."""

    def __init__(self, ext: str, d: Path):
        self.ext = ext
        if ext == "doc":
            self.manifest, self.corpus, self.golden = d / "corpus" / "manifest.json", d / "corpus", d / "golden"
            self.baseline, self.struct_baseline = d / "baseline.json", d / "struct_baseline.json"
        else:
            r = d / ext
            self.manifest, self.corpus, self.golden = r / "manifest.json", r / "corpus", r / "golden"
            self.baseline, self.struct_baseline = r / "baseline.json", r / "struct_baseline.json"

    def path(self, fid: str, m: dict) -> Path:
        return self.corpus / m["split"] / f"{fid}.{self.ext}"


# ------------------------------------------------------------------ running

def run_extractor(path: Path, impl: str, timeout: float, dump: Path | None = None) -> dict:
    cmd = [sys.executable, str(HERE / "run_one.py"), str(path), "--impl", impl]
    if dump:
        cmd += ["--dump", str(dump)]
    env = dict(os.environ, PYTHONPATH=str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f">{timeout}s", "elapsed": timeout}
    if p.returncode != 0 or not p.stdout:
        return {"status": "crash", "error_type": f"exit {p.returncode}", "error": p.stderr[-500:],
                "elapsed": round(time.perf_counter() - t0, 3)}
    return json.loads(p.stdout)


def select(manifest: dict, args) -> list[str]:
    ids = []
    for fid, m in sorted(manifest.items()):
        if args.ids and fid not in args.ids:
            continue
        if args.split != "all" and m["split"] != args.split:
            continue
        if args.kind and m["kind"] not in args.kind:
            continue
        ids.append(fid)
    return ids


def show_diff(fid: str, lay: "Layout", m: dict, impl: str, timeout: float) -> None:
    path = lay.path(fid, m)
    dump = REPO / "reports" / "dump" / fid
    r = run_extractor(path, impl, timeout, dump)
    g = load_json(lay.golden / f"{fid}.json")
    print(f"# {fid}  kind={m['kind']} source={m['source']}:{m['path']}")
    print(f"# file: {path}")
    print(json.dumps(score_file(m, g, r), ensure_ascii=False, indent=1))
    if r["status"] != "ok":
        print(r.get("traceback", r.get("error", "")))
        return
    if not g or g["status"] != "ok":
        return
    for k in STORY_KEYS:
        gl, rl = norm_story(k, g["stories"].get(k, [])), norm_story(k, r["stories"].get(k, []))
        if gl == rl:
            continue
        print(f"\n## story {k}{' (unordered)' if k in UNORDERED_STORIES else ''}: golden {len(gl)} pars, ours {len(rl)}")
        for line in difflib.unified_diff(gl, rl, "libreoffice", "msdoc", n=1, lineterm=""):
            print(line[:300])
    if g.get("struct") is not None and r.get("struct") is not None:
        from structscore import tokens
        gt, rt = tokens(g["struct"])[0], tokens(r["struct"])[0]
        if gt != rt:
            print(f"\n## structure tokens: golden {len(gt)}, ours {len(rt)}  (H=heading, L=list, T=table RxC, I=picture)")
            for line in difflib.unified_diff(gt, rt, "libreoffice", "msdoc", n=2, lineterm=""):
                print(line[:200])
    if g.get("links") is not None and r.get("links") is not None and sorted(x["url"] for x in g["links"]) != sorted(x["url"] for x in r["links"]):
        print("\n## links  (golden url | text)")
        for tag, lst in (("golden", g["links"]), ("ours", r["links"])):
            for ln in lst:
                print(f"  {tag:6} {ln['url'][:100]} | {ln['text'][:60]}")
    print("\n## images  (golden: %s/%s/, ours: reports/dump/%s/)" % (lay.golden, fid, fid))
    for tag, lst in (("golden", g["images"]), ("ours", r["images"])):
        for im in lst:
            print(f"  {tag:6} {im['kind']:8} {im['story']:9} {im['mime']:16} {im['bytes']:>8}B "
                  f"{(str(im['w']) + 'x' + str(im['h'])) if im.get('w') else '':>10} {im['sha256'][:12]} {im.get('file', '')}")


def write_report(results: dict, manifest: dict, label: str, elapsed: float, ext: str = "doc") -> Path:
    rep = REPO / "reports"
    rep.mkdir(exist_ok=True)
    save_json(rep / f"{label}.json", results)
    scored = {k: v for k, v in results.items() if v.get("score") is not None}
    mean = sum(v["score"] for v in scored.values()) / max(1, len(scored))
    by_status: dict[str, int] = {}
    for v in results.values():
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1
    lines = [f"# msdoc vs LibreOffice — {label}", "",
             f"files: {len(results)}, scored: {len(scored)}, mean score: **{mean:.4f}**, time {elapsed:.0f}s",
             "status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())), ""]
    by_kind: dict[str, list[float]] = {}
    for fid, v in scored.items():
        by_kind.setdefault(v["kind"], []).append(v["score"])
    lines += ["| kind | n | mean |", "|---|---|---|"]
    lines += [f"| {k} | {len(s)} | {sum(s) / len(s):.4f} |" for k, s in sorted(by_kind.items())]
    normal = [v for v in scored.values() if "text" in v]
    if normal:
        lines.append(f"\ntext mean {sum(v['text'] for v in normal) / len(normal):.4f}, "
                     f"image F1 mean {sum(v['img_f1'] for v in normal) / len(normal):.4f}")
        # Fuzzer-origin files (LibreOffice's ww8/pass, ofz*, hang-*, ...) are kept by LO to prove it does not
        # crash; LO's own text for them is a best-effort recovery, so report them separately.
        fz = [v for fid, v in scored.items() if "text" in v and FUZZ_RE.search(manifest.get(fid, {}).get("path", ""))]
        rest = [v for fid, v in scored.items() if "text" in v and not FUZZ_RE.search(manifest.get(fid, {}).get("path", ""))]
        if fz and rest:
            lines.append(f"text mean excluding fuzzer-origin files: {sum(v['text'] for v in rest) / len(rest):.4f} "
                         f"(n={len(rest)}); fuzzer-origin only: {sum(v['text'] for v in fz) / len(fz):.4f} (n={len(fz)})")
    st = [v["struct"] for v in scored.values() if "struct" in v]
    if st:
        lines.append(f"structure score mean {sum(st) / len(st):.4f} (n={len(st)}; headings/lists/tables/picture anchors vs LibreOffice)")
    lk = [v["links"] for v in scored.values() if "links" in v]
    if lk:
        lines.append(f"hyperlink URL F1 mean {sum(lk) / len(lk):.4f} (n={len(lk)})")
    lines += ["", "## worst files", "", "| id | kind | score | text | img | note |", "|---|---|---|---|---|---|"]
    worst = sorted(scored.items(), key=lambda kv: kv[1]["score"])[:40]
    for fid, v in worst:
        note = v.get("note") or (json.dumps(v.get("stories")) if v.get("stories") else "")
        img = v.get("images", {})
        imgs = f"{img.get('ours', '')}/{img.get('golden', '')} f1={v.get('img_f1', '')}" if img else ""
        lines.append(f"| {fid} | {v['kind']} | {v['score']:.3f} | {v.get('text', '')} | {imgs} | {note[:120]} |")
    lines += ["", "Inspect one file: `python3 oracle/compare.py%s --diff <id>`" % ("" if ext == "doc" else " --ext " + ext)]
    out = rep / f"{label}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--kind", nargs="*")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--impl", default="msdoc:extract", help="module:function returning a Document-like object")
    ap.add_argument("--check", action="store_true", help="exit 1 if any file regressed below baseline")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--diff", metavar="ID")
    ap.add_argument("--label", default="latest")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--ext", choices=["doc", "rtf"], default="doc", help="corpus to score (default doc)")
    args = ap.parse_args()

    d = data_dir()
    lay = Layout(args.ext, d)
    if args.ext != "doc" and args.label == "latest":
        args.label = args.ext
    manifest = load_json(lay.manifest)
    if not manifest:
        sys.exit(f"no corpus at {lay.manifest.parent} — run oracle/fetch_corpus.py (rtf: oracle/office_fetch.py --ext rtf) "
                 "and oracle/make_golden.py [--ext rtf]")
    if args.diff:
        show_diff(args.diff, lay, manifest[args.diff], args.impl, args.timeout)
        return
    if args.split == "holdout" and (args.check or args.update_baseline):
        sys.exit("--check/--update-baseline work on the dev split only")

    ids = select(manifest, args)
    t0 = time.perf_counter()

    def one(fid):
        m = manifest[fid]
        r = run_extractor(lay.path(fid, m), args.impl, args.timeout)
        g = load_json(lay.golden / f"{fid}.json")
        return fid, score_file(m, g, r)

    with ThreadPoolExecutor(args.jobs) as ex:
        results = dict(ex.map(one, ids))
    elapsed = time.perf_counter() - t0
    report = write_report(results, manifest, args.label if args.split == "dev" else f"{args.label}-{args.split}", elapsed, args.ext)

    scored = [v["score"] for v in results.values() if v.get("score") is not None]
    mean = sum(scored) / max(1, len(scored))
    if not args.quiet:
        print(report.read_text(encoding="utf-8"))
    else:
        print(f"mean score {mean:.4f} over {len(scored)} files ({elapsed:.0f}s); report: {report}")

    baseline_path = lay.baseline
    baseline = load_json(baseline_path, {})
    rc = 0
    # Integrator-only escape hatch: MSDOC_WAIVE_FUZZ=1 lets scores of fuzzer-origin files (where LibreOffice's
    # own output is best-effort garbage) go DOWN when a rule that was fitted to them is removed.
    waive = os.environ.get("MSDOC_WAIVE_FUZZ") == "1"
    is_fuzz = lambda fid: manifest.get(fid, {}).get("kind") == "fuzz" or bool(FUZZ_RE.search(manifest.get(fid, {}).get("path", "")))  # noqa: E731
    if args.check:
        regressions = [(fid, baseline[fid], v["score"]) for fid, v in results.items()
                       if fid in baseline and v.get("score") is not None and v["score"] < baseline[fid] - EPS
                       and not (waive and is_fuzz(fid))]
        if regressions:
            rc = 1
            print(f"RATCHET FAILED: {len(regressions)} file(s) scored below baseline:", file=sys.stderr)
            for fid, b, s in sorted(regressions, key=lambda x: x[2] - x[1])[:30]:
                print(f"  {fid}: baseline {b:.4f} -> now {s:.4f}  ({results[fid].get('note') or results[fid].get('stories')})",
                      file=sys.stderr)
            print("Fix the regressions (python3 oracle/compare.py --diff <id>) before finishing.", file=sys.stderr)
        elif not args.quiet:
            print("ratchet OK: no file below baseline")
    sb_path = lay.struct_baseline
    sbase = load_json(sb_path, {})
    if args.check and sbase:
        sreg = [(fid, sbase[fid], v["struct"]) for fid, v in results.items()
                if fid in sbase and "struct" in v and v["struct"] < sbase[fid] - EPS and not (waive and is_fuzz(fid))]
        if sreg:
            rc = 1
            print(f"STRUCT RATCHET FAILED: {len(sreg)} file(s) scored below struct baseline:", file=sys.stderr)
            for fid, b, sc in sorted(sreg, key=lambda x: x[2] - x[1])[:30]:
                print(f"  {fid}: struct baseline {b:.4f} -> now {sc:.4f}", file=sys.stderr)
            print("Fix with: python3 oracle/compare.py --diff <id> (see the 'structure' section).", file=sys.stderr)
    if args.update_baseline:
        for fid, v in results.items():
            if "struct" in v:
                sbase[fid] = v["struct"] if (waive and is_fuzz(fid)) else max(sbase.get(fid, 0.0), v["struct"])
        if sbase:
            save_json(sb_path, sbase)
            print(f"struct baseline updated: {len(sbase)} files")
        for fid, v in results.items():
            if v.get("score") is not None:
                baseline[fid] = v["score"] if (waive and is_fuzz(fid)) else max(baseline.get(fid, 0.0), v["score"])
        save_json(baseline_path, baseline)
        print(f"baseline updated: {len(baseline)} files, mean {sum(baseline.values()) / max(1, len(baseline)):.4f}")
    sys.exit(rc)


if __name__ == "__main__":
    main()