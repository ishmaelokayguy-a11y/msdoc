# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Build synthetic OpenDocument packages in memory (no binary fixtures)."""
from __future__ import annotations

import io
import struct
import zipfile
import zlib

NSDECL = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:loext="urn:org:documentfoundation:names:experimental:office:xmlns:loext:1.0" '
    'office:version="1.3"'
)
TEXT_MT = "application/vnd.oasis.opendocument.text"


def png(w: int = 1, h: int = 1, seed: int = 0) -> bytes:
    """A valid tiny RGB PNG (pixel colour from `seed`, so pictures differ)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + bytes([seed & 255, 2, 3]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def content_xml(body: str, auto_styles: str = "", kind: str = "text") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-content {NSDECL}>'
            f'<office:automatic-styles>{auto_styles}</office:automatic-styles>'
            f'<office:body><office:{kind}>{body}</office:{kind}></office:body>'
            '</office:document-content>')


def styles_xml(styles: str = "", auto: str = "", master: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-styles {NSDECL}>'
            f'<office:styles>{styles}</office:styles>'
            f'<office:automatic-styles>{auto}</office:automatic-styles>'
            f'<office:master-styles>{master}</office:master-styles>'
            '</office:document-styles>')


def manifest_xml(mimetype: str, entries: dict | None = None, extra: str = "") -> str:
    rows = [f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mimetype}"/>']
    for path, mt in (entries or {}).items():
        rows.append(f'<manifest:file-entry manifest:full-path="{path}" manifest:media-type="{mt}"/>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            'manifest:version="1.3">' + "".join(rows) + extra + '</manifest:manifest>')


def package(files: dict, mimetype: str | None = TEXT_MT, mimetype_first: bool = True,
            manifest: str | None = None) -> bytes:
    """Zip `files` ({name: str|bytes}) as an ODF package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        items = []
        if mimetype is not None:
            items.append(("mimetype", mimetype.encode(), zipfile.ZIP_STORED))
        if manifest is None and mimetype is not None:
            manifest = manifest_xml(mimetype)
        body = [(n, d.encode() if isinstance(d, str) else d, zipfile.ZIP_DEFLATED) for n, d in files.items()]
        if manifest is not None:
            body.append(("META-INF/manifest.xml", manifest.encode(), zipfile.ZIP_DEFLATED))
        items = items + body if mimetype_first else body + items
        for name, data, method in items:
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return buf.getvalue()


def odt(body: str, auto_styles: str = "", styles: str = "", styles_auto: str = "", master: str = "",
        extra_files: dict | None = None, **kw) -> bytes:
    files = {"content.xml": content_xml(body, auto_styles),
             "styles.xml": styles_xml(styles, styles_auto, master)}
    files.update(extra_files or {})
    return package(files, **kw)
