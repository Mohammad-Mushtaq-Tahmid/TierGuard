"""Strict argument validation + normalisation (no extra deps)."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

SMUGGLED = {"customer_id", "role", "user_id", "account_owner", "principal", "session_id", "tier", "owner", "admin",
            "is_admin", "privilege", "permissions", "scope", "skip_confirmation", "bypass", "override", "confirmed", "otp", "token"}
# same family as agent/sanitize.py: control, zero-width, bidi marks/overrides/isolates, soft hyphen, invisible operators
_BAD_CHARS = re.compile("[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb]")


class ArgError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def validate(params: dict, args) -> dict:
    if not isinstance(args, dict):
        raise ArgError("BAD_ARGS", "Arguments must be an object")
    for k in args:
        if k not in params:
            raise ArgError("SMUGGLED_IDENTITY_ARG" if k in SMUGGLED else "UNKNOWN_ARG", f"Unexpected argument '{k}'")
    out = {}
    for name, p in params.items():
        if name not in args or args[name] is None:
            if p.get("required"):
                raise ArgError("MISSING_ARG", f"Missing required argument '{name}'")
            if "default" in p:
                out[name] = p["default"]
            continue
        v, t = args[name], p["type"]
        if t == "string":
            if not isinstance(v, str):
                raise ArgError("BAD_TYPE", f"'{name}' must be a string")
            if _BAD_CHARS.search(v):
                raise ArgError("ILLEGAL_CHARACTERS", f"'{name}' contains control or zero-width characters")
            if len(v) > p.get("maxlen", 200):
                raise ArgError("TOO_LONG", f"'{name}' is too long")
            if "pattern" in p and not re.fullmatch(p["pattern"], v):
                raise ArgError("BAD_FORMAT", f"'{name}' has an invalid format")
            if "enum" in p and v not in p["enum"]:
                raise ArgError("BAD_ENUM", f"'{name}' must be one of {p['enum']}")
            out[name] = v
        elif t == "money":
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ArgError("BAD_TYPE", f"'{name}' must be a number")
            try:
                d = Decimal(str(v).strip())
            except InvalidOperation:
                raise ArgError("BAD_AMOUNT", f"'{name}' is not a valid amount")
            if not d.is_finite():
                raise ArgError("BAD_AMOUNT", f"'{name}' is not a finite number")
            if d.copy_abs() > Decimal("10000000"):                       # range first: huge exponents must not reach quantize()
                raise ArgError("BAD_AMOUNT", f"'{name}' is out of range")
            try:
                two_decimals = d == d.quantize(Decimal("0.01"))
            except InvalidOperation:
                two_decimals = False
            if not two_decimals:
                raise ArgError("BAD_AMOUNT", f"'{name}' must have at most 2 decimals")
            if d < Decimal("0.01"):
                raise ArgError("BAD_AMOUNT", f"'{name}' is out of range")
            out[name] = f"{d:.2f}"  # canonical: 500, 500.0, "500.00" all hash the same
        elif t == "date":
            try:
                out[name] = dt.date.fromisoformat(v).isoformat()
            except (TypeError, ValueError):
                raise ArgError("BAD_DATE", f"'{name}' must be YYYY-MM-DD")
        elif t == "bool":
            if not isinstance(v, bool):
                raise ArgError("BAD_TYPE", f"'{name}' must be true/false")
            out[name] = v
        elif t == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not (p.get("min", 0) <= v <= p.get("max", 10**6)):
                raise ArgError("BAD_TYPE", f"'{name}' must be an integer in range")
            out[name] = v
    return out