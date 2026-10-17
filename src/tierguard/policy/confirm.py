"""Single-use, short-lived confirmation tokens bound to the SHA-256 of the exact action."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from tierguard.audit.log import canonical


def action_hash(customer_id: str, tool: str, args: dict, bound: dict | None = None) -> str:
    """SHA-256 of the canonical action. `bound` holds facts about the records the action will touch (payee account
    fingerprint, card status, subscription price...). If any of them changes after the card was shown, the hash
    changes and the confirmation is rejected."""
    body = {"customer_id": customer_id, "tool": tool, "args": args}
    if bound is not None:
        body["bound"] = bound
    return hashlib.sha256(canonical(body).encode()).hexdigest()


@dataclass
class Challenge:
    token: str
    action_hash: str
    expires_at: float
    needs_step_up: bool


class ConfirmationService:
    def __init__(self, ttl: int = 120, otp_max_attempts: int = 3, clock=time.time):
        self.ttl, self.otp_max_attempts, self.clock = ttl, otp_max_attempts, clock
        self._tokens: dict[str, dict] = {}
        self.sms_outbox: list[dict] = []  # simulated SMS channel; the OTP never appears in API responses

    def issue(self, session_id: str, ahash: str, needs_step_up: bool) -> Challenge:
        token = secrets.token_urlsafe(16)
        exp = self.clock() + self.ttl
        rec = {"session": session_id, "hash": ahash, "exp": exp, "used": False, "attempts": 0,
               "otp": f"{secrets.randbelow(10**6):06d}" if needs_step_up else None}
        self._tokens[token] = rec
        if needs_step_up:
            self.sms_outbox.append({"session": session_id, "token": token, "otp": rec["otp"]})
        return Challenge(token, ahash, exp, needs_step_up)

    def redeem(self, session_id: str, token: str, ahash: str, otp: str | None = None) -> tuple[bool, str]:
        rec = self._tokens.get(token)
        if rec is None:
            return False, "UNKNOWN_TOKEN"
        if rec["used"]:
            return False, "REPLAYED_TOKEN"
        if rec["session"] != session_id:
            return False, "SESSION_MISMATCH"
        if self.clock() > rec["exp"]:
            rec["used"] = True
            return False, "EXPIRED_TOKEN"
        if not hmac.compare_digest(rec["hash"], ahash):
            rec["used"] = True  # burn on mismatch: no probing
            return False, "HASH_MISMATCH"
        if rec["otp"] is not None:
            if otp is None:
                return False, "OTP_REQUIRED"
            rec["attempts"] += 1
            if not hmac.compare_digest(rec["otp"], str(otp)):
                if rec["attempts"] >= self.otp_max_attempts:
                    rec["used"] = True
                return False, "OTP_WRONG"
        rec["used"] = True
        return True, "OK"