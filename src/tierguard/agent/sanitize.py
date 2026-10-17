"""Input hygiene applied to every user message before anything else looks at it.
NFKC folds full-width / compatibility forms (ｙｅｓ -> yes, １２３ -> 123); invisible, control and bidi-override
characters are removed so they cannot hide instructions or split an amount ('5<ZWSP>00')."""
from __future__ import annotations

import re
import unicodedata

_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb]")


def count_invisible(text: str) -> int:
    return len(_INVISIBLE.findall(unicodedata.normalize("NFKC", text)))


def sanitize(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).strip()