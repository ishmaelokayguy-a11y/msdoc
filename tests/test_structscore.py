import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oracle"))
from structscore import blocks_to_neutral, struct_score, table_shape  # noqa: E402

from msdoc.model import Cell, Paragraph, Row, Table  # noqa: E402


def P(**kw):
    return {"t": "p", "h": 0, "l": None, "img": 0, "notes": 0, "links": 0, **kw}


def test_table_shape_rowspan_occupancy():
    rows = [[{"cs": 1, "rs": 2}, {"cs": 2, "rs": 1}], [{"cs": 2, "rs": 1}]]
    assert table_shape(rows) == (2, 3, 3)


def test_identical_and_plain_vs_structured():
    g = [P(h=1), P(), {"t": "tbl", "rows": [[{"cs": 1, "rs": 1, "b": [P()]}] * 2]}]
    assert struct_score(g, g) == 1.0
    assert struct_score(g, [P(), P(), P()]) < 0.2
    assert struct_score([P()], [P()]) == 1.0


def test_blocks_to_neutral_roundtrip():
    t = Table([Row([Cell([Paragraph("a", images=[0])], colspan=2)])])
    n = blocks_to_neutral([Paragraph("h", heading=2), t])
    assert n[0]["h"] == 2 and n[1]["rows"][0][0]["cs"] == 2
    assert struct_score(n, n) == 1.0
