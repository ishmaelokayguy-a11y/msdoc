# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Contract tests that must hold at every stage of development."""
import sys
from pathlib import Path

import pytest

import msdoc
from msdoc.model import STORY_KEYS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
import common  # noqa: E402


def test_story_keys_match_oracle():
    assert tuple(STORY_KEYS) == tuple(common.STORY_KEYS)


@pytest.mark.parametrize("data", [b"", b"hello world", b"{\\rtf1 not a doc}"])
def test_non_ole_input_raises_library_error(data):
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(data)


def test_document_defaults():
    d = msdoc.Document()
    assert set(d.stories) == set(STORY_KEYS)
    assert d.text == ""
