# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Portions of this file are derived from LibreOffice
# sw/source/writerfilter/rtftok/ (rtftokenizer.cxx, rtfdocumentimpl.cxx,
# rtfdispatchdestination.cxx, rtfdispatchflag.cxx, rtfdispatchsymbol.cxx,
# rtfdispatchvalue.cxx, rtfcharsets.cxx) (MPL-2.0).
"""Rich Text Format (.rtf) reader -> the unified Document model.

The tokenizer and the group/destination state machine follow LibreOffice's
writerfilter RTF reader (rtftok), which is the oracle: text is decoded the way
RTFDocumentImpl::checkUnicode does it (bytes of a run are collected and decoded
with the encoding of the current state when the run ends, \\uN code units are
collected separately), destinations LibreOffice does not handle are skipped,
and tables are assembled from the \\intbl / \\cell / \\row buffer the way
RTFDocumentImpl buffers a row until \\row. RTF 1.9.1 is the spec reference.

Everything read from the file is untrusted: group nesting, \\itap depth and
\\binN lengths are bounded, and every loop advances over the input.
"""
from __future__ import annotations

import binascii
import codecs
import re
import struct

from .images import _dib_to_bmp, blip_looks_valid, dib_complete, wmf_looks_valid
from .model import IMAGE_MARK, NOTE_MARK, Cell, Document, Image, Link, Paragraph, Row, Table

# ------------------------------------------------------------------ limits
_MAX_DEPTH = 3000        # group states kept; deeper groups are tracked by a counter only
_MAX_ITAP = 64           # nested table depth (\itapN); rtfdispatchvalue.cxx grows a buffer per level
_MAX_FIELD_INST = 1 << 16
_MAX_CELLS = 4096        # cell definitions per row
_FUZZ = 20               # twips: column edges this close are one grid line (sw xmltble.cxx COLFUZZY)

_UTF8_BOM = b"\xef\xbb\xbf"


def rtf_offset(data: bytes):
    """Offset of the opening '{' of an RTF document, or None.

    The signature is '{\\rtf' (RTF 1.9.1 "RTF Syntax": <File> = '{' <header>
    <document> '}', <header> starts with \\rtfN). A UTF-8 BOM and leading
    white space before it are tolerated (the tokenizer ignores CR/LF outside
    groups, rtftokenizer.cxx resolveParse)."""
    head = bytes(data[:4096])
    p = 3 if head.startswith(_UTF8_BOM) else 0
    while p < len(head) and head[p] in b" \t\r\n\x0c\x0b\x00":
        p += 1
    return p if head[p:p + 5] == b"{\\rtf" else None


# ------------------------------------------------------------------ encodings
_SYMBOL = "symbol"
# \fcharset1 (DEFAULT_CHARSET) is osl_getThreadTextEncoding() in LibreOffice: UTF-8 on the
# reference machine. Bytes that are not UTF-8 are read in the ANSI code page instead (see
# docs/lo_disagreements.md: LibreOffice turns them into U+FFFD).
_THREAD = "thread"
# rtfcharsets.cxx aRTFEncodings: \fcharsetN -> Windows code page (0 = CP_ACP default).
_CHARSET_CP = {0: 1252, 1: 0, 2: 42, 77: 10000, 78: 10001, 79: 10003, 80: 10008, 81: 10002,
               83: 10005, 84: 10004, 85: 10006, 86: 10081, 87: 10021, 88: 10029, 89: 10007,
               128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253, 162: 1254,
               163: 1258, 177: 1255, 178: 1256, 186: 1257, 204: 1251, 222: 874, 238: 1250,
               254: 437, 255: 850}
# rtfcharsets.cxx aRTFFontNameSuffixes: "Arial CE" is Arial in code page 1250, etc.
_NAME_SUFFIX_CP = {"Baltic": 1257, "CE": 1250, "Cyr": 1251, "Greek": 1253, "Tur": 1254,
                   "(Hebrew)": 1255, "(Arabic)": 1256, "(Vietnamese)": 1258}
# rtl_getTextEncodingFromWindowsCodePage equivalents that Python spells differently.
_CP_CODEC = {42: _SYMBOL, 1361: "johab", 10000: "mac_roman", 10001: "shift_jis", 10002: "big5",
             10003: "cp949", 10004: "mac_arabic", 10005: "cp1255", 10006: "mac_greek",
             10007: "mac_cyrillic", 10008: "gb2312", 10021: "cp874", 10029: "mac_latin2",
             10079: "mac_iceland", 10081: "mac_turkish", 20127: "ascii", 20866: "koi8_r",
             21866: "koi8_u", 28591: "latin_1", 28592: "iso8859_2", 28595: "iso8859_5",
             65001: "utf_8", 932: "cp932", 936: "gbk", 949: "cp949", 950: "cp950"}
_DEFAULT_CODEC = "cp1252"   # locale guess of getEncoding() / CP_ACP for an en-US LibreOffice
_DBCS = {"cp932", "shift_jis", "gbk", "gb2312", "cp949", "cp950", "big5", "johab", "utf_8"}


def _codec_for_cp(cp: int):
    if cp == 0:
        return _DEFAULT_CODEC
    c = _CP_CODEC.get(cp)
    if c is not None:
        return c
    name = "cp%d" % cp
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


_SB_TABLES: dict = {}


def _decode(b: bytes, enc: str) -> str:
    """OStringToOUString(bytes, encoding). Single-byte code pages map bytes
    the code page leaves undefined to the same code point (as rtl does for
    cp1252's holes); Symbol maps byte b to U+F000+b (RTL_TEXTENCODING_SYMBOL)."""
    if enc == _SYMBOL:
        return "".join(chr(0xF000 + x) if x >= 0x20 else chr(x) for x in b)
    if enc == _THREAD:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            enc = _DEFAULT_CODEC
    if enc in _DBCS:
        return b.decode(enc, "replace")
    tbl = _SB_TABLES.get(enc)
    if tbl is None:
        tbl = {}
        try:
            for i in range(256):
                try:
                    ch = bytes([i]).decode(enc)
                except UnicodeDecodeError:
                    ch = chr(i)
                if ch != chr(i):
                    tbl[i] = ch
        except LookupError:
            tbl = {}
        _SB_TABLES[enc] = tbl
    s = b.decode("latin-1")
    return s.translate(tbl) if tbl else s


def _filter_controls(s: str) -> str:
    """FilterControlChars: drop C0 controls except TAB/CR/LF."""
    if any(c < " " and c not in "\t\r\n" for c in s):
        return "".join(c for c in s if c >= " " or c in "\t\r\n")
    return s


# ------------------------------------------------------------------ destinations
(D_NORMAL, D_SKIP, D_FONTTBL, D_FONTENTRY, D_COLORTBL, D_STYLESHEET, D_STYLEENTRY, D_LISTTABLE,
 D_LIST, D_LISTLEVEL, D_LEVELTEXT, D_LEVELNUMBERS, D_LOTABLE, D_LO, D_FIELD, D_FLDINST, D_FLDRSLT,
 D_PICT, D_SHPPICT, D_SHP, D_SHPINST, D_SP, D_SN, D_SV, D_SHPTXT, D_OBJECT, D_RESULT, D_COLLECT,
 D_PN, D_PNTXTA, D_PNTXTB, D_NESTPROPS, D_UPR, D_LISTPICTURE, D_SILENT, D_FOOTNOTE, D_SHPGRP,
 D_DO, D_LFOLEVEL, D_BACKGROUND, D_INFO) = range(41)

# Destinations whose text is kept as "destination text" (rtfdocumentimpl.cxx text():
# appendDestinationText) instead of going to the document.
_COLLECTING = {D_FONTTBL, D_FONTENTRY, D_LEVELTEXT, D_SN, D_SV, D_COLLECT, D_PNTXTA, D_PNTXTB}
# Destinations whose text is dropped (stylesheet/revtbl need groups; colour table ';').
_SILENT = {D_SKIP, D_COLORTBL, D_STYLESHEET, D_SILENT, D_LEVELNUMBERS, D_PICT, D_UPR}
# LibreOffice pushState(): a group inside these destinations is NORMAL again.
_CHILD_NORMAL = {D_FLDRSLT, D_SHPTXT, D_FLDINST, D_PICT, D_FOOTNOTE}

# Every control word rtftokenizer.cxx classifies as a destination. One that
# dispatchDestination does not handle is skipped together with its group, with
# or without \* (rtfdispatchdestination.cxx default branch).
_LO_DESTINATIONS = frozenset(b"""aftncn aftnsep aftnsepc annotation atnauthor atndate atnicn atnid
atnparent atnref atntime atrfend atrfstart author background bkmkend bkmkstart blipuid buptim category
colorschememapping colortbl comment company creatim datafield datastore defchp defpap do doccomm
docvar dptxbxtext ebcend ebcstart factoidname falt fchars ffdeftext ffentrymcr ffexitmcr ffformat
ffhelptext ffl ffname ffstattext field file filetbl fldinst fldrslt fldtype fname fontemb fontfile
fonttbl footer footerf footerl footerr footnote formfield ftncn ftnsep ftnsepc g generator gridtbl
header headerf headerl headerr hl hlfr hlinkbase hlloc hlsrc hsv htmltag info keycode keywords
latentstyles lchars levelnumbers leveltext lfolevel linkval list listlevel listname listoverride
listoverridetable listpicture liststylename listtable listtext lsdlockedexcept mailmerge manager
mmath mmathPict mmathPr moMath moMathPara moMathParaPr nesttableprops nextfile nonesttables objalias
objclass objdata object objname objsect objtime oldcprops oldpprops oldsprops oldtprops oleclsid
operator panose password passwordhash pgp pgptbl picprop pict pn pnseclvl pntext pntxta pntxtb
printim private propname protend protstart protusertbl pxe result revtbl revtim rsidtbl rtf rxe shp
shpgrp shpinst shppict shprslt shptxt sn sp staticval stylesheet subject sv svb tc template themedata
title txe ud upr userprops wgrffmtfilter windowcaption writereservation writereservhash xe xform
xmlattrname xmlattrvalue xmlclose xmlname xmlnstbl xmlopen flymaincnt""".split())


