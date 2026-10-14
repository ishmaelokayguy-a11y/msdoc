# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file follow LibreOffice xmloff/source/draw (ximppage.cxx, ximpbody.cxx,
# ximpnote.cxx, shapeimport.cxx, ximpshap.cxx, ximplink.cxx) and sd/source/filter/xml (MPL-2.0).
# Those LibreOffice files incorporate work covered by the following notice:
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements. See the NOTICE file distributed
#   with this work for additional information regarding copyright
#   ownership. The ASF licenses this file to you under the Apache
#   License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of
#   the License at http://www.apache.org/licenses/LICENSE-2.0 .
"""OpenDocument presentation (.odp / .otp) -> Document(format="odp") with Document.slides.

One Slide per draw:page of office:presentation, in document order (hidden slides,
presentation:visibility="hidden", included: LibreOffice keeps them in the document).
Slide.texts: every paragraph of the page's shapes in document (= z) order: frames with
draw:text-box (titles, outline placeholders, text boxes), text of drawing shapes
(draw:custom-shape, draw:rect, ...), text of picture shapes, table:table frames cell by
cell, groups (draw:g) and shape links (draw:a) recursively. Slide.notes: paragraphs of
presentation:notes (the notes frame; the draw:page-thumbnail has no text).
Slide.comments: paragraphs of the page's office:annotation / officeooo:annotation.
Master pages (styles.xml) are not read: their text is not slide text.
Pictures: draw:image of draw:frame (best alternative of the frame, as LibreOffice's
MultiImageImportHelper picks it), no OLE/chart replacement images, one Document.images
entry per distinct picture (LibreOffice's graphic manager keeps one copy of equal data).
"""
from __future__ import annotations

import hashlib

from . import odf
from .errors import CorruptDocumentError, MsDocError
from .model import IMAGE_MARK, Document, Image, Link, Paragraph, Slide, Table

_MAX_SLIDES = 20000          # draw:page elements read
_MAX_SHAPES = 200000         # shapes walked in the whole document
_MAX_DEPTH = 64              # draw:g / draw:a nesting
_MAX_LINKS = 2000            # links kept per slide
_MAX_TEXT = 64 << 20         # characters of slide text emitted

_q = odf.qn
_NOTES = _q("presentation:notes")
_ANNOTATIONS = {_q("office:annotation"), _q("officeooo:annotation")}
_FRAME, _G, _DA = _q("draw:frame"), _q("draw:g"), _q("draw:a")
_TEXT_BOX, _IMAGE, _TABLE = _q("draw:text-box"), _q("draw:image"), _q("table:table")
_PLUGIN, _PAGE_NUMBER = _q("draw:plugin"), _q("text:page-number")
_EVENTS, _PRES_EVENT = _q("office:event-listeners"), _q("presentation:event-listener")
_OBJECTS = {_q("draw:object"), _q("draw:object-ole")}
# children of draw:frame that make it a shape (XMLShapeImportHelper::CreateFrameChildContext)
_FRAME_SHAPES = {_TEXT_BOX, _IMAGE, _TABLE} | _OBJECTS | {_q("draw:" + x) for x in ("plugin", "floating-frame", "applet")}
_TEXT_BLOCKS = {_q("text:p"), _q("text:h"), _q("text:list"), _q("text:numbered-paragraph")}
# annotation children that are not text (DrawAnnotationContext::createFastChildContext, ximppage.cxx)
_ANNOT_META = {_q("dc:creator"), _q("dc:date"), _q("meta:date-string"), _q("text:sender-initials"),
               _q("loext:sender-initials"), _q("meta:creator-initials")}
_TITLES = ("title",)


# namespaces LibreOffice's importer knows (xmloff/source/core/xmlimp.cxx SvXMLImport registers the ODF,
# OpenOffice.org and LibreOffice extension namespaces); see _unwrap_foreign
_KNOWN_NS = set(odf.NS.values()) | {
    "http://www.w3.org/1998/Math/MathML", "http://openoffice.org/2005/report", "http://openoffice.org/2004/writer",
    "http://openoffice.org/2004/calc", "http://www.w3.org/2001/xml-events", "http://www.w3.org/2002/xforms",
    "http://www.w3.org/2001/XMLSchema", "http://www.w3.org/2001/XMLSchema-instance",
    "urn:openoffice:names:experimental:ooxml-odf-interop:xmlns:form:1.0", "http://www.w3.org/2003/g/data-view#",
    "http://www.w3.org/TR/css3-text/", "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "urn:oasis:names:tc:opendocument:xmlns:database:1.0", "urn:org:documentfoundation:names:experimental:xforms:xmlns:xforms:1.0",
    "http://openoffice.org/2010/chart", "http://openoffice.org/2004/database", "http://openoffice.org/2004/form",
    "http://www.w3.org/2000/svg", "http://docs.oasis-open.org/ns/office/1.2/meta/pkg#",
    "urn:oasis:names:tc:opendocument:xmlns:smil-compatible:1.0"}


