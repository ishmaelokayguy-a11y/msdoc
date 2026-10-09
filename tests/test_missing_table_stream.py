# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import pytest

import msdoc
from msdoc.api import _open_table


class FakeContainer:
    def __init__(self, streams):
        self.streams = streams

    def exists(self, name):
        return name in self.streams

    def open_stream(self, name):
        return self.streams[name]


class FakeFib:
    def __init__(self, name):
        self.table_stream_name = name


def test_preferred_table_stream_used():
    c = FakeContainer({"0Table": b"zero", "1Table": b"one"})
    assert _open_table(c, FakeFib("1Table")) == (b"one", None)
    assert _open_table(c, FakeFib("0Table")) == (b"zero", None)


@pytest.mark.parametrize("want,other", [("1Table", "0Table"), ("0Table", "1Table")])
def test_falls_back_to_other_table_stream(want, other):
    table, warning = _open_table(FakeContainer({other: b"t"}), FakeFib(want))
    assert table == b"t"
    assert warning == f"{want} missing; used {other}"


def test_no_table_stream_is_corrupt():
    # No piece table to recover from: FibBase 0x18/0x1C are reserved in Ver8
    # ([MS-DOC] 2.5.2), so there is no flat-text fallback.
    with pytest.raises(msdoc.CorruptDocumentError):
        _open_table(FakeContainer({"WordDocument": b""}), FakeFib("1Table"))
