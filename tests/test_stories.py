# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Unit tests for msdoc.stories (synthetic inputs, no corpus needed)."""
import struct

from msdoc import stories as S


class FakePieces:
    """Single uncompressed piece, CP == index into `text`."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.prm = b"\0\0"

    def text(self, lo, hi):
        return self._text[max(0, lo):max(0, hi)]

    def cp_to_fc(self, cp):
        return cp * 2, False

    def piece_at_cp(self, cp):
        return self

    def prc_grpprl(self, i):
        return b""


class FakeProps:
    def __init__(self, runs=()):
        self._runs = list(runs)

    def runs(self):
        return iter(self._runs)


class FakePapx:
    """mark CP -> sprm dict."""

    def __init__(self, by_cp=None):
        self.by_cp = by_cp or {}

    def sprms_at(self, cp):
        return self.by_cp.get(cp, {})


def paras(text, runs=(), anchors=None):
    flags = S._RunFlags(FakeProps(runs))
    out, _links = S._paragraphs(FakePieces(text), 0, len(text), flags, anchors if anchors is not None else [])
    return [t for t, _ in out]


SPEC = {S.SPRM_CFSPEC: b"\x01"}


# ------------------------------------------------------------ characters

def test_paragraph_marks_and_last_paragraph():
    assert paras("one\rtwo\r") == ["one", "two"]
    assert paras("one\rtwo") == ["one", "two"]


def test_cell_marks_split_paragraphs():
    assert paras("a\x07b\x07\x07") == ["a", "b", ""]


def test_line_break_tab_and_hyphens():
    assert paras("a\x0bb\tc\x1ed\x1fe\r") == ["a\nb\tc‑de"]


def test_field_keeps_result_only():
    assert paras("x\x13 PAGE \x141\x15y\r") == ["x1y"]


def test_field_without_separator_is_dropped():
    assert paras("x\x13 TC \"entry\" \x15y\r") == ["xy"]


def test_nested_fields():
    # nested field in the code part is dropped, in the result part kept
    t = "\x13 IF \x13 REF a \x14A\x15 = 1 \x14r\x13 PAGE \x142\x15s\x15\r"
    assert paras(t) == ["r2s"]


def test_paragraph_mark_inside_field_code_does_not_split():
    assert paras("\x13 TOC \r\\o\x14T1\rT2\x15\r") == ["T1", "T2"]


def test_unbalanced_field_chars_are_harmless():
    assert paras("\x15\x14a\x13b\r") == ["a"]


def test_special_chars_and_anchors():
    t = "a\x01b\x08c\x02\r"
    anchors = []
    got = paras(t, runs=[(1, 2, SPEC), (3, 4, SPEC), (5, 6, SPEC)], anchors=anchors)
    assert got == ["abc"]
    assert anchors == [1, 3]


def test_picture_char_without_cfspec_is_not_an_anchor():
    anchors = []
    assert paras("a\x01b\r", anchors=anchors) == ["ab"]
    assert anchors == []


def test_tracked_deletion_is_dropped():
    t = "keep\rgone\rtail\r"
    assert paras(t, runs=[(5, 10, {S.SPRM_CFRMARKDEL: b"\x01"})]) == ["keep", "tail"]


def test_toggle_operands():
    assert S._toggle(b"\x01") and S._toggle(b"\x81")
    assert not S._toggle(b"\x00") and not S._toggle(b"\x80") and not S._toggle(None)
    t = "keep\rgone\r"
    assert paras(t, runs=[(5, 10, {S.SPRM_CFRMARKDEL: b"\x80"})]) == ["keep", "gone"]


def test_run_segments_cover_range():
    f = S._RunFlags(FakeProps([(2, 4, SPEC), (6, 8, {S.SPRM_CFRMARKDEL: b"\x01"})]))
    segs = list(f.segments(0, 10))
    assert segs == [(0, 2, 0), (2, 4, f.SPEC), (4, 6, 0), (6, 8, f.DELETED), (8, 10, 0)]
    assert list(f.segments(3, 7)) == [(3, 4, f.SPEC), (4, 6, 0), (6, 7, f.DELETED)]


def test_symbol_character_uses_xchar():
    sym = {S.SPRM_CSYMBOL: struct.pack("<HH", 3, 0xF06D), S.SPRM_CFSPEC: b"\x01"}
    assert paras("15 (g\r", runs=[(3, 4, sym)]) == ["15 g"]


def test_macrobutton_shows_display_text():
    assert paras("\x13MACROBUTTON JaNein o\x15unchecked\r") == ["ounchecked"]
    assert paras("\x13 MACROBUTTON NoMacro [Click]\x14[Click]\x15!\r") == ["[Click]!"]


def test_macrobutton_inside_hidden_field_code_is_hidden():
    assert paras("\x13 IF \x13MACROBUTTON m x\x15\x14r\x15\r") == ["r"]


# ------------------------------------------------------------ HYPERLINK fields


def test_hyperlink_url_variants():
    assert S._hyperlink_url('HYPERLINK "http://example.com"') == "http://example.com"
    assert S._hyperlink_url('HYPERLINK \\l "bookmark1"') == "#bookmark1"
    assert S._hyperlink_url('HYPERLINK "http://example.com" \\l "frag"') == "http://example.com#frag"
    assert S._hyperlink_url('HYPERLINK "http://example.com" \\t "_blank"') == "http://example.com"
    assert S._hyperlink_url('NOTAHYPERLINK "x"') is None
    assert S._hyperlink_url("HYPERLINK") is None


def test_paragraphs_reports_hyperlink_span():
    text = '\x13HYPERLINK "http://x"\x14http://x\x15 after\r'
    pars, links = S._paragraphs(FakePieces(text), 0, len(text), S._RunFlags(FakeProps()), [])
    assert [t for t, _ in pars] == ["http://x after"]
    assert links == [(0, 0, 8, "http://x")]


def test_paragraphs_ignores_hyperlink_split_across_paragraph_break():
    # A field whose result contains a paragraph mark (malformed/unusual) is
    # not attributable to a single paragraph's text offsets; it must not be
    # reported rather than reported against the wrong paragraph.
    text = '\x13HYPERLINK "http://x"\x14a\rb\x15\r'
    pars, links = S._paragraphs(FakePieces(text), 0, len(text), S._RunFlags(FakeProps()), [])
    assert [t for t, _ in pars] == ["a", "b"]
    assert links == []


def test_note_mark_placeholder():
    flags = S._RunFlags(FakeProps([(0, 1, SPEC), (4, 5, SPEC)]))
    text = "\x02 a(\x02)\r"
    got, _links = S._paragraphs(FakePieces(text), 0, len(text), flags, [], note=True)
    assert [t for t, _ in got] == [" a(?)"]
    # outside notes the mark is just dropped
    assert paras(text, runs=[(0, 1, SPEC), (4, 5, SPEC)]) == [" a()"]


def test_note_table_cells_run_together():
    text = "a\x07b\x07\x07c\r"
    pars, _links = S._paragraphs(FakePieces(text), 0, len(text), S._RunFlags(FakeProps()), [])
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01"}
    got = S._flatten_cells(pars, FakePieces(text), FakePapx({4: row_end}))
    assert [t for t, _ in got] == ["ab", "c"]


# ------------------------------------------------------------ routing

def _h(v):
    return struct.pack("<h", v)


APO = {S.SPRM_PPC: b"\x20", S.SPRM_PDXAABS: _h(100)}


def test_apo_paragraph_goes_to_textboxes():
    flow, frames = S._route([("a", 0), ("b", 1), ("c", 2)], FakePapx({1: APO}))
    assert flow == ["a", "c"] and frames == ["b"]


def test_empty_apo_stays_in_flow():
    # WW8FlyPara::IsEmpty: all-default frame data is not a frame
    flow, frames = S._route([("a", 0)], FakePapx({0: {S.SPRM_PWR: b"\x02"}}))
    assert flow == ["a"] and frames == []


def test_floating_table_row_goes_to_textboxes():
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01", S.SPRM_TPC: b"\x60", S.SPRM_TDYAABS: _h(10)}
    pars = [("before", 0), ("c1", 1), ("c2", 2), ("", 3), ("after", 4)]
    flow, frames = S._route(pars, FakePapx({1: cell, 2: cell, 3: row_end}))
    assert flow == ["before", "after"]
    assert frames == ["c1", "c2", ""]


def test_table_starting_in_apo_stays_in_frame():
    apo_cell = {**APO, S.SPRM_PFINTABLE: b"\x01"}
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01"}
    pars = [("r1", 0), ("", 1), ("r2", 2), ("", 3), ("after", 4)]
    flow, frames = S._route(pars, FakePapx({0: apo_cell, 1: row_end, 2: cell, 3: row_end}))
    assert flow == ["after"] and frames == ["r1", "", "r2", ""]


def test_floating_table_followed_by_plain_table():
    apo_cell = {**APO, S.SPRM_PFINTABLE: b"\x01"}
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    float_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01", S.SPRM_TPC: b"\x20", S.SPRM_TDYAABS: _h(1)}
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01"}
    pars = [("first", 0), ("", 1), ("second", 2), ("", 3)]
    flow, frames = S._route(pars, FakePapx({0: apo_cell, 1: float_end, 2: cell, 3: row_end}))
    assert frames == ["first", ""] and flow == ["second", ""]


def test_plain_table_stays_in_flow():
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01"}
    flow, frames = S._route([("c1", 1), ("", 2)], FakePapx({1: cell, 2: row_end}))
    assert flow == ["c1", ""] and frames == []


def test_nested_floating_table_only_moves_inner_rows():
    itap = lambda n: struct.pack("<i", n)  # noqa: E731
    outer = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PITAP: itap(1)}
    inner = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PITAP: itap(2)}
    inner_end = {**inner, S.SPRM_PFINNERTTP: b"\x01", S.SPRM_TPC: b"\x60"}
    outer_end = {**outer, S.SPRM_PFTTP: b"\x01"}
    pars = [("o1", 0), ("i1", 1), ("", 2), ("o2", 3), ("", 4)]
    flow, frames = S._route(pars, FakePapx({0: outer, 1: inner, 2: inner_end, 3: outer, 4: outer_end}))
    assert flow == ["o1", "o2", ""]
    assert frames == ["i1", ""]


def test_drop_cap_merges_into_next_paragraph():
    dc = {**APO, S.SPRM_PDCS: b"\x1a\x00"}
    flow, frames = S._route([("T", 0), ("his is it", 1)], FakePapx({0: dc}))
    assert flow == ["This is it"] and frames == []


def test_unterminated_table_is_flushed():
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    flow, frames = S._route([("c1", 0), ("c2", 1)], FakePapx({0: cell, 1: cell}))
    assert flow == ["c1", "c2"]


# ------------------------------------------------------------ PAPX FKP

def test_papx_fkp_page_parsing():
    page = bytearray(512)
    struct.pack_into("<3I", page, 0, 100, 110, 120)
    page[511] = 2
    bx = 3 * 4
    page[bx] = 100          # bOffset -> byte 200
    page[bx + 13] = 0       # no PAPX
    # PapxInFkp: cb=2 -> 3 bytes GrpPrlAndIstd: istd(2) + 1 byte? use cb=3 -> 5 bytes
    page[200] = 3
    page[201:206] = b"\x00\x00" + struct.pack("<H", S.SPRM_PFINTABLE) + b"\x01"
    runs = list(S._ParaProps._parse_page(bytes(page)))
    assert runs[0] == (100, 110, b"\x00\x00" + struct.pack("<H", S.SPRM_PFINTABLE) + b"\x01")
    assert runs[1] == (110, 120, b"")


def _stsh(styles, cb_base=10):
    """styles: list of (stk, istd_base, papx_grpprl[, sti]) or None (empty slot)."""
    stshi = struct.pack("<HH", len(styles), cb_base) + b"\0" * 14
    out = struct.pack("<H", len(stshi)) + stshi
    for st in styles:
        if st is None:
            out += b"\0\0"
            continue
        stk, base, grpprl = st[0], st[1], st[2]
        sti = st[3] if len(st) > 3 else 0
        std = struct.pack("<HHHHH", sti, stk | (base << 4), 2 if stk == 1 else 1, 0, 0)
        std += b"\0" * (cb_base - 10)
        std += struct.pack("<H", 1) + "N".encode("utf-16-le") + b"\0\0"
        if len(std) & 1:
            std += b"\0"
        if stk == 1:
            upx = struct.pack("<H", 0) + grpprl
            std += struct.pack("<H", len(upx)) + upx
            if len(upx) & 1:
                std += b"\0"
            std += struct.pack("<H", 0)  # empty chpx upx
        out += struct.pack("<H", len(std)) + std
    return out


def test_styles_inherit_paragraph_sprms():
    ppc = struct.pack("<H", S.SPRM_PPC) + b"\x20"
    wr = struct.pack("<H", S.SPRM_PWR) + b"\x01"
    stsh = _stsh([(1, 0xFFF, b""), None, (1, 0, ppc), (1, 2, wr), (2, 0xFFF, b"")], cb_base=18)
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert st.para_sprms(0) == {}
    assert st.para_sprms(3) == {S.SPRM_PPC: b"\x20", S.SPRM_PWR: b"\x01"}
    assert st.para_sprms(1) == {} and st.para_sprms(99) == {}


def test_styles_base_cycle_terminates():
    ppc = struct.pack("<H", S.SPRM_PPC) + b"\x20"
    stsh = _stsh([(1, 1, ppc), (1, 0, b"")])
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert st.para_sprms(1) == {S.SPRM_PPC: b"\x20"}


# ------------------------------------------------------------ headings


def test_heading_level_mapping():
    assert S.heading_level({}) == 0
    for v in range(9):
        assert S.heading_level({S.SPRM_POUTLVL: bytes([v])}) == v + 1
    assert S.heading_level({S.SPRM_POUTLVL: bytes([9])}) == 0
    assert S.heading_level({S.SPRM_POUTLVL: bytes([200])}) == 0
    assert S.heading_level({S.SPRM_POUTLVL: b""}) == 0


def test_builtin_heading_style_gets_implicit_outline_level():
    # sti 1 == "heading 1" (ww8par.hxx IsWW8BuiltInHeadingStyle); the style's
    # own grpprl carries no sprmPOutLvl, so the sti-1 default applies.
    stsh = _stsh([(1, 0xFFF, b"", 0), (1, 0xFFF, b"", 1), (1, 0xFFF, b"", 9)], cb_base=18)
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert S.heading_level(st.para_sprms(0)) == 0
    assert S.heading_level(st.para_sprms(1)) == 1
    assert S.heading_level(st.para_sprms(2)) == 9


def test_explicit_style_outline_overrides_sti_default():
    # A style's own sprmPOutLvl (set while reading STSH, ww8par6.cxx
    # Read_POutLvl) wins over the sti-based default.
    outlvl = struct.pack("<H", S.SPRM_POUTLVL) + b"\x02"
    stsh = _stsh([(1, 0xFFF, outlvl, 1)], cb_base=18)
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert S.heading_level(st.para_sprms(0)) == 3


def test_custom_style_inherits_outline_level_from_heading_base():
    # A user style (sti 0, i.e. not itself a built-in heading style) based on
    # "heading 2" inherits its outline level like any other paragraph sprm.
    stsh = _stsh([(1, 0xFFF, b"", 2), (1, 0, b"", 0)], cb_base=18)
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert S.heading_level(st.para_sprms(1)) == 2


def test_non_heading_style_has_no_outline_level():
    stsh = _stsh([(1, 0xFFF, b"", 0)], cb_base=18)
    st = S._Styles(FakeFib({"Stshf": (0, len(stsh))}), stsh)
    assert S.heading_level(st.para_sprms(0)) == 0


# ------------------------------------------------------------ _route_structured


def test_route_structured_matches_route_text_and_order():
    pars = [("a", 0), ("b", 1), ("c", 2)]
    papx = FakePapx({0: {}, 1: APO, 2: {}})
    flow_text, frames_text = S._route(pars, papx)
    structured = S._route_structured(pars, papx)
    assert [r.text for r in structured] == flow_text == ["a", "c"]


def test_route_structured_attaches_links_by_original_index():
    pars = [("a", 0), ("b link", 1), ("c", 2)]
    papx = FakePapx({0: {}, 1: {}, 2: {}})
    links = [(1, 2, 6, "http://x")]
    structured = S._route_structured(pars, papx, links)
    by_text = {r.text: r for r in structured}
    assert by_text["a"].links == ()
    assert by_text["b link"].links == ((2, 6, "http://x"),)
    assert by_text["c"].links == ()


def test_route_structured_shifts_link_offsets_for_drop_cap_merge():
    dc = {**APO, S.SPRM_PDCS: b"\x1a\x00"}
    pars = [("T", 0), ("his is it", 1)]
    # the link lives on the second original paragraph ("his is it"), which
    # gets the 1-char drop cap prepended to it -- offsets must shift by 1.
    links = [(1, 0, 4, "http://x")]
    structured = S._route_structured(pars, FakePapx({0: dc}), links)
    assert len(structured) == 1 and structured[0].text == "This is it"
    assert structured[0].links == ((1, 5, "http://x"),)


def test_route_structured_reports_table_depth_and_row_end():
    cell = {S.SPRM_PFINTABLE: b"\x01"}
    row_end = {S.SPRM_PFINTABLE: b"\x01", S.SPRM_PFTTP: b"\x01"}
    pars = [("before", 0), ("c1", 1), ("c2", 2), ("after", 3)]
    structured = S._route_structured(pars, FakePapx({1: cell, 2: row_end}))
    by_text = {r.text: r for r in structured}
    assert by_text["before"].depth == 0 and not by_text["before"].row_end
    assert by_text["c1"].depth == 1 and not by_text["c1"].row_end
    assert by_text["c2"].depth == 1 and by_text["c2"].row_end
    assert by_text["after"].depth == 0


def test_papx_fkp_bad_count_is_ignored():
    page = bytearray(512)
    page[511] = 200
    assert list(S._ParaProps._parse_page(bytes(page))) == []


# ------------------------------------------------------------ PLC helpers

def test_header_ranges_select_used_stories():
    # 6 separators, then 2 sections x 6 stories, then the closing entry
    lens = [1] * 6 + [1, 2, 0, 3, 4, 5] + [0, 0, 0, 0, 0, 6] + [9]
    cps = [0]
    for n in lens:
        cps.append(cps[-1] + n)
    base = 100
    rng = lambda i: (base + cps[i], base + cps[i + 1])  # noqa: E731
    # no facing pages, no title page: odd header + odd footer, inherited
    got = S._header_ranges(cps, base, 1000, [False, False], False)
    assert got == [rng(7), rng(9), rng(7), rng(9)]
    # section 2 has a title page: its first footer is its own, its first
    # header is inherited from section 1
    got = S._header_ranges(cps, base, 1000, [False, True], True)
    assert got == [rng(6), rng(7), rng(9), rng(6), rng(7), rng(9), rng(10), rng(17)]


def _rec(rtype, body, container=False):
    return struct.pack("<HHI", 0xF if container else 0x2, rtype, len(body)) + body


def _sp(spid):
    return _rec(0xF004, _rec(0xF00A, struct.pack("<II", spid, 0)), True)


class FakeFib:
    def __init__(self, pairs):
        self.pairs = pairs

    def pair(self, name, n):
        return self.pairs.get(name)


def test_shape_groups_maps_children_to_top_level_group():
    group = _rec(0xF003, _sp(1030) + _sp(1031) + _rec(0xF003, _sp(1032) + _sp(1033), True), True)
    patriarch = _rec(0xF003, _sp(1024) + group + _sp(1040), True)
    dg = _rec(0xF002, patriarch, True)
    content = _rec(0xF000, b"\0" * 16, True) + b"\x00" + dg
    table = b"junk" + content
    groups = S._shape_groups(FakeFib({"DggInfo": (4, len(content))}), table)
    assert groups == {1030: 1030, 1031: 1030, 1032: 1030, 1033: 1030}


def _opt(props):
    """props: list of (pid, value) where bytes values are complex."""
    head, tail = b"", b""
    for pid, val in props:
        if isinstance(val, bytes):
            head += struct.pack("<HI", pid | 0x8000, len(val))
            tail += val
        else:
            head += struct.pack("<HI", pid, val)
    body = head + tail
    return struct.pack("<HHI", 0x3 | (len(props) << 4), 0xF00B, len(body)) + body


def test_wordart_text_is_read_from_gtext():
    text = "DRAFT\0".encode("utf-16-le")
    sp = _rec(0xF004, _rec(0xF00A, struct.pack("<II", 2050, 0)) +
              _opt([(0x7F, 1), (0xC5, "Arial\0".encode("utf-16-le")), (0xC0, text)]), True)
    patriarch = _rec(0xF003, _sp(1024) + sp + _sp(2051), True)
    content = _rec(0xF000, b"\0" * 16, True) + b"\x01" + _rec(0xF002, patriarch, True)
    fib = FakeFib({"DggInfo": (0, len(content))})
    assert S._wordart(fib, content) == [(2050, "DRAFT")]


def test_shape_groups_survives_garbage():
    table = b"\xff" * 64
    assert S._shape_groups(FakeFib({"DggInfo": (0, 64)}), table) == {}


def test_ranges_clamped_and_skipped():
    assert S._ranges([0, 5, 9, 50], 100, 20) == [(100, 105), (105, 109), (109, 120)]
    assert S._ranges([0, 1, 2, 3], 10, 3, skip=1) == [(11, 12), (12, 13)]
    assert S._ranges([5, 3, -1], 0, 10) == []


# ------------------------------------------------------------ note/header CP ranges


class FakeFibAttrs(FakeFib):
    """FakeFib plus plain ccpXxx attributes, for _cp_layout/note_ranges/
    header_ranges (which read both PLCs via .pair() and story lengths via
    plain attribute access, like the real FIB)."""

    def __init__(self, pairs, **ccp):
        super().__init__(pairs)
        for k, v in ccp.items():
            setattr(self, k, v)


def test_cp_layout_accumulates_offsets():
    fib = FakeFibAttrs({}, ccpText=10, ccpFtn=3, ccpHdd=2)
    layout = S._cp_layout(fib)
    assert layout["main"] == (0, 10)
    assert layout["footnotes"] == (10, 3)
    assert layout["headers"] == (13, 2)
    assert layout["comments"] == (15, 0)


def test_note_ranges_from_plc():
    # A plain Plc ([MS-DOC] 2.2.2) with cb_data=0 (PlcffndTxt/PlcfendTxt
    # carry no per-entry data, just the aCP boundaries).
    table = struct.pack("<3i", 0, 2, 6)
    fib = FakeFibAttrs({"PlcffndTxt": (0, len(table))}, ccpText=10, ccpFtn=6)
    assert S.note_ranges(fib, table, "footnotes") == [(10, 12), (12, 16)]


def test_note_ranges_no_plc_falls_back_to_whole_story():
    fib = FakeFibAttrs({"PlcffndTxt": None}, ccpText=10, ccpFtn=6)
    assert S.note_ranges(fib, b"", "footnotes") == [(10, 16)]


def test_note_ranges_empty_story_is_empty():
    fib = FakeFibAttrs({}, ccpText=10, ccpFtn=0)
    assert S.note_ranges(fib, b"", "footnotes") == []


def test_header_ranges_empty_without_plc():
    fib = FakeFibAttrs({"PlcfHdd": None}, ccpText=10, ccpFtn=0, ccpHdd=5)
    assert S.header_ranges(fib, b"", b"") == []