def _is_math_dest(w: bytes) -> bool:
    # Office Math (\mmath, \moMath, \mr, ...) becomes an embedded formula object in
    # LibreOffice, which the ODT walker of the oracle (and this library) does not report.
    return len(w) > 1 and w[:1] == b"m" and (w[1:2].isupper() or w in _MATH_LOWER)


_MATH_LOWER = frozenset(b"""macc maccPr mbar mbarPr mbox mboxPr mchr md mdeg mden mdPr me mf mfName
mfPr mfunc mfuncPr mlim mlimloc mlimlow mlimupp mm mmath mmathPict mmathPr mmPr mmr mnary mnaryPr
mnum mpos mr mrad mradPr msub msup mtype""".split())


# ------------------------------------------------------------------ small records
class _PP:
    """Paragraph properties that matter for structure (copy-on-write: replaced, never mutated)."""
    __slots__ = ("style", "outline", "ls", "ilvl", "pn", "intbl", "itap", "frame")

    def __init__(self, style=0, outline=None, ls=None, ilvl=None, pn=None, intbl=False, itap=None, frame=False):
        self.style = style
        self.outline = outline
        self.ls = ls
        self.ilvl = ilvl
        self.pn = pn
        self.intbl = intbl
        self.itap = itap
        self.frame = frame

    def but(self, **kw):
        n = _PP(self.style, self.outline, self.ls, self.ilvl, self.pn, self.intbl, self.itap, self.frame)
        for k, v in kw.items():
            setattr(n, k, v)
        return n


_PP_DEFAULT = _PP()


class _Style:
    __slots__ = ("name", "kind", "outline", "basedon", "ls", "ilvl")

    def __init__(self):
        self.name = ""
        self.kind = "p"
        self.outline = None
        self.basedon = None
        self.ls = None
        self.ilvl = None


class _Level:
    __slots__ = ("nfc", "start", "text", "norestart")

    def __init__(self):
        self.nfc = 0
        self.start = 1
        self.text = None
        self.norestart = False


class _Field:
    __slots__ = ("inst", "start", "sink", "serial", "result", "done", "url")

    def __init__(self):
        self.url = None
        self.done = False
        self.inst = []
        self.start = None
        self.sink = None
        self.serial = -1
        self.result = False


class _Pict:
    __slots__ = ("kind", "hex", "bin", "picw", "pich", "goalw", "goalh", "wbm", "size", "shape")

    def __init__(self):
        self.shape = None
        self.kind = None
        self.hex = []
        self.bin = None
        self.picw = self.pich = self.goalw = self.goalh = 0
        self.wbm = [0, 1, 0]   # bits/pixel, planes, width bytes
        self.size = 0


class _Shape:
    __slots__ = ("props", "pib", "had_text", "in_group")

    def __init__(self):
        self.props = {}
        self.pib = None
        self.had_text = False
        self.in_group = False


class _State:
    __slots__ = ("dest", "sink", "enc", "uc", "skip", "deleted", "pp", "field", "inst", "pict", "shape",
                 "sp", "obj", "dtext", "listpic", "bg", "inbuf", "entry", "level", "lo", "pn", "font")

    def copy(self) -> "_State":
        n = _State.__new__(_State)
        for k in _State.__slots__:
            setattr(n, k, getattr(self, k))
        return n


# ------------------------------------------------------------------ output sink
class _Sink:
    """One story being written (main text, one footnote, one header, one text box...).

    Paragraphs are appended to `blocks`; table content is buffered per row the
    way RTFDocumentImpl buffers it (m_aTableBufferStack) until \\row / \\nestrow."""

    def __init__(self, story: str):
        self.story = story
        self.blocks: list = []
        self.parts: list = []
        self.images: list = []
        self.notes: list = []
        self.links: list = []
        self.plen = 0
        self.serial = 0
        self.has = False           # current paragraph got any content (text, picture, note)
        self.bufs: list = [[]]     # table buffer stack ('p', Paragraph) / ('c',) / ('n', Row)
        self.epoch = 0             # incremented by \row: states pointing at the old buffer leave it
        self.table = None          # open top-level table (rows are added until a normal paragraph)
        self.rowdef: list = []     # top-level cell definitions (right edge, hmerge, vmerge)
        self.prevdef: list = []
        self.nestdef: list = []
        self.cell = [0, 0]         # pending \clmgf/\clmrg (1/2), \clvmgf/\clvmrg (1/2)
        self.trleft = 0
        self.nest_trleft = 0
        self.header_row = False
        self.nest_header = False
        self.floating = False
        self.width_after = 0
        self.open_links: list = []
        self.had_picture = False

    def text(self, s: str) -> None:
        if s:
            self.parts.append(s)
            self.plen += len(s)
            self.has = True

    def take(self) -> Paragraph:
        for f in self.open_links:
            # a hyperlink result that spans paragraphs links each paragraph's part
            start = f.start if f.serial == self.serial else 0
            if self.plen > start and len(self.links) < 10000:
                self.links.append(Link(start, self.plen, f.url))
        p = Paragraph("".join(self.parts), images=self.images, notes=self.notes, links=self.links)
        self.parts, self.images, self.notes, self.links = [], [], [], []
        self.plen = 0
        self.has = False
        self.serial += 1
        self.had_picture = False
        return p


