# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Render a msdoc.model.Document (its `blocks`) to Markdown.

Tables are emitted as HTML (they keep colspan/rowspan and nesting), pictures as
placeholders ``![Media N](image:N)`` where N is the 0-based index into
``Document.images``.
"""
from __future__ import annotations

import re
from html import escape as _esc

from .model import IMAGE_MARK, NOTE_MARK, Document, Paragraph, Table

_LINE_START = re.compile(r"^(\s*)(#{1,6}\s|[-+*]\s|\d+[.)]\s|>|```)")


def _inline(p: Paragraph, doc: Document, image_fmt: str, label: str, html: bool) -> str:
    text = p.text
    ins: dict[int, list[str]] = {}
    for l in p.links:
        if 0 <= l.start < l.end <= len(text) and l.url:
            if html:
                ins.setdefault(l.start, []).append('<a href="%s">' % _esc(l.url, quote=True))
                ins.setdefault(l.end, []).insert(0, "</a>")
            else:
                ins.setdefault(l.start, []).append("[")
                ins.setdefault(l.end, []).insert(0, "](%s)" % l.url.replace(")", "%29").replace(" ", "%20"))
    out: list[str] = []
    ik = nk = 0
    for i, ch in enumerate(text + "\0"):
        out.extend(ins.get(i, ()))
        if i == len(text):
            break
        if ch == IMAGE_MARK:
            n = p.images[ik] if ik < len(p.images) else None
            ik += 1
            out.append(image_fmt.format(n=n, label=label) if n is not None else "")
        elif ch == NOTE_MARK:
            if nk < len(p.notes):
                kind, n = p.notes[nk]
                out.append("[^%s%d]" % ("e" if kind == "endnote" else "", n))
            nk += 1
        elif html:
            out.append(_esc(ch, quote=False))
        else:
            out.append(ch)
    s = "".join(out)
    return s if html else s.replace("\t", " ").rstrip()


def _table(t: Table, doc: Document, fmt: str, label: str, depth: int = 0) -> str:
    pad = "  " * depth
    lines = [pad + "<table>"]
    for r in t.rows:
        lines.append(pad + " <tr>")
        for c in r.cells:
            tag = "th" if (c.header or r.header) else "td"
            attrs = ""
            if c.colspan > 1:
                attrs += ' colspan="%d"' % c.colspan
            if c.rowspan > 1:
                attrs += ' rowspan="%d"' % c.rowspan
            parts: list[str] = []
            for b in c.blocks:
                if isinstance(b, Table):
                    parts.append("\n" + _table(b, doc, fmt, label, depth + 2) + "\n" + pad + "  ")
                else:
                    s = _inline(b, doc, fmt, label, True)
                    if s.strip():
                        parts.append(s)
            inner = "<br>".join(parts) if parts else ""
            lines.append("%s  <%s%s>%s</%s>" % (pad, tag, attrs, inner, tag))
        lines.append(pad + " </tr>")
    lines.append(pad + "</table>")
    return "\n".join(lines)


def _para(p: Paragraph, doc: Document, fmt: str, label: str) -> str:
    s = _inline(p, doc, fmt, label, False)
    if not s.strip():
        return ""
    if p.heading:
        return "#" * min(max(p.heading, 1), 6) + " " + s.strip()
    if p.list_level is not None:
        ind = "  " * min(p.list_level, 8)
        mark = (p.list_number or "1.") if p.list_ordered else "-"
        if p.list_ordered and not mark.endswith((".", ")")):
            mark += "."
        return "%s%s %s" % (ind, mark, s.strip())
    if _LINE_START.match(s):
        s = "\\" + s.lstrip() if not s.startswith(" ") else s
    return s


def render_blocks(blocks, doc: Document, fmt: str, label: str) -> str:
    out: list[str] = []
    prev_list = False
    for b in blocks:
        if isinstance(b, Table):
            out.append(_table(b, doc, fmt, label))
            prev_list = False
            continue
        s = _para(b, doc, fmt, label)
        if not s:
            continue
        is_list = b.list_level is not None
        if is_list and prev_list and out:
            out[-1] += "\n" + s  # keep list items tight
        else:
            out.append(s)
        prev_list = is_list
    return "\n\n".join(out)


def to_markdown(doc: Document, *, image_format: str = "![{label} {n}](image:{n})",
                label: str = "Media", notes: bool = True, extra: bool = False) -> str:
    """Markdown text of the main story (+ footnotes/endnotes, optionally text boxes etc.)."""
    blocks = doc.blocks or [Paragraph(t) for t in doc.stories.get("main", [])]
    parts = [render_blocks(blocks, doc, image_format, label)]
    if notes:
        for key, prefix in (("footnotes", ""), ("endnotes", "e")):
            for i, note in enumerate(doc.notes.get(key) or [], 1):
                body = render_blocks(note, doc, image_format, label).strip().replace("\n\n", "\n    ")
                if body:
                    parts.append("[^%s%d]: %s" % (prefix, i, body))
    if extra:
        for key, title in (("textboxes", "Надписи"), ("comments", "Примечания"), ("headers", "Колонтитулы")):
            body = render_blocks(doc.extra.get(key) or [], doc, image_format, label)
            if body:
                parts.append("---\n**%s**\n\n%s" % (title, body))
    return "\n\n".join(p for p in parts if p).strip() + "\n"
