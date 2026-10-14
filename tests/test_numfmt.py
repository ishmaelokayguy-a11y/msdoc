# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Excel number formats: the displayed text of a numeric cell (LibreOffice conventions)."""
import pytest

from msdoc.numfmt import format_number as fmt
from msdoc.numfmt import general


@pytest.mark.parametrize("value, expected", [
    (0.0, "0"), (4.0, "4"), (-3.0, "-3"), (1.5, "1.5"), (0.1, "0.1"),
    (123456789012345.0, "123456789012345"), (1e15, "1E+15"), (1.5e20, "1.5E+20"),
    (0.0000007, "0.0000007"), (0.00012345, "0.00012345"),
    (9.43093540526649e-05, "9.43093540526649E-05"), (1e-20, "1E-20"),
    (float("nan"), "NaN"),
])
def test_general(value, expected):
    assert general(value) == expected


@pytest.mark.parametrize("value, code, expected", [
    (1.0, "0.00", "1.00"), (1234.5, "#,##0.00", "1,234.50"), (0.256, "0%", "26%"),
    (0.256, "0.0%", "25.6%"), (39.782262, ".0", "39.8"), (1234567.0, "#,##0,", "1,235"),
    (12345.678, "0.00E+00", "1.23E+04"), (-5.0, "0;(0)", "(5)"), (0.0, '0;-0;"zero"', "zero"),
    (7.0, '0.00" kg"', "7.00 kg"), (5.0, "000", "005"), (42272000203.0, "0-00000-00000-0", "0-42272-00020-3"),
    (3.0, "[$$-409]#,##0.00", "$3.00"), (40293.6, "[$-F400]h:mm:ss AM/PM", "40293.6"),
])
def test_numbers(value, code, expected):
    assert fmt(value, 0, code) == expected


@pytest.mark.parametrize("value, code, expected", [
    (0.82, "#\\ ?/10", " 8/10"), (-0.5, "# ?/?", "- 1/2"), (1.5, "# ?/?", "1 1/2"),
    (0.75, "?/?", "3/4"), (2.25, "# ??/??", "2 1/4"),
])
def test_fractions(value, code, expected):
    assert fmt(value, 0, code) == expected


@pytest.mark.parametrize("value, ifmt, code, expected", [
    (44927.0, 14, None, "1/1/2023"),
    (44927.5, 22, None, "1/1/23 12:00"),
    (44927.0, 0, "yyyy-mm-dd", "2023-01-01"),
    (44927.0, 0, "dddd, mmmm d, yyyy", "Sunday, January 1, 2023"),
    (0.75, 0, "h:mm AM/PM", "6:00 PM"),
    (1.5, 0, "[h]:mm:ss", "36:00:00"),
    # LibreOffice (impop.cxx) always uses the null date 1899-12-30, so serials below 61
    # are one day earlier than in Excel's 1900 date system
    (59.0, 0, "d/m/yyyy", "27/2/1900"),
    (1.0, 0, "d/m/yyyy", "31/12/1899"),
    (61.0, 0, "d/m/yyyy", "1/3/1900"),
])
def test_dates(value, ifmt, code, expected):
    assert fmt(value, ifmt, code) == expected


def test_date_1904():
    assert fmt(0.0, 0, "yyyy-mm-dd", date1904=True) == "1904-01-01"


def test_builtin_ids_and_unknown_degrade_to_general():
    assert fmt(1.5, 2, None) == "1.50"
    assert fmt(1.5, 200, None) == "1.5"
    assert fmt(1.5, 0, "") == "1.5"


@pytest.mark.parametrize("text, ifmt, code, expected", [
    ("abc", 0, None, "abc"),
    ("abc", 0, '0;0;0;"[" @ "]"', "[ abc ]"),
    ("abc", 0, "@", "abc"),
    ("abc", 0, '"x"@', "xabc"),
    ("abc", 0, "0.00;-0.00;;", "abc"),
    ("abc", 0, "0.00;-0.00;0;", "abc"),
    ("abc", 0, "0.00", "abc"),
])
def test_format_text(text, ifmt, code, expected):
    from msdoc.numfmt import format_text
    assert format_text(text, ifmt, code) == expected