def _foreign(tag) -> bool:
    if not isinstance(tag, str):
        return False
    if tag[:1] != "{":
        return True
    return tag[1:tag.find("}")] not in _KNOWN_NS


def _unwrap_foreign(el, depth: int = 0) -> None:
    """Elements in a namespace LibreOffice does not know are transparent: SvXMLImport::
    startUnknownElement (xmlimp.cxx) keeps the current context on top for them, so their
    character data and children are read as if they were the parent's. Splice them out."""
    if depth > odf.MAX_XML_DEPTH + 2:
        return
    kids = list(el)
    if not kids:
        return
    if not any(_foreign(k.tag) for k in kids):
        for k in kids:
            _unwrap_foreign(k, depth + 1)
        return
    new: list = []
    text = el.text or ""

    def add_text(t):
        nonlocal text
        if not t:
            return
        if new:
            new[-1].tail = (new[-1].tail or "") + t
        else:
            text += t

    for k in kids:
        _unwrap_foreign(k, depth + 1)
        if _foreign(k.tag):
            add_text(k.text)
            for g in list(k):
                tail = g.tail
                g.tail = None
                new.append(g)
                add_text(tail)
            add_text(k.tail)
        else:
            new.append(k)
    el[:] = new
    el.text = text or None


class _OdpWalker(odf.Walker):
    def field_text(self, el) -> str:
        """Impress text fields: text:page-number is a field of the drawing layer
        (SvxPageField) that LibreOffice writes with its unexpanded presentation "<number>"
        (editeng SvxUnoTextField::getPresentation); every other field keeps the cached
        text, as for Writer."""
        if el.tag == _PAGE_NUMBER:
            return "<number>"
        return super().field_text(el)

    def drawing(self, el, story: str, text_story: str = "textboxes", depth: int = 0):
        """A frame or shape inside shape text: XMLParaContext creates it without a shapes
        collection to insert it into (txtparai.cxx, xShapes is empty), so Impress text, which
        has no anchored objects, does not show it."""
        return [], []


class _Ctx:
    """What one page (or its notes) produces: items [(block, presentation class)], picture
    indexes, shape-level links [(text, url)]."""
    __slots__ = ("items", "images", "loose", "seen")

    def __init__(self):
        self.items: list = []
        self.images: list = []
        self.loose: list = []
        self.seen: set = set()

    def add_image(self, i) -> None:
        if i is not None and i not in self.seen:
            self.seen.add(i)
            self.images.append(i)


