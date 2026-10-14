#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""RTF end to end without any input file: build a small RTF in code, extract it, print Markdown.
RTF end to end без входного файла: собираем маленький RTF в коде, извлекаем, печатаем Markdown.

    python3 examples/rtf_demo.py

Shows what the parser understands: code pages (\\'hh with \\fcharset), \\uN, paragraphs, a table,
a hyperlink field and a picture (a 1x1 PNG written as hex in a \\pict group).
"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import msdoc  # noqa: E402


def tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def build_rtf() -> bytes:
    privet = "".join("\\'%02x" % b for b in "Привет".encode("cp1251"))
    lines = [
        "{\\rtf1\\ansi\\ansicpg1251\\deff0{\\fonttbl{\\f0\\fcharset204 Arial;}}",
        "\\pard\\b Report\\b0\\par",
        privet + ", world \\u8364? \\par",
        "\\trowd\\cellx3000\\cellx6000 \\intbl A1\\cell B1\\cell\\row",
        "\\trowd\\cellx3000\\cellx6000 \\intbl A2\\cell B2\\cell\\row",
        '\\pard {\\field{\\*\\fldinst HYPERLINK "https://example.com"}{\\fldrslt site}}\\par',
        "{\\pict\\pngblip\\picw1\\pich1 " + tiny_png().hex() + "}",
        "\\par}",
    ]
    return "\n".join(lines).encode("ascii")


def main() -> None:
    data = build_rtf()
    doc = msdoc.extract(data)                       # bytes are accepted as well as a path
    print("format:", doc.format, "| images:", [(i.mime, len(i.data)) for i in doc.images])
    print("main story:", doc.stories["main"])
    print()
    print(msdoc.to_markdown(data).text)


if __name__ == "__main__":
    main()
