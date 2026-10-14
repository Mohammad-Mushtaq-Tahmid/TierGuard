"""Deterministic parsing of colloquial Chinese/mixed money amounts. The LLM never does this arithmetic.
五百=500, 两千=2000, 1.5万=15000, 一千二=1200, 三万五=35000, 两百五=250, 2k=2000."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
       "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}   # 壹贰叁...: formal bank numerals
SMALL = {"十": 10, "百": 100, "千": 1000, "拾": 10, "佰": 100, "仟": 1000}
BIG = {"万": 10_000, "亿": 100_000_000}
CN_CHARS = "零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟"
MARKERS = ("块钱", "块", "元", "圆", "人民币", "rmb", "RMB", "cny", "CNY")
ARABIC_UNIT = {"万": Decimal(10_000), "w": Decimal(10_000), "W": Decimal(10_000), "千": Decimal(1000),
               "k": Decimal(1000), "K": Decimal(1000), "百": Decimal(100)}


def parse_cn(s: str) -> Decimal | None:
    """Parse a pure Chinese numeral like 五百, 一千二, 三万五, 一百零五, 十五. None if not a numeral."""
    if not s or any(ch not in CN_CHARS for ch in s):
        return None
    total, section, num = 0, 0, 0
    last_unit, zero_after_unit = None, False
    for ch in s:
        if ch in DIG:
            if ch in "零〇":
                zero_after_unit = True
                continue
            num = DIG[ch]
        elif ch in SMALL:
            section += (num if num else 1) * SMALL[ch]
            num, last_unit, zero_after_unit = 0, SMALL[ch], False
        else:  # 万 / 亿
            total += (section + num) * BIG[ch]
            section, num, last_unit, zero_after_unit = 0, 0, BIG[ch], False
    if num:
        if last_unit in (100, 1000, 10_000) and not zero_after_unit and len(s) >= 2:
            section += num * (last_unit // 10)   # shorthand: 一千二 = 1200, 三万五 = 35000
        else:
            section += num
    return Decimal(total + section)


def parse_amount(text: str) -> Decimal | None:
    """Parse one amount mention (with optional 块/元/rmb suffix). Returns Decimal or None."""
    s = unicodedata.normalize("NFKC", text).strip().replace(",", "").replace(" ", "")
    for m in sorted(MARKERS, key=len, reverse=True):
        if s.endswith(m):
            s = s[: -len(m)]
            break
    if not s:
        return None
    mt = re.fullmatch(r"(\d+(?:\.\d+)?)([万千百kKwW]?)", s)
    if mt:
        try:
            return Decimal(mt.group(1)) * ARABIC_UNIT.get(mt.group(2), Decimal(1))
        except InvalidOperation:
            return None
    # mixed like 1万5 / 2千5
    mt = re.fullmatch(r"(\d+)([万千百])(\d)", s)
    if mt:
        return Decimal(mt.group(1)) * ARABIC_UNIT[mt.group(2)] + Decimal(mt.group(3)) * (ARABIC_UNIT[mt.group(2)] / 10)
    return parse_cn(s)


_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[万千百kKwW]?\d?|[" + CN_CHARS + r"]+")


def extract_amounts(text: str) -> list[Decimal]:
    """Find amounts in a sentence. Arabic numbers always count; Chinese numerals count only if they carry a
    currency marker or a 百/千/万 unit (so 一下 / 一个 are not amounts)."""
    s = unicodedata.normalize("NFKC", text)
    out = []
    for m in _TOKEN.finditer(s):
        tok = m.group(0)
        tail = s[m.end(): m.end() + 3]
        has_marker = any(tail.startswith(mk) for mk in MARKERS)
        if tok[0].isdigit():
            v = parse_amount(tok)
        elif has_marker or any(u in tok for u in "百千万亿佰仟"):
            v = parse_cn(tok)
        else:
            v = None
        if v is not None:
            out.append(v)
    return out