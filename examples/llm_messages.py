#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Turn a .doc/.xls/.ppt/.rtf into an OpenAI-style multimodal `content` list.  /  документ -> content для LLM.

    from llm_messages import to_openai_content
    content = to_openai_content("report.doc")
    messages = [{"role": "user", "content": [{"type": "text", "text": "Summarise:"}, *content]}]

The Markdown is split at every image placeholder; each raster picture becomes an
``image_url`` part (data: URI) right where it stood in the document, preceded by a
short text marker ``[Media N]`` so the model can refer to it. Vector pictures
(WMF/EMF/PICT) cannot be shown by most models; they stay in the text as
``[Media N: image/x-wmf, not shown]`` (rasterise them yourself if you need them).

    python3 examples/llm_messages.py report.doc             # print a summary of the parts
    python3 examples/llm_messages.py report.doc --captions  # text-only mode, see below

``--captions`` shows how to run a text-only model: ``caption(image_bytes, mime)`` is a stub
where you call your own vision model; its answer replaces the picture in the text.
"""
from __future__ import annotations

import base64
import re
import sys

import msdoc

PLACEHOLDER = re.compile(r"!\[[^\]]*\]\(image:(\d+)\)")
RASTER = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff"}


def to_openai_content(source, *, max_images: int = 20, max_bytes: int = 5_000_000) -> list[dict]:
    text, media = msdoc.to_markdown(source)
    parts: list[dict] = []
    shown = 0
    buf: list[str] = []
    pos = 0

    def flush():
        s = "".join(buf)
        if s.strip():
            parts.append({"type": "text", "text": s})
        buf.clear()

    for m in PLACEHOLDER.finditer(text):
        buf.append(text[pos:m.start()])
        pos = m.end()
        i = int(m.group(1))
        im = media[i] if i < len(media) else None
        if im is not None and im.mime in RASTER and len(im.data) <= max_bytes and shown < max_images:
            buf.append(f"[Media {i}]")
            flush()
            b64 = base64.b64encode(im.data).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{im.mime};base64,{b64}"}})
            shown += 1
        else:
            why = im.mime if im is not None else "missing"
            buf.append(f"[Media {i}: {why}, not shown]")
    buf.append(text[pos:])
    flush()
    return parts


def caption(data: bytes, mime: str) -> str:  # replace with a call to your vision model
    return f"image {mime}, {len(data)} bytes"


def to_text_with_captions(source) -> str:
    """Text-only variant: every picture is replaced by a caption you generate."""
    text, media = msdoc.to_markdown(source)
    return PLACEHOLDER.sub(lambda m: f"[Image: {caption(media[int(m.group(1))].data, media[int(m.group(1))].mime)}]"
                           if int(m.group(1)) < len(media) else m.group(0), text)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if "--captions" in sys.argv:
        print(to_text_with_captions(sys.argv[1]))
    else:
        for p in to_openai_content(sys.argv[1]):
            if p["type"] == "text":
                print("TEXT ", len(p["text"]), "chars:", p["text"][:70].replace("\n", " "))
            else:
                print("IMAGE", p["image_url"]["url"][:40], "...")
