#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Извлечь текст и картинки из всех .doc, .xls и .ppt в каталоге (с жёстким таймаутом на файл).

    python3 examples/extract_folder.py ВХОДНОЙ_КАТАЛОГ ВЫХОДНОЙ_КАТАЛОГ [--jobs 4] [--timeout 60] [--no-images]

Для каждого файла <rel/path/name.doc> создаётся каталог ВЫХОДНОЙ/rel__path__name/:
    text.txt        основной текст (по абзацу в строке)
    stories.json    все истории: main, footnotes, endnotes, headers, comments, textboxes
    images/NN.ext   картинки в исходном формате
Общий отчёт: ВЫХОДНОЙ/report.jsonl (одна строка JSON на файл: status, ошибка, число картинок).

Каждый файл обрабатывается в отдельном процессе: зависание или нехватка памяти на одном
файле не останавливают пакетную обработку, а таймаут убивает процесс.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SUFFIXES = {".doc", ".xls", ".ppt"}
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tif",
       "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct"}


def one(path: Path, out: Path, with_images: bool) -> dict:
    """Обработать один файл в текущем процессе; вернуть строку отчёта."""
    import msdoc

    res = {"file": str(path), "status": "ok"}
    try:
        doc = msdoc.extract(path, images_enabled=with_images)
    except msdoc.MsDocError as e:
        res.update(status=type(e).__name__, error=str(e)[:300])
        return res
    out.mkdir(parents=True, exist_ok=True)
    (out / "text.txt").write_text(doc.text, encoding="utf-8")
    (out / "stories.json").write_text(json.dumps(doc.stories, ensure_ascii=False, indent=1), encoding="utf-8")
    if doc.images:
        (out / "images").mkdir(exist_ok=True)
    for i, im in enumerate(doc.images):
        (out / "images" / f"{i:02d}.{EXT.get(im.mime, 'bin')}").write_bytes(im.data)
    res.update(images=len(doc.images), chars=len(doc.text), warnings=doc.warnings[:5])
    return res


def run_isolated(path: Path, out: Path, args) -> dict:
    cmd = [sys.executable, __file__, "--one", str(path), "--out", str(out)]
    if args.no_images:
        cmd.append("--no-images")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        return {"file": str(path), "status": "timeout"}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"file": str(path), "status": "crash", "error": (p.stderr or "")[-300:]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, nargs="?")
    ap.add_argument("dst", type=Path, nargs="?")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60, help="секунд на файл")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one:  # режим рабочего процесса
        print(json.dumps(one(args.one, args.out, not args.no_images), ensure_ascii=False))
        return
    if not (args.src and args.dst):
        ap.error("нужны входной и выходной каталоги")

    files = sorted(p for p in args.src.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES)
    args.dst.mkdir(parents=True, exist_ok=True)

    def job(p: Path) -> dict:
        name = "__".join(p.relative_to(args.src).with_suffix("").parts) + ("" if p.suffix.lower() == ".doc" else p.suffix.lower().replace(".", "_"))
        return run_isolated(p, args.dst / name, args)

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(args.jobs) as ex, open(args.dst / "report.jsonl", "w", encoding="utf-8") as rep:
        for r in ex.map(job, files):
            rep.write(json.dumps(r, ensure_ascii=False) + "\n")
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{len(files)} файлов: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
