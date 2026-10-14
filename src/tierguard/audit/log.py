"""Append-only, hash-chained audit log (tamper-evident, replayable)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class AuditLog:
    GENESIS = "0" * 64

    def __init__(self, path: str | None = None, clock=time.time):
        self.entries: list[dict] = []
        self.path = Path(path) if path else None
        self.clock = clock

    def append(self, session_id: str, event: str, data: dict) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else self.GENESIS
        body = {"seq": len(self.entries), "ts": round(self.clock(), 3), "session": session_id,
                "event": event, "data": data, "prev": prev}
        body["hash"] = hashlib.sha256((prev + canonical(body)).encode()).hexdigest()
        self.entries.append(body)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(canonical(body) + "\n")
        return body

    def verify(self) -> tuple[bool, int | None]:
        """Returns (ok, index_of_first_bad_entry)."""
        prev = self.GENESIS
        for i, e in enumerate(self.entries):
            body = {k: v for k, v in e.items() if k != "hash"}
            if e["prev"] != prev or e["hash"] != hashlib.sha256((prev + canonical(body)).encode()).hexdigest():
                return False, i
            prev = e["hash"]
        return True, None

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.entries if name is None or e["event"] == name]