# ------------------------------------------------------------------ numbering labels
def _roman(n: int) -> str:
    if n <= 0 or n >= 4000:
        return str(n)
    out = []
    for v, s in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                 (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def _letters(n: int) -> str:
    if n <= 0:
        return str(n)
    # Word repeats the letter: a..z, aa..zz, aaa..
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _fmt_num(n: int, nfc: int) -> str:
    """[MS-DOC]/RTF \\levelnfcN number formats (0 decimal, 1/2 roman, 3/4 letters,
    5 ordinal, 22 leading zero); others render as decimal."""
    if nfc == 1:
        return _roman(n).upper()
    if nfc == 2:
        return _roman(n)
    if nfc == 3:
        return _letters(n).upper()
    if nfc == 4:
        return _letters(n)
    if nfc == 5:
        suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return "%d%s" % (n, suf)
    if nfc == 22:
        return "%02d" % n
    if nfc in (23, 255):
        return ""
    return str(n)


# ------------------------------------------------------------------ tokens
_WORD = re.compile(rb"\\([a-zA-Z]+)(-?)(\d*) ?")
_TEXT = re.compile(rb"[^\\{}\r\n]+")
_HEXDIGITS = b"0123456789abcdefABCDEF"
_HEX_CLEAN = re.compile(rb"[^0-9a-fA-F \r\n]")


# ------------------------------------------------------------------ the reader
class _Parser:
    def __init__(self, data: bytes, images_enabled: bool):
        self.data = data
        self.n = len(data)
        self.images_enabled = images_enabled
        self.doc = Document(format="rtf")
        self.fonts: dict = {}          # font index -> codec (m_aFontEncodings)
        self.cur_font = None           # font table entry being read (m_nCurrentFontIndex)
        self.cur_font_enc = None       # m_nCurrentEncoding
        self.default_enc = _DEFAULT_CODEC
        self.deff = 0
        self.styles: dict = {}
        self.lists: dict = {}          # \listid -> {ilvl: _Level}
        self.overrides: dict = {}      # \ls -> (listid, {ilvl: start})
        self.counters: dict = {}
        self.hexbuf = bytearray()
        self.ubuf: list = []
        self.star = False
        self.main = _Sink("main")
        self.note_sinks = {"footnotes": [], "endnotes": []}
        self.extra_sinks = {"textboxes": [], "comments": [], "headers": []}
        self.frames: list = []         # main-story paragraphs in positioned frames (-> text boxes)
        self.section_hf: set = set()
        self.row_meta: dict = {}       # id(Row) -> [(left, right, vmerge)] for the kept cells
        self.cellxmax: dict = {}       # id(Sink) -> widest row edge of the open table
        self.stack: list = []
        self.overflow = 0
        self.info: dict = {}
        st = _State()
        st.dest = D_NORMAL
        st.sink = self.main
        st.enc = _DEFAULT_CODEC
        st.uc = 1
        st.skip = 0
        st.deleted = False
        st.pp = _PP_DEFAULT
        st.field = st.inst = st.pict = st.shape = st.sp = st.obj = st.dtext = None
        st.listpic = st.bg = False
        st.inbuf = -1
        st.entry = st.level = st.lo = st.pn = st.font = None
        self.st = st
        self._build_handlers()

    # ---------------------------------------------------------- main loop
    def run(self, start: int) -> None:
        data, n = self.data, self.n
        word_match, text_match = _WORD.match, _TEXT.match
        pos = start
        while pos < n:
            c = data[pos]
            if c == 0x7B:      # {
                pos += 1
                self._flush()
                if len(self.stack) >= _MAX_DEPTH:
                    self.overflow += 1
                else:
                    self._push()
            elif c == 0x7D:    # }
                pos += 1
                self._flush()
                if self.overflow:
                    self.overflow -= 1
                    continue
                if not self.stack:
                    break
                if len(self.stack) == 1 and self.st.sink.has:
                    # popState at group 1: the last paragraph is ended with the state it was written in
                    self._end_par(self.st, False)
                self._pop()
                if not self.stack:
                    # rtftokenizer.cxx: the closing brace of the outermost group ends the document
                    break
            elif c == 0x5C:    # backslash
                m = word_match(data, pos)
                if m is not None:
                    pos = m.end()
                    word = m.group(1)
                    digits = m.group(3)
                    if digits:
                        v = int(digits) if len(digits) <= 10 else 1 << 40
                        if v > 0x7FFFFFFF:
                            v = 0   # rtl toInt32 overflow
                        if m.group(2):
                            v = -v
                        has = True
                    else:
                        v, has = 0, False
                    pos = self._keyword(word, v, has, pos)
                else:
                    if pos + 1 >= n:
                        break
                    sym = data[pos + 1]
                    pos += 2
                    if sym == 0x27:
                        pos = self._hexchar(pos)
                    else:
                        self._symbol(sym, pos)
            elif c == 0x0D or c == 0x0A:
                pos += 1
            else:
                m = text_match(data, pos)
                end = m.end()
                if not self.stack:
                    break   # CHAR_OVER: text outside the outermost group
                pos = self._chars(pos, end)
        self._flush()

    # ---------------------------------------------------------- groups
    def _push(self) -> None:
        st = self.st
        self.stack.append(st)
        n = st.copy()
        d = st.dest
        if d == D_FONTTBL:
            n.dest = D_FONTENTRY
            n.dtext = []
        elif d == D_STYLESHEET:
            n.dest = D_STYLEENTRY
            n.dtext = []
            n.entry = _Style()
            n.pp = _PP_DEFAULT
        elif d in _CHILD_NORMAL:
            n.dest = D_NORMAL
        elif d in _COLLECTING:
            n.dtext = st.dtext
        self.st = n

    def _pop(self) -> None:
        old = self.st
        self.st = self.stack.pop()
        d = old.dest
        if d == D_NORMAL or d == D_SKIP:
            return
        h = self._pop_handlers.get(d)
        if h is not None:
            h(old)

    # ---------------------------------------------------------- characters
    def _chars(self, pos: int, end: int) -> int:
        st = self.st
        d = st.dest
        data = self.data
        if d == D_SKIP:
            return end
        if d == D_PICT:
            if st.pict is not None and st.pict.size < self.n:
                seg = data[pos:end]
                st.pict.hex.append(seg)
                st.pict.size += len(seg)
            return end
        enc = st.enc
        if enc in ("cp932", "shift_jis"):
            # rtfdocumentimpl.cxx resolveChars: the second byte of a Shift-JIS pair
            # may be '\\', '{' or '}' and is still text.
            i = pos
            while i < end:
                b = data[i]
                i += 2 if (0x81 <= b <= 0x9F or b >= 0xE0) else 1
            if i > end:
                end = min(i, self.n)
        seg = data[pos:end]
        if st.skip:
            k = min(st.skip, len(seg))
            st.skip -= k
            seg = seg[k:]
            if not seg:
                return end
        if self.ubuf:
            self._flush_u()
        self.hexbuf += seg
        self._flush_h()
        return end

    def _hexchar(self, pos: int) -> int:
        """\\'hh: two characters (CR/LF ignored) form one byte; a non-hex
        character still counts (rtftokenizer.cxx: "\\'<number><junk> means \\'0<number>")."""
        data, n = self.data, self.n
        b = 0
        count = 2
        while count and pos < n:
            ch = data[pos]
            if ch in (0x0D, 0x0A):
                pos += 1
                continue
            if ch in (0x5C, 0x7B, 0x7D):
                break
            pos += 1
            if ch in _HEXDIGITS:
                b = (b << 4) | int(chr(ch), 16)
            count -= 1
        st = self.st
        if st.dest == D_SKIP or st.dest == D_PICT:
            return pos
        if st.skip:
            st.skip -= 1
            return pos
        if self.ubuf:
            self._flush_u()
        if b in (0x0D, 0x0A) and st.dest not in (D_LEVELTEXT, D_LEVELNUMBERS):
            # "\'0d\'0a is interpreted as 2 breaks"
            self._flush_h()
            self._par()
        else:
            self.hexbuf.append(b)
        return pos

    def _flush(self) -> None:
        if self.ubuf:
            self._flush_u()
        if self.hexbuf:
            self._flush_h()

    def _flush_u(self) -> None:
        u = self.ubuf
        self.ubuf = []
        s = struct.pack("<%dH" % len(u), *u).decode("utf-16-le", "surrogatepass")
        if any("\ud800" <= c <= "\udfff" for c in s):
            # an unpaired surrogate is not a character (dropped from the ODF output)
            s = "".join(c for c in s if not "\ud800" <= c <= "\udfff")
        if self.st.dest not in (D_LEVELTEXT, D_LEVELNUMBERS):
            s = _filter_controls(s)
        self._text(s)

    def _flush_h(self) -> None:
        b = bytes(self.hexbuf)
        self.hexbuf = bytearray()
        st = self.st
        enc = st.enc
        if enc == _SYMBOL and (st.dest == D_FONTENTRY or st.inst is not None):
            enc = "cp1252"   # checkUnicode: Symbol inside font names and field instructions
        s = _decode(b, enc)
        if st.dest not in (D_LEVELTEXT, D_LEVELNUMBERS):
            s = _filter_controls(s)
        self._text(s)

    def _text(self, s: str, raw: bool = False) -> None:
        if not s:
            return
        if not raw and len(s) == 1 and (s == "\r" or s == "\n"):
            return   # text(): a lone CR/LF is not text (\line goes through singleChar)
        st = self.st
        d = st.dest
        if d in _SILENT:
            return
        if d in _COLLECTING:
            if st.dtext is not None:
                st.dtext.append(s)
            return
        if d == D_STYLEENTRY:
            st.dtext.append(s)
            if s.endswith(";"):
                self._style_done(st)
            return
        if st.inst is not None:
            if len(st.inst.inst) < _MAX_FIELD_INST:
                st.inst.inst.append(s)
            return
        if st.deleted:
            return
        st.sink.text(s)

    def _emit_char(self, s: str) -> None:
        self._flush()
        self._text(s)

    # ---------------------------------------------------------- control symbols
    def _symbol(self, sym: int, pos: int) -> None:
        st = self.st
        if st.dest == D_SKIP:
            return
        if sym == 0x2A:    # \*
            self._flush()
            self.star = True
            return
        star = self.star
        self.star = False
        if sym in (0x5C, 0x7B, 0x7D):
            self._emit_char(chr(sym))
        elif sym == 0x7E:   # \~ non-breaking space
            self._emit_char("\u00a0")
        elif sym == 0x5F:   # \_ non-breaking hyphen (SVT_HARD_HYPHEN)
            self._emit_char("\u2011")
        elif sym == 0x2D:   # \- optional hyphen (SVT_SOFT_HYPHEN)
            self._emit_char("\u00ad")
        elif sym in (0x0A, 0x0D):   # "\<LF>" is \par
            self._flush()
            self._par()
        elif sym in (0x7C, 0x3A):   # \| formula, \: index subentry
            self._flush()
        elif star:
            st.dest = D_SKIP

    # ---------------------------------------------------------- control words
    def _keyword(self, word: bytes, v: int, has: bool, pos: int) -> int:
        st = self.st
        if st.dest == D_SKIP:
            # rtftokenizer.cxx dispatchKeyword: skip binary data explicitly in skipped groups
            if word == b"bin" and v > 0:
                return min(self.n, pos + v)
            return pos
        if word == b"u":
            if self.hexbuf:
                self._flush_h()
            self._u(v)
            self.star = False
            return pos
        self._flush()
        star = self.star
        self.star = False
        h = self._handlers.get(word)
        if star and word not in self._star_ok:
            st.dest = D_SKIP
            return pos
        if h is None:
            if st.dest == D_UPR:
                st.dest = D_SKIP
            elif word in _LO_DESTINATIONS or _is_math_dest(word):
                st.dest = D_SKIP
            return pos
        if st.dest == D_UPR and word != b"ud":
            st.dest = D_SKIP
            return pos
        r = h(v, has, pos)
        return pos if r is None else r

    def _u(self, v: int) -> None:
        st = self.st
        if -32768 <= v <= 65535:
            if st.dest == D_SKIP:
                return
            self.ubuf.append(v & 0xFFFF)
            st.skip = st.uc

    # ---------------------------------------------------------- handler table
    def _build_handlers(self) -> None:
        H: dict = {}

        def dest(code, collect=False, **extra):
            def f(v, has, pos):
                st = self.st
                st.dest = code
                if collect:
                    st.dtext = []
            return f

        for w in (b"title", b"subject", b"author", b"operator", b"keywords", b"doccomm", b"company"):
            H[w] = self._make_info(w)
        for w in (b"comment", b"docvar", b"listname", b"bkmkstart", b"bkmkend", b"xe", b"tc", b"tcn",
                  b"atnid", b"atnauthor", b"atndate", b"atnref", b"atrfstart", b"atrfend", b"falt",
                  b"ffname", b"ffl", b"datafield", b"formfield", b"objclass", b"propname", b"staticval",
                  b"generator", b"userprops", b"creatim", b"revtim", b"printim"):
            H[w] = dest(D_COLLECT, True)
        for w in (b"listtext", b"pntext", b"nonesttables", b"shprslt", b"objdata",
                  b"nonshppict", b"mmathPict", b"ftnsep", b"revtbl", b"themedata", b"colorschememapping",
                  b"datastore", b"latentstyles", b"xmlnstbl", b"rsidtbl", b"pgdsctbl", b"blipuid"):
            H[w] = dest(D_SKIP)
        H[b"rtf"] = lambda v, has, pos: None
        H[b"fonttbl"] = dest(D_FONTTBL, True)
        H[b"colortbl"] = dest(D_COLORTBL)
        H[b"stylesheet"] = dest(D_STYLESHEET)
        H[b"info"] = dest(D_INFO)
        H[b"flymaincnt"] = lambda v, has, pos: None
        H[b"listtable"] = dest(D_LISTTABLE)
        H[b"listoverridetable"] = dest(D_LOTABLE)
        H[b"listpicture"] = self._kw_listpicture
        H[b"list"] = self._kw_list
        H[b"listlevel"] = self._kw_listlevel
        H[b"leveltext"] = self._kw_leveltext
        H[b"levelnumbers"] = dest(D_LEVELNUMBERS)
        H[b"listoverride"] = self._kw_listoverride
        H[b"lfolevel"] = self._kw_lfolevel
        H[b"field"] = self._kw_field
        H[b"fldinst"] = self._kw_fldinst
        H[b"fldrslt"] = self._kw_fldrslt
        H[b"pict"] = self._kw_pict
        H[b"picprop"] = self._kw_picprop
        H[b"objautlink"] = H[b"objlink"] = self._kw_objlink
        H[b"shppict"] = dest(D_SHPPICT)
        H[b"shp"] = self._kw_shp
        H[b"shpgrp"] = self._kw_shpgrp
        H[b"shpinst"] = dest(D_SHPINST)
        H[b"sp"] = self._kw_sp
        H[b"sn"] = dest(D_SN, True)
        H[b"sv"] = dest(D_SV, True)
        H[b"shptxt"] = self._kw_shptxt
        H[b"dptxbxtext"] = self._kw_shptxt
        H[b"do"] = dest(D_DO)
        H[b"background"] = self._kw_background
        H[b"object"] = self._kw_object
        H[b"result"] = dest(D_RESULT)
        H[b"nesttableprops"] = self._kw_nesttableprops
        for w in (b"header", b"footer", b"headerl", b"headerr", b"headerf", b"footerl", b"footerr", b"footerf"):
            H[w] = self._make_hf(w)
        H[b"footnote"] = self._kw_footnote
        H[b"annotation"] = self._kw_annotation
        H[b"pn"] = self._kw_pn
        H[b"pntxta"] = self._make_pntxt(D_PNTXTA)
        H[b"pntxtb"] = self._make_pntxt(D_PNTXTB)
        H[b"upr"] = dest(D_UPR)
        H[b"ud"] = dest(D_NORMAL)
        # encodings / fonts
        H[b"ansi"] = self._kw_ansi
        H[b"mac"] = self._kw_cpdefault(10000)
        H[b"pc"] = self._kw_cpdefault(437)
        H[b"pca"] = self._kw_cpdefault(850)
        H[b"ansicpg"] = self._kw_ansicpg
        H[b"cpg"] = self._kw_cpg
        H[b"deff"] = self._kw_deff
        H[b"f"] = self._kw_f
        H[b"fcharset"] = self._kw_fcharset
        H[b"plain"] = self._kw_plain
        H[b"uc"] = self._kw_uc
        H[b"bin"] = self._kw_bin
        H[b"rtlch"] = self._kw_rtlch
        H[b"deleted"] = self._kw_deleted
        # characters
        for w, ch in ((b"tab", "\t"), (b"emdash", "\u2014"), (b"endash", "\u2013"), (b"bullet", "\u2022"),
                      (b"lquote", "\u2018"), (b"rquote", "\u2019"), (b"ldblquote", "\u201c"),
                      (b"rdblquote", "\u201d"), (b"line", "\n")):
            H[w] = self._make_char(ch)
        H[b"par"] = lambda v, has, pos: self._par()
        H[b"sect"] = self._kw_sect
        H[b"page"] = self._kw_page
        H[b"column"] = self._kw_page
        H[b"cell"] = self._kw_cell
        H[b"nestcell"] = self._kw_cell
        H[b"row"] = self._kw_row
        H[b"nestrow"] = self._kw_nestrow
        H[b"chftn"] = H[b"chatn"] = lambda v, has, pos: None
        H[b"chpgn"] = self._make_char("1")
        # paragraph properties
        H[b"pard"] = self._kw_pard
        H[b"intbl"] = self._kw_intbl
        H[b"itap"] = self._kw_itap
        H[b"s"] = self._kw_s
        H[b"cs"] = self._kw_cs
        H[b"ds"] = self._kw_cs
        H[b"ts"] = self._kw_cs
        H[b"sbasedon"] = self._kw_sbasedon
        H[b"outlinelevel"] = self._kw_outlinelvl
        H[b"ls"] = self._kw_ls
        H[b"ilvl"] = self._kw_ilvl
        H[b"listid"] = self._kw_listid
        H[b"levelnfc"] = H[b"levelnfcn"] = self._kw_levelnfc
        H[b"levelstartat"] = self._kw_levelstartat
        H[b"levelnorestart"] = self._kw_levelnorestart
        H[b"listoverridestartat"] = lambda v, has, pos: None
        H[b"pnlvlbody"] = self._make_pn("body")
        H[b"pnlvlblt"] = self._make_pn("blt")
        for w, nfc in ((b"pndec", 0), (b"pnucrm", 1), (b"pnlcrm", 2), (b"pnucltr", 3), (b"pnlcltr", 4),
                       (b"pnord", 5), (b"pnordt", 0), (b"pncard", 0)):
            H[w] = self._make_pnfmt(nfc)
        H[b"pnstart"] = self._kw_pnstart
        # tables
        H[b"trowd"] = self._kw_trowd
        H[b"cellx"] = self._kw_cellx
        H[b"clmgf"] = self._make_merge(0, 1)
        H[b"clmrg"] = self._make_merge(0, 2)
        H[b"clvmgf"] = self._make_merge(1, 1)
        H[b"clvmrg"] = self._make_merge(1, 2)
        H[b"trleft"] = self._kw_trleft
        H[b"trhdr"] = self._kw_trhdr
        H[b"trwWidthA"] = self._kw_trwwidtha
        # positioned frames (RTFFrame::hasProperties)
        for w in (b"posx", b"posy", b"absw", b"absh"):
            H[w] = self._kw_frame_value
        for w in (b"tposx", b"tposy", b"tposnegx", b"tposnegy", b"tphmrg", b"tphpg", b"tphcol", b"tpvmrg",
                  b"tpvpg", b"tpvpara", b"tposxc", b"tposxi", b"tposxo", b"tposxl", b"tposxr", b"tposyc",
                  b"tposyb", b"tposyt", b"tposyil", b"tposyin", b"tposyout"):
            H[w] = self._kw_tfloat
        for w in (b"posxc", b"posxr", b"posxi", b"posxo", b"phmrg", b"phpg", b"posyt", b"posyb",
                  b"posyc", b"posyin", b"posyout", b"pvpara", b"pvpg", b"nowrap", b"overlay"):
            H[w] = self._kw_frame_flag
        # pictures
        for w, kind in ((b"pngblip", "png"), (b"jpegblip", "jpeg"), (b"emfblip", "emf"),
                        (b"wmetafile", "wmf"), (b"dibitmap", "dib"), (b"wbitmap", "wbitmap"),
                        (b"macpict", "pict"), (b"pmmetafile", "pmm")):
            H[w] = self._make_pictkind(kind)
        for w in (b"picw", b"pich", b"picwgoal", b"pichgoal", b"wbmbitspixel", b"wbmplanes", b"wbmwidthbytes"):
            H[w] = self._make_pictval(w)
        self._handlers = H
        self._star_ok = {w for w in H if w in _LO_DESTINATIONS} | {b"cs", b"ds", b"ts", b"s", b"ud"}
        self._pop_handlers = {
            D_FONTENTRY: self._pop_fontentry, D_FONTTBL: self._pop_fontentry,
            D_STYLEENTRY: lambda old: None,
            D_LIST: self._pop_list, D_LISTLEVEL: self._pop_listlevel, D_LEVELTEXT: self._pop_leveltext,
            D_LO: self._pop_lo, D_FIELD: self._pop_field, D_PICT: self._pop_pict, D_SP: self._pop_sp,
            D_SN: self._pop_sn, D_SV: self._pop_sv, D_SHP: self._pop_shp, D_PN: self._pop_pn,
            D_PNTXTA: self._pop_pntxt, D_PNTXTB: self._pop_pntxt, D_FOOTNOTE: self._pop_substream,
            D_SHPTXT: self._pop_substream, D_COLLECT: self._pop_collect,
        }

    def _make_info(self, word):
        def f(v, has, pos):
            st = self.st
            st.dest = D_COLLECT
            st.dtext = []
            st.entry = ("info", word.decode())
        return f

    def _pop_collect(self, old) -> None:
        e = old.entry
        if isinstance(e, tuple) and e is not self.st.entry and e[0] == "info":
            self.info[e[1]] = "".join(old.dtext or ())

    # ---------------------------------------------------------- encodings / fonts
    def _kw_ansi(self, v, has, pos):
        self.st.enc = "cp1252"

    def _kw_cpdefault(self, cp):
        def f(v, has, pos):
            self.default_enc = _codec_for_cp(cp) or _DEFAULT_CODEC
            self.st.enc = self.default_enc
        return f

    def _kw_ansicpg(self, v, has, pos):
        enc = _codec_for_cp(v) or _DEFAULT_CODEC
        self.default_enc = enc
        self.st.enc = enc

    def _kw_cpg(self, v, has, pos):
        enc = _codec_for_cp(v) or _DEFAULT_CODEC
        self.cur_font_enc = enc
        self.st.enc = enc

    def _kw_deff(self, v, has, pos):
        self.deff = v

    def _font_enc(self, idx: int) -> str:
        """RTFDocumentImpl::getEncoding."""
        e = self.fonts.get(idx)
        if e is not None:
            return e
        return self.default_enc

    def _kw_f(self, v, has, pos):
        st = self.st
        if st.dest in (D_FONTTBL, D_FONTENTRY):
            if st.dtext:
                self._font_done(st)
            self.cur_font = v
        elif st.dest != D_LISTLEVEL:
            st.font = v
            st.enc = self._font_enc(v)

    def _kw_fcharset(self, v, has, pos):
        cp = _CHARSET_CP.get(v)
        if cp is None:
            return
        enc = _THREAD if cp == 0 else (_codec_for_cp(cp) or _DEFAULT_CODEC)
        self.cur_font_enc = enc
        self.st.enc = enc

    def _font_done(self, st) -> None:
        """handleFontTableEntry."""
        name = "".join(st.dtext or ())
        st.dtext[:] = [] if st.dtext is not None else []
        if not name:
            return
        if name.endswith(";"):
            name = name[:-1]
        sp = name.rfind(" ")
        if sp >= 0:
            cp = _NAME_SUFFIX_CP.get(name[sp + 1:])
            if cp is not None:
                self.cur_font_enc = _codec_for_cp(cp)
                st.enc = self.cur_font_enc
        if self.cur_font is not None and self.cur_font_enc is not None:
            if len(self.fonts) < 65536:
                self.fonts[self.cur_font] = self.cur_font_enc
            self.cur_font_enc = None

    def _pop_fontentry(self, old) -> None:
        if old.dtext:
            self._font_done(old)

    def _kw_plain(self, v, has, pos):
        self.st.enc = self._font_enc(self.deff)
        self.st.deleted = False

    def _kw_uc(self, v, has, pos):
        if -32768 <= v <= 32767:
            self.st.uc = max(v, 0)

    def _kw_bin(self, v, has, pos):
        """\\binN: N raw bytes follow (at least one byte is consumed, rtfdocumentimpl.cxx resolveChars BIN)."""
        st = self.st
        cnt = max(v, 1)
        end = min(self.n, pos + cnt)
        if st.pict is not None:
            # m_pBinaryData: also when \bin sits in a group nested in the \pict
            st.pict.bin = self.data[pos:end]
        return end

    def _kw_rtlch(self, v, has, pos):
        if self.default_enc == "cp1255":
            self.st.enc = self.default_enc

    def _kw_deleted(self, v, has, pos):
        self.st.deleted = (not has) or v != 0

    def _make_char(self, ch):
        def f(v, has, pos):
            self._text(ch, True)
        return f

    # ---------------------------------------------------------- paragraphs and tables
    def _par(self) -> None:
        st = self.st
        if st.dest == D_SKIP:
            return
        self._end_par(st, False)

    def _end_par(self, st, cell: bool) -> None:
        sink = st.sink
        p = sink.take()
        self._decorate(p, st.pp)
        top = sink.bufs[-1]
        if cell:
            top.append(("p", p))
            top.append(("c",))
            return
        if st.inbuf == sink.epoch and (st.pp.intbl or top):
            # dispatchSymbol(PAR) with a current table buffer that already holds
            # \intbl properties, a \par or a \cell: the paragraph is table content.
            if len(top) < (1 << 20):
                top.append(("p", p))
            return
        self._emit(sink, p, st.pp)

    def _emit(self, sink, p, pp) -> None:
        if pp.frame and (sink is self.main or sink.story == "headers"):
            # RTFFrame: a paragraph with frame properties becomes a text frame
            self.frames.append(p)
            return
        sink.table = None
        sink.blocks.append(p)

    def _kw_sect(self, v, has, pos):
        st = self.st
        if st.sink.has:
            self._end_par(st, False)
        if st.sink is self.main:
            self.section_hf = set()

    def _kw_page(self, v, has, pos):
        st = self.st
        sink = st.sink
        if st.inbuf == sink.epoch:
            return   # page breaks inside tables are ignored
        if sink.has:
            # dmapper splits the paragraph at a page break (GetSplitPgBreakAndParaMark)
            self._end_par(st, False)

    def _kw_cell(self, v, has, pos):
        st = self.st
        if st.dest == D_SKIP:
            return
        self._end_par(st, True)

    def _kw_pard(self, v, has, pos):
        st = self.st
        if st.sink.had_picture and st.dest != D_SKIP:
            # m_bHadPicture: \pard after a picture ends that paragraph (dispatchFlag PARD)
            self._end_par(st, False)
        st.pp = _PP_DEFAULT
        # pard keeps the current table buffer (it is allowed between \cell and \row)

    def _kw_intbl(self, v, has, pos):
        st = self.st
        st.inbuf = st.sink.epoch
        if not st.pp.intbl:
            st.pp = st.pp.but(intbl=True)

    def _kw_itap(self, v, has, pos):
        st = self.st
        st.pp = st.pp.but(itap=v)
        if v > 0:
            sink = st.sink
            while len(sink.bufs) < min(v, _MAX_ITAP):
                sink.bufs.append([])
            self._kw_intbl(0, False, pos)

    def _kw_trowd(self, v, has, pos):
        st = self.st
        sink = st.sink
        if st.dest == D_NESTPROPS:
            sink.nestdef = []
            sink.nest_trleft = 0
            sink.nest_header = False
        else:
            sink.rowdef = []
            sink.trleft = 0
            sink.header_row = False
            sink.floating = False
            st.inbuf = sink.epoch
        sink.cell = [0, 0]

    def _kw_cellx(self, v, has, pos):
        st = self.st
        sink = st.sink
        target = sink.nestdef if st.dest == D_NESTPROPS else sink.rowdef
        if len(target) < _MAX_CELLS:
            target.append((v, sink.cell[0], sink.cell[1]))
        sink.cell = [0, 0]

    def _make_merge(self, i, val):
        def f(v, has, pos):
            self.st.sink.cell[i] = val
        return f

    def _kw_trwwidtha(self, v, has, pos):
        st = self.st
        if st.dest != D_NESTPROPS:
            st.sink.width_after = v

    def _kw_tfloat(self, v, has, pos):
        st = self.st
        if st.dest != D_NESTPROPS:
            st.sink.floating = True

    def _kw_trleft(self, v, has, pos):
        st = self.st
        if st.dest == D_NESTPROPS:
            st.sink.nest_trleft = v
        else:
            st.sink.trleft = v

    def _kw_trhdr(self, v, has, pos):
        st = self.st
        if st.dest == D_NESTPROPS:
            st.sink.nest_header = True
        else:
            st.sink.header_row = True

    def _kw_nesttableprops(self, v, has, pos):
        st = self.st
        st.dest = D_NESTPROPS
        st.sink.nestdef = []
        st.sink.cell = [0, 0]

    def _kw_row(self, v, has, pos):
        st = self.st
        sink = st.sink
        if sink.has and st.inbuf == sink.epoch:
            # text after the last \cell ends a paragraph of the row buffer
            self._end_par(st, False)
        del sink.bufs[1:]          # "dropping extra table buffer"
        items = sink.bufs[0]
        sink.bufs[0] = []
        sink.epoch += 1
        defs = sink.rowdef or sink.prevdef
        if sink.rowdef:
            sink.prevdef = sink.rowdef
        trleft, header = sink.trleft, sink.header_row
        if sink.width_after > 0 and defs and items:
            # \trwWidthA: a fake last cell of that width (dispatchSymbol(ROW), the RTF
            # equivalent of OOXML gridAfter)
            items = items + [("p", Paragraph("")), ("c",)]
            defs = list(defs) + [(defs[-1][0] + sink.width_after, 0, 0)]
        sink.width_after = 0
        sink.rowdef = []
        sink.cell = [0, 0]
        if not items:
            return
        key = id(sink)
        if sink.table is None:
            self.cellxmax[key] = 0
        row = self._make_row(items, defs, trleft, header, self.cellxmax.get(key, 0))
        if row is None:
            return
        meta = self.row_meta.get(id(row))
        if meta:
            self.cellxmax[key] = max(self.cellxmax.get(key, 0), meta[-1][1])
        if sink.table is None:
            sink.table = Table()
            if sink.floating and (sink is self.main or sink.story == "headers"):
                # a positioned table (tblpPr) is imported as a floating table: a text frame
                self.frames.append(sink.table)
            else:
                sink.blocks.append(sink.table)
        sink.table.rows.append(row)

    def _kw_nestrow(self, v, has, pos):
        st = self.st
        sink = st.sink
        if len(sink.bufs) < 2:
            # LibreOffice rejects the document ("mismatch between \itap and number of \nestrow");
            # keep the text in the current buffer instead.
            return
        items = sink.bufs.pop()
        defs = sink.nestdef
        sink.nestdef = []
        if not items:
            return
        row = self._make_row(items, defs, sink.nest_trleft, sink.nest_header, 0)
        if row is not None:
            sink.bufs[-1].append(("n", row))

    def _make_row(self, items, defs, trleft, header, cellxmax):
        groups: list = [[]]
        for it in items:
            if it[0] == "c":
                groups.append([])
            else:
                groups[-1].append(it)
        trailing = groups.pop()
        if not groups:
            groups = [trailing]
        elif any(it[0] == "n" or it[1].text.strip() for it in trailing):
            groups[-1].extend(trailing)
        defs = list(defs)
        if defs and cellxmax and cellxmax - defs[-1][0] >= 23:
            # WW8TabDesc::CalcDefaults: widen the last cell to the widest row so far (MINLAY = 23)
            defs[-1] = (cellxmax,) + tuple(defs[-1][1:])
        row = Row(header=header)
        meta = []
        left = trleft
        for i, g in enumerate(groups):
            if i < len(defs):
                right, hm, vm = defs[i]
            else:
                right, hm, vm = (meta[-1][1] if meta else left) + 1440, 0, 0
            blocks = self._cell_blocks(g)
            if hm == 2 and row.cells:
                prev = row.cells[-1]
                prev.blocks.extend(b for b in blocks if isinstance(b, Table) or b.text.strip())
                meta[-1] = (meta[-1][0], max(right, meta[-1][1]), meta[-1][2])
            else:
                row.cells.append(Cell(blocks=blocks, header=header))
                meta.append((left, max(right, left), vm))
            left = max(right, left)
        self.row_meta[id(row)] = meta
        return row

    @staticmethod
    def _cell_blocks(items) -> list:
        blocks: list = []
        cur = None
        for it in items:
            if it[0] == "p":
                blocks.append(it[1])
                cur = None
            else:
                if cur is None:
                    cur = Table()
                    blocks.append(cur)
                cur.rows.append(it[1])
        if not blocks:
            blocks.append(Paragraph(""))
        return blocks

    # ---------------------------------------------------------- frames
    def _kw_frame_value(self, v, has, pos):
        if v != 0:
            self._set_frame()

    def _kw_frame_flag(self, v, has, pos):
        self._set_frame()

    def _set_frame(self) -> None:
        st = self.st
        if st.dest in (D_NORMAL, D_FLDRSLT, D_RESULT) and not st.pp.frame:
            st.pp = st.pp.but(frame=True)

    # ---------------------------------------------------------- styles
    def _kw_s(self, v, has, pos):
        st = self.st
        if st.dest == D_STYLEENTRY and st.entry is not None:
            st.entry.kind = "p"
            self._entry_idx(st, v)
        elif st.dest not in (D_LISTLEVEL, D_STYLESHEET):
            st.pp = st.pp.but(style=v)

    def _kw_cs(self, v, has, pos):
        st = self.st
        if st.dest == D_STYLEENTRY and st.entry is not None:
            st.entry.kind = "c"
            self._entry_idx(st, v)

    def _entry_idx(self, st, v) -> None:
        st.lo = v   # the style index travels in `lo` while inside a style entry

    def _kw_sbasedon(self, v, has, pos):
        st = self.st
        if st.dest == D_STYLEENTRY and st.entry is not None:
            st.entry.basedon = v

    def _kw_outlinelvl(self, v, has, pos):
        st = self.st
        if 0 <= v <= 9:     # DomainMapper: an invalid value is ignored by MS Word
            st.pp = st.pp.but(outline=v)

    def _style_done(self, st) -> None:
        e = st.entry
        name = "".join(st.dtext)
        st.dtext[:] = []
        if e is None:
            return
        if name.endswith(";"):
            name = name[:-1]
        e.name = name.strip()
        e.outline = st.pp.outline
        e.ls = st.pp.ls
        e.ilvl = st.pp.ilvl
        idx = st.lo if st.lo is not None else 0
        if len(self.styles) < 65536:
            self.styles[idx] = e
        st.entry = _Style()

    def _style_chain(self, idx):
        seen = set()
        while idx is not None and idx not in seen and len(seen) < 32:
            seen.add(idx)
            s = self.styles.get(idx)
            if s is None:
                return
            yield s
            idx = s.basedon

    def _outline(self, pp) -> int:
        o = pp.outline
        if o is None:
            for s in self._style_chain(pp.style):
                # only an explicit \outlinelvl makes a heading: dmapper does not give the
                # "heading N" styles of an RTF stylesheet an outline level by name
                if s.outline is not None:
                    o = s.outline
                    break
        if o is None or not 0 <= o <= 8:
            return 0
        return o + 1

    # ---------------------------------------------------------- lists
    def _kw_listpicture(self, v, has, pos):
        st = self.st
        st.dest = D_LISTPICTURE
        st.listpic = True

    def _kw_list(self, v, has, pos):
        st = self.st
        st.dest = D_LIST
        st.entry = {"id": None, "levels": {}}

    def _kw_listlevel(self, v, has, pos):
        st = self.st
        if st.dest == D_LIST and isinstance(st.entry, dict):
            st.level = _Level()
            st.entry["levels"][len(st.entry["levels"])] = st.level
        st.dest = D_LISTLEVEL

    def _kw_leveltext(self, v, has, pos):
        st = self.st
        st.dest = D_LEVELTEXT
        st.dtext = []

    def _pop_leveltext(self, old) -> None:
        if old.level is not None and old.dtext is not None:
            old.level.text = "".join(old.dtext)

    def _kw_listid(self, v, has, pos):
        st = self.st
        if st.dest == D_LIST and isinstance(st.entry, dict):
            st.entry["id"] = v
        elif st.dest == D_LO and isinstance(st.lo, dict):
            st.lo["id"] = v

    def _kw_levelnfc(self, v, has, pos):
        if self.st.level is not None:
            self.st.level.nfc = v

    def _kw_levelstartat(self, v, has, pos):
        st = self.st
        if st.level is not None:
            st.level.start = v
        elif st.dest in (D_LFOLEVEL,) and isinstance(st.lo, dict):
            st.lo["starts"][st.lo.get("cur", 0)] = v

    def _kw_levelnorestart(self, v, has, pos):
        if self.st.level is not None:
            self.st.level.norestart = (not has) or v != 0

    def _pop_listlevel(self, old) -> None:
        pass

    def _pop_list(self, old) -> None:
        e = old.entry
        if e is not self.st.entry and isinstance(e, dict) and e.get("id") is not None and len(self.lists) < 65536:
            self.lists[e["id"]] = e["levels"]

    def _kw_listoverride(self, v, has, pos):
        st = self.st
        st.dest = D_LO
        st.lo = {"id": None, "ls": None, "starts": {}, "cur": 0}

    def _kw_lfolevel(self, v, has, pos):
        st = self.st
        if isinstance(st.lo, dict):
            st.lo["cur"] = st.lo.get("n", 0)
            st.lo["n"] = st.lo.get("n", 0) + 1
        st.dest = D_LFOLEVEL

    def _kw_ls(self, v, has, pos):
        st = self.st
        if st.dest == D_LO and isinstance(st.lo, dict):
            st.lo["ls"] = v
        elif st.dest not in (D_LISTLEVEL, D_LIST, D_LFOLEVEL):
            st.pp = st.pp.but(ls=v)

    def _kw_ilvl(self, v, has, pos):
        st = self.st
        st.pp = st.pp.but(ilvl=v)

    def _pop_lo(self, old) -> None:
        lo = old.lo
        if lo is not self.st.lo and isinstance(lo, dict) and lo.get("ls") is not None and lo.get("id") is not None:
            if len(self.overrides) < 65536:
                self.overrides[lo["ls"]] = (lo["id"], lo["starts"])

    def _make_pn(self, kind):
        def f(v, has, pos):
            st = self.st
            if st.dest == D_PN and isinstance(st.pn, dict):
                st.pn["kind"] = kind
                if kind == "blt":
                    st.pn["nfc"] = 23
        return f

    def _make_pnfmt(self, nfc):
        def f(v, has, pos):
            st = self.st
            if st.dest == D_PN and isinstance(st.pn, dict) and st.pn.get("kind") != "blt":
                st.pn["nfc"] = nfc
        return f

    def _kw_pnstart(self, v, has, pos):
        st = self.st
        if st.dest == D_PN and isinstance(st.pn, dict):
            st.pn["start"] = v

    def _kw_pn(self, v, has, pos):
        st = self.st
        st.dest = D_PN
        st.pn = {"kind": None, "nfc": 0, "start": 1, "before": "", "after": ""}

    def _make_pntxt(self, code):
        def f(v, has, pos):
            st = self.st
            st.dest = code
            st.dtext = []
        return f

    def _pop_pntxt(self, old) -> None:
        if isinstance(self.st.pn, dict) and old.dtext is not None:
            self.st.pn["after" if old.dest == D_PNTXTA else "before"] = "".join(old.dtext)

    def _pop_pn(self, old) -> None:
        pn = old.pn
        if pn is not self.st.pn and isinstance(pn, dict) and pn.get("kind") in ("blt", "body"):
            st = self.st
            st.pp = st.pp.but(pn=(pn["kind"], pn["nfc"], pn["start"], pn["before"], pn["after"]))

    def _decorate(self, p: Paragraph, pp) -> None:
        h = self._outline(pp)
        if h:
            p.heading = h
            return
        ls, ilvl = pp.ls, pp.ilvl
        if ls is None:
            for s in self._style_chain(pp.style):
                if s.ls is not None:
                    ls = s.ls
                    if ilvl is None:
                        ilvl = s.ilvl
                    break
        lvl = min(max(ilvl or 0, 0), 8)
        if ls is not None and ls in self.overrides:
            listid, starts = self.overrides[ls]
            # a \listoverride whose \listid has no \list still numbers the paragraph
            levels = self.lists.get(listid) or {}
            info = levels.get(lvl) or _Level()
            p.list_level = lvl
            p.list_ordered = info.nfc != 23
            if p.list_ordered:
                p.list_number = self._label(("l", listid), levels, lvl, starts)
            return
        if pp.pn is not None:
            kind, nfc, start, before, after = pp.pn
            p.list_level = 0
            p.list_ordered = kind == "body" and nfc != 23
            if p.list_ordered:
                cnt = self.counters.setdefault(("pn", kind, nfc), [start - 1])
                cnt[0] += 1
                p.list_number = (before + _fmt_num(cnt[0], nfc) + after).strip() or None

    def _label(self, key, levels, lvl, starts):
        cnt = self.counters.get(key)
        if cnt is None:
            cnt = self.counters[key] = [None] * 9

        def start_of(k):
            if k in starts:
                return starts[k]
            info = levels.get(k)
            return info.start if info is not None else 1
        cnt[lvl] = start_of(lvl) if cnt[lvl] is None else cnt[lvl] + 1
        for k in range(lvl + 1, 9):
            info = levels.get(k)
            if info is None or not info.norestart:
                cnt[k] = None
        info = levels.get(lvl) or _Level()
        text = info.text
        if not text:
            return _fmt_num(cnt[lvl], info.nfc) + "."
        if text.endswith(";"):
            text = text[:-1]
        size = ord(text[0]) if text else 0
        body = text[1:1 + size]
        out = []
        for ch in body:
            o = ord(ch)
            if o < 9:
                li = levels.get(o) or _Level()
                n = cnt[o] if cnt[o] is not None else start_of(o)
                out.append(_fmt_num(n, li.nfc))
            else:
                out.append(ch)
        return "".join(out).strip() or None

    # ---------------------------------------------------------- fields
    def _kw_field(self, v, has, pos):
        st = self.st
        st.dest = D_FIELD
        st.field = _Field()

    def _kw_fldinst(self, v, has, pos):
        st = self.st
        st.dest = D_FLDINST
        if st.field is None:
            st.field = _Field()
        st.inst = st.field

    def _kw_fldrslt(self, v, has, pos):
        st = self.st
        st.dest = D_FLDRSLT
        f = st.field
        if f is not None and st.inst is not f:
            f.result = True
            f.sink = st.sink
            f.serial = st.sink.serial
            f.start = st.sink.plen
            if st.inst is not None:
                st.inst.inst.append(" ")   # a nested field's result is a separate argument
            else:
                f.url = _hyperlink("".join(f.inst))
                if f.url is not None and len(st.sink.open_links) < 1000:
                    st.sink.open_links.append(f)
            if st.inst is None and not f.done:
                f.done = True
                repl = self._field_text("".join(f.inst), st.sink)
                if repl is not None:
                    # dmapper builds these fields itself; the cached result is not shown
                    if not st.deleted:
                        st.sink.text(repl)
                    st.dest = D_SKIP

    def _pop_field(self, old) -> None:
        f = old.field
        if f is None or f is self.st.field:
            return
        if old.inst is not None and old.inst is not f:
            old.inst.inst.append(" ")
        if not f.done and old.inst is None and self.st.inst is None:
            f.done = True
            repl = self._field_text("".join(f.inst), old.sink, False)
            if repl and not old.deleted:
                old.sink.text(repl)
        if not f.result or f.sink is None or f.url is None:
            return
        sink = f.sink
        if f in sink.open_links:
            sink.open_links.remove(f)
        start = f.start if sink.serial == f.serial else 0
        if sink.plen > start and len(sink.links) < 10000:
            sink.links.append(Link(start, sink.plen, f.url))

    _INFO_FIELDS = {"TITLE": "title", "SUBJECT": "subject", "AUTHOR": "author", "KEYWORDS": "keywords",
                    "COMMENTS": "doccomm", "LASTSAVEDBY": "operator"}

    def _field_text(self, inst: str, sink, result: bool = True):
        """Text LibreOffice's dmapper shows for a field instead of its cached result,
        or None to keep the \\fldrslt (DomainMapper_Impl::CloseFieldCommand)."""
        s = inst.strip().lstrip("\\")
        name = s.split(None, 1)[0].upper() if s else ""
        if name in self._INFO_FIELDS:
            # document-information fields display the \\info value
            return self.info.get(self._INFO_FIELDS[name], "")
        if name == "SYMBOL":
            m = re.match(r"(?i)SYMBOL\s+(0x[0-9a-f]+|\d+)", s)
            if not m:
                return ""
            code = int(m.group(1), 0)
            return chr(code) if 0 < code < 0x110000 and not 0xD800 <= code < 0xE000 else ""
        if name == "EQ":
            return _eq_text(s)
        if name in ("FORMDROPDOWN", "FORMCHECKBOX"):
            return ""   # form controls: no text in the document body
        if name in ("GOTOBUTTON", "MACROBUTTON"):
            # the display text is the rest of the instruction (nested field results included)
            parts = s.split(None, 2)
            return parts[2] if len(parts) > 2 else ""
        if name == "PAGE" and (sink.story == "headers" or not result):
            # a page number field is recomputed; in a header (and without a cached result)
            # LibreOffice writes the value of the first page
            return "1"
        return None

    # ---------------------------------------------------------- pictures
    def _kw_pict(self, v, has, pos):
        st = self.st
        st.dest = D_PICT
        st.pict = _Pict()

    def _kw_picprop(self, v, has, pos):
        st = self.st
        st.dest = D_SHPINST
        sh = _Shape()
        if st.pict is not None:
            st.pict.shape = sh
        st.shape = sh

    def _kw_objlink(self, v, has, pos):
        st = self.st
        if st.obj is not None:
            st.obj = {"ole": False}   # a linked object has no embedded data: its result is a picture

    def _make_pictkind(self, kind):
        def f(v, has, pos):
            st = self.st
            if st.pict is not None and st.dest == D_PICT:
                st.pict.kind = kind
        return f

    def _make_pictval(self, word):
        attr = {b"picw": "picw", b"pich": "pich", b"picwgoal": "goalw", b"pichgoal": "goalh"}.get(word)
        idx = {b"wbmbitspixel": 0, b"wbmplanes": 1, b"wbmwidthbytes": 2}.get(word)

        def f(v, has, pos):
            p = self.st.pict
            if p is None:
                return
            if attr:
                setattr(p, attr, v)
            else:
                p.wbm[idx] = v
        return f

    def _pop_pict(self, old) -> None:
        pict = old.pict
        if pict is None or pict is self.st.pict or not self.images_enabled:
            return
        st = self.st
        if pict.shape is not None and pict.shape.props.get("shapeType", "75") != "75":
            return   # RTFSdrImport::isFakePict: a \picprop of another shape type is a shape, not a picture
        if st.dest == D_SV:
            # \pict as a shape property value: only the picture of a picture shape (pib)
            if st.sp is not None and st.sp[0] == "pib" and st.shape is not None:
                st.shape.pib = pict
            return
        if st.listpic or st.bg or old.listpic or old.bg:
            return
        if st.obj is not None and st.obj.get("ole"):
            # \result picture of an OLE object outside a table is the object's replacement
            # graphic (RTFDocumentImpl::resolvePict with m_bObject): not a picture of its own
            return
        self._add_image(pict, st.sink, "inline")

    def _add_image(self, pict, sink, kind: str) -> None:
        data, mime = _picture_bytes(pict)
        if data is None:
            return
        if len(self.doc.images) >= 100000:
            return
        self.doc.images.append(Image(data=data, mime=mime, kind=kind, story=sink.story))
        sink.text(IMAGE_MARK)
        sink.images.append(len(self.doc.images) - 1)
        if self.st.inbuf != sink.epoch:
            sink.had_picture = True

    # ---------------------------------------------------------- shapes
    def _kw_shp(self, v, has, pos):
        st = self.st
        st.dest = D_SHP
        sh = _Shape()
        sh.in_group = st.shape is not None and st.dest == D_SHP
        st.shape = sh

    def _kw_shpgrp(self, v, has, pos):
        st = self.st
        st.dest = D_SHPGRP

    def _kw_sp(self, v, has, pos):
        st = self.st
        st.dest = D_SP
        st.sp = [None, None]

    def _pop_sn(self, old) -> None:
        if self.st.sp is not None:
            self.st.sp[0] = "".join(old.dtext or ()).strip()

    def _pop_sv(self, old) -> None:
        if self.st.sp is not None:
            self.st.sp[1] = "".join(old.dtext or ()).strip()

    def _pop_sp(self, old) -> None:
        sp, sh = old.sp, old.shape
        if sp is not None and sh is not None and sp[0] and len(sh.props) < 4096:
            sh.props[sp[0]] = sp[1]

    def _pop_shp(self, old) -> None:
        sh = old.shape
        if sh is None or sh is self.st.shape:
            return
        text = sh.props.get("gtextUNICODE")
        if text and not old.bg:
            # RTFSdrImport::resolve: the WordArt text becomes the shape's text
            sink = _Sink("textboxes")
            sink.text(text)
            self.extra_sinks["textboxes"].append(sink)
        if sh.pib is None or not self.images_enabled:
            return
        st = self.st
        if st.listpic or st.bg or old.bg:
            return
        self._add_image(sh.pib, st.sink, "floating")

    def _kw_background(self, v, has, pos):
        st = self.st
        st.dest = D_BACKGROUND
        st.bg = True

    def _kw_shptxt(self, v, has, pos):
        st = self.st
        sh = st.shape
        if sh is not None and sh.props.get("shapeType") == "75":
            st.dest = D_SKIP   # "Skip text on picture frames."
            return
        if st.bg:
            st.dest = D_SKIP
            return
        sink = _Sink("textboxes")
        self.extra_sinks["textboxes"].append(sink)
        self._enter_substream(st, sink)
        st.dest = D_SHPTXT

    def _kw_object(self, v, has, pos):
        st = self.st
        st.dest = D_OBJECT
        # dispatchDestination(OBJECT): outside a table buffer the object is an OLE
        # object; inside one only its \result is used.
        st.obj = {"ole": st.inbuf != st.sink.epoch}

    # ---------------------------------------------------------- substreams
    def _enter_substream(self, st, sink) -> None:
        """A fresh RTFDocumentImpl parses footnotes, comments, headers (resolveSubstream):
        default paragraph state, the current encoding as the default one."""
        st.sink = sink
        st.pp = _PP_DEFAULT
        st.inbuf = -1
        st.field = None
        st.inst = None
        st.obj = None
        st.uc = 1
        st.skip = 0
        st.deleted = False

    def _pop_substream(self, old) -> None:
        sink = old.sink
        if sink.has:
            self._end_par(old, False)

    def _make_hf(self, word):
        first_wins = word in (b"header", b"footer", b"headerf", b"footerf")
        key = {b"header": "headerr", b"footer": "footerr"}.get(word, word.decode())

        def f(v, has, pos):
            st = self.st
            if st.sink is not self.main:
                return   # m_pSuperstream: not handled inside a substream
            if first_wins and key in self.section_hf:
                st.dest = D_SKIP
                return
            self.section_hf.add(key)
            sink = _Sink("headers")
            self.extra_sinks["headers"].append(sink)
            self._enter_substream(st, sink)
            st.dest = D_FOOTNOTE
        return f

    def _kw_footnote(self, v, has, pos):
        st = self.st
        if st.sink is not self.main and st.sink.story not in ("textboxes",):
            return
        kind = "endnotes" if self.data[pos:pos + 7] == b"\\ftnalt" else "footnotes"
        parent = st.sink
        sink = _Sink(kind)
        self.note_sinks[kind].append(sink)
        if not st.deleted:
            parent.text(NOTE_MARK)
            parent.notes.append(("endnote" if kind == "endnotes" else "footnote", len(self.note_sinks[kind])))
        self._enter_substream(st, sink)
        st.dest = D_FOOTNOTE

    def _kw_annotation(self, v, has, pos):
        st = self.st
        if st.sink is not self.main and st.sink.story not in ("textboxes",):
            return
        sink = _Sink("comments")
        self.extra_sinks["comments"].append(sink)
        self._enter_substream(st, sink)
        st.dest = D_FOOTNOTE

    # ---------------------------------------------------------- result
    def finish(self) -> Document:
        st = self.st
        # unwind open groups (truncated file): substreams end where the data ends
        while self.stack:
            self._pop()
        for sinks in (self.note_sinks["footnotes"], self.note_sinks["endnotes"], *self.extra_sinks.values(), [self.main]):
            for sink in sinks:
                self._close_sink(sink)
        doc = self.doc
        for sink in [self.main, *self.note_sinks["footnotes"], *self.note_sinks["endnotes"],
                     *(s for v in self.extra_sinks.values() for s in v)]:
            for b in sink.blocks:
                if isinstance(b, Table):
                    self._finish_table(b, 0)
        doc.blocks = self.main.blocks
        doc.stories["main"] = _flatten(self.main.blocks)
        for kind in ("footnotes", "endnotes"):
            doc.notes[kind] = [s.blocks for s in self.note_sinks[kind]]
            doc.stories[kind] = [t for s in self.note_sinks[kind] for t in _flatten(s.blocks)]
        tb = list(self.frames)
        for s in self.extra_sinks["textboxes"]:
            tb.extend(s.blocks)
        doc.extra["textboxes"] = tb
        doc.stories["textboxes"] = _flatten(tb)
        if not (self.main.serial or self.main.has or self.frames):
            # popState: the final section break (which resolves the headers and
            # footers, sectBreak) only happens once the body had a first run
            self.extra_sinks["headers"] = []
        for key in ("comments", "headers"):
            blocks = [b for s in self.extra_sinks[key] for b in s.blocks]
            doc.extra[key] = blocks
            doc.stories[key] = _flatten(blocks)
        del st
        return doc

    def _close_sink(self, sink) -> None:
        st = self.st
        if sink.has:
            fake = st.copy()
            fake.sink = sink
            fake.inbuf = -1
            fake.pp = _PP_DEFAULT
            self._end_par(fake, False)
        # content of a row that never got its \row
        if any(sink.bufs):
            leftover = [it[1] for buf in sink.bufs for it in buf if it[0] == "p"]
            sink.bufs = [[]]
            if any(p.text.strip() for p in leftover):
                self.doc.warnings.append("table row without \\row at the end of a story")
                sink.table = None
                sink.blocks.extend(leftover)

    def _finish_table(self, t: Table, depth: int) -> None:
        if depth > _MAX_ITAP:
            return
        metas = [self.row_meta.get(id(r)) or [] for r in t.rows]
        edges = sorted({e for m in metas for (l, r, _v) in m for e in (l, r)})
        reps: list = []
        for e in edges:
            if not reps or e - reps[-1] > _FUZZ:
                reps.append(e)
        import bisect

        def idx(x):
            return max(0, bisect.bisect_right(reps, x) - 1)
        spans = []
        for r, m in zip(t.rows, metas):
            rs = []
            for i, c in enumerate(r.cells):
                if i < len(m):
                    a, b = idx(m[i][0]), idx(m[i][1])
                    c.colspan = max(1, b - a)
                    rs.append((a, b, m[i][2]))
                else:
                    rs.append((None, None, 0))
            spans.append(rs)
        # vertical merges: \clvmgf starts, \clvmrg continues (same column span)
        covered = set()
        for ri, rs in enumerate(spans):
            for ci, (a, b, v) in enumerate(rs):
                if v != 1 or a is None or (ri, ci) in covered:
                    continue
                top = t.rows[ri].cells[ci]
                n = 1
                for rj in range(ri + 1, len(spans)):
                    hit = next((cj for cj, (a2, b2, v2) in enumerate(spans[rj]) if a2 == a and b2 == b and v2 == 2), None)
                    if hit is None:
                        break
                    covered.add((rj, hit))
                    extra = t.rows[rj].cells[hit].blocks
                    top.blocks.extend(x for x in extra if isinstance(x, Table) or x.text.strip())
                    n += 1
                top.rowspan = n
        if covered:
            for ri, r in enumerate(t.rows):
                r.cells = [c for ci, c in enumerate(r.cells) if (ri, ci) not in covered]
        for r in t.rows:
            for c in r.cells:
                for b in c.blocks:
                    if isinstance(b, Table):
                        self._finish_table(b, depth + 1)


def _flatten(blocks, out=None, depth=0) -> list:
    out = [] if out is None else out
    for b in blocks:
        if isinstance(b, Table):
            if depth > _MAX_ITAP:
                continue
            for r in b.rows:
                for c in r.cells:
                    _flatten(c.blocks, out, depth + 1)
        else:
            out.append(b.text.replace(IMAGE_MARK, "").replace(NOTE_MARK, ""))
    return out


_FIELD_ARG = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)', re.S)