class _Parser:
    def __init__(self, pkg: odf.Package, images_enabled: bool):
        self.pkg = pkg
        self.content = pkg.content()
        try:
            styles = pkg.styles()
        except MsDocError:
            styles = None
        self.doc = Document(format="odp")
        self.w = _OdpWalker(pkg, odf.StyleSheet(self.content, styles), self.doc, images_enabled=images_enabled)
        self.w.textbox_story = "main"
        self.images_enabled = images_enabled
        self.media: dict = {}       # sha1 of picture data -> index in doc.images
        self.href_cache: dict = {}  # package href -> index (or None)
        self.shapes = 0
        self.shape_warned = False
        self.chars = 0

    # ---- pictures
    def image(self, image_el, name):
        """Index in doc.images of a draw:image (deduplicated by content), or None."""
        if not self.images_enabled:
            return None
        href = odf.attr(image_el, "xlink:href")
        if href and href in self.href_cache:
            return self.href_cache[href]
        got = odf.image_bytes(self.pkg, image_el, self.w.warnings)
        idx = None
        if got is not None:
            data, mime = got
            key = hashlib.sha1(data).hexdigest()
            idx = self.media.get(key)
            if idx is None and len(self.doc.images) < 100000:
                idx = len(self.doc.images)
                self.media[key] = idx
                self.doc.images.append(Image(data=data, mime=mime, kind="floating", story="main", name=name))
        if href:
            self.href_cache[href] = idx
        return idx

    # ---- text
    def text_blocks(self, container, cls, ctx: _Ctx) -> None:
        """Paragraphs of shape text (EditEngine text: XMLTextType::Shape, no tables or frames)."""
        w = self.w
        out: list = []
        w.in_frame += 1
        try:
            for ch in container:
                if ch.tag in _TEXT_BLOCKS:
                    w.within("shape", w.block, ch, "main", out)
        finally:
            w.in_frame -= 1
        for b in out:
            ctx.items.append((b, cls))

    def table(self, el, cls, ctx: _Ctx) -> None:
        w = self.w
        w.in_frame += 1
        try:
            t = w.within("cell", w.table, el, "main")
        finally:
            w.in_frame -= 1
        ctx.items.append((t, cls))

    # ---- shapes
    def shapes_of(self, container, ctx: _Ctx, depth: int = 0) -> None:
        for el in container:
            self.shape(el, ctx, depth)

    def shape(self, el, ctx: _Ctx, depth: int = 0) -> None:
        tag = el.tag
        if not isinstance(tag, str) or depth > _MAX_DEPTH:
            return
        first = len(ctx.items)
        if tag == _G:
            if not self._count():
                return
            self.shapes_of(el, ctx, depth + 1)
        elif tag == _DA:
            # draw:a around shapes (SdXMLShapeLinkContext, ximplink.cxx): the shapes get a
            # click action to the URL
            self.shapes_of(el, ctx, depth + 1)
            self._link(odf.attr(el, "xlink:href"), el, ctx, first)
            return
        elif tag == _FRAME:
            if not self._count():
                return
            self.frame(el, ctx)
        elif tag in odf.SHAPES:
            if not self._count():
                return
            self.text_blocks(el, odf.attr(el, "presentation:class"), ctx)
        else:
            # draw:control, draw:page-thumbnail, dr3d:scene, presentation:animations, anim:*,
            # office:forms ... hold no slide text
            return
        # click action of the shape (presentation:event-listener, ODF 1.2 19.46 / 9.9.4,
        # sdxmlimp eventimp.cxx): "show" goes to a slide ("#name") or a document / URL
        evs = el.find(_EVENTS)
        if evs is not None:
            for ev in evs:
                if ev.tag == _PRES_EVENT and odf.attr(ev, "presentation:action") == "show":
                    self._link(odf.attr(ev, "xlink:href"), el, ctx, first)

    def _link(self, href, el, ctx: _Ctx, first: int) -> None:
        if not href or len(ctx.loose) >= _MAX_LINKS:
            return
        label = " ".join(odf.flatten([b for b, _c in ctx.items[first:]], self.w.synthetic))[:200].strip()
        ctx.loose.append((label or odf.attr(el, "draw:name") or href, href))

    def frame(self, el, ctx: _Ctx) -> None:
        """draw:frame (SdXMLFrameShapeContext::createFastChildContext, ximpshap.cxx): the first
        child that is a frame shape (XMLShapeImportHelper::CreateFrameChildContext) decides what the
        frame is. Further draw:image children are alternatives of a picture (the best one is kept,
        MultiImageImportHelper::solveMultipleImages) or the replacement graphic of an embedded
        object / media (not a picture of the slide); any other later shape child is ignored. So a
        table's preview picture and an object's (chart, formula, OLE) replacement are not
        pictures, and an embedded object gives no text."""
        cls = odf.attr(el, "presentation:class")
        kind = None
        for ch in el:
            if ch.tag in _FRAME_SHAPES:
                kind = ch
                break
        if kind is None:
            return
        tag = kind.tag
        if tag == _TEXT_BOX:
            self.text_blocks(kind, cls, ctx)
        elif tag == _IMAGE:
            best = odf.best_image([ch for ch in el if ch.tag == _IMAGE], self.pkg)
            i = self.image(best, odf.attr(el, "draw:name"))
            ctx.add_image(i)
            self.text_blocks(best, cls, ctx)
        elif tag == _TABLE:
            self.table(kind, cls, ctx)
        elif tag == _PLUGIN:
            # a media plugin takes its first following draw:image as its preview graphic
            # (bMedia -> mbSupportsReplacement), which LibreOffice shows and saves as a picture
            prev = next((ch for ch in el if ch.tag == _IMAGE), None)
            if prev is not None:
                i = self.image(prev, odf.attr(el, "draw:name"))
                ctx.add_image(i)

    def _count(self) -> bool:
        self.shapes += 1
        if self.shapes > _MAX_SHAPES:
            if not self.shape_warned:
                self.shape_warned = True
                self.w.warnings.append("presentation truncated after %d shapes" % _MAX_SHAPES)
            return False
        return True

    # ---- pages
    def comments(self, el) -> list:
        w = self.w
        out: list = []
        w.in_frame += 1
        try:
            for ch in el:
                if ch.tag in _ANNOT_META:
                    continue
                w.within("shape", w.block, ch, "comments", out)
        finally:
            w.in_frame -= 1
        return [s for s in odf.flatten(out, w.synthetic) if s.strip()]

    def page(self, page_el, n: int):
        slide = Slide(index=n)
        ctx, nctx = _Ctx(), _Ctx()
        for ch in page_el:
            tag = ch.tag
            if tag == _NOTES:
                self.shapes_of(ch, nctx)
            elif tag in _ANNOTATIONS:
                slide.comments.extend(self.comments(ch))
            else:
                self.shape(ch, ctx)
        self.w.comments.clear()   # comments anchored inside shape text are not page comments
        self.w.textboxes.clear()
        items = ctx.items
        slide.texts = [s for s in odf.flatten([b for b, _c in items], self.w.synthetic) if s.strip()]
        slide.notes = [s for s in odf.flatten([b for b, _c in nctx.items], self.w.synthetic) if s.strip()]
        for i in nctx.images:
            ctx.add_image(i)
        slide.images = ctx.images
        links = []
        for b, _c in items:
            for p in _paras(b):
                for lk in p.links:
                    if lk.end > lk.start:
                        links.append((p.text[lk.start:lk.end], lk.url))
        slide.links = _dedupe(links + ctx.loose)[:_MAX_LINKS]
        self.chars += sum(len(s) + 1 for s in slide.texts + slide.notes + slide.comments)
        return slide, items, ctx.loose

    def run(self) -> Document:
        doc = self.doc
        top = odf.find(self.content, "office:body")
        if top is not None:
            _unwrap_foreign(top)
        body = odf.find(self.content, "office:body", "office:presentation")
        if body is None:
            body = odf.find(self.content, "office:body")
            if body is None:
                raise CorruptDocumentError("content.xml has no office:body")
            # another body (office:drawing ...): its draw:page children are read the same way
            body = next((ch for ch in body if odf.children(ch, "draw:page")), None)
            if body is None:
                doc.warnings.append("no office:presentation body")
        pages = odf.children(body, "draw:page") if body is not None else []
        if len(pages) > _MAX_SLIDES:
            doc.warnings.append("presentation truncated after %d slides" % _MAX_SLIDES)
            pages = pages[:_MAX_SLIDES]
        meta = []
        for n, page_el in enumerate(pages, 1):
            if self.chars > _MAX_TEXT:
                doc.warnings.append("presentation text limit reached; remaining slides skipped")
                break
            slide, items, loose = self.page(page_el, n)
            doc.slides.append(slide)
            meta.append((items, loose))
        if not doc.slides:
            # an empty presentation is shown (and saved) by LibreOffice with one empty slide
            doc.slides.append(Slide(index=1))
            meta.append(([], []))
        _fill_flat(doc)
        _build_blocks(doc, meta, self.w.synthetic)
        doc.warnings.extend(self.pkg.warnings + self.w.warnings)
        return doc


