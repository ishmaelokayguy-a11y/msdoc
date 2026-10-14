#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Convert .doc, .xls and .ppt files to Markdown + media files.  /  Конвертация .doc в Markdown + картинки.

    python3 examples/to_markdown.py report.doc                # -> report.md and report_media/
    python3 examples/to_markdown.py docs_dir out_dir --jobs 4 # every .doc, .xls and .ppt under docs_dir
    python3 examples/to_markdown.py report.doc --stdout       # print Markdown only

Placeholders ``![Media N](image:N)`` in the text are rewritten to relative links
``![Media N](report_media/N.png)`` (use --keep-placeholders to leave them as they are).
Each file is converted in its own process with a hard timeout, so one broken file
cannot stop a batch.  /  Каждый файл — в отдельном процессе с таймаутом.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SUFFIXES = {".doc", ".xls", ".ppt"}
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tif",
       "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct"}
PLACEHOLDER = re.compile(r"!\[([^\]]*)\]\(image:(\d+)\)")


def convert(src: Path, dst_md: Path, keep_placeholders: bool = False, stdout: bool = False) -> str:
    import msdoc

    text, media = msdoc.to_markdown(src)
    if stdout:
        sys.stdout.write(text)
        return "ok"
    media_dir = dst_md.with_name(dst_md.stem + "_media")
    names = {}
    for i, im in enumerate(media):
        names[i] = f"{i}.{EXT.get(im.mime, 'bin')}"
    if media:
        media_dir.mkdir(parents=True, exist_ok=True)
        for i, im in enumerate(media):
            (media_dir / names[i]).write_bytes(im.data)
    if not keep_placeholders:
        text = PLACEHOLDER.sub(
            lambda m: f"![{m.group(1)}]({media_dir.name}/{names[int(m.group(2))]})" if int(m.group(2)) in names else m.group(0),
            text)
    dst_md.parent.mkdir(parents=True, exist_ok=True)
    dst_md.write_text(text, encoding="utf-8")
    return "ok"


def run_isolated(src: Path, dst_md: Path, args) -> tuple[Path, str]:
    cmd = [sys.executable, __file__, "--one", str(src), "--out", str(dst_md)]
    if args.keep_placeholders:
        cmd.append("--keep-placeholders")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return src, "timeout"
    if p.returncode != 0:
        return src, "error: " + (p.stderr.strip().splitlines() or ["?"])[-1][:200]
    return src, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, nargs="?")
    ap.add_argument("dst", type=Path, nargs="?", help="output directory (for a directory input)")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--keep-placeholders", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one:
        convert(args.one, args.out, args.keep_placeholders)
        return
    if not args.src:
        ap.error("source file or directory required")
    if args.src.is_file():
        if args.stdout:
            convert(args.src, Path("-"), stdout=True)
            return
        out = (args.dst / (args.src.stem + ".md")) if args.dst else args.src.with_suffix(".md")
        print(*run_isolated(args.src, out, args)[::-1])
        return
    if not args.dst:
        ap.error("output directory required for a directory input")
    files = sorted(p for p in args.src.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES)

    def job(p: Path):
        rel = p.relative_to(args.src)
        rel = rel.with_suffix(".md") if p.suffix.lower() == ".doc" else rel.with_name(rel.name + ".md")  # a.xls -> a.xls.md: no clash with a.doc
        return run_isolated(p, args.dst / rel, args)

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(args.jobs) as ex:
        for src, status in ex.map(job, files):
            key = status.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
            if status != "ok":
                print(f"{src}: {status}")
    print(f"{len(files)} files: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