def _hyperlink(inst: str):
    """URL of a HYPERLINK field instruction (target + "#" + \\l bookmark), or None."""
    s = inst.strip()
    if not s[:9].upper() == "HYPERLINK":
        return None
    args = []
    for m in _FIELD_ARG.finditer(s[9:]):
        if m.group(1) is not None:
            args.append((m.group(1).replace("\\\\", "\\"), True))
        else:
            args.append((m.group(2), False))
    url = None
    anchor = None
    i = 0
    while i < len(args):
        a, quoted = args[i]
        if not quoted and a.startswith("\\") and len(a) == 2:
            sw = a[1].lower()
            if sw in ("l", "o", "t", "m"):
                if sw == "l" and i + 1 < len(args):
                    anchor = args[i + 1][0]
                i += 2 if sw != "m" else 1
                continue
            i += 1
            continue
        if url is None:
            url = a
        i += 1
    url = url or ""
    if anchor:
        url += "#" + anchor
    return url or None


def _wmf_placeable(data: bytes, pict) -> bytes:
    """Prepend an Aldus placeable header to a \\wmetafile (METAFILEPICT bits only)."""
    w = pict.goalw or pict.picw or 1440
    h = pict.goalh or pict.pich or 1440
    w = max(1, min(w, 0x7FFF))
    h = max(1, min(h, 0x7FFF))
    head = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, 0, 0, w, h, 1440, 0)
    words = struct.unpack("<10H", head[:20])
    chk = 0
    for x in words:
        chk ^= x
    return head + struct.pack("<H", chk) + data