def _paras(b):
    if isinstance(b, Table):
        for r in b.rows:
            for c in r.cells:
                for x in c.blocks:
                    yield from _paras(x)
    else:
        yield b


def _dedupe(links: list) -> list:
    seen: set = set()
    return [lk for lk in links if not (lk in seen or seen.add(lk))]


def _fill_flat(doc: Document) -> None:
    main: list = []
    for s in doc.slides:
        main.extend(s.texts)
        main.extend(s.comments)
        main.extend(s.notes)
    doc.stories["main"] = main


def _build_blocks(doc: Document, meta: list, synthetic: set) -> None:
    """Markdown structure as for .ppt (ppt._build_blocks): '## Slide N', titles as '###',
    outline paragraphs as bullets at their list level, tables, pictures, links, comments, notes."""
    blocks: list = []
    for slide, (items, loose) in zip(doc.slides, meta):
        blocks.append(Paragraph("Slide %d" % slide.index, heading=2))
        for b, cls in items:
            if isinstance(b, Table):
                blocks.append(b)
                continue
            if id(b) in synthetic:
                continue
            text = b.text.replace(IMAGE_MARK, "").strip()
            if not text:
                continue
            links = [lk for lk in b.links if lk.end <= len(text)] if text == b.text else []
            if cls in _TITLES:
                # a line break inside a title stays one heading line
                blocks.append(Paragraph(text.replace("\n", " "), heading=3, links=links))
            elif b.list_level is not None:
                blocks.append(Paragraph(text, list_level=b.list_level, list_ordered=b.list_ordered,
                                        list_number=b.list_number, links=links))
            elif cls == "outline":
                blocks.append(Paragraph(text, list_level=0, links=links))
            else:
                blocks.append(Paragraph(text, links=links))
        if slide.images:
            blocks.append(Paragraph(IMAGE_MARK * len(slide.images), images=list(slide.images)))
        if loose:
            blocks.append(Paragraph("Links", heading=3))
            blocks.extend(Paragraph(t, list_level=0, links=[Link(0, len(t), u)]) for t, u in _dedupe(loose))
        if slide.comments:
            blocks.append(Paragraph("Comments", heading=3))
            blocks.extend(Paragraph(t) for t in slide.comments)
        if slide.notes:
            blocks.append(Paragraph("Notes", heading=3))
            blocks.extend(Paragraph(t) for t in slide.notes)
    doc.blocks = blocks


def parse(pkg: odf.Package, images_enabled: bool = True) -> Document:
    """Parse an opened OpenDocument presentation package."""
    return _Parser(pkg, images_enabled).run()
