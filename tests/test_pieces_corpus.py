# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Loose corpus check: golden main-story paragraphs appear, in order, in the
raw piece-table text. Special characters/fields are left to stories.py, so
this only checks aggregate coverage, not exact output."""
from __future__ import annotations

import re

import pytest

import common
from msdoc.pieces import PieceTable
from msdoc.props import CharProps
from wd_helpers import open_doc_streams

_MAX_FILES = 60
_MAX_PARS = 50
_CTRL = re.compile(r"[\x00-\x1f ]+")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", _CTRL.sub(" ", s)).strip()


def _candidates():
    d = common.data_dir()
    manifest = common.load_json(d / "corpus" / "manifest.json")
    if not manifest:
        return []
    out = []
    for fid, m in sorted(manifest.items()):
        if m["kind"] != "normal" or m["split"] != "dev":
            continue
        g = common.load_json(d / "golden" / f"{fid}.json")
        if not g or g.get("status") != "ok" or not g["stories"].get("main"):
            continue
        out.append((d / "corpus" / "dev" / f"{fid}.doc", g["stories"]["main"]))
        if len(out) >= _MAX_FILES:
            break
    return out


CANDIDATES = _candidates()


@pytest.mark.skipif(not CANDIDATES, reason="corpus/golden not available")
def test_golden_main_paragraphs_in_order():
    found = total = 0
    for path, golden_main in CANDIDATES:
        try:
            wd, table, fib = open_doc_streams(path)
        except Exception:
            continue  # container problems are cfb.py's concern
        fc, lcb = fib.fclcb.get("Clx", (0, 0))
        pt = PieceTable(table, fc, lcb, wd)
        text = _norm(pt.text(0, max(0, fib.ccp_text)))
        CharProps(fib, wd, table, pt).sprms_at(0)  # must not raise
        cursor = 0
        for par in golden_main[:_MAX_PARS]:
            p = _norm(par)
            if len(p) < 3:
                continue
            total += 1
            pos = text.find(p, cursor)
            if pos >= 0:
                found += 1
                cursor = pos + len(p)
    assert total > 100
    assert found / total >= 0.8, f"{found}/{total} golden paragraphs found in order"
