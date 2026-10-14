#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""OpenDocument end to end without any input file: build tiny .odt/.ods/.odp packages in code and extract them.
OpenDocument целиком без входных файлов: собираем маленькие .odt/.ods/.odp в коде и извлекаем.

    python3 examples/odf_demo.py

An ODF file is a zip with a ``mimetype`` entry and ``content.xml``; the same ``msdoc.extract`` /
``msdoc.to_markdown`` calls read it, ``doc.format`` tells which of the three it was.
"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import msdoc  # noqa: E402

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink"')
KINDS = {"odt": "text", "ods": "spreadsheet", "odp": "presentation"}
BODY = {
    "odt": '<office:text><text:h text:outline-level="1">Report</text:h>'
           '<text:p>Привет, <text:a xlink:href="https://example.com">site</text:a></text:p>'
           '<table:table table:name="T"><table:table-column table:number-columns-repeated="2"/><table:table-row><table:table-cell><text:p>A1</text:p></table:table-cell>'
           '<table:table-cell><text:p>B1</text:p></table:table-cell></table:table-row></table:table></office:text>',
    "ods": '<office:spreadsheet><table:table table:name="Sheet1"><table:table-row>'
           '<table:table-cell office:value-type="string"><text:p>Item</text:p></table:table-cell>'
           '<table:table-cell office:value-type="float" office:value="2.5"><text:p>2.5</text:p></table:table-cell>'
           '</table:table-row></table:table></office:spreadsheet>',
    "odp": '<office:presentation><draw:page draw:name="page1"><draw:frame presentation:class="title">'
           '<draw:text-box><text:p>Quarterly plan</text:p></draw:text-box></draw:frame>'
           '<draw:frame presentation:class="outline"><draw:text-box><text:list><text:list-item><text:p>First point'
           '</text:p></text:list-item></text:list></draw:text-box></draw:frame></draw:page></office:presentation>',
}


def build(ext: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument." + KINDS[ext], compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS} '
                                  f'office:version="1.2"><office:body>{BODY[ext]}</office:body></office:document-content>')
    return buf.getvalue()


def main() -> None:
    for ext in ("odt", "ods", "odp"):
        data = build(ext)
        doc = msdoc.extract(data)
        print(f"== {ext}: format={doc.format}, sheets={len(doc.sheets)}, slides={len(doc.slides)}")
        print(msdoc.to_markdown(data).text)


if __name__ == "__main__":
    main()
