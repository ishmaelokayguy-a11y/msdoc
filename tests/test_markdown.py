from msdoc.model import (IMAGE_MARK, NOTE_MARK, Cell, Document, Link, Paragraph,
                         Row, Table)


def md(blocks, **kw):
    d = Document(stories={"main": []})
    d.blocks = blocks
    return d.to_markdown(**kw)


def test_heading_and_plain():
    assert md([Paragraph("Title", heading=2), Paragraph("text")]) == "## Title\n\ntext\n"


def test_lists_tight_and_nested():
    out = md([Paragraph("a", list_level=0), Paragraph("b", list_level=1),
              Paragraph("c", list_level=0, list_ordered=True, list_number="3.")])
    assert out == "- a\n  - b\n3. c\n"


def test_image_placeholder_and_note():
    p = Paragraph("see " + IMAGE_MARK + " x" + NOTE_MARK, images=[0], notes=[("footnote", 1)])
    d = Document(stories={"main": []}, blocks=[p])
    d.notes["footnotes"] = [[Paragraph("note")]]
    assert d.markdown == "see ![Media 0](image:0) x[^1]\n\n[^1]: note\n"


def test_link():
    p = Paragraph("go here", links=[Link(3, 7, "http://a/b")])
    assert md([p]) == "go [here](http://a/b)\n"


def test_html_table_spans_and_escape():
    t = Table([Row([Cell([Paragraph("a<b")], colspan=2, header=True)]),
               Row([Cell([Paragraph("1"), Paragraph("2")]), Cell([Paragraph("x")], rowspan=2)])])
    out = md([t])
    assert '<th colspan="2">a&lt;b</th>' in out
    assert "<td>1<br>2</td>" in out and '<td rowspan="2">x</td>' in out


def test_escape_line_start():
    assert md([Paragraph("# not heading")]).startswith("\\# not")


def test_fallback_from_extract(tmp_path):
    from msdoc.blocks import build_blocks
    d = Document(stories={"main": ["x", "y"], "footnotes": ["n"]})
    build_blocks(d)
    assert d.markdown == "x\n\ny\n\n[^1]: n\n"


def test_to_markdown_api_tuple():
    import glob, msdoc
    files = sorted(glob.glob("data/corpus/dev/*.doc"))[:1]
    if not files:
        return
    text, media = msdoc.to_markdown(files[0])
    assert isinstance(text, str) and isinstance(media, list)
    for m in media:
        assert isinstance(m.data, bytes) and m.mime


def test_note_body_has_no_leading_space():
    d = Document(stories={"main": []}, blocks=[Paragraph("x" + NOTE_MARK, notes=[("footnote", 1)])])
    d.notes["footnotes"] = [[Paragraph("  note text ")]]
    assert "[^1]: note text" in d.markdown


def test_public_exports_and_version():
    import msdoc
    for name in ("to_markdown", "MarkdownResult", "Paragraph", "Table", "Row", "Cell", "Link"):
        assert hasattr(msdoc, name)
    assert msdoc.__version__ == "0.5.0"
