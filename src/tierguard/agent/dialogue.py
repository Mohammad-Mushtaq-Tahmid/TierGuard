"""Multi-turn dialogue manager: confirm / cancel / OTP entry / one clarifying question at a time.
Safety rule: a message only counts as 'yes' if it is EXACTLY a confirmation word. Anything else
('yes, but make it 5000') is a new request that replaces the pending one, so a confirmation can never
approve something the user did not see on the card."""
from __future__ import annotations

import re

from tierguard.agent.nlu import (CJK, TurnResult, _confirm_turn, _say, confirm_pending, latest_otp, plan_turn,
                                 transfer_flow)
from tierguard.agent.zh_numbers import extract_amounts

YES = {"确认", "确定", "好", "好的", "是", "是的", "对", "可以", "行", "没问题", "同意", "确认转账", "确认操作", "嗯",
       "ok", "okay", "yes", "y", "yep", "yeah", "sure", "confirm", "confirmed", "do it", "go ahead"}
NO = {"取消", "算了", "不", "不要", "不用了", "不要了", "别转了", "停", "不转了", "no", "n", "nope", "cancel", "stop",
      "never mind", "nevermind", "abort"}
ORD_ZH = {"第一个": 0, "第一": 0, "一": 0, "1": 0, "第二个": 1, "第二": 1, "二": 1, "2": 1, "第三个": 2, "第三": 2,
          "三": 2, "3": 2}
ORD_EN = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
MAX_OTP_TRIES = 3


def norm(t: str) -> str:
    return re.sub(r"[\s，。！!,.?？~～、]+", " ", t.strip().lower()).strip()


def pick_candidate(text: str, cands: list[dict]):
    t = norm(text)
    m = re.search(r"\d{4}", t)
    if m:
        hits = [c for c in cands if c["account_masked"].endswith(m.group(0))]
        if len(hits) == 1:
            return hits[0]
    idx = ORD_ZH.get(t.replace(" ", ""))
    if idx is None:
        idx = next((ORD_EN[w] for w in re.findall(r"[a-z0-9]+", t) if w in ORD_EN), None)
    if idx is not None and idx < len(cands):
        return cands[idx]
    return None


class Dialogue:
    def __init__(self, gateway, session, llm):
        self.g, self.s, self.llm = gateway, session, llm
        self.pending = None          # confirmation card awaiting yes / no
        self.awaiting_otp = False
        self.otp_tries = 0
        self.clarify = None          # state of the one open clarifying question
        self.lang = "en"

    # ---- demo helper: the simulated SMS inbox (never returned by the gateway itself) ----
    def demo_sms(self):
        if self.pending and self.pending.get("needs_step_up"):
            return latest_otp(self.g, self.pending["token"])
        return None

    def _reset(self):
        self.pending, self.awaiting_otp, self.otp_tries, self.clarify = None, False, 0, None

    def handle(self, text: str) -> TurnResult:
        if CJK.search(text):
            self.lang = "zh"
        elif re.search(r"[A-Za-z]", text):
            self.lang = "en"
        lang = self.lang
        if self.s.locked:
            return TurnResult("error", _say(lang, "为了您的资金安全，本次会话已被锁定，请联系人工客服。",
                                           "For your security this session is locked. Please contact support."))
        t = norm(text)
        prefix = ""

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
            elif t in NO:
                return self._cancel()
            # anything else is a NEW request: the pending one is dropped (its token simply expires unused)
            prefix = _say(lang, "（上一个待确认的操作已取消。）", "(The previous pending action was cancelled.) ")
            self._reset()

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
        if r.kind == "confirm" and r.pending:
            self.pending, self.awaiting_otp, self.otp_tries = r.pending, False, 0
        return r

    def _cancel(self):
        self._reset()
        return TurnResult("answer", _say(self.lang, "已取消，未执行任何操作。", "Cancelled. Nothing was done."))

    def _redeem(self, otp):
        r = confirm_pending(self.g, self.s, self.pending, self.lang, otp=otp)
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
        return r

    def _follow_up(self, text: str):
        st, lang = self.clarify, self.lang
        if st["type"] == "payee_choice":
            c = pick_candidate(text, st["candidates"])
            if c is None:
                return None
            args = {"payee_id": c["payee_id"], "amount": f"{float(st['amount']):.2f}"}
            return _confirm_turn(self.g, self.s, "transfer", args, lang, [])
        if st["type"] == "need_amount":
            amts = extract_amounts(text)
            if len(amts) != 1:
                return None
            return transfer_flow(self.g, self.s, lang, st["payee_query"], amts[0], [])
        if st["type"] == "need_payee":
            q = re.sub(r"^(转给|转账给|给|to|send to)\s*", "", text.strip(), flags=re.I)[:60]
            if not q or extract_amounts(text) or len(q) > 20:
                return None
            probe = self.g.call(self.s, "resolve_payee", {"query": q})
            if probe.status != "executed" or not probe.data["candidates"]:
                return None     # not a payee name: treat it as a brand-new request
            from decimal import Decimal
            return transfer_flow(self.g, self.s, lang, q, Decimal(st["amount"]), [])
        return None