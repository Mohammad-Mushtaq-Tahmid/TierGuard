"""Policy Decision Point. Pure function of (tool, args, session, context, taint).
The LLM never sees or edits this; policy is data (tiers.yaml) plus a few lines of code."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ORDER = {"green": 0, "yellow": 1, "red": 2, "black": 3}
VERDICT = {"green": "ALLOW", "yellow": "REQUIRE_CONFIRM", "red": "REQUIRE_STEP_UP", "black": "DENY"}


@dataclass
class Decision:
    verdict: str
    tier: str
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    risk_warning: str | None = None

    def to_dict(self):
        return {"verdict": self.verdict, "tier": self.tier, "reasons": self.reasons,
                "warnings": self.warnings, "risk_warning": self.risk_warning}


class Policy:
    def __init__(self, path: str | None = None):
        path = path or str(Path(__file__).with_name("tiers.yaml"))
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.params = cfg["params"]
        self.tools = cfg["tools"]

    def baseline(self, tool: str) -> str | None:
        t = self.tools.get(tool)
        return t["tier"] if t else None

    def decide(self, tool, session, context, taint=frozenset()) -> Decision:
        spec = self.tools.get(tool)
        if spec is None:
            return Decision("DENY", "black", ["UNKNOWN_TOOL"])  # default deny
        if getattr(session, "role", None) != "customer":
            return Decision("DENY", "black", ["ROLE_NOT_ALLOWED"])
        tier, reasons = spec["tier"], []
        for rule in spec.get("escalate", []):
            if context.get(rule["when"]) and ORDER[rule["to"]] > ORDER[tier]:
                tier = rule["to"]
                reasons.append(rule["reason"])
        if ORDER[tier] >= ORDER["yellow"] or spec.get("sensitive_args"):
            if set(spec.get("sensitive_args", [])) & set(taint) and ORDER["red"] > ORDER[tier]:
                tier = "red"
                reasons.append("TAINTED_ARGUMENT")
            if (ORDER[tier] >= ORDER["yellow"]
                    and getattr(session, "failure_score", 0) >= self.params["escalate_after_failures"]
                    and tier != "black" and ORDER["red"] > ORDER[tier]):
                tier = "red"
                reasons.append("RECENT_FAILURES")
        if tier == "black":
            reasons.append("NEVER_EXECUTED_BY_AGENT")
        warnings = [w["code"] for w in spec.get("warnings", []) if context.get(w["when"])]
        risk = spec.get("risk_warning") if tier in ("red",) or (tier == "yellow" and warnings) else None
        if tier == "red" and not risk:
            risk = "This is a high-risk operation and needs strong authentication."
        return Decision(VERDICT[tier], tier, reasons, warnings, risk)
