# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import pytest

import msdoc


# ww8scan.cxx WW8Fib::GetFIBVersion: 0xA59B/0xA59C -> eWW1, 0xA5DB -> eWW2.
@pytest.mark.parametrize("wident", [0xA59B, 0xA59C, 0xA5DB])
def test_legacy_non_ole_is_unsupported(wident):
    with pytest.raises(msdoc.UnsupportedFormatError):
        msdoc.extract(wident.to_bytes(2, "little") + b"\x2d\x00" + b"\x00" * 100)


# Boundary: neighbouring magics are versioned by nFib (Word 6+), which only
# exists inside an OLE container, so a flat file with them is not OLE.
@pytest.mark.parametrize("wident", [0xA5DC, 0xA5DA, 0xA59A, 0xA59D, 0xA5EC])
def test_other_flat_widents_are_not_ole(wident):
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(wident.to_bytes(2, "little") + b"\x65\x00" + b"\x00" * 100)


def test_garbage_still_not_ole():
    with pytest.raises(msdoc.NotOleFileError):
        msdoc.extract(b"PK\x03\x04" + b"\x00" * 100)
