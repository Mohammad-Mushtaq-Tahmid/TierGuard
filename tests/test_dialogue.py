import json
import unittest

from tierguard.agent.dialogue import Dialogue
from tierguard.agent.gateway import Gateway, Session
from tierguard.bank.mock import Bank
from tierguard.llm.adapter import ScriptedLLM
from tierguard.policy.confirm import ConfirmationService


def J(intent, payee=None, amount=None, period=None):
    return json.dumps({"intent": intent, "payee_query": payee, "amount_text": amount, "period": period})


def make(replies, clock=None):
    bank = Bank()
    conf = ConfirmationService(ttl=120, clock=clock) if clock else None
    g = Gateway(bank, confirmations=conf)
    llm = ScriptedLLM(replies)
    return bank, g, Dialogue(g, Session("C001"), llm), llm


class Clock:
    t = 1000.0
    def __call__(self): return self.t


class Flow(unittest.TestCase):
    def test_yellow_confirm_executes_once(self):
        bank, g, d, _ = make([J("transfer", "妈", "五百"), J("unknown")])
        r = d.handle("给我妈转五百")
        self.assertEqual(r.kind, "confirm")
        r = d.handle("确认")
        self.assertEqual(r.kind, "answer")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_150_000)
        d.handle("确认")                                           # second "yes" must not repeat the transfer
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_150_000)

    def test_cancel_moves_nothing(self):
        bank, _, d, _ = make([J("transfer", "妈", "五百")])
        d.handle("给我妈转五百")
        r = d.handle("取消")
        self.assertIn("已取消", r.message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertIsNone(d.pending)

    def test_english_yes_and_no(self):
        bank, _, d, _ = make([J("transfer", "mom", "300"), J("transfer", "mom", "300")])
        d.handle("send 300 to mom")
        self.assertEqual(d.handle("yes").kind, "answer")
        d.handle("send 300 to mom")
        self.assertIn("Cancelled", d.handle("no").message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_170_000)

    def test_red_needs_otp_and_wrong_codes_lock_out(self):
        bank, g, d, _ = make([J("transfer", "妈", "两千")])
        d.handle("转两千给我妈")
        r = d.handle("确认")
        self.assertIn("验证码", r.message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        real = d.demo_sms()
        wrong = "000000" if real != "000000" else "111111"
        self.assertIn("2", d.handle(wrong).message)                # 2 attempts left
        d.handle(wrong)
        r = d.handle(wrong)
        self.assertEqual(r.code, "OTP_LOCKOUT")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertIsNone(d.pending)

    def test_red_correct_otp_executes(self):
        bank, _, d, _ = make([J("transfer", "妈", "两千")])
        d.handle("转两千给我妈")
        d.handle("确认")
        r = d.handle(d.demo_sms())
        self.assertEqual(r.kind, "answer")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_000_000)

    def test_otp_prompt_when_user_types_junk(self):
        _, _, d, _ = make([J("transfer", "妈", "两千")])
        d.handle("转两千给我妈"); d.handle("确认")
        self.assertIn("6-digit", d.handle("abc").message)          # English junk -> English prompt
        self.assertIn("6位", d.handle("啊啊").message)              # Chinese junk -> Chinese prompt
        self.assertIsNotNone(d.pending)

    def test_yes_but_change_is_a_new_request_not_a_confirmation(self):
        bank, _, d, _ = make([J("transfer", "妈", "五百"), J("transfer", "妈", "5000")])
        d.handle("给我妈转五百")
        r = d.handle("确认，改成5000")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)   # nothing executed
        self.assertEqual(r.card["summary"]["amount_cny"], "5000.00")
        self.assertEqual(r.card["tier"], "red")
        self.assertIn("已取消", r.message)

    def test_new_request_replaces_pending(self):
        bank, _, d, _ = make([J("transfer", "妈", "五百"), J("spending", period="last_month")])
        d.handle("给我妈转五百")
        r = d.handle("上个月花了多少")
        self.assertEqual(r.kind, "answer")
        self.assertIsNone(d.pending)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_confirmation_expires(self):
        clk = Clock()
        bank, _, d, _ = make([J("transfer", "妈", "五百")], clock=clk)
        d.handle("给我妈转五百")
        clk.t += 121
        r = d.handle("确认")
        self.assertIn("超时", r.message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertIsNone(d.pending)

    def test_locked_session_refuses_everything(self):
        _, _, d, _ = make([])
        d.s.locked = True
        self.assertEqual(d.handle("查余额").kind, "error")


class Clarify(unittest.TestCase):
    def _ambiguous(self):
        bank, g, d, llm = make([J("transfer", "王小明", "200")])
        r = d.handle("给王小明转200")
        self.assertEqual(r.kind, "clarify")
        return bank, d, llm

    def test_choose_by_ordinal_zh(self):
        _, d, llm = self._ambiguous()
        r = d.handle("第二个")
        self.assertEqual(r.pending["args"]["payee_id"], "P003")
        self.assertEqual(len(llm.replies), 0)

    def test_choose_by_tail_digits(self):
        _, d, _ = self._ambiguous()
        self.assertEqual(d.handle("尾号1234").pending["args"]["payee_id"], "P002")

    def test_choose_english_ordinal_and_bare_digits(self):
        _, d, _ = self._ambiguous()
        self.assertEqual(d.handle("the second one").pending["args"]["payee_id"], "P003")
        _, d2, _ = self._ambiguous()
        self.assertEqual(d2.handle("5678").pending["args"]["payee_id"], "P003")

    def test_unrelated_reply_becomes_new_request(self):
        bank, g, d, llm = make([J("transfer", "王小明", "200"), J("balance")])
        d.handle("给王小明转200")
        r = d.handle("查余额")
        self.assertEqual(r.kind, "answer")
        self.assertIn("12000.00", r.message)

    def test_missing_amount_followup_needs_no_llm(self):
        _, g, d, llm = make([J("transfer", "妈", None)])
        r = d.handle("给我妈转钱")
        self.assertEqual(r.kind, "clarify")
        r2 = d.handle("五百")
        self.assertEqual(r2.card["summary"]["amount_cny"], "500.00")
        self.assertEqual(len(llm.replies), 0)

    def test_missing_payee_followup(self):
        _, g, d, llm = make([J("transfer", None, "五百")])
        self.assertEqual(d.handle("转五百").kind, "clarify")
        r = d.handle("妈妈")
        self.assertEqual(r.pending["args"], {"payee_id": "P001", "amount": "500.00"})

    def test_missing_payee_but_user_changes_topic(self):
        _, g, d, llm = make([J("transfer", None, "五百"), J("balance")])
        d.handle("转五百")
        r = d.handle("查余额")
        self.assertEqual(r.kind, "answer")


if __name__ == "__main__":
    unittest.main()