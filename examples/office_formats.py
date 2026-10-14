#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Structured access to .xls, .ppt and .rtf.  /  Структурный доступ к .xls, .ppt и .rtf.

    python3 examples/office_formats.py book.xls
    python3 examples/office_formats.py deck.ppt
    python3 examples/office_formats.py letter.rtf
    python3 examples/office_formats.py book.xls --markdown      # the Markdown a model would receive

.xls  -> doc.sheets: name, hidden, cells {(row, col): text} (formatted as Excel shows it), merges.
.ppt  -> doc.slides: texts, notes, comments, links, images (indexes into doc.images).
.rtf  -> like .doc: doc.stories (main, footnotes, ...), doc.blocks (paragraphs, tables), doc.images.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import msdoc  # noqa: E402


def show(path: Path) -> None:
    doc = msdoc.extract(path)
    print(f"{path.name}: format={doc.format}, images={len(doc.images)}, warnings={doc.warnings}")
    for sh in doc.sheets:
        print(f"\n[sheet] {sh.name}{' (hidden)' if sh.hidden else ''}, {len(sh.cells)} cells, merges={sh.merges}")
        rows = sorted({r for r, _ in sh.cells})
        for r in rows[:20]:
            cols = sorted(c for rr, c in sh.cells if rr == r)
            print("  " + "\t".join(sh.cells[(r, c)] for c in cols))
    if doc.format == "rtf":
        kinds = {}
        for b in doc.blocks:
            kinds[type(b).__name__] = kinds.get(type(b).__name__, 0) + 1
        print("blocks:", kinds)
        for name, paras in doc.stories.items():
            if paras:
                print(f"  story {name}: {len(paras)} paragraphs, first: {paras[0][:60]!r}")
    for s in doc.slides:
        print(f"\n[slide {s.index}] images={s.images}")
        for label, items in (("text", s.texts), ("notes", s.notes), ("comments", s.comments), ("links", s.links)):
            for it in items:
                print(f"  {label}: {it}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    try:
        if args.markdown:
            print(msdoc.to_markdown(args.path).text)
        else:
            show(args.path)
    except msdoc.EncryptedDocumentError:
        sys.exit("encrypted file: password-protected documents are not supported")
    except msdoc.UnsupportedFormatError as e:
        sys.exit(f"unsupported: {e}")


if __name__ == "__main__":
    main()
