#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Run the extractor on ONE file in a fresh process and print JSON to stdout.

Isolated per file so that hangs (killed by compare.py's timeout) and
interpreter-level crashes are attributed to the right document.

    python3 oracle/run_one.py FILE [--impl msdoc:extract] [--dump DIR]
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from common import fingerprint  # noqa: E402

sys.setrecursionlimit(5000)


def _links(doc) -> list:
    """URLs of Paragraph.links in blocks, notes and extra (text boxes, comments, headers)."""
    out: list = []

    def walk(blocks):
        for b in blocks:
            if hasattr(b, "rows"):
                for row in b.rows:
                    for c in row.cells:
                        walk(c.blocks)
            else:
                out.extend({"url": ln.url, "text": b.text[ln.start:ln.end]} for ln in getattr(b, "links", []))

    walk(doc.blocks)
    for notes in (getattr(doc, "notes", None) or {}).values():
        for note in notes:
            walk(note)
    for blocks in (getattr(doc, "extra", None) or {}).values():
        walk(blocks)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--impl", default="msdoc:extract")
    ap.add_argument("--dump", type=Path)
    args = ap.parse_args()

    mod_name, fn_name = args.impl.split(":")
    t0 = time.perf_counter()
    res: dict = {"status": "ok"}
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        doc = fn(str(args.file))
        res["stories"] = {k: list(v) for k, v in doc.stories.items()}
        imgs = []
        for i, im in enumerate(doc.images):
            fp = fingerprint(bytes(im.data), getattr(im, "mime", None))
            fp.update({"kind": im.kind, "story": getattr(im, "story", "main"), "declared_mime": getattr(im, "mime", None)})
            imgs.append(fp)
            if args.dump:
                args.dump.mkdir(parents=True, exist_ok=True)
                (args.dump / f"{i:02d}.{fp['mime'].split('/')[-1].replace('x-', '')}").write_bytes(bytes(im.data))
        res["images"] = imgs
        if getattr(doc, "blocks", None) is not None:
            from structscore import blocks_to_neutral
            res["struct"] = blocks_to_neutral(doc.blocks)
            res["links"] = _links(doc)
        # Spreadsheets / presentations (xls, ppt, ods, odp): only emitted when present, so the output
        # for .doc/.rtf is unchanged. Scored by oracle/odf_score.py for ods/odp.
        if getattr(doc, "sheets", None):
            res["sheets"] = [{"name": sh.name, "hidden": bool(sh.hidden),
                              "cells": [[r, c, t] for (r, c), t in sorted(sh.cells.items())],
                              "merges": [list(mg) for mg in sh.merges],
                              "links": [[r, c, u] for (r, c), u in sorted(getattr(sh, "links", {}).items())]}
                             for sh in doc.sheets]
        if getattr(doc, "slides", None):
            res["slides"] = [{"texts": list(sl.texts), "notes": list(sl.notes),
                              "comments": list(getattr(sl, "comments", [])),
                              "links": [[t, u] for t, u in getattr(sl, "links", [])]} for sl in doc.slides]
        res["warnings"] = list(getattr(doc, "warnings", []))[:50]
    except NotImplementedError as e:
        res = {"status": "not_implemented", "error": str(e)}
    except Exception as e:
        try:
            import msdoc
            is_lib_error = isinstance(e, msdoc.MsDocError)
        except Exception:
            is_lib_error = False
        res = {"status": "error" if is_lib_error else "crash", "error_type": type(e).__name__,
               "error": str(e)[:500], "traceback": traceback.format_exc()[-3000:]}
        if is_lib_error and "internal error:" in str(e):
            res["status"] = "crash"  # wrapped internal exception from api.py guard
    res["elapsed"] = round(time.perf_counter() - t0, 3)
    sys.stdout.write(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
