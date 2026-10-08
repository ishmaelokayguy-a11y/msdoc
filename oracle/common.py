# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Shared helpers for the oracle: data paths, text normalisation, image fingerprints."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STORY_KEYS = ("main", "footnotes", "endnotes", "headers", "comments", "textboxes")
# Stories compared as a set of unique normalised paragraphs (order and
# duplication are not meaningful, e.g. LibreOffice copies inherited headers).
UNORDERED_STORIES = {"headers", "comments", "textboxes"}


def data_dir() -> Path:
    """Shared data directory (corpus, golden, baseline, reports).

    Lives in the MAIN checkout so every git worktree (one per agent) sees the
    same corpus and baseline. Override with MSDOC_DATA.
    """
    env = os.environ.get("MSDOC_DATA")
    if env:
        return Path(env).resolve()
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(common).parent / "data"
    except Exception:
        return REPO / "data"


def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------- text

_SPACES = re.compile(r"\s+")
_DROP = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_TO_SPACE = dict.fromkeys(map(ord, "   \t\n\r\x0b\x0c"), " ")


def norm_par(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = s.translate(_DROP).translate(_TO_SPACE).replace("\x1e", "-")
    s = "".join(ch for ch in s if ch >= " " or ch == " ")
    return _SPACES.sub(" ", s).strip()


def norm_story(key: str, pars: list[str]) -> list[str]:
    out = [p for p in (norm_par(x) for x in pars) if p]
    if key in UNORDERED_STORIES:
        out = sorted(set(out))
    return out


# ---------------------------------------------------------------- images

def sniff_mime(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:2] == b"BM":
        return "image/bmp"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if b[:4] == b"\xd7\xcd\xc6\x9a" or b[:4] in (b"\x01\x00\x09\x00", b"\x02\x00\x09\x00"):
        return "image/x-wmf"
    if b[:4] == b"\x01\x00\x00\x00" and b[40:44] == b" EMF":
        return "image/x-emf"
    if b[:6] == b"VCLMTF":
        return "image/x-svm"
    if b[:5] == b"<?xml" or b[:4] == b"<svg":
        return "image/svg+xml"
    if len(b) > 0x20C and b[0x20A:0x20C] in (b"\x00\x11", b"\x11\x01"):
        return "image/x-pict"
    return "application/octet-stream"


RASTER = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff"}


def fingerprint(b: bytes, mime: str | None = None) -> dict:
    """sha256 + (for rasters) size and an 8x8 average hash."""
    sniffed = sniff_mime(b)
    if sniffed == "application/octet-stream" and mime:
        sniffed = mime
    fp = {"sha256": hashlib.sha256(b).hexdigest(), "mime": sniffed,
          "bytes": len(b), "w": None, "h": None, "ahash": None}
    if fp["mime"] in RASTER:
        try:
            from PIL import Image  # oracle-only dependency
            with Image.open(io.BytesIO(b)) as im:
                im.load()
                fp["w"], fp["h"] = im.size
                g = im.convert("L").resize((8, 8))
                px = list(g.getdata())
                avg = sum(px) / 64.0
                fp["ahash"] = int("".join("1" if p >= avg else "0" for p in px), 2)
        except Exception:
            pass
    return fp
