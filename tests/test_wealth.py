import json
import unittest

from tierguard.agent.dialogue import Dialogue
from tierguard.agent.gateway import Gateway, Session
from tierguard.agent.nlu import plan_turn
from tierguard.bank.mock import Bank, BankError
from tierguard.llm.adapter import ScriptedLLM
from tierguard.policy.pdp import Policy


def J(intent, amount=None, target=None):
    return json.dumps({"intent": intent, "payee_query": None, "amount_text": amount, "period": None, "target_query": target})


def mk(replies):
    bank = Bank(); g = Gateway(bank)
    return bank, g, Dialogue(g, Session("C001"), ScriptedLLM(replies))


def pay_otp(d):
    d.handle("确认")
    return d.handle(d.demo_sms())


class BankRules(unittest.TestCase):
    def test_catalogue_and_suitability(self):
        b = Bank()
        d = b.list_products("C001")
        self.assertEqual(d["profile"], "R2")
        self.assertEqual({p["product_id"]: p["suitable"] for p in d["products"]}, {"F001": True, "F002": True, "F003": False})
        self.assertEqual({p["product_id"]: p["suitable"] for p in b.list_products("C002")["products"]}, {"F001": True, "F002": False, "F003": False})

    def test_buy_rules(self):
        b = Bank()
        for pid, amt, code in (("F003", 10000, "UNSUITABLE"), ("F002", 5000, "BELOW_MINIMUM"), ("F001", 99_999_999, "INSUFFICIENT_FUNDS"),
                               ("F999", 100, "NOT_FOUND")):
            with self.assertRaises(BankError) as cm:
                b.buy_product("C001", pid, amt)
            self.assertEqual(cm.exception.code, code, pid)
        self.assertEqual(b.accounts["A001"]["balance_fen"], 1_200_000)

    def test_buy_redeem_roundtrip_and_idempotency(self):
        b = Bank()
        r = b.buy_product("C001", "F001", 10_000, "k1")
        self.assertEqual((r["holding_after_cny"], r["balance_after_cny"]), ("100.00", "11900.00"))
        self.assertTrue(b.buy_product("C001", "F001", 10_000, "k1")["duplicate"])
        self.assertEqual(b.accounts["A001"]["balance_fen"], 1_190_000)                      # not charged twice
        with self.assertRaises(BankError):
            b.redeem_product("C001", "F001", 20_000)
        b.redeem_product("C001", "F001", 10_000)
        self.assertEqual(b.accounts["A001"]["balance_fen"], 1_200_000)

    def test_holdings_are_per_customer_and_not_counted_as_spending(self):
        b = Bank()
        b.buy_product("C001", "F001", 10_000)
        self.assertEqual(b.get_holdings("C002"), [])
        self.assertEqual(b.spending_summary("C001", "2026-10-01", "2026-10-31")["total_cny"], "462.00")

    def test_fuzzy_product_lookup(self):
        b = Bank()
        self.assertEqual(b.find_products("C001", "货基")[0]["product_id"], "F001")
        self.assertEqual(b.find_products("C001", "money market")[0]["product_id"], "F001")
        self.assertEqual(len({x["score"] for x in b.find_products("C001", "基金")}), 1)     # generic word: a tie, so we ask


class Tiers(unittest.TestCase):
    def test_tiers(self):
        p, s = Policy(), Session("C001")
        for tool, verdict in (("list_products", "ALLOW"), ("resolve_product", "ALLOW"), ("get_holdings", "ALLOW"),
                              ("buy_product", "REQUIRE_STEP_UP"), ("redeem_product", "REQUIRE_STEP_UP")):
            self.assertEqual(p.decide(tool, s, {}).verdict, verdict, tool)

    def test_buy_needs_the_sms_code_even_with_a_valid_token(self):
        bank = Bank(); g = Gateway(bank); s = Session("C001")
        r = g.call(s, "buy_product", {"product_id": "F001", "amount": 100})
        self.assertTrue(r.card["needs_step_up"])
        r2 = g.call(s, "buy_product", {"product_id": "F001", "amount": 100}, token=r.token)
        self.assertEqual(r2.error_code, "OTP_REQUIRED")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)