def _picture_bytes(pict):
    """(bytes, mime) of a \\pict group, or (None, None) if LibreOffice would not get a graphic."""
    if pict.bin is not None:
        raw = bytes(pict.bin)
    else:
        hexdata = b"".join(pict.hex)
        if _HEX_CLEAN.search(hexdata):
            return None, None   # resolvePict: an invalid hex character drops the picture
        hexdata = hexdata.replace(b" ", b"").replace(b"\r", b"").replace(b"\n", b"")
        if len(hexdata) & 1:
            hexdata = hexdata[:-1]
        try:
            raw = binascii.unhexlify(hexdata)
        except (binascii.Error, ValueError):
            return None, None
    if not raw:
        return None, None
    kind = pict.kind
    if kind == "dib":
        if not dib_complete(raw):
            return None, None
        bmp = _dib_to_bmp(raw)
        return (bmp, "image/bmp") if bmp else (None, None)
    if kind == "wmf":
        if wmf_looks_valid(raw):
            if struct.unpack_from("<I", raw, 0)[0] != 0x9AC6CDD7:
                raw = _wmf_placeable(raw, pict)
            return raw, "image/x-wmf"
        return None, None
    if kind == "pict":
        return b"\0" * 512 + raw, "image/x-pict"
    # GraphicFilter sniffs the content, whatever \pngblip / \jpegblip say
    mime = _sniff(raw)
    if mime is None:
        return None, None
    if mime in ("image/png", "image/jpeg", "image/gif") and not blip_looks_valid(raw, mime):
        return None, None
    return raw, mime


def _sniff(b: bytes):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:2] == b"BM":
        return "image/bmp"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if len(b) >= 44 and b[:4] == b"\x01\x00\x00\x00" and b[40:44] == b" EMF":
        return "image/x-emf"
    if wmf_looks_valid(b):
        return "image/x-wmf"
    return None


def parse(data: bytes, images_enabled: bool = True) -> Document:
    """Parse an RTF document. `data` must start (after BOM/white space) with '{\\rtf'."""
    start = rtf_offset(data)
    if start is None:
        start = 0
    p = _Parser(bytes(data), images_enabled)
    p.run(start)
    doc = p.finish()
    if p.overflow or len(p.stack) >= _MAX_DEPTH:
        doc.warnings.append("group nesting deeper than %d" % _MAX_DEPTH)
    return doc


def _eq_text(cmd: str) -> str:
    """Text of an EQ field as dmapper renders it (DomainMapper_Impl::CloseFieldCommand FIELD_EQ):
    combined characters \\o(\\s\\up(a),\\s\\do(b)) -> "ab", \\s\\do(x) -> "x", a ruby
    "\\* jc ... \\o(\\s\\up(ruby),base)" -> base followed by the ruby text; anything else -> ""."""
    rest = cmd[2:].strip()
    if rest[:2].lower() == "\\o":
        m = re.match(r"(?is)\\o\s*\(\s*\\s\s*\\up\s*\d*\s*\((.*?)\)\s*,\s*\\s\s*\\do\s*\d*\s*\((.*?)\)\s*\)", rest)
        return (m.group(1) + m.group(2)) if m else ""
    if rest.lower().startswith("\\s\\do"):
        a, b = rest.find("("), rest.find(")")
        return rest[a + 1:b] if 0 < a < b else ""
    if rest.startswith("\\* jc"):
        i = rest.find("\\o")
        if i < 0:
            return ""
        a = rest.find("(", i)
        b = rest.rfind(")")
        if a < 0 or b <= a:
            return ""
        parts = rest[a + 1:b].split(",")
        p1 = parts[0]
        p2 = parts[1] if len(parts) > 1 else ""
        x, y = p1.find("("), p1.rfind(")")
        ruby = p1[x + 1:y] if 0 <= x < y else ""
        return p2 + ruby
    return ""
