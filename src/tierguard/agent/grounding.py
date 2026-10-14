"""Hallucination control: every number in a draft reply must appear in a tool result
(or in the user's own message). Blocks replies with ungrounded figures."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canon(tok: str) -> str | None:
    try:
        return format(Decimal(tok.replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[str]:
    return {c for t in _NUM.findall(text or "") if (c := _canon(t)) is not None}


def _walk(obj, acc: set):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(k, acc)
            _walk(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, acc)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float, str)):
        acc |= numbers_in(str(obj))


def verify_reply(reply: str, tool_results: list, user_text: str = "") -> tuple[bool, list[str]]:
    allowed: set[str] = set()
    _walk(tool_results, allowed)
    allowed |= numbers_in(user_text)
    missing = sorted(n for n in numbers_in(reply) if n not in allowed)
    return (not missing), missing
