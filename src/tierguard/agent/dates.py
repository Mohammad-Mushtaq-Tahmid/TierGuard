"""Deterministic parsing of dates in a user's message (Chinese and English). Like amounts, the model never decides a date:
the code reads it from the USER's own words. Returns every distinct date found; the caller insists on exactly one."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from tierguard.agent.zh_numbers import parse_cn

_WD_ZH = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_WD_EN = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
          "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_MON_EN = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
           "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
           "nov": 11, "november": 11, "dec": 12, "december": 12}
_NUM = r"(\d{1,3}|[零〇一二两三四五六七八九十百]{1,4})"


def _n(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    v = parse_cn(tok)
    return int(v) if v is not None else None


def _safe(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _next_day_of_month(today: dt.date, day: int) -> dt.date | None:
    """The next occurrence of day-of-month strictly after today (this month if still ahead, else next month)."""
    for add in (0, 1, 2):
        y, m = today.year + (today.month - 1 + add) // 12, (today.month - 1 + add) % 12 + 1
        d = _safe(y, m, day)
        if d and d > today:
            return d
    return None


def _month_day(today: dt.date, m: int, d: int) -> dt.date | None:
    x = _safe(today.year, m, d)
    if x and x <= today:
        x = _safe(today.year + 1, m, d)
    return x


def parse_when(text: str, today: dt.date) -> list[dt.date]:
    s = unicodedata.normalize("NFKC", text).lower()
    found: list[dt.date] = []

    def add(d):
        if d and d not in found:
            found.append(d)

    for y, m, d in re.findall(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s):
        add(_safe(int(y), int(m), int(d)))
    spans = [mt.span() for mt in re.finditer(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}", s)]
    s2 = s
    for a, b in reversed(spans):                      # do not re-read digits already used by a full date
        s2 = s2[:a] + " " * (b - a) + s2[b:]
    if "大后天" in s2:
        add(today + dt.timedelta(days=3))
    elif "后天" in s2:
        add(today + dt.timedelta(days=2))
    if "明天" in s2 or "明日" in s2 or re.search(r"\btomorrow\b", s2):
        add(today + dt.timedelta(days=1))
    if "day after tomorrow" in s2:
        add(today + dt.timedelta(days=2))
    for tok in re.findall(_NUM + r"\s*天[后後]", s2):
        n = _n(tok)
        if n and 0 < n <= 365:
            add(today + dt.timedelta(days=n))
    for tok in re.findall(r"in\s+(\d{1,3})\s+days?", s2):
        add(today + dt.timedelta(days=int(tok)))
    for tok in re.findall(r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:个)?(?:周|星期)[后後]|in\s+(\d{1,2})\s+weeks?", s2):
        n = _n(tok[0] or tok[1])
        if n:
            add(today + dt.timedelta(weeks=n))
    for wd in re.findall(r"下(?:个)?(?:周|星期|礼拜)([一二三四五六日天1-7])", s2):
        add(today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=1, days=_WD_ZH[wd]))
    s3 = re.sub(r"下(?:个)?(?:周|星期|礼拜)[一二三四五六日天1-7]", " ", s2)
    for wd in re.findall(r"(?:周|星期|礼拜)([一二三四五六日天])", s3):
        delta = (_WD_ZH[wd] - today.weekday()) % 7
        add(today + dt.timedelta(days=delta or 7))
    for name in re.findall(r"next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", s2):
        add(today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=1, days=_WD_EN[name]))
    s4 = re.sub(r"next\s+[a-z]+", " ", s2)
    for name in re.findall(r"\b(?:on\s+|this\s+|coming\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", s4):
        delta = (_WD_EN[name] - today.weekday()) % 7
        add(today + dt.timedelta(days=delta or 7))
    if "月底" in s2 or "end of the month" in s2 or "end of month" in s2:
        nxt = dt.date(today.year + today.month // 12, today.month % 12 + 1, 1)
        add(nxt - dt.timedelta(days=1))
    nm = re.search(r"下(?:个)?月" + _NUM + r"\s*[日号號]", s2)
    if nm and _n(nm.group(1)):
        y, m = today.year + today.month // 12, today.month % 12 + 1
        add(_safe(y, m, _n(nm.group(1))))
        s2 = s2.replace(nm.group(0), " ")
    for mo, da in re.findall(_NUM + r"\s*月\s*" + _NUM + r"\s*[日号號]", s2):
        mm, dd = _n(mo), _n(da)
        if mm and dd:
            add(_month_day(today, mm, dd))
    s5 = re.sub(_NUM + r"\s*月\s*" + _NUM + r"\s*[日号號]", " ", s2)
    for da in re.findall(_NUM + r"\s*[日号號]", s5):
        dd = _n(da)
        if dd and 1 <= dd <= 31:
            add(_next_day_of_month(today, dd))
    for name, d in re.findall(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", s2):
        add(_month_day(today, _MON_EN[name], int(d)))
    for d, name in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", s2):
        add(_month_day(today, _MON_EN[name], int(d)))
    for mo, da in re.findall(r"\b(\d{1,2})/(\d{1,2})\b", s2):
        add(_month_day(today, int(mo), int(da)))
    return sorted(found)


RECURRING = re.compile(r"每(?:个)?(?:月|周|星期|天|日|年)|每个月|定期|循环|\bevery\b|\bweekly\b|\bmonthly\b|\bdaily\b|\brecurring\b|\bstanding order\b", re.I)


def is_recurring(text: str) -> bool:
    return bool(RECURRING.search(unicodedata.normalize("NFKC", text)))


_DATE_DIGIT_PATTERNS = [
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}\s*[日号號]?",
    r"下(?:个)?月\s*" + _NUM + r"\s*[日号號]",
    _NUM + r"\s*月\s*" + _NUM + r"\s*[日号號]",
    _NUM + r"\s*[日号號]",
    _NUM + r"\s*天[后後]",
    r"in\s+\d{1,3}\s+days?",
    r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:个)?(?:周|星期)[后後]",
    r"in\s+\d{1,2}\s+weeks?",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b",
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    r"\b\d{1,2}/\d{1,2}\b",
]


def strip_dates(text: str) -> str:
    """Remove date expressions that contain digits, so '10月28号转500' leaves only the 500 for amount extraction."""
    s = unicodedata.normalize("NFKC", text)
    for pat in _DATE_DIGIT_PATTERNS:
        s = re.sub(pat, " ", s, flags=re.I)
    return s