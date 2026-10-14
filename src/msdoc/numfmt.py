# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Excel number-format codes: the displayed text of a numeric cell.

Covers the part of the format-code grammar that real workbooks use: sections
(`pos;neg;zero;text`), conditions, digit placeholders `0 # ?`, thousands
separators and scaling commas, percent, scientific, simple fractions, quoted
and escaped literals, `[$cur-lcid]`, and dates/times (en-US names). Anything it
does not understand degrades to the General format instead of failing.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction

# BIFF built-in number formats (ifmt < 164). [MS-XLS] 2.4.126 Format.
BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "M/D/YYYY", 15: "D-MMM-YY", 16: "D-MMM", 17: "MMM-YY",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "M/D/YY h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    41: '_(* #,##0_);_(* (#,##0);_(* "-"_);_(@_)',
    42: '_($* #,##0_);_($* (#,##0);_($* "-"_);_(@_)',
    43: '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)',
    44: '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)',
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0", 48: "##0.0E+0", 49: "@",
}
# East-Asian / locale ranges (27-36, 50-58) are date formats in most locales.
_DATE_IFMT = {14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!",
          0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A", 0x2B: "#GETTING_DATA"}


def general(v: float) -> str:
    if v != v:
        return "NaN"
    if math.isinf(v):
        return "#NUM!"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    a = abs(v)
    sci = a >= 1e15
    if not sci and a < 1e-3:
        # LibreOffice keeps plain notation while the decimals fit in 15 places
        m, e = ("%.14E" % a).split("E")
        nsig = len(m.replace(".", "").rstrip("0")) or 1
        sci = (-int(e) - 1 + nsig) > 15
    if sci:
        s = "%.14E" % v
        m, e = s.split("E")
        m = m.rstrip("0").rstrip(".")
        return "%sE%s%02d" % (m, e[0], int(e[1:]))
    if a < 1e-3:
        m, e = ("%.14E" % a).split("E")
        nsig = len(m.replace(".", "").rstrip("0")) or 1
        return ("-" if v < 0 else "") + ("%.*f" % (-int(e) - 1 + nsig, a))
    s = "%.15g" % v
    if "e" in s:
        s = repr(v)
    return s


def is_date_format(ifmt: int, code: str | None) -> bool:
    if code is None:
        return ifmt in _DATE_IFMT
    return _split_kind(code) == "date"


# --------------------------------------------------------------- tokenizer

def _sections(code: str) -> list[str]:
    out, cur, i, n = [], [], 0, len(code)
    while i < n:
        ch = code[i]
        if ch == '"':
            j = code.find('"', i + 1)
            j = n - 1 if j < 0 else j
            cur.append(code[i:j + 1]); i = j + 1
        elif ch == "\\" and i + 1 < n:
            cur.append(code[i:i + 2]); i += 2
        elif ch == "[":
            j = code.find("]", i)
            j = n - 1 if j < 0 else j
            cur.append(code[i:j + 1]); i = j + 1
        elif ch == ";":
            out.append("".join(cur)); cur = []; i += 1
        else:
            cur.append(ch); i += 1
    out.append("".join(cur))
    return out


_COND = re.compile(r"\[(<=|>=|<>|<|>|=)\s*(-?[0-9.]+)\]")


def _strip_meta(sec: str):
    """Return (section without [conditions]/[colors], condition or None)."""
    cond = None
    m = _COND.search(sec)
    if m:
        cond = (m.group(1), float(m.group(2)))
    return sec, cond


def _tokens(sec: str):
    """Yield (kind, text): kind in lit, digit, point, comma, pct, exp, frac, date, text, elapsed."""
    i, n = 0, len(sec)
    toks: list[tuple[str, str]] = []
    while i < n:
        ch = sec[i]
        if ch == '"':
            j = sec.find('"', i + 1)
            j = n if j < 0 else j
            toks.append(("lit", sec[i + 1:j])); i = j + 1
        elif ch == "\\":
            if i + 1 < n:
                toks.append(("lit", sec[i + 1]))
            i += 2
        elif ch == "_":
            toks.append(("lit", " ")); i += 2
        elif ch == "*":
            i += 2
        elif ch == "[":
            j = sec.find("]", i)
            j = n - 1 if j < 0 else j
            body = sec[i + 1:j]
            if body.startswith("$"):
                cur = body[1:].split("-", 1)[0]
                if cur:
                    toks.append(("lit", cur))
            elif re.fullmatch(r"(h+|m+|s+)", body, re.I):
                toks.append(("elapsed", body.lower()))
            i = j + 1
        elif ch in "0#?":
            toks.append(("digit", ch)); i += 1
        elif ch == ".":
            toks.append(("point", ".")); i += 1
        elif ch == ",":
            toks.append(("comma", ",")); i += 1
        elif ch == "%":
            toks.append(("pct", "%")); i += 1
        elif ch in "Ee" and i + 1 < n and sec[i + 1] in "+-":
            toks.append(("exp", ch + sec[i + 1])); i += 2
        elif ch == "/":
            toks.append(("frac", "/")); i += 1
            j = i
            while j < n and sec[j].isdigit():
                j += 1
            if j > i:
                toks.append(("fixden", sec[i:j])); i = j
        elif ch == "@":
            toks.append(("text", "@")); i += 1
        elif ch in "yYdDhHsS" or (ch in "mM"):
            j = i
            while j < n and sec[j].lower() == ch.lower():
                j += 1
            toks.append(("date", sec[i:j].lower())); i = j
        elif sec[i:i + 5].upper() == "AM/PM":
            toks.append(("date", "am/pm")); i += 5
        elif sec[i:i + 3].upper() == "A/P":
            toks.append(("date", "a/p")); i += 3
        elif sec[i:i + 7].lower() == "general":
            toks.append(("general", "General")); i += 7
        elif ch in "bBgGeE" and i + 1 < n and sec[i].lower() in "bge":
            toks.append(("lit", ch)); i += 1  # buddhist/era markers: ignored literal
        else:
            toks.append(("lit", ch)); i += 1
    return toks


def _split_kind(code: str) -> str:
    secs = _sections(code)
    for sec in secs[:2]:
        toks = _tokens(sec)
        if any(k in ("date", "elapsed") for k, _ in toks):
            # a lone 'e'/'E'/'g' literal is not a date marker; 'd' 'm' 'y' 'h' 's' tokens are
            return "date"
    return "number"


# ----------------------------------------------------------------- numbers

def _round(v: float, decimals: int) -> Decimal:
    try:
        d = Decimal(repr(abs(v)))
    except InvalidOperation:
        d = Decimal(abs(v))
    return d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _fmt_number(v: float, toks, negative_prefix: bool) -> str:
    # scaling
    pct = sum(1 for k, _ in toks if k == "pct")
    # trailing commas right after the last digit placeholder scale by 1000
    last_digit = max((i for i, (k, _) in enumerate(toks) if k == "digit"), default=-1)
    scale = 0
    j = last_digit + 1
    while j < len(toks) and toks[j][0] == "comma":
        scale += 1; j += 1
    # when there is a decimal point, commas right before it also scale
    has_point = any(k == "point" for k, _ in toks)
    x = abs(v) * (100 ** pct) / (1000 ** scale)
    exp_i = next((i for i, (k, _) in enumerate(toks) if k == "exp"), None)
    frac_i = next((i for i, (k, _) in enumerate(toks) if k == "frac"), None)
    if frac_i is not None and exp_i is None:
        return _fmt_fraction(x, toks, frac_i, v < 0 and negative_prefix)
    pt = next((i for i, (k, _) in enumerate(toks) if k == "point"), None)
    int_end = pt if pt is not None else (exp_i if exp_i is not None else len(toks))
    int_digits = [t for t in toks[:int_end] if t[0] == "digit"]
    frac_end = exp_i if exp_i is not None else len(toks)
    frac_digits = [t for t in toks[pt + 1:frac_end] if t[0] == "digit"] if pt is not None else []
    thousands = False
    for i, (k, _) in enumerate(toks[:int_end]):
        if k == "comma" and any(kk == "digit" for kk, _ in toks[:i]) and any(kk == "digit" for kk, _ in toks[i + 1:int_end]):
            thousands = True
    exp_txt = ""
    if exp_i is not None:
        exp_digits = sum(1 for k, _ in toks[exp_i + 1:] if k == "digit")
        n_int = max(len(int_digits), 1)
        if x == 0:
            e = 0
        else:
            e = int(math.floor(math.log10(x))) - (n_int - 1)
        x = x / (10 ** e) if x else 0.0
        dec = len(frac_digits)
        if _round(x, dec) >= Decimal(10 ** n_int):
            x /= 10; e += 1
        sign = "-" if e < 0 else ("+" if toks[exp_i][1][1] == "+" else "")
        exp_txt = toks[exp_i][1][0] + sign + str(abs(e)).zfill(max(exp_digits, 1))
    dec = len(frac_digits)
    r = _round(x, dec)
    s = format(r, "f")
    ip, _, fp = s.partition(".")
    # integer part
    min_int = sum(1 for _, c in int_digits if c == "0")
    q_int = sum(1 for _, c in int_digits if c == "?")
    ip = ip.lstrip("0")
    raw_ip = ip
    ph = [c for _, c in int_digits]
    inter = (exp_i is None and len(ph) > 1 and not thousands and any(
        k == "lit" for k, _ in toks[next(i for i, (kk, _) in enumerate(toks) if kk == "digit"):
                                    max(i for i, (kk, _) in enumerate(toks[:int_end]) if kk == "digit")]))
    placed: list[str] = []
    if inter:
        rem = raw_ip
        for idx in range(len(ph) - 1, -1, -1):
            if idx == 0:
                take, rem = rem, ""
            else:
                take, rem = rem[-1:], rem[:-1]
            placed.append(take or {"0": "0", "#": "", "?": " "}[ph[idx]])
    if len(ip) < min_int:
        ip = ip.rjust(min_int, "0")
    elif len(ip) < min_int + q_int:
        ip = ip.rjust(min_int + q_int, " ")
    if thousands and ip.strip():
        core = ip.strip()
        pad = ip[:len(ip) - len(ip.lstrip(" "))]
        ip = pad + "{:,}".format(int(core)) if core.isdigit() else ip
    # fraction part: placeholders decide trailing zeros
    out_frac = ""
    if frac_digits:
        chars = list(fp.ljust(dec, "0"))
        for k in range(dec - 1, -1, -1):
            if chars[k] == "0" and frac_digits[k][1] in "#?":
                if frac_digits[k][1] == "#":
                    chars[k] = ""
                else:
                    chars[k] = " "
            else:
                break
        out_frac = "".join(chars)
    out: list[str] = []
    int_done = False
    i = 0
    while i < len(toks):
        k, t = toks[i]
        if k == "digit" and i < int_end:
            if inter:
                out.append(placed.pop()); int_done = True
            elif not int_done:
                out.append(ip); int_done = True
        elif k == "comma":
            pass
        elif k == "point":
            if not int_done and not int_digits and raw_ip:
                out.append(raw_ip); int_done = True
            if out_frac.strip() or any(c == "0" for _, c in frac_digits):
                out.append("." + out_frac)
            else:
                out.append("")
            while i + 1 < len(toks) and toks[i + 1][0] == "digit":
                i += 1
        elif k == "digit":
            pass  # fraction digits consumed above
        elif k == "exp":
            out.append(exp_txt)
            while i + 1 < len(toks) and toks[i + 1][0] == "digit":
                i += 1
        elif k == "pct":
            out.append("%")
        elif k == "lit":
            out.append(t)
        elif k == "general":
            out.append(general(v))
        i += 1
    if not int_done and int_digits == []:
        pass
    res = "".join(out)
    if negative_prefix and v < 0 and (r != 0):
        res = "-" + res
    return res


def _fmt_fraction(x: float, toks, frac_i: int, neg: bool) -> str:
    den_digits = 0
    fixed = ""
    for k, c in toks[frac_i + 1:]:
        if k == "digit":
            den_digits += 1
        elif k == "fixden":
            fixed += c
    whole_part = any(k == "digit" for k, _ in toks[:frac_i]) and any(
        k == "lit" and t == " " for k, t in toks[:frac_i])
    fx = Fraction(x)
    if fixed and int(fixed) > 0:
        den = int(fixed)
        w = int(fx) if whole_part else 0
        num = int((fx - w) * den + Fraction(1, 2))  # round half up
        if num >= den and whole_part:
            w += 1; num -= den
        n_, d_ = num, den
    else:
        maxden = 10 ** max(den_digits, 1) - 1
        f = fx.limit_denominator(maxden)
        if whole_part:
            w, n_ = divmod(f.numerator, f.denominator)
            d_ = f.denominator
        else:
            w, n_, d_ = 0, f.numerator, f.denominator
    if whole_part:
        if w:
            s = str(w) + (" %d/%d" % (n_, d_) if n_ else "")
        else:
            s = " %d/%d" % (n_, d_) if n_ else "0"  # the empty integer placeholder leaves a space
    else:
        s = "%d/%d" % (n_, d_)
    return ("-" if neg and s != "0" else "") + s


# ------------------------------------------------------------------- dates

def _serial_to_datetime(serial: float, date1904: bool):
    days = math.floor(serial)
    frac = serial - days
    secs = int(round(frac * 86400))
    if secs >= 86400:
        days += 1; secs -= 86400
    if date1904:
        base = _dt.datetime(1904, 1, 1)
        d = base + _dt.timedelta(days=days)
    else:
        d = _dt.datetime(1899, 12, 30) + _dt.timedelta(days=days)
    return d + _dt.timedelta(seconds=secs)


def _fmt_date(v: float, toks, date1904: bool) -> str | None:
    if v < 0 or v > 2958465.99999:
        return None
    has_ampm = any(k == "date" and t in ("am/pm", "a/p") for k, t in toks)
    elapsed = [t for k, t in toks if k == "elapsed"]
    d = _serial_to_datetime(v, date1904)
    total_secs = int(round(v * 86400))
    out: list[str] = []
    n = len(toks)
    for i, (k, t) in enumerate(toks):
        if k == "elapsed":
            if t[0] == "h":
                out.append(str(total_secs // 3600).zfill(len(t)))
            elif t[0] == "m":
                out.append(str(total_secs // 60).zfill(len(t)))
            else:
                out.append(str(total_secs).zfill(len(t)))
        elif k == "date":
            c = t[0]
            if t == "am/pm":
                out.append("AM" if d.hour < 12 else "PM")
            elif t == "a/p":
                out.append("A" if d.hour < 12 else "P")
            elif c == "y":
                out.append(str(d.year).zfill(4) if len(t) >= 3 else str(d.year % 100).zfill(2))
            elif c == "d":
                if len(t) <= 2:
                    out.append(str(d.day).zfill(len(t)))
                elif len(t) == 3:
                    out.append(_DAYS[d.weekday()][:3])
                else:
                    out.append(_DAYS[d.weekday()])
            elif c == "h":
                h = d.hour % 12 or 12 if has_ampm else d.hour
                out.append(str(h).zfill(len(t)))
            elif c == "s":
                out.append(str(d.second).zfill(len(t)))
            elif c == "m":
                # minute if directly after h/elapsed-h or directly before s
                prev = next((toks[j] for j in range(i - 1, -1, -1) if toks[j][0] in ("date", "elapsed")), None)
                nxt = next((toks[j] for j in range(i + 1, n) if toks[j][0] in ("date", "elapsed")), None)
                minute = (prev is not None and prev[1][0] == "h") or (nxt is not None and nxt[1][0] == "s")
                if minute:
                    out.append(str(d.minute).zfill(len(t)))
                elif len(t) <= 2:
                    out.append(str(d.month).zfill(len(t)))
                elif len(t) == 3:
                    out.append(_MONTHS[d.month - 1][:3])
                elif len(t) == 4:
                    out.append(_MONTHS[d.month - 1])
                else:
                    out.append(_MONTHS[d.month - 1][0])
        elif k == "lit":
            out.append(t)
        elif k == "point":
            # fractional seconds: ".0" / ".00"
            j = i + 1
            digits = 0
            while j < n and toks[j][0] == "digit":
                digits += 1; j += 1
            if digits and any(kk == "date" and tt[0] == "s" for kk, tt in toks[:i]):
                out.append("." + ("%.*f" % (digits, (v * 86400) % 1))[2:])
            else:
                out.append(".")
        elif k == "digit" and i > 0 and toks[i - 1][0] in ("point", "digit"):
            continue
        elif k in ("comma", "frac", "pct"):
            out.append(t)  # punctuation inside a date format is literal
    return "".join(out)


# -------------------------------------------------------------------- API

def format_text(text: str, ifmt: int, code: str | None) -> str:
    """Displayed text of a string cell under a number format (SvNumberformat::GetOutputString
    for strings): a text section (the fourth one, or the only one of a pure text format) is
    applied with `@` standing for the cell text; an explicitly empty fourth section
    (`;;;`) and formats without a text section leave the text unchanged."""
    if code is None:
        code = BUILTIN.get(ifmt)
        if code is None:
            return text
    try:
        secs = _sections(code)
        if len(secs) >= 4:
            sec = secs[3]
            if not sec.strip():
                return text  # an empty section leaves the text unchanged (LibreOffice is inconsistent here)
        elif len(secs) == 1:
            sec = secs[0]
            toks = _tokens(sec)
            if not any(k == "text" for k, _ in toks) or any(k in ("digit", "date", "elapsed", "point", "frac", "exp") for k, _ in toks):
                return text
        else:
            return text
        out = []
        for k, t in _tokens(sec):
            if k == "lit":
                out.append(t)
            elif k in ("text", "general"):
                out.append(text)
        return "".join(out)
    except Exception:  # pragma: no cover - degrade, never fail
        return text


def format_number(v: float, ifmt: int, code: str | None, date1904: bool = False) -> str:
    """Displayed text of numeric value `v` for the format (`code`, or the built-in `ifmt`)."""
    if code is None:
        code = BUILTIN.get(ifmt)
        if code is None:
            return general(v)
    if code.strip().lower() in ("general", "") or re.search(r"\[\$-F[48]00\]", code, re.I):
        return general(v)  # system date/time formats: LibreOffice shows the plain number
    try:
        secs = _sections(code)
        parsed = []
        for s in secs:
            sec, cond = _strip_meta(s)
            parsed.append((sec, cond))
        # choose the section
        idx = 0
        if len(parsed) >= 2 and any(c for _, c in parsed[:2]):
            def hit(c):
                if c is None:
                    return None
                op, lim = c
                return {"<": v < lim, ">": v > lim, "<=": v <= lim, ">=": v >= lim, "=": v == lim, "<>": v != lim}[op]
            h0 = hit(parsed[0][1])
            h1 = hit(parsed[1][1])
            if h0:
                idx = 0
            elif h1 or (parsed[1][1] is None and len(parsed) >= 2 and h0 is not None):
                idx = 1
            else:
                idx = min(2, len(parsed) - 1)
        elif len(parsed) == 1:
            idx = 0
        elif v < 0:
            idx = 1
        elif v == 0 and len(parsed) >= 3:
            idx = 2
        sec = parsed[idx][0]
        toks = _tokens(sec)
        auto_sign = len(parsed) == 1 or idx == 0 and not any(c for _, c in parsed[:2])
        if len(parsed) >= 2 and not any(c for _, c in parsed[:2]) and v < 0:
            auto_sign = False  # negative section supplies its own minus
        if any(k in ("date", "elapsed") for k, _ in toks):
            r = _fmt_date(v, toks, date1904)
            return r if r is not None else general(v)
        if any(k == "text" for k, _ in toks) and not any(k in ("digit", "general") for k, _ in toks):
            return "".join(t if k == "lit" else (general(v) if k == "text" else "") for k, t in toks)
        if any(k == "general" for k, _ in toks) and not any(k == "digit" for k, _ in toks):
            body = general(abs(v))
            res = "".join(body if k == "general" else (t if k == "lit" else "") for k, t in toks)
            return ("-" + res) if (v < 0 and auto_sign) else res
        return _fmt_number(v, toks, auto_sign)
    except Exception:  # pragma: no cover - degrade, never fail
        return general(v)