class Flows(unittest.TestCase):
    def test_recommend_is_informative_and_makes_no_promises(self):
        _, g, s = Bank(), None, None
        bank = Bank(); g = Gateway(bank); s = Session("C001")
        r = plan_turn(g, s, ScriptedLLM([J("wealth_recommend")]), "有什么低风险理财可以买")
        for must in ("稳健货币基金", "1.80", "超出您的风险等级", "不构成收益承诺", "稳健型"):
            self.assertIn(must, r.message)
        self.assertNotIn("保证收益", r.message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        en = plan_turn(g, s, ScriptedLLM([J("wealth_recommend")]), "what investment products can I buy")
        self.assertIn("returns are not promised", en.message)

    def test_buy_end_to_end_with_sms(self):
        bank, g, d = mk([J("wealth_buy", "一百", "货币基金")])
        r = d.handle("买一百块货币基金")
        self.assertEqual((r.kind, r.card["tier"]), ("confirm", "red"))
        self.assertEqual(r.pending["args"], {"product_id": "F001", "amount": "100.00"})
        self.assertIn("投资有风险", r.message)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        out = pay_otp(d)
        self.assertEqual(out.kind, "answer")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_190_000)
        self.assertEqual(bank.get_holdings("C001")[0]["amount_cny"], "100.00")

    def test_model_cannot_change_the_amount(self):
        _, _, d = mk([J("wealth_buy", "五万", "货币基金")])
        self.assertEqual(d.handle("买一百块货币基金").pending["args"]["amount"], "100.00")

    def test_unsuitable_product_is_refused_without_a_card(self):
        bank, g, d = mk([J("wealth_buy", "一万", "混合基金")])
        r = d.handle("买一万块混合基金")
        self.assertEqual(r.kind, "refuse")
        self.assertIn("R3", r.message)
        self.assertIsNone(d.pending)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_below_minimum_then_followup_amount(self):
        _, _, d = mk([J("wealth_buy", "50", "短债")])
        r = d.handle("买50块短债")
        self.assertEqual(r.kind, "clarify")
        self.assertIn("100.00", r.message)
        r2 = d.handle("200")
        self.assertEqual(r2.pending["args"], {"product_id": "F002", "amount": "200.00"})

    def test_missing_amount_and_missing_product_followups(self):
        _, _, d = mk([J("wealth_buy", None, "货币基金")])
        self.assertEqual(d.handle("买货币基金").kind, "clarify")
        self.assertEqual(d.handle("五百").pending["args"], {"product_id": "F001", "amount": "500.00"})
        _, _, d2 = mk([J("wealth_buy", "五百", None)])
        self.assertEqual(d2.handle("买五百").kind, "clarify")
        self.assertEqual(d2.handle("货币基金").pending["args"]["product_id"], "F001")

    def test_generic_name_asks_which_one(self):
        _, _, d = mk([J("wealth_buy", "一百", "基金")])
        r = d.handle("买一百块基金")
        self.assertEqual(r.kind, "clarify")
        for name in ("稳健货币基金", "短债增强基金", "均衡混合基金"):
            self.assertIn(name, r.message)
        self.assertEqual(d.handle("货币").pending["args"]["product_id"], "F001")

    def test_holdings_and_redeem_all(self):
        bank, g, d = mk([J("wealth_holdings"), J("wealth_redeem", "全部", "货币基金")])
        self.assertIn("没有理财持仓", d.handle("我买了哪些理财").message)
        bank.buy_product("C001", "F001", 10_000)
        r = d.handle("赎回全部货币基金")
        self.assertEqual(r.pending["args"], {"product_id": "F001", "amount": "100.00"})
        pay_otp(d)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertEqual(bank.get_holdings("C001"), [])

    def test_redeem_something_not_held_or_too_much(self):
        bank, g, d = mk([J("wealth_redeem", "一百", "货币基金"), J("wealth_redeem", "一千", "货币基金")])
        self.assertEqual(d.handle("赎回一百块货币基金").kind, "refuse")
        bank.buy_product("C001", "F001", 10_000)
        r = d.handle("赎回一千块货币基金")
        self.assertEqual(r.kind, "clarify")
        self.assertIn("100.00", r.message)


if __name__ == "__main__":
    unittest.main()