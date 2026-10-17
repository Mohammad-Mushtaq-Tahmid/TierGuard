"""Cross-scenario plan: review spending -> read subscriptions -> pick unused ones -> pause each (you confirm each)
-> offer to put the saving into a low-risk fund (SMS code).

Safety rules built into the shape of the plan:
  * The plan only ever calls tools through the Gateway, so every step keeps its own tier and confirmation.
  * Approving the plan covers its READ-ONLY yellow step (the subscription query). The helper refuses anything
    that changes state or is red, so a plan can never pre-approve a money-moving step.
  * Every state-changing step asks for its own confirmation card (red steps also need the SMS code).
  * The user can skip a step or cancel the rest at any time. Nothing is retried blindly after a failure.
  * Rollback is only claimed where it is real: a paused subscription can be resumed; a purchase is not auto-reversed.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from tierguard.agent.nlu import TurnResult, _confirm_turn, _grounded, _say, period_dates
from tierguard.audit.log import canonical

HUMAN_ZH = "您也可以说“转人工”，由人工客服接手。"
HUMAN_EN = "You can also say 'human' and a person will take over."
TIER_ZH = {"green": "绿色，自动", "yellow": "黄色，需确认", "red": "红色，需短信验证"}


@dataclass
class Step:
    id: str
    zh: str
    en: str
    tool: str
    deps: list
    tier: str
    state: str = "pending"       # pending | running | awaiting_confirmation | succeeded | failed | skipped | cancelled
    write: bool = False


class SavingsPlan:
    def __init__(self, gateway, session, lang, max_pause=2, min_days=30):
        self.g, self.s, self.lang = gateway, session, lang
        self.max_pause, self.min_days = max_pause, min_days
        self.plan_id = secrets.token_hex(4)
        self.stage = "preview"       # preview | executing | offer | undo_offer | done | cancelled
        self.mode = "pause"          # pause | undo
        self.queue: list[dict] = []
        self.paused: list[dict] = []
        self.item: dict | None = None
        self.card_kind = None        # "sub" | "buy": what the open confirmation card is for
        self.saving = Decimal("0")
        self.product: dict | None = None
        b = gateway.policy.baseline
        self.steps = [
            Step("S1", "回顾上月消费", "Review last month's spending", "summarise_spending", [], b("summarise_spending")),
            Step("S2", "读取订阅列表（随计划一并确认）", "Read your subscription list (approved with the plan)",
                 "list_subscriptions", [], b("list_subscriptions")),
            Step("S3", f"筛选 {min_days} 天以上没用的订阅，取最便宜的 {max_pause} 个",
                 f"Pick the cheapest {max_pause} subscriptions unused for {min_days}+ days", "", ["S1", "S2"], "green"),
            Step("S4", "逐个暂停（每个单独确认）", "Pause each one (you confirm each)",
                 "pause_or_cancel_subscription", ["S3"], b("pause_or_cancel_subscription"), write=True),
            Step("S5", "把节省的钱存入低风险基金（由您决定，需短信验证）",
                 "Offer: put the saving into a low-risk fund (your call, SMS code)", "buy_product", ["S4"],
                 b("buy_product"), write=True),
        ]

    # ---------- helpers ----------
    def _t(self, st: Step) -> str:
        return st.zh if self.lang == "zh" else st.en

    def _step(self, sid: str) -> Step:
        return next(x for x in self.steps if x.id == sid)

    def _audit(self, event: str, data: dict):
        self.g.audit.append(self.s.session_id, event, {"plan_id": self.plan_id, **data})

    def view(self) -> dict:
        return {"id": self.plan_id, "stage": self.stage,
                "title": "省钱计划" if self.lang == "zh" else "Savings plan",
                "steps": [{"id": x.id, "title": self._t(x), "tier": x.tier, "state": x.state, "deps": x.deps}
                          for x in self.steps]}

    def _cancel_remaining(self):
        for x in self.steps:
            if x.state in ("pending", "running", "awaiting_confirmation"):
                x.state = "cancelled"

    def summary(self) -> str:
        groups = {"succeeded": [], "skipped": [], "cancelled": [], "failed": []}
        for x in self.steps:
            if x.write and x.state in groups and x.id not in ("S4",):
                groups[x.state].append(self._t(x))
        zh = {"succeeded": "已完成", "skipped": "已跳过", "cancelled": "未执行", "failed": "失败"}
        en = {"succeeded": "done", "skipped": "skipped", "cancelled": "not run", "failed": "failed"}
        zh_mode = self.lang == "zh"
        parts = []
        for k, items in groups.items():
            if items or k == "succeeded":
                label = (zh if zh_mode else en)[k]
                none = "无" if zh_mode else "none"
                parts.append(f"{label}{'：' if zh_mode else ': '}" + (("、" if zh_mode else ", ").join(items) if items else none))
        if zh_mode:
            return "计划进展——" + "；".join(parts) + "。"
        return "Plan status - " + "; ".join(parts) + "."

    @staticmethod
    def _merge(a: TurnResult, b: TurnResult) -> TurnResult:
        return TurnResult(b.kind, a.message + "\n\n" + b.message, pending=b.pending, card=b.card,
                          notes=list(a.notes) + list(b.notes), clarify_state=b.clarify_state, code=b.code)

    def _abort(self, msg: str) -> TurnResult:
        self._cancel_remaining()
        self.stage = "done"
        return TurnResult("error", msg + " " + _say(self.lang, HUMAN_ZH, HUMAN_EN))

    def _approved_read(self, tool: str, args: dict):
        """Plan approval covers a READ-ONLY yellow step. Anything that changes state or is red is refused here."""
        spec = self.g.tools[tool]
        if spec.state_changing:
            raise PermissionError(f"{tool} changes state: it needs its own confirmation")
        r = self.g.call(self.s, tool, args)
        if r.status != "needs_confirmation":
            return r
        if r.card["tier"] != "yellow":
            raise PermissionError(f"{tool} is {r.card['tier']}: a plan cannot pre-approve it")
        self._audit("plan_read_approved", {"tool": tool, "tier": r.card["tier"]})
        return self.g.call(self.s, tool, args, token=r.token)

    # ---------- stages ----------
    def preview(self, notes=None) -> TurnResult:
        msg = _say(self.lang,
                   "这是一个多步骤计划（见下方）。回复“确认”开始：每次修改账户我都会再让您确认一次；随时可以说“取消”或“跳过”。",
                   "This is a multi-step plan (shown below). Reply 'confirm' to start: I'll ask you to confirm every change "
                   "to your account, and you can say 'cancel' or 'skip' at any time.")
        return TurnResult("plan", msg, plan=self, notes=notes or [])

    def start(self) -> TurnResult:
        g, s, lang = self.g, self.s, self.lang
        self.stage = "executing"
        shape = [{"id": x.id, "tool": x.tool, "tier": x.tier} for x in self.steps]
        self._audit("plan_approved", {"plan": "savings_review", "steps": shape,
                                      "plan_hash": hashlib.sha256(canonical(shape).encode()).hexdigest()})
        s1, s2, s3 = self._step("S1"), self._step("S2"), self._step("S3")
        s1.state = "running"
        start, end = period_dates(g.bank.today, "last_month")
        r1 = g.call(s, "summarise_spending", {"start": start, "end": end})
        if r1.status != "executed":
            s1.state = "failed"
            return self._abort(_say(lang, "读取上月消费失败，计划已停止，未做任何修改。",
                                    "Could not read last month's spending. The plan stopped; nothing was changed."))
        s1.state = "succeeded"
        s2.state = "running"
        r2 = self._approved_read("list_subscriptions", {})
        if r2.status != "executed":
            s2.state = "failed"
            return self._abort(_say(lang, "读取订阅列表失败，计划已停止，未做任何修改。",
                                    "Could not read your subscriptions. The plan stopped; nothing was changed."))
        s2.state = "succeeded"
        s3.state = "running"
        subs = r2.data["subscriptions"]
        low = [x for x in subs if x["status"] == "active" and x["days_since_last_use"] >= self.min_days]
        low.sort(key=lambda x: (Decimal(x["amount_cny"]), x["subscription_id"]))        # cheapest first, as in the brief
        chosen = low[:self.max_pause]
        s3.state = "succeeded"
        spend = r1.data
        sub_total = spend["by_category"].get("subscription", {}).get("total_cny")
        head = _say(lang, f"上个月共支出 {spend['total_cny']} 元" + (f"，其中订阅类 {sub_total} 元" if sub_total else "") + "。",
                    f"Last month you spent {spend['total_cny']} CNY" + (f", of which {sub_total} CNY was subscriptions" if sub_total else "") + ". ")
        notes: list = []
        if not chosen:
            for x in self.steps:
                if x.state == "pending":
                    x.state = "cancelled"
            self.stage = "done"
            msg = head + _say(lang, f"没有发现超过 {self.min_days} 天未使用的订阅，无需操作。",
                              f"I found no subscription unused for {self.min_days}+ days, so there is nothing to do.")
            return TurnResult("answer", _grounded(msg, [spend, r2.data], "", notes, derived=[self.min_days]))
        items = [{"id": x["subscription_id"], "name": x["name"], "amount": Decimal(x["amount_cny"]),
                  "days": x["days_since_last_use"]} for x in chosen]
        planned = sum(i["amount"] for i in items)
        s4 = self._step("S4")
        at = self.steps.index(s4)
        new = []
        for k, i in enumerate(items, 1):
            i["step"] = f"S4.{k}"
            new.append(Step(i["step"], f"暂停 {i['name']}（{i['amount']:.2f} 元/月）", f"Pause {i['name']} ({i['amount']:.2f} CNY/month)",
                            "pause_or_cancel_subscription", ["S3"], s4.tier, write=True))
        self.steps[at:at + 1] = new
        self._step("S5").deps = [new[-1].id]
        self.queue = list(items)
        listing = "；".join(f"{i['name']}（{i['amount']:.2f} 元/月，已 {i['days']} 天未使用）" for i in items) if lang == "zh" else \
            "; ".join(f"{i['name']} ({i['amount']:.2f} CNY/month, unused for {i['days']} days)" for i in items)
        body = _say(lang, f"发现 {len(items)} 个超过 {self.min_days} 天未使用的订阅：{listing}。全部暂停预计每月节省 {planned:.2f} 元。下面逐个请您确认。",
                    f"I found {len(items)} subscriptions unused for {self.min_days}+ days: {listing}. Pausing all of them would save about "
                    f"{planned:.2f} CNY a month. I'll ask you to confirm them one by one.")
        info = TurnResult("answer", _grounded(head + body, [spend, r2.data], "", notes,
                                              derived=[len(items), self.min_days, f"{planned:.2f}"] + [f"{i['amount']:.2f}" for i in items]))
        return self._merge(info, self._next_item())

    def _next_item(self) -> TurnResult:
        item = self.queue.pop(0)
        self.item = item
        act = "pause" if self.mode == "pause" else "resume"
        st = self._step(item["step"])
        st.state = "awaiting_confirmation"
        r = _confirm_turn(self.g, self.s, "pause_or_cancel_subscription", {"subscription_id": item["id"], "action": act}, self.lang, [])
        if r.kind != "confirm":
            st.state = "failed"
            self._cancel_remaining()
            self.stage = "done"
            return TurnResult("error", r.message + " " + _say(self.lang, HUMAN_ZH, HUMAN_EN))
        self.card_kind = "sub"
        return r

    def step_done(self, result: TurnResult, pending: dict) -> TurnResult:
        """Called by the dialogue after a plan step's confirmation was redeemed (success or failure)."""
        if pending["tool"] == "pause_or_cancel_subscription":
            return self._after_sub(result, pending["args"]["action"])
        if pending["tool"] == "buy_product":
            ok = result.kind == "answer"
            self._step("S5").state = "succeeded" if ok else "failed"
            self.stage = "done"
            tail = self.summary() + ("" if ok else " " + _say(self.lang, HUMAN_ZH, HUMAN_EN))
            return self._merge(result, TurnResult("answer", tail))
        return result

    def _after_sub(self, result: TurnResult, act: str) -> TurnResult:
        st = self._step(self.item["step"])
        ok = result.kind == "answer"
        st.state = "succeeded" if ok else "failed"
        if not ok:
            self._cancel_remaining()
            if act == "pause" and self.paused:
                self.stage = "undo_offer"
                names = "、".join(i["name"] for i in self.paused) if self.lang == "zh" else ", ".join(i["name"] for i in self.paused)
                msg = self.summary() + "\n" + _say(self.lang, f"已暂停的订阅：{names}。要撤销（恢复它们）吗？回复“确认”恢复，回复“取消”保持现状。" + HUMAN_ZH,
                                                    f"Already paused: {names}. Undo (resume them)? Reply 'confirm' to resume, 'cancel' to keep things as they are. " + HUMAN_EN)
                return self._merge(result, TurnResult("answer", msg))
            self.stage = "done"
            return self._merge(result, TurnResult("answer", self.summary() + " " + _say(self.lang, HUMAN_ZH, HUMAN_EN)))
        if act == "pause":
            self.paused.append(self.item)
        if self.queue:
            return self._merge(result, self._next_item())
        if act == "pause":
            return self._merge(result, self._make_offer())
        self.stage = "done"
        return self._merge(result, TurnResult("answer", self.summary()))

    def _make_offer(self) -> TurnResult:
        g, lang = self.g, self.lang
        self.saving = sum((i["amount"] for i in self.paused), Decimal("0"))
        pr = g.call(self.s, "list_products", {})
        low = [p for p in (pr.data or {}).get("products", []) if p["suitable"] and p["risk"] == "R1"] if pr.status == "executed" else []
        if not self.paused or not low or self.saving < Decimal(low[0]["min_amount_cny"]):
            self._cancel_remaining()
            self.stage = "done"
            return TurnResult("answer", self.summary())
        self.stage = "offer"
        self.product = low[0]
        p = self.product
        pname = p["name"] if lang == "zh" else p["name_en"]
        msg = _say(lang,
                   f"每月可节省 {self.saving:.2f} 元。要把 {self.saving:.2f} 元存入低风险产品「{pname}」吗？（{p['risk_zh']} {p['risk']}，起购 {p['min_amount_cny']} 元，"
                   f"参考七日年化 {p['ref_yield_percent']}%，历史数据，不代表未来收益。）回复“确认”继续（需要短信验证码），回复“取消”则不投资。",
                   f"You can save {self.saving:.2f} CNY a month. Put {self.saving:.2f} CNY into the low-risk product '{pname}'? ({p['risk_en']} {p['risk']}, "
                   f"minimum {p['min_amount_cny']} CNY, reference yield {p['ref_yield_percent']}%, historical, not guaranteed.) Reply 'confirm' to continue "
                   f"(SMS code needed) or 'cancel' to skip investing.")
        return TurnResult("answer", _grounded(msg, [pr.data], "", [], derived=[f"{self.saving:.2f}"]))

    def accept_offer(self) -> TurnResult:
        self.stage = "executing"
        st = self._step("S5")
        st.state = "awaiting_confirmation"
        r = _confirm_turn(self.g, self.s, "buy_product",
                          {"product_id": self.product["product_id"], "amount": f"{self.saving:.2f}"}, self.lang, [])
        if r.kind != "confirm":
            st.state = "failed"
            self.stage = "done"
            return TurnResult("error", r.message)
        self.card_kind = "buy"
        return r

    def decline_offer(self) -> TurnResult:
        self._step("S5").state = "cancelled"
        self.stage = "done"
        return TurnResult("answer", _say(self.lang, "好的，不做投资。已暂停的订阅保持不变。", "OK, no investment. The subscriptions you paused stay paused.")
                          + " " + self.summary())

    def skip(self) -> TurnResult:
        if self.card_kind == "buy":
            return self.decline_offer()
        st = self._step(self.item["step"])
        st.state = "skipped"
        note = TurnResult("answer", _say(self.lang, "好的，已跳过这一项。", "OK, skipped that one."))
        if self.queue:
            return self._merge(note, self._next_item())
        if self.mode == "pause":
            return self._merge(note, self._make_offer())
        self.stage = "done"
        return self._merge(note, TurnResult("answer", self.summary()))

    def cancel(self) -> TurnResult:
        self._cancel_remaining()
        self.stage = "cancelled"
        self._audit("plan_cancelled", {"paused_so_far": [i["id"] for i in self.paused]})
        return TurnResult("answer", _say(self.lang, "已取消计划，后面的步骤不会执行。", "Plan cancelled; the remaining steps will not run.")
                          + " " + self.summary())

    def start_undo(self) -> TurnResult:
        self.stage, self.mode = "executing", "undo"
        tier = self.g.policy.baseline("pause_or_cancel_subscription")
        items = []
        for k, i in enumerate(self.paused, 1):
            st = Step(f"U.{k}", f"恢复 {i['name']}", f"Resume {i['name']}", "pause_or_cancel_subscription", [], tier, write=True)
            self.steps.append(st)
            items.append(dict(i, step=st.id))
        self.queue = items
        return self._next_item()

    def decline_undo(self) -> TurnResult:
        self.stage = "done"
        return TurnResult("answer", _say(self.lang, "好的，保持现状。", "OK, I'll leave things as they are.") + " " + self.summary())