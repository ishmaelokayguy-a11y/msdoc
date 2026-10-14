# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Scoring of .odt / .ods / .odp against the LibreOffice golden (used by oracle/compare.py --ext).

    python3 oracle/compare.py --ext odt|ods|odp [--kind normal template] [--split dev|holdout]
    python3 oracle/compare.py --ext ods --diff ID
    python3 oracle/compare.py --ext odp --check              # ratchet vs data/odf/baseline-odp.json
    python3 oracle/compare.py --ext odt --update-baseline    # integrator: seed / raise the baseline

Golden: oracle/odf_golden.py (LibreOffice round trip of the file, parsed; plain-text export as a
cross-check). The exact text definitions are in odf_golden.README_TEXT_DEFS (also data/odf/README.md).

score (normal, template) = 0.7 * text + 0.3 * picture F1 (compare.image_score)
  odt text = compare.text_score over Document.stories (same as .doc/.rtf); + "struct"
             (structscore, own ratchet struct_baseline-odt.json) and "links" (URL F1), reported
  ods text = similarity of office_compare.xls_lines(Document.sheets) (per sheet: name, then one
             tab-joined line of non-empty displayed cell texts per row); reported: "hidden" (flag
             accuracy per sheet index), "merges" F1, "links" F1 (sheet, row, col, url)
  odp text = similarity of office_compare.ppt_lines(slides with texts = Slide.texts + Slide.comments)
             ("--- slide N", paragraphs, then "notes: ..." lines); reported: "comments" similarity,
             "links" URL F1
  all: "plain" = similarity of our text to LibreOffice's txt/CSV export (odt main story; ods
       workbook lines), reported only; null when the golden has no plain export.
other kinds: fail / notodf -> 1 unless crash, hang or NotImplementedError; encrypted -> 1 for
EncryptedDocumentError, 0.5 for another MsDocError, 0 if text was "extracted"; fuzz -> 1 for a
clean MsDocError, else scored like normal; a golden with status lo_failed (LibreOffice cannot load
the file) or too_large (ods expanding to > 1e6 non-empty cells) -> robustness only (1 for ok or MsDocError). crash / timeout / NotImplementedError -> 0.
"""
from __future__ import annotations

from collections import Counter

ODF_EXTS = ("odt", "ods", "odp")
TEXT_W, IMG_W = 0.7, 0.3


def _f1(g: Counter, r: Counter):
    if not g and not r:
        return None
    hit = sum((g & r).values())
    return round(2 * hit / (sum(g.values()) + sum(r.values())), 4)


def _sim(a: str, b: str) -> float:
    from compare import _str_sim
    if not a and not b:
        return 1.0
    return _str_sim(a, b)


def sheets_of(r: dict) -> list:
    return r.get("sheets") or []


def slides_of(r: dict) -> list:
    """Our slides with texts + comments, the order LibreOffice writes them (comments after shapes)."""
    return [{"name": None, "texts": list(s.get("texts", [])) + list(s.get("comments", [])),
             "notes": s.get("notes", [])} for s in (r.get("slides") or [])]


def text_of(ext: str, d: dict, ours: bool) -> str:
    from office_compare import ppt_lines, xls_lines
    if ext == "ods":
        return xls_lines(sheets_of(d))
    return ppt_lines(slides_of(d) if ours else d.get("slides", []))


def score_normal(ext: str, g: dict, r: dict) -> dict:
    from common import norm_par
    from compare import image_score, link_f1, text_score
    from structscore import struct_score
    out: dict = {}
    if ext == "odt":
        ts, per = text_score(g["stories"], r.get("stories", {}))
        out["stories"] = per
        if g.get("struct") is not None and r.get("struct") is not None:
            out["struct"] = round(struct_score(g["struct"], r["struct"]), 4)
        if g.get("links") is not None and r.get("links") is not None:
            out["links"] = round(link_f1(g["links"], r["links"]), 4)
        if g.get("plain") is not None:
            ours = "\n".join(x for x in (norm_par(p) for p in r.get("stories", {}).get("main", [])) if x)
            pl = "\n".join(x for x in (norm_par(p) for p in g["plain"]) if x)
            out["plain"] = round(_sim(pl, ours), 4)
    else:
        ga, ra = text_of(ext, g, False), text_of(ext, r, True)
        ts = _sim(ga, ra)
        out["chars"] = {"golden": len(ga), "ours": len(ra)}
        if ext == "ods":
            gs, rs = g["sheets"], sheets_of(r)
            n = max(len(gs), len(rs))
            if n:
                out["hidden"] = round(sum(1 for i in range(min(len(gs), len(rs)))
                                          if bool(gs[i]["hidden"]) == bool(rs[i].get("hidden"))) / n, 4)
            mg = _f1(Counter((i, *map(int, m)) for i, s in enumerate(gs) for m in s["merges"]),
                     Counter((i, *map(int, m)) for i, s in enumerate(rs) for m in s.get("merges", [])))
            lk = _f1(Counter((i, l[0], l[1], l[2]) for i, s in enumerate(gs) for l in s.get("links", [])),
                     Counter((i, l[0], l[1], l[2]) for i, s in enumerate(rs) for l in s.get("links", [])))
            if mg is not None:
                out["merges"] = mg
            if lk is not None:
                out["links"] = lk
            if g.get("plain"):
                from odf_golden import plain_text_ods
                if all(s["lines"] is not None for s in g["plain"]["sheets"]):
                    out["plain"] = round(_sim(plain_text_ods(g["plain"]["sheets"]), ra), 4)
        else:
            gc = "\n".join(norm_par(c) for s in g["slides"] for c in s.get("comments", []))
            rc = "\n".join(norm_par(c) for s in (r.get("slides") or []) for c in s.get("comments", []))
            if gc or rc:
                out["comments"] = round(_sim(gc, rc), 4)
            lk = _f1(Counter(l[1] for s in g["slides"] for l in s.get("links", [])),
                     Counter(l[1] for s in (r.get("slides") or []) for l in s.get("links", [])))
            if lk is not None:
                out["links"] = lk
    im, idet = image_score(g["images"], r.get("images", []))
    out.update(score=round(TEXT_W * ts + IMG_W * im, 4), text=round(ts, 4), images=idet, img_f1=round(im, 4))
    return out


def score_file(m: dict, g: dict | None, r: dict) -> dict:
    st = r["status"]
    kind = m["kind"]
    out = {"status": st, "kind": kind}
    err = f"{r.get('error_type')}: {r.get('error', '')[:120]}"
    if st in ("crash", "timeout", "not_implemented"):
        out["score"] = 0.0
        out["note"] = (r.get("error_type", "") + ": " + r.get("error", "")).strip(": ")[:200] or st
        return out
    if kind in ("fail", "notodf"):
        out["score"] = 1.0
        if st == "error":
            out["note"] = err
        return out
    if kind == "encrypted":
        if st == "error":
            out["score"] = 1.0 if r.get("error_type") == "EncryptedDocumentError" else 0.5
            out["note"] = err
        else:
            out["score"] = 0.0
            out["note"] = "expected EncryptedDocumentError, got a document"
        return out
    if kind == "fuzz" and st == "error":
        out["score"] = 1.0
        out["note"] = err
        return out
    if g is None:
        out["score"] = None
        out["note"] = "no golden (run make_golden.py --ext %s)" % m["ext"]
        return out
    if g["status"] != "ok":
        out["score"] = 1.0
        out["note"] = ("LibreOffice could not open this file" if g["status"] == "lo_failed" else
                       f"golden {g['status']}: {g.get('error', '')}") + ": robustness-only scoring"
        return out
    if st == "error":
        out["score"] = 0.0
        out["note"] = "not supported: " + err if r.get("error_type") == "NotOleFileError" else err
        return out
    out.update(score_normal(m["ext"], g, r))
    return out


def show_diff(ext: str, fid: str, m: dict, g: dict | None, r: dict, golden_dir) -> None:
    """--diff for ods/odp (odt uses compare.show_diff: stories, structure, links, images)."""
    import difflib
    import json
    print(f"# {fid}  ext={ext} kind={m['kind']} source={m['source']}:{m['path']}")
    print(json.dumps(score_file(m, g, r), ensure_ascii=False, indent=1))
    if r["status"] != "ok":
        print(r.get("traceback", r.get("error", "")))
        return
    if not g or g["status"] != "ok":
        return
    ga, ra = text_of(ext, g, False).splitlines(), text_of(ext, r, True).splitlines()
    if ga != ra:
        print(f"\n## text lines: golden {len(ga)}, ours {len(ra)}")
        for n, line in enumerate(difflib.unified_diff(ga, ra, "libreoffice", "msdoc", n=1, lineterm="")):
            print(line[:300])
            if n > 200:
                print("...")
                break
    if ext == "ods":
        for i, s in enumerate(g["sheets"]):
            rs = sheets_of(r)[i] if i < len(sheets_of(r)) else {}
            gm, rm = sorted(map(tuple, s["merges"])), sorted(map(tuple, rs.get("merges", [])))
            gl, rl = sorted((l[0], l[1], l[2]) for l in s.get("links", [])), sorted((l[0], l[1], l[2]) for l in rs.get("links", []))
            if bool(s["hidden"]) != bool(rs.get("hidden")) or gm != rm or gl != rl:
                print(f"\n## sheet {i} {s['name']!r}: hidden golden={s['hidden']} ours={rs.get('hidden')}")
                if gm != rm:
                    print("  merges golden-only:", sorted(set(gm) - set(rm))[:20], "ours-only:", sorted(set(rm) - set(gm))[:20])
                if gl != rl:
                    print("  links golden-only:", sorted(set(gl) - set(rl))[:20], "ours-only:", sorted(set(rl) - set(gl))[:20])
    else:
        gl = [l[1] for s in g["slides"] for l in s.get("links", [])]
        rl = [l[1] for s in (r.get("slides") or []) for l in s.get("links", [])]
        if sorted(gl) != sorted(rl):
            print("\n## links golden:", gl[:30], "\n   ours:", rl[:30])
    print(f"\n## images  (golden: {golden_dir}/{fid}/, ours: reports/dump/{fid}/)")
    for tag, lst in (("golden", g["images"]), ("ours", r.get("images", []))):
        for im in lst:
            print(f"  {tag:6} {im['mime']:16} {im['bytes']:>8}B "
                  f"{(str(im['w']) + 'x' + str(im['h'])) if im.get('w') else '':>10} {im['sha256'][:12]} {im.get('file', '')}")
