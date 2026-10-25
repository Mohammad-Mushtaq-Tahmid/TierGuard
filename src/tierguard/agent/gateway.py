"""The ONLY path from the agent to the bank. Validates, decides, confirms, executes, audits.
The LLM proposes tool calls; this deterministic layer disposes."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from tierguard.audit.log import AuditLog
from tierguard.bank.mock import Bank, BankError
from tierguard.policy.confirm import ConfirmationService, action_hash
from tierguard.policy.pdp import Policy
from tierguard.tools.registry import build_registry
from tierguard.tools.schema import ArgError, validate

# how much each kind of failure moves the circuit-breaker score
FAIL_WEIGHT = {"SMUGGLED_IDENTITY_ARG": 2, "UNKNOWN_TOOL": 2, "BLACK_TIER": 2, "HASH_MISMATCH": 2,
               "REPLAYED_TOKEN": 2, "SESSION_MISMATCH": 2, "NOT_FOUND": 1}


@dataclass
class Session:
    customer_id: str
    role: str = "customer"          # set by authentication only, never by chat text
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    failure_score: int = 0
    locked: bool = False


@dataclass
class GatewayResult:
    status: str                      # executed | needs_confirmation | denied | error | locked
    tool: str
    data: dict | None = None
    decision: dict | None = None
    card: dict | None = None
    token: str | None = None
    error_code: str | None = None
    message: str | None = None
    retryable: bool = False
    untrusted_outputs: tuple = ()

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v not in (None, (), False)} | {"status": self.status}


class Gateway:
    def __init__(self, bank: Bank, policy: Policy | None = None, audit: AuditLog | None = None,
                 confirmations: ConfirmationService | None = None):
        self.bank = bank
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.conf = confirmations or ConfirmationService(self.policy.params["confirmation_ttl_seconds"],
                                                         self.policy.params["otp_max_attempts"])
        self.tools = build_registry()
        self.audit.append("system", "policy_loaded", {"sha256": self.policy.sha256, "tools": sorted(self.tools)})

    # ----- internals -----
    def _fail(self, s: Session, code: str):
        s.failure_score += FAIL_WEIGHT.get(code, 1)
        if s.failure_score >= self.policy.params["max_failure_score"] and not s.locked:
            s.locked = True
            self.audit.append(s.session_id, "security_lock", {"score": s.failure_score, "last": code})

    def register_failure(self, s: Session, code: str):
        """Public hook for other layers (rate limiter...) to feed the circuit breaker."""
        self._fail(s, code)

    def _deny(self, s, tool, code, msg, decision=None, weighted=True):
        if weighted:
            self._fail(s, code)
        self.audit.append(s.session_id, "denied", {"tool": tool, "code": code, "message": msg})
        return GatewayResult("denied", tool, decision=decision.to_dict() if decision else None,
                             error_code=code, message=msg)

    # ----- public -----
    def call(self, s: Session, tool: str, args: dict, *, taint=(), token: str | None = None,
             otp: str | None = None) -> GatewayResult:
        self.audit.append(s.session_id, "request", {"tool": tool, "args": args, "taint": sorted(taint),
                                                    "has_token": bool(token)})
        if s.locked:
            return self._deny(s, tool, "SESSION_LOCKED", "Session is locked for security. Human review required.",
                              weighted=False)
        spec = self.tools.get(tool)
        if spec is None:
            return self._deny(s, tool, "UNKNOWN_TOOL", "That tool does not exist")
        try:
            norm = validate(spec.params, args)
        except ArgError as e:
            return self._deny(s, tool, e.code, e.message)
        try:
            ctx = spec.context_fn(self.bank, s.customer_id, norm, self.policy.params) if spec.context_fn else {}
            bound = spec.bind_fn(self.bank, s.customer_id, norm) if spec.bind_fn else None
        except BankError as e:
            self._fail(s, e.code)
            self.audit.append(s.session_id, "error", {"tool": tool, "code": e.code})
            return GatewayResult("error", tool, error_code=e.code, message=e.message)
        d = self.policy.decide(tool, s, ctx, frozenset(taint))
        self.audit.append(s.session_id, "decision", {"tool": tool, "args": norm, **d.to_dict(), "context": ctx})
        if d.verdict == "DENY":
            return self._deny(s, tool, "BLACK_TIER" if d.tier == "black" and "UNKNOWN_TOOL" not in d.reasons else d.reasons[0],
                              "This operation is never performed by the assistant", d)
        ahash = action_hash(s.customer_id, tool, norm, bound)
        if d.verdict == "ALLOW":
            return self._execute(s, spec, norm, d, idem=None)
        # confirmation / step-up path
        if token is None:
            ch = self.conf.issue(s.session_id, ahash, d.verdict == "REQUIRE_STEP_UP")
            try:
                summary = spec.card_fn(self.bank, s.customer_id, norm)
            except BankError as e:
                return GatewayResult("error", tool, error_code=e.code, message=e.message)
            tainted = sorted(set(self.policy.tools[tool].get("sensitive_args", [])) & set(taint))
            card = {"tool": tool, "tier": d.tier, "summary": summary, "warnings": d.warnings,
                    "risk_warning": d.risk_warning, "needs_step_up": ch.needs_step_up,
                    "untrusted_values": tainted, "action_hash": ahash[:12], "expires_in_s": self.conf.ttl}
            self.audit.append(s.session_id, "confirmation_issued", {"tool": tool, "action_hash": ahash,
                                                                    "tier": d.tier, "step_up": ch.needs_step_up,
                                                                    "bound": bound})
            return GatewayResult("needs_confirmation", tool, decision=d.to_dict(), card=card, token=ch.token)
        ok, why = self.conf.redeem(s.session_id, token, ahash, otp)
        self.audit.append(s.session_id, "confirmation_redeemed" if ok else "confirmation_rejected",
                          {"tool": tool, "action_hash": ahash, "result": why})
        if not ok:
            return self._deny(s, tool, why, f"Confirmation rejected: {why}", d,
                              weighted=why not in ("OTP_REQUIRED", "EXPIRED_TOKEN"))
        return self._execute(s, spec, norm, d, idem=token)

    def _execute(self, s, spec, args, d, idem):
        last = None
        for attempt in range(3):  # retries reuse the same idempotency key: no double spend
            try:
                data = spec.fn(self.bank, s.customer_id, args, idem)
                if spec.name == "schedule_transfer" and not data.get("duplicate"):
                    self.bank.scheduled[data["schedule_id"]]["auth_tier"] = d.tier      # what the user actually authorised
                s.failure_score = 0
                self.audit.append(s.session_id, "executed", {"tool": spec.name, "args": args, "result": data,
                                                             "attempt": attempt + 1})
                return GatewayResult("executed", spec.name, data=data, decision=d.to_dict(),
                                     untrusted_outputs=spec.untrusted_outputs)
            except BankError as e:
                last = e
                if e.code != "UPSTREAM_ERROR":
                    break
        self.audit.append(s.session_id, "error", {"tool": spec.name, "code": last.code})
        if last.code == "NOT_FOUND":
            self._fail(s, "NOT_FOUND")
        return GatewayResult("error", spec.name, decision=d.to_dict(), error_code=last.code, message=last.message,
                             retryable=last.code == "UPSTREAM_ERROR")

    def run_scheduled(self, s: Session) -> list[dict]:
        """Execute scheduled transfers that are due. The authorisation was given when they were created (a confirmed card);
        here everything is re-checked, and anything the user did not authorise at that strength is NOT run."""
        limit = self.policy.params["yellow_daily_transfer_limit_fen"]
        out = []
        for sid in self.bank.due_scheduled(s.customer_id):
            x = self.bank.scheduled[sid]
            base = {"schedule_id": sid, "payee_id": x["payee_id"], "amount_fen": x["amount_fen"], "date": x["date"]}
            try:
                self.bank.get_payee(s.customer_id, x["payee_id"])
                if x["auth_tier"] not in ("yellow", "red"):
                    raise BankError("NOT_AUTHORISED", "No confirmed authorisation is recorded for this instruction")
                if x["auth_tier"] != "red" and self.bank.daily_transfer_total_fen(s.customer_id) + x["amount_fen"] > limit:
                    raise BankError("LIMIT_NEEDS_RECONFIRM", "Other transfers today push the total over 1,000 CNY: please confirm again")
                res = self.bank.transfer(s.customer_id, x["payee_id"], x["amount_fen"], x["memo"], idem_key=f"sched:{sid}")
                x["status"], x["detail"] = "executed", res["transaction_id"]
                self.audit.append(s.session_id, "scheduled_executed", {**base, "tier": x["auth_tier"], "transaction_id": res["transaction_id"]})
                out.append({**base, "status": "executed", "payee_name": res["payee_name"], "amount_cny": res["amount_cny"]})
            except BankError as e:
                x["status"], x["detail"] = "failed", e.code
                self.audit.append(s.session_id, "scheduled_failed", {**base, "reason": e.code})
                out.append({**base, "status": "failed", "reason": e.code})
        return out