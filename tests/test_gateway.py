import unittest
from helpers import fresh, confirm


class GatewayFlows(unittest.TestCase):
    def test_green_is_automatic(self):
        _, g, s = fresh()
        r = g.call(s, "get_balance", {})
        self.assertEqual((r.status, r.data["balance_cny"]), ("executed", "12000.00"))

    def test_yellow_transfer_needs_confirmation_then_executes(self):
        bank, g, s = fresh()
        r1, r2 = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 500})
        self.assertEqual(r1.card["tier"], "yellow")
        self.assertEqual(r2.status, "executed")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_150_000)

    def test_nothing_moves_before_confirmation(self):
        bank, g, s = fresh()
        g.call(s, "transfer", {"payee_id": "P001", "amount": 500})
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_amount_changed_after_confirmation_fails(self):
        bank, g, s = fresh()
        r1 = g.call(s, "transfer", {"payee_id": "P001", "amount": 500})
        r2 = g.call(s, "transfer", {"payee_id": "P001", "amount": 900}, token=r1.token)
        self.assertEqual((r2.status, r2.error_code), ("denied", "HASH_MISMATCH"))
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_payee_changed_after_confirmation_fails(self):
        bank, g, s = fresh()
        r1 = g.call(s, "transfer", {"payee_id": "P001", "amount": 500})
        r2 = g.call(s, "transfer", {"payee_id": "P004", "amount": 500}, token=r1.token)
        self.assertEqual(r2.error_code, "HASH_MISMATCH")

    def test_replay_does_not_double_spend(self):
        bank, g, s = fresh()
        r1, r2 = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 100})
        r3 = g.call(s, "transfer", {"payee_id": "P001", "amount": 100}, token=r1.token)
        self.assertEqual(r3.error_code, "REPLAYED_TOKEN")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_190_000)

    def test_amount_formats_hash_identically(self):
        _, g, s = fresh()
        r1 = g.call(s, "transfer", {"payee_id": "P001", "amount": 500})
        r2 = g.call(s, "transfer", {"payee_id": "P001", "amount": "500.00"}, token=r1.token)
        self.assertEqual(r2.status, "executed")

    def test_over_1000_requires_step_up(self):
        _, g, s = fresh()
        r1, r2 = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 1500})
        self.assertEqual((r1.card["tier"], r1.card["needs_step_up"]), ("red", True))
        self.assertEqual(r2.status, "executed")

    def test_step_up_without_otp_rejected(self):
        bank, g, s = fresh()
        r1 = g.call(s, "transfer", {"payee_id": "P001", "amount": 1500})
        r2 = g.call(s, "transfer", {"payee_id": "P001", "amount": 1500}, token=r1.token)
        self.assertEqual((r2.status, r2.error_code), ("denied", "OTP_REQUIRED"))
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_exactly_1000_is_yellow(self):
        _, g, s = fresh()
        self.assertEqual(g.call(s, "transfer", {"payee_id": "P001", "amount": 1000}).card["tier"], "yellow")

    def test_1000_01_is_red(self):
        _, g, s = fresh()
        self.assertEqual(g.call(s, "transfer", {"payee_id": "P001", "amount": "1000.01"}).card["tier"], "red")

    def test_cumulative_split_transfers_escalate(self):
        _, g, s = fresh()
        _, r = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 600})
        self.assertEqual(r.status, "executed")
        r2 = g.call(s, "transfer", {"payee_id": "P001", "amount": 600})
        self.assertEqual(r2.card["tier"], "red")
        self.assertIn("DAILY_CUMULATIVE_OVER_1000", r2.decision["reasons"])

    def test_tainted_payee_goes_red_and_card_flags_it(self):
        _, g, s = fresh()
        r = g.call(s, "transfer", {"payee_id": "P004", "amount": 50}, taint={"payee_id"})
        self.assertEqual(r.card["tier"], "red")
        self.assertEqual(r.card["untrusted_values"], ["payee_id"])

    def test_new_payee_gets_warning(self):
        _, g, s = fresh()
        r = g.call(s, "transfer", {"payee_id": "P004", "amount": 50})
        self.assertIn("FIRST_TIME_PAYEE", r.card["warnings"])

    def test_insufficient_funds(self):
        bank, g, s = fresh()
        _, r = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 20000})
        self.assertEqual((r.status, r.error_code), ("error", "INSUFFICIENT_FUNDS"))

    def test_smuggled_customer_id_rejected(self):
        _, g, s = fresh()
        r = g.call(s, "get_balance", {"customer_id": "C002"})
        self.assertEqual(r.error_code, "SMUGGLED_IDENTITY_ARG")

    def test_other_customers_objects_not_found(self):
        _, g, s = fresh()
        for tool, args in [("get_balance", {"account_id": "A002"}),
                           ("transfer", {"payee_id": "P101", "amount": 10}),
                           ("freeze_card", {"card_id": "K101"})]:
            self.assertEqual(g.call(s, tool, args).error_code, "NOT_FOUND", tool)

    def test_unknown_tool_denied(self):
        _, g, s = fresh()
        self.assertEqual(g.call(s, "wire_all_funds", {}).error_code, "UNKNOWN_TOOL")

    def test_black_tier_denied_even_with_token(self):
        _, g, s = fresh()
        r = g.call(s, "export_customer_data", {}, token="whatever")
        self.assertEqual((r.status, r.error_code), ("denied", "BLACK_TIER"))

    def test_circuit_breaker_locks_session(self):
        _, g, s = fresh()
        g.call(s, "get_balance", {"customer_id": "C002"})
        g.call(s, "get_balance", {"role": "admin"})
        self.assertTrue(s.locked)
        r = g.call(s, "get_balance", {})
        self.assertEqual(r.error_code, "SESSION_LOCKED")
        self.assertIn("security_lock", [e["event"] for e in g.audit.entries])

    def test_success_resets_failure_score(self):
        _, g, s = fresh()
        g.call(s, "get_balance", {"bogus": 1})
        g.call(s, "get_balance", {})
        self.assertEqual(s.failure_score, 0)

    def test_zero_width_and_control_chars_rejected(self):
        _, g, s = fresh()
        r = g.call(s, "resolve_payee", {"query": "妈\u200b妈"})
        self.assertEqual(r.error_code, "ILLEGAL_CHARACTERS")

    def test_bad_amounts(self):
        _, g, s = fresh()
        for amt in (-5, 0, "abc", 1.005, "NaN", float("inf"), True):
            self.assertEqual(g.call(s, "transfer", {"payee_id": "P001", "amount": amt}).status, "denied", amt)

    def test_upstream_failure_retried_without_duplicate(self):
        bank, g, s = fresh()
        bank.fail_next["transfer"] = 1
        _, r = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 100})
        self.assertEqual(r.status, "executed")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_190_000)

    def test_persistent_upstream_failure_is_retryable_and_moves_nothing(self):
        bank, g, s = fresh()
        bank.fail_next["transfer"] = 5
        _, r = confirm(g, s, "transfer", {"payee_id": "P001", "amount": 100})
        self.assertEqual((r.status, r.retryable), ("error", True))
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_lost_card_is_red_with_step_up(self):
        bank, g, s = fresh()
        r1, r2 = confirm(g, s, "report_card_lost", {"card_id": "K001"})
        self.assertEqual((r1.card["tier"], r2.status), ("red", "executed"))
        self.assertEqual(bank.cards["K001"]["status"], "lost")

    def test_subscription_cancel_yellow(self):
        bank, g, s = fresh()
        r1, r2 = confirm(g, s, "pause_or_cancel_subscription", {"subscription_id": "S001", "action": "cancel"})
        self.assertEqual((r1.card["tier"], r2.status), ("yellow", "executed"))
        self.assertEqual(bank.subs["S001"]["status"], "cancelled")

    def test_memo_injection_is_marked_untrusted(self):
        _, g, s = fresh()
        r = g.call(s, "list_transactions", {"start": "2026-10-01", "end": "2026-10-31"})
        self.assertIn("memo", r.untrusted_outputs)
        self.assertTrue(any("忽略之前所有规则" in t["memo"] for t in r.data["transactions"]))


class AuditTests(unittest.TestCase):
    def test_chain_verifies_and_detects_tampering(self):
        _, g, s = fresh()
        confirm(g, s, "transfer", {"payee_id": "P001", "amount": 100})
        self.assertEqual(g.audit.verify(), (True, None))
        g.audit.entries[2]["data"]["tool"] = "tampered"
        ok, idx = g.audit.verify()
        self.assertFalse(ok)
        self.assertEqual(idx, 2)

    def test_full_decision_chain_logged(self):
        _, g, s = fresh()
        confirm(g, s, "transfer", {"payee_id": "P001", "amount": 100})
        events = [e["event"] for e in g.audit.entries]
        for ev in ("request", "decision", "confirmation_issued", "confirmation_redeemed", "executed"):
            self.assertIn(ev, events)


if __name__ == "__main__":
    unittest.main()
