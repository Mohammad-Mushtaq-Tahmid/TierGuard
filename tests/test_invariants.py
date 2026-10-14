"""Randomised invariant tests (stdlib stand-in for Hypothesis; swap in later if desired)."""
import random
import unittest

from tierguard.agent.gateway import Gateway, Session
from tierguard.bank.mock import Bank

TOOLS = ["get_balance", "transfer", "freeze_card", "unfreeze_card", "report_card_lost", "set_card_limit",
         "pause_or_cancel_subscription", "apply_virtual_card", "export_customer_data", "bogus"]
ARGS = [{}, {"payee_id": "P001", "amount": 100}, {"payee_id": "P004", "amount": "2500.50"},
        {"payee_id": "P101", "amount": 1}, {"card_id": "K001"}, {"card_id": "K101"},
        {"card_id": "K001", "daily_limit": 9999}, {"subscription_id": "S001", "action": "cancel"},
        {"customer_id": "C002"}, {"payee_id": "P002", "amount": -1}]


class Invariants(unittest.TestCase):
    def test_no_state_change_without_matching_redeemed_confirmation(self):
        for seed in range(40):
            rnd = random.Random(seed)
            bank = Bank(); g = Gateway(bank); s = Session("C001")
            other_before = (bank.accounts["A002"]["balance_fen"], dict(bank.cards["K101"]), dict(bank.subs["S101"]))
            tokens = []
            for _ in range(60):
                tool, args = rnd.choice(TOOLS), dict(rnd.choice(ARGS))
                if rnd.random() < 0.3 and tokens:
                    tok = rnd.choice(tokens)           # random (possibly stale or mismatched) token
                else:
                    tok = None
                taint = {"payee_id"} if rnd.random() < 0.2 else set()
                otp = rnd.choice([None, "000000"])
                r = g.call(s, tool, args, token=tok, otp=otp, taint=taint)
                if r.token:
                    tokens.append(r.token)
                if s.locked and rnd.random() < 0.5:
                    s.locked, s.failure_score = False, 0   # human unlocks, keep fuzzing
            # invariant 1: every executed state-changing call has an earlier redeemed confirmation, same hash
            redeemed = set()
            for e in g.audit.entries:
                if e["event"] == "confirmation_redeemed":
                    redeemed.add(e["data"]["action_hash"])
            self.assertTrue(g.audit.verify()[0])
            for e in g.audit.entries:
                if e["event"] == "executed" and g.tools[e["data"]["tool"]].state_changing:
                    self.assertTrue(redeemed, f"seed {seed}: state change with no confirmation")
            n_exec = sum(1 for e in g.audit.entries if e["event"] == "executed" and g.tools[e["data"]["tool"]].state_changing)
            n_red = sum(1 for e in g.audit.entries if e["event"] == "confirmation_redeemed")
            self.assertEqual(n_exec, n_red, f"seed {seed}: executions must equal redemptions")
            # invariant 2: the other customer is never touched
            self.assertEqual(other_before, (bank.accounts["A002"]["balance_fen"], bank.cards["K101"], bank.subs["S101"]))
            # invariant 3: money moved == sum of executed transfers; balance never negative
            moved = sum(-t["amount_fen"] for t in bank.txns if t["category"] == "transfer")
            self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000 - moved)
            self.assertGreaterEqual(bank.accounts["A001"]["balance_fen"], 0)

    def test_black_tier_never_executes(self):
        bank = Bank(); g = Gateway(bank); s = Session("C001")
        for _ in range(10):
            s.locked, s.failure_score = False, 0
            for t in ("export_customer_data", "change_security_settings"):
                self.assertNotEqual(g.call(s, t, {}).status, "executed")


if __name__ == "__main__":
    unittest.main()
