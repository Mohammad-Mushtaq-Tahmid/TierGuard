"""Multi-turn dialogue manager: confirm / cancel / OTP entry / one clarifying question at a time.
Safety rule: a message only counts as 'yes' if it is EXACTLY a confirmation word. Anything else
('yes, but make it 5000') is a new request that replaces the pending one, so a confirmation can never
approve something the user did not see on the card."""
from __future__ import annotations

import re
import time
from collections import deque

from decimal import Decimal

from tierguard.agent.nlu import (ALL_WORDS, CJK, TurnResult, _confirm_turn, _say, confirm_pending, latest_otp, plan_turn,
                                 product_flow, subscription_flow, transfer_flow, wealth_flow)
from tierguard.agent.sanitize import count_invisible, sanitize
from tierguard.agent.dates import strip_dates
from tierguard.agent.zh_numbers import extract_amounts

YES = {"确认", "确定", "好", "好的", "是", "是的", "对", "可以", "行", "没问题", "同意", "确认转账", "确认操作", "嗯",
       "ok", "okay", "yes", "y", "yep", "yeah", "sure", "confirm", "confirmed", "do it", "go ahead"}
NO = {"取消", "算了", "不", "不要", "不用了", "不要了", "别转了", "停", "不转了", "no", "n", "nope", "cancel", "stop",
      "never mind", "nevermind", "abort"}
ORD_ZH = {"第一个": 0, "第一": 0, "一": 0, "1": 0, "第二个": 1, "第二": 1, "二": 1, "2": 1, "第三个": 2, "第三": 2,
          "三": 2, "3": 2, "第一张": 0, "第二张": 1, "第三张": 2, "第一笔": 0, "第二笔": 1, "第三笔": 2}
ORD_EN = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
MAX_OTP_TRIES = 3
SKIP = {"跳过", "跳过这个", "下一个", "skip", "next", "skip this", "skip it"}
HUMAN = {"转人工", "人工", "人工客服", "找人工", "human", "a human", "agent", "talk to a human", "speak to a human"}


def norm(t: str) -> str:
    return re.sub(r"[\s，。！!,.?？~～、]+", " ", t.strip().lower()).strip()


def ordinal_index(text: str):
    """'第二个' / '2' / 'the second one' -> 1 ; None when the text is not an ordinal."""
    t = norm(text)
    idx = ORD_ZH.get(t.replace(" ", ""))
    if idx is None:
        idx = next((ORD_EN[w] for w in re.findall(r"[a-z0-9]+", t) if w in ORD_EN), None)
    return idx


def pick_candidate(text: str, cands: list[dict]):
    """Choose a payee by last 4 digits of the account or by ordinal."""
    m = re.search(r"\d{4}", norm(text))
    if m:
        hits = [c for c in cands if c["account_masked"].endswith(m.group(0))]
        if len(hits) == 1:
            return hits[0]
    idx = ordinal_index(text)
    return cands[idx] if idx is not None and idx < len(cands) else None


class RateLimiter:
    """Sliding window: at most `max_events` messages per `window` seconds per session."""
    def __init__(self, max_events: int = 20, window: float = 60.0, clock=time.time):
        self.max_events, self.window, self.clock = max_events, window, clock
        self._hits: deque = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self._hits and now - self._hits[0] > self.window:
            self._hits.popleft()
        if len(self._hits) >= self.max_events:
            return False
        self._hits.append(now)
        return True


