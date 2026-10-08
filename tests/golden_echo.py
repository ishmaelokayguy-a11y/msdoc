# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Fake extractor that returns the golden output. Used to self-test the oracle:

    python3 oracle/compare.py --impl golden_echo:extract   (with tests/ on PYTHONPATH)

Every file with a golden must then score 1.0.
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
from common import data_dir, load_json  # noqa: E402

from msdoc.model import Document, Image  # noqa: E402


def extract(path):
    data = Path(path).read_bytes()
    fid = hashlib.sha256(data).hexdigest()[:16]
    d = data_dir()
    g = load_json(d / "golden" / f"{fid}.json")
    if not g or g["status"] != "ok":
        return Document()
    imgs = [Image(data=(d / "golden" / m["file"]).read_bytes(), mime=m["mime"], kind=m["kind"], story=m["story"])
            for m in g["images"]]
    return Document(stories=g["stories"], images=imgs)
