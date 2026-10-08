# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Structure score: compares the block structure of the main story with LibreOffice's.

Neutral block format (JSON; produced by make_golden.Walker and by run_one for msdoc):
    {"t": "p", "h": 0..9, "l": null | [level, ordered], "img": n, "notes": n, "links": n}
    {"t": "tbl", "rows": [[{"cs": 1, "rs": 1, "b": [blocks]}, ...], ...]}
A merged-away (covered) cell is NOT listed (HTML semantics).

Tokens (plain paragraphs produce none): H<level>, L<level><o|u>, I (one picture),
T<rows>x<cols>[s<n merged cells>] followed by the tokens of the cell contents.
score = 0.8 * SequenceMatcher(tokens) + 0.1 * link-count ratio + 0.1 * note-ref ratio
(a component where both sides have 0 is dropped and the weights renormalised).
"""
from __future__ import annotations

import difflib

MAX_TOKENS = 20000


def table_shape(rows: list) -> tuple[int, int, int]:
    carry: dict[int, int] = {}
    width = spans = 0
    for row in rows:
        occupied = {c for c, v in carry.items() if v > 0}
        col = 0
        new: dict[int, int] = {}
        for cell in row:
            while col in occupied:
                col += 1
            cs, rs = max(1, cell.get("cs", 1)), max(1, cell.get("rs", 1))
            if cs > 1 or rs > 1:
                spans += 1
            if rs > 1:
                for c in range(col, col + cs):
                    new[c] = rs - 1
            col += cs
        for c in occupied:
            carry[c] -= 1
        carry.update(new)
        width = max(width, col, (max(occupied) + 1) if occupied else 0)
    return len(rows), width, spans


def tokens(blocks: list, out: list | None = None, counts: dict | None = None) -> tuple[list, dict]:
    out = [] if out is None else out
    counts = {"links": 0, "notes": 0} if counts is None else counts
    for b in blocks:
        if len(out) > MAX_TOKENS:
            break
        if b.get("t") == "tbl":
            r, c, s = table_shape(b["rows"])
            out.append(f"T{r}x{c}" + (f"s{s}" if s else ""))
            for row in b["rows"]:
                for cell in row:
                    tokens(cell.get("b", []), out, counts)
            continue
        if b.get("h"):
            out.append(f"H{b['h']}")
        elif b.get("l"):
            out.append(f"L{b['l'][0]}{'o' if b['l'][1] else 'u'}")
        out.extend(["I"] * b.get("img", 0))
        counts["links"] += b.get("links", 0)
        counts["notes"] += b.get("notes", 0)
    return out, counts


def _ratio(a: int, b: int):
    return None if a == 0 and b == 0 else min(a, b) / max(a, b)


def struct_score(g: list, r: list) -> float:
    gt, gc = tokens(g)
    rt, rc = tokens(r)
    parts = []
    if gt or rt:
        parts.append((0.8, difflib.SequenceMatcher(None, gt[:MAX_TOKENS], rt[:MAX_TOKENS], autojunk=False).ratio()))
    else:
        parts.append((0.8, 1.0))
    for k in ("links", "notes"):
        v = _ratio(gc[k], rc[k])
        if v is not None:
            parts.append((0.1, v))
    return sum(w * s for w, s in parts) / sum(w for w, _ in parts)


def blocks_to_neutral(blocks) -> list:
    """msdoc.model blocks -> neutral JSON (duck-typed, no import of msdoc needed)."""
    out = []
    for b in blocks:
        if hasattr(b, "rows"):
            out.append({"t": "tbl", "rows": [[{"cs": c.colspan, "rs": c.rowspan, "b": blocks_to_neutral(c.blocks)}
                                               for c in row.cells] for row in b.rows]})
        else:
            out.append({"t": "p", "h": b.heading,
                        "l": None if b.list_level is None else [b.list_level, bool(b.list_ordered)],
                        "img": len(b.images), "notes": len(b.notes), "links": len(b.links)})
    return out