class Dialogue:
    def __init__(self, gateway, session, llm, limiter: RateLimiter | None = None):
        self.g, self.s, self.llm = gateway, session, llm
        self.limiter = limiter or RateLimiter()
        self.pending = None          # confirmation card awaiting yes / no
        self.awaiting_otp = False
        self.otp_tries = 0
        self.clarify = None          # state of the one open clarifying question
        self.lang = "en"
        self.plan = None             # the multi-step plan in progress (agent/planner.py)
        self.plan_view = None        # last plan state shown to the user (kept after the plan ends)
        self.handoff = False         # a human has taken over: the agent stops acting

    # ---- demo helper: the simulated SMS inbox (never returned by the gateway itself) ----
    def demo_sms(self):
        if self.pending and self.pending.get("needs_step_up"):
            return latest_otp(self.g, self.pending["token"])
        return None

    def _reset(self):
        self.pending, self.awaiting_otp, self.otp_tries, self.clarify = None, False, 0, None

    def handle(self, text: str) -> TurnResult:
        r = self._handle(text)
        if self.plan:
            self.plan_view = self.plan.view()
            if self.plan.stage in ("done", "cancelled"):
                self.plan = None
        return r

    def _handle(self, text: str) -> TurnResult:
        raw = text
        text = sanitize(raw)
        if CJK.search(text):
            self.lang = "zh"
        elif re.search(r"[A-Za-z]", text):
            self.lang = "en"
        lang = self.lang
        if self.s.locked:
            return TurnResult("error", _say(lang, "为了您的资金安全，本次会话已被锁定，请联系人工客服。",
                                           "For your security this session is locked. Please contact support."))
        if not self.limiter.allow():
            self.g.audit.append(self.s.session_id, "rate_limited", {"limit": self.limiter.max_events})
            self.g.register_failure(self.s, "RATE_LIMITED")        # sustained flooding trips the circuit breaker
            return TurnResult("error", _say(lang, "消息过于频繁，请稍后再试。", "Too many messages, please slow down."),
                              code="RATE_LIMITED")
        if count_invisible(raw):
            self.g.audit.append(self.s.session_id, "input_sanitized", {"invisible_chars": count_invisible(raw)})
        if not text:
            return TurnResult("clarify", _say(lang, "请输入内容。", "Please type a message."))
        t = norm(text)
        prefix = ""

        if self.handoff:
            return TurnResult("answer", _say(lang, "本会话已转人工客服，自动操作已暂停。点击“重置演示”可重新开始。",
                                            "This session was handed to a person; automatic actions are paused. Use 'Reset demo' to start again."))
        if t in HUMAN:
            if self.plan:
                self._drop_plan()
            self._reset()
            self.handoff = True
            self.g.audit.append(self.s.session_id, "human_handoff", {})
            return TurnResult("answer", _say(lang, "已为您转接人工客服（演示）。为了安全，本会话的自动操作已暂停；点击“重置演示”可重新开始。",
                                            "You are being handed to a person (demo). For safety, automatic actions are paused; use 'Reset demo' to start again."))

        if self.plan and self.plan.stage in ("preview", "offer", "undo_offer"):
            r = self._plan_decision(t)
            if r is not None:
                return self._finish(r)
            self._drop_plan()
            prefix = _say(lang, "（上一个计划已取消。）", "(The previous plan was cancelled.) ")

        if self.pending:
            if self.awaiting_otp:
                if re.fullmatch(r"\d{6}", text.strip()):
                    return self._redeem(text.strip())
                if t in NO:
                    return self._cancel()
                if not (len(CJK.findall(text)) >= 3 or len(re.findall(r"[A-Za-z]", text)) >= 5):
                    return TurnResult("confirm", _say(lang, "请输入6位短信验证码，或回复“取消”。",
                                                      "Please enter the 6-digit SMS code, or say 'cancel'."))
            elif t in YES:
                if self.pending["needs_step_up"]:
                    self.awaiting_otp = True
                    return TurnResult("confirm", _say(lang, "验证码已发送到您的手机（演示模式：见短信面板）。请输入6位验证码。",
                                                      "A code was sent to your phone (demo: see the SMS panel). Enter the 6-digit code."))
                return self._redeem(None)
            elif self.plan and self.plan.stage == "executing" and t in SKIP:
                self._reset()
                return self._finish(self.plan.skip())
            elif t in NO:
                return self._cancel()
            # anything else is a NEW request: the pending one is dropped (its token simply expires unused)
            prefix = _say(lang, "（上一个待确认的操作已取消。）", "(The previous pending action was cancelled.) ")
            self._reset()
            if self.plan:
                self._drop_plan()
                prefix = _say(lang, "（待确认的操作和计划已取消。）", "(The pending action and the plan were cancelled.) ")

        if self.clarify:
            r = self._follow_up(text)
            if r is not None:
                return self._finish(r, prefix)
            self.clarify = None

        return self._finish(plan_turn(self.g, self.s, self.llm, text), prefix)

    # ---- internals ----
    def _finish(self, r: TurnResult, prefix: str = "") -> TurnResult:
        if prefix:
            r.message = prefix + r.message
        self.clarify = r.clarify_state if r.kind == "clarify" else None
        if r.plan is not None:
            self.plan = r.plan
            self.plan_view = r.plan.view()
        if r.kind == "confirm" and r.pending:
            self.pending, self.awaiting_otp, self.otp_tries = r.pending, False, 0
        return r

    def _cancel(self):
        if self.plan and self.plan.stage == "executing":
            self._reset()
            return self._finish(self.plan.cancel())
        self._reset()
        return TurnResult("answer", _say(self.lang, "已取消，未执行任何操作。", "Cancelled. Nothing was done."))

    def _drop_plan(self):
        """A new request replaces the plan: remaining steps are cancelled, finished steps stay as they are."""
        if self.plan:
            self.plan.cancel()
            self.plan_view = self.plan.view()
            self.plan = None

    def _plan_decision(self, t: str):
        p, lang = self.plan, self.lang
        if p.stage == "preview":
            if t in YES:
                return p.start()
            if t in NO:
                r = p.cancel()
                r.message = _say(lang, "已取消计划，没有执行任何操作。", "Plan cancelled. Nothing was done.")
                return r
        elif p.stage == "offer":
            if t in YES:
                return p.accept_offer()
            if t in NO or t in SKIP:
                return p.decline_offer()
        elif p.stage == "undo_offer":
            if t in YES:
                return p.start_undo()
            if t in NO:
                return p.decline_undo()
        return None

    def _redeem(self, otp):
        pending = self.pending
        r = confirm_pending(self.g, self.s, pending, self.lang, otp=otp)
        if r.code == "OTP_WRONG":
            self.otp_tries += 1
            left = MAX_OTP_TRIES - self.otp_tries
            if left <= 0:
                self._reset()
                return TurnResult("error", _say(self.lang, "验证码错误次数过多，操作已取消，未执行任何操作。",
                                               "Too many wrong codes. The action was cancelled and nothing was done."),
                                  code="OTP_LOCKOUT")
            r.message += _say(self.lang, f" 还可以尝试 {left} 次。", f" {left} attempt(s) left.")
            r.kind = "confirm"
            return r
        self._reset()    # executed, or failed for good (expired / no funds / bank down): the token is spent
        if self.plan and self.plan.stage == "executing":
            return self._finish(self.plan.step_done(r, pending))
        return r

    def _choose_subscription(self, text: str, st: dict):
        """Ordinal ('第二个'), or re-rank the open candidates with the same fuzzy lookup ('爱奇艺那个', 'tencent')."""
        cands = st["candidates"]
        idx = ordinal_index(text)
        if idx is not None:
            return cands[idx] if idx < len(cands) else None
        if len(text.strip()) > 40:
            return None
        probe = self.g.call(self.s, "resolve_subscription", {"query": text.strip(), "action": st["action"]})
        if probe.status != "executed":
            return None
        by_id = {c["subscription_id"]: c for c in cands}
        hits = [x for x in probe.data["candidates"] if x["subscription_id"] in by_id]
        if hits and (len(hits) == 1 or hits[0]["score"] > hits[1]["score"]):
            return by_id[hits[0]["subscription_id"]]
        return None

    def _follow_up(self, text: str):
        st, lang = self.clarify, self.lang
        if st["type"] == "payee_choice":
            c = pick_candidate(text, st["candidates"])
            if c is None:
                return None
            args = {"payee_id": c["payee_id"], "amount": f"{Decimal(st['amount']):.2f}"}
            if st.get("memo"):
                args["memo"] = st["memo"]
            if st.get("date"):
                args["date"] = st["date"]
                return _confirm_turn(self.g, self.s, "schedule_transfer", args, lang, [])
            return _confirm_turn(self.g, self.s, "transfer", args, lang, [])
        if st["type"] == "need_amount":
            amts = extract_amounts(strip_dates(text))
            if len(amts) != 1:
                return None
            return transfer_flow(self.g, self.s, lang, st["payee_query"], amts[0], [], heard=st.get("heard"), date=st.get("date"), memo=st.get("memo"))
        if st["type"] == "need_payee":
            q = re.sub(r"^(转给|转账给|给|to|send to)\s*", "", text.strip(), flags=re.I)[:60]
            if not q or extract_amounts(text) or len(q) > 20:
                return None
            probe = self.g.call(self.s, "resolve_payee", {"query": q})
            if probe.status != "executed" or not probe.data["candidates"]:
                return None     # not a payee name: treat it as a brand-new request
            return transfer_flow(self.g, self.s, lang, q, Decimal(st["amount"]), [], date=st.get("date"), memo=st.get("memo"))
        if st["type"] == "sub_choice":
            c = self._choose_subscription(text, st)
            if c is None:
                return None
            return _confirm_turn(self.g, self.s, "pause_or_cancel_subscription",
                                 {"subscription_id": c["subscription_id"], "action": st["action"]}, lang, [])
        if st["type"] == "need_target":
            q = text.strip()[:60]
            if not q or extract_amounts(text) or len(q) > 24:
                return None
            probe = self.g.call(self.s, "resolve_subscription", {"query": q, "action": st["action"]})
            if probe.status != "executed" or not probe.data["candidates"]:
                return None          # not a subscription name: treat as a brand-new request
            return subscription_flow(self.g, self.s, lang, q, st["action"], [])
        if st["type"] == "need_amount_wealth":
            amts = extract_amounts(text)
            whole = st["kind"] == "wealth_redeem" and any(w in text.lower() for w in ALL_WORDS)
            if len(amts) != 1 and not whole:
                return None
            amount = amts[0] if amts else None
            if st.get("product_id"):
                return product_flow(self.g, self.s, lang, st["kind"], st["product_id"], amount, whole, [])
            if not st.get("target_query"):
                return TurnResult("clarify", _say(lang, "您想选择哪个产品？（例如：货币基金）", "Which product? (for example: money market)"),
                                  clarify_state={"type": "need_product", "kind": st["kind"],
                                                 "amount": str(amount) if amount is not None else None, "whole": whole})
            return wealth_flow(self.g, self.s, lang, st["kind"], st["target_query"], amount, whole, [], heard=st.get("heard"))
        if st["type"] == "need_product":
            q = text.strip()[:60]
            if not q or len(q) > 24:
                return None
            probe = self.g.call(self.s, "resolve_product", {"query": q})
            if probe.status != "executed" or not probe.data["candidates"]:
                return None          # not a product name: treat as a brand-new request
            amount = Decimal(st["amount"]) if st.get("amount") else None
            if amount is None and not st.get("whole"):
                return TurnResult("clarify", _say(lang, "金额是多少？", "How much?"),
                                  clarify_state={"type": "need_amount_wealth", "kind": st["kind"], "target_query": q})
            return wealth_flow(self.g, self.s, lang, st["kind"], q, amount, st.get("whole", False), [])
        if st["type"] == "wealth_choice":
            cands = st["candidates"]
            idx = ordinal_index(text)
            chosen = None
            if idx is not None:
                chosen = cands[idx] if idx < len(cands) else None
            elif len(text.strip()) <= 40:
                probe = self.g.call(self.s, "resolve_product", {"query": text.strip()})
                if probe.status == "executed":
                    by_id = {c["product_id"]: c for c in cands}
                    hits = [x for x in probe.data["candidates"] if x["product_id"] in by_id]
                    if hits and (len(hits) == 1 or hits[0]["score"] > hits[1]["score"]):
                        chosen = by_id[hits[0]["product_id"]]
            if chosen is None:
                return None
            amount = Decimal(st["amount"]) if st.get("amount") else None
            return product_flow(self.g, self.s, lang, st["kind"], chosen["product_id"], amount, st.get("whole", False), [])
        if st["type"] == "card_choice":
            from tierguard.agent.features import card_action
            cards = st["cards"]
            m = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
            chosen = None
            if m:
                hits = [c for c in cards if c["last4_masked"].endswith(m.group(1))]
                chosen = hits[0] if len(hits) == 1 else None
            else:
                i = ordinal_index(text)
                chosen = cards[i] if i is not None and i < len(cards) else None
            if chosen is None:
                return None
            amount = Decimal(st["amount"]) if st.get("amount") else None
            return card_action(self.g, self.s, lang, st["kind"], chosen["card_id"], amount, [])
        if st["type"] == "need_limit":
            from tierguard.agent.features import card_limit_amount
            amts = extract_amounts(strip_dates(text))
            if len(amts) != 1:
                return None
            return card_limit_amount(self.g, self.s, lang, st.get("utterance", ""), amts[0], [])
        if st["type"] == "sched_choice":
            from tierguard.agent.features import cancel_scheduled_card
            i = ordinal_index(text)
            if i is None or i >= len(st["items"]):
                return None
            return cancel_scheduled_card(self.g, self.s, lang, st["items"][i], [])
        return None