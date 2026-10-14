import unittest
from tierguard.policy.pdp import Policy, ORDER
from tierguard.agent.gateway import Session

P = Policy()
S = Session("C001")


class TierMatrix(unittest.TestCase):
    def test_official_baselines(self):
        exp = {"get_balance": "ALLOW", "list_transactions": "ALLOW", "summarise_spending": "ALLOW",
               "transfer": "REQUIRE_CONFIRM", "pause_or_cancel_subscription": "REQUIRE_CONFIRM",
               "apply_virtual_card": "REQUIRE_CONFIRM", "report_card_lost": "REQUIRE_STEP_UP",
               "unfreeze_card": "REQUIRE_STEP_UP", "export_customer_data": "DENY"}
        for tool, verdict in exp.items():
            self.assertEqual(P.decide(tool, S, {}).verdict, verdict, tool)

    def test_unknown_tool_default_deny(self):
        self.assertEqual(P.decide("rm_rf", S, {}).verdict, "DENY")

    def test_daily_limit_boundary(self):
        self.assertEqual(P.decide("transfer", S, {"projected_over_limit": False}).tier, "yellow")  # exactly 1000
        d = P.decide("transfer", S, {"projected_over_limit": True})
        self.assertEqual((d.tier, d.verdict), ("red", "REQUIRE_STEP_UP"))

    def test_taint_escalates_to_red(self):
        d = P.decide("transfer", S, {}, taint={"payee_id"})
        self.assertEqual(d.tier, "red")
        self.assertIn("TAINTED_ARGUMENT", d.reasons)

    def test_taint_on_non_sensitive_arg_is_ignored(self):
        self.assertEqual(P.decide("transfer", S, {}, taint={"memo"}).tier, "yellow")

    def test_role_from_session_only(self):
        admin = Session("C001", role="admin")
        self.assertEqual(P.decide("get_balance", admin, {}).verdict, "DENY")

    def test_failures_escalate_never_relax(self):
        s = Session("C001"); s.failure_score = 2
        self.assertEqual(P.decide("transfer", s, {}).tier, "red")
        self.assertEqual(P.decide("get_balance", s, {}).tier, "green")  # reads are unaffected

    def test_card_limit_raise_vs_lower(self):
        self.assertEqual(P.decide("set_card_limit", S, {"limit_is_raise": False}).tier, "yellow")
        self.assertEqual(P.decide("set_card_limit", S, {"limit_is_raise": True}).tier, "red")

    def test_dynamic_rules_never_relax_baseline(self):
        for tool, spec in P.tools.items():
            for taint in (set(), set(spec.get("sensitive_args", []))):
                for ctx in ({}, {"projected_over_limit": True, "limit_is_raise": True, "new_payee": True}):
                    d = P.decide(tool, S, ctx, taint)
                    self.assertGreaterEqual(ORDER[d.tier], ORDER[spec["tier"]], (tool, ctx, taint))


if __name__ == "__main__":
    unittest.main()


class SubscriptionTierAssumption(unittest.TestCase):
    def test_subscription_query_follows_official_wording(self):
        # Official yellow examples list "subscription query and cancellation". Literal reading = both yellow.
        self.assertEqual(P.decide("list_subscriptions", S, {}).verdict, "REQUIRE_CONFIRM")
