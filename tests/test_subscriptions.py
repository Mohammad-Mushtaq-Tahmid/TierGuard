import json
import unittest

from helpers import fresh
from tierguard.agent.dialogue import Dialogue
from tierguard.agent.gateway import Gateway, Session
from tierguard.agent.nlu import confirm_pending, parse_model_json, plan_turn
from tierguard.bank.mock import Bank
from tierguard.llm.adapter import ScriptedLLM
from tierguard.policy.pdp import Policy
from tierguard.tools.registry import build_registry


def J(intent, target=None, **kw):
    return json.dumps({"intent": intent, "payee_query": kw.get("payee"), "amount_text": kw.get("amount"),
                       "period": None, "target_query": target})


def dlg(replies):
    bank = Bank(); g = Gateway(bank)
    return bank, g, Dialogue(g, Session("C001"), ScriptedLLM(replies))


class Resolve(unittest.TestCase):
    def test_exact_alias_and_english_alias(self):
        b = Bank()
        self.assertEqual([c["subscription_id"] for c in b.find_subscriptions("C001", "爱奇艺")], ["S001"])
        self.assertEqual([c["subscription_id"] for c in b.find_subscriptions("C001", "iqiyi")], ["S001"])
        self.assertEqual(b.find_subscriptions("C001", "腾讯视频")[0]["subscription_id"], "S002")   # beats the weaker 视频 match

    def test_generic_name_is_a_tie(self):
        c = Bank().find_subscriptions("C001", "视频会员")
        self.assertEqual({x["subscription_id"] for x in c[:2]}, {"S001", "S002"})
        self.assertEqual(c[0]["score"], c[1]["score"])

    def test_other_customers_subscription_is_invisible(self):
        self.assertEqual(Bank().find_subscriptions("C001", "Other Sub"), [])

    def test_status_filters(self):
        b = Bank()
        b.subscription_action("C001", "S001", "cancel")
        self.assertEqual(b.find_subscriptions("C001", "爱奇艺", "cancel"), [])
        b.subscription_action("C001", "S003", "pause")
        self.assertEqual(b.find_subscriptions("C001", "网易云", "pause"), [])
        self.assertEqual(b.find_subscriptions("C001", "网易云", "cancel")[0]["subscription_id"], "S003")

    def test_tool_is_green_and_listing_cancel_are_yellow(self):
        p = Policy()
        self.assertEqual(p.decide("resolve_subscription", Session("C001"), {}).verdict, "ALLOW")
        self.assertEqual(p.decide("list_subscriptions", Session("C001"), {}).verdict, "REQUIRE_CONFIRM")
        self.assertEqual(p.decide("pause_or_cancel_subscription", Session("C001"), {}).verdict, "REQUIRE_CONFIRM")

    def test_every_tool_has_a_tier_entry_and_vice_versa(self):
        self.assertEqual(set(build_registry()), set(Policy().tools))

    def test_cannot_touch_other_customers_subscription(self):
        _, g, s = fresh()
        self.assertEqual(g.call(s, "pause_or_cancel_subscription", {"subscription_id": "S101", "action": "cancel"}).error_code, "NOT_FOUND")


class Flows(unittest.TestCase):
    def test_cancel_iqiyi_end_to_end(self):
        bank, g, s = fresh()
        r = plan_turn(g, s, ScriptedLLM([J("subscription_cancel", "爱奇艺")]), "把爱奇艺会员停了")
        self.assertEqual((r.kind, r.card["tier"]), ("confirm", "yellow"))
        self.assertEqual(r.pending["args"], {"subscription_id": "S001", "action": "cancel"})
        self.assertIn("25.00", r.message)
        self.assertEqual(bank.subs["S001"]["status"], "active")            # nothing happens before confirmation
        done = confirm_pending(g, s, r.pending, "zh")
        self.assertIn("已取消", done.message)
        self.assertEqual(bank.subs["S001"]["status"], "cancelled")

    def test_pause_and_english(self):
        bank, g, s = fresh()
        r = plan_turn(g, s, ScriptedLLM([J("subscription_pause", "netease")]), "pause my netease music subscription")
        self.assertEqual(r.pending["args"]["action"], "pause")
        self.assertIn("Pause subscription", r.message)
        self.assertIn("paused", confirm_pending(g, s, r.pending, "en").message)
        self.assertEqual(bank.subs["S003"]["status"], "paused")

    def test_ambiguous_generic_name_asks(self):
        _, g, s = fresh()
        r = plan_turn(g, s, ScriptedLLM([J("subscription_cancel", "视频会员")]), "把每个月自动扣的视频会员停了")
        self.assertEqual(r.kind, "clarify")
        self.assertIn("爱奇艺视频会员", r.message)
        self.assertIn("腾讯视频VIP", r.message)

    def test_not_found_and_bulk_requests_do_nothing(self):
        bank, g, s = fresh()
        for q in ("B站会员", "所有订阅"):
            r = plan_turn(g, s, ScriptedLLM([J("subscription_cancel", q)]), "取消" + q)
            self.assertEqual(r.kind, "clarify", q)
        self.assertTrue(all(x["status"] == "active" for x in bank.subs.values()))

    def test_already_cancelled_is_not_offered_again(self):
        bank, g, s = fresh()
        bank.subscription_action("C001", "S001", "cancel")
        r = plan_turn(g, s, ScriptedLLM([J("subscription_cancel", "爱奇艺")]), "取消爱奇艺")
        self.assertEqual(r.kind, "clarify")

    def test_list_is_a_yellow_confirmation_then_grounded_summary(self):
        _, g, s = fresh()
        r = plan_turn(g, s, ScriptedLLM([J("subscription_list")]), "我订阅了哪些服务")
        self.assertEqual((r.kind, r.card["tier"], r.pending["tool"]), ("confirm", "yellow", "list_subscriptions"))
        out = confirm_pending(g, s, r.pending, "zh")
        self.assertEqual(out.kind, "answer")
        for must in ("爱奇艺视频会员", "25.00", "72.00", "4 项"):       # 72.00 = 25+20+15+12, computed by code
            self.assertIn(must, out.message)
        self.assertNotIn("无法验证", out.message)
        self.assertNotIn("Other Sub", out.message)

    def test_list_english(self):
        _, g, s = fresh()
        r = plan_turn(g, s, ScriptedLLM([J("subscription_list")]), "what subscriptions do I have")
        out = confirm_pending(g, s, r.pending, "en")
        self.assertIn("4 subscriptions", out.message)
        self.assertIn("72.00 CNY", out.message)

    def test_parse_target_query(self):
        d = parse_model_json(J("subscription_cancel", "爱奇艺"))
        self.assertEqual(d["target_query"], "爱奇艺")
        self.assertIsNone(parse_model_json(J("subscription_cancel", "null"))["target_query"])


class DialogueFollowUps(unittest.TestCase):
    def test_choose_by_ordinal_and_by_name(self):
        _, _, d = dlg([J("subscription_cancel", "视频会员")])
        self.assertEqual(d.handle("停掉视频会员").kind, "clarify")
        r = d.handle("第二个")
        self.assertEqual(r.pending["args"]["subscription_id"], "S002")
        _, _, d2 = dlg([J("subscription_cancel", "视频会员")])
        d2.handle("停掉视频会员")
        self.assertEqual(d2.handle("爱奇艺那个").pending["args"]["subscription_id"], "S001")
        _, _, d3 = dlg([J("subscription_cancel", "视频会员")])
        d3.handle("cancel the video one")
        self.assertEqual(d3.handle("tencent").pending["args"]["subscription_id"], "S002")   # alias re-ranks the open candidates

    def test_vague_reply_asks_again_instead_of_guessing(self):
        _, _, d = dlg([J("subscription_cancel", "视频会员"), json.dumps({"intent": "unknown"})])
        d.handle("停掉视频会员")
        r = d.handle("视频")                 # matches both again: still a tie -> new request path, never a guess
        self.assertNotEqual(r.kind, "confirm")

    def test_missing_target_followup_needs_no_llm(self):
        bank, _, d = dlg([J("subscription_cancel", None)])
        self.assertEqual(d.handle("帮我停掉").kind, "clarify")
        r = d.handle("云盘")
        self.assertEqual(r.pending["args"], {"subscription_id": "S004", "action": "cancel"})
        self.assertEqual(d.handle("确认").kind, "answer")
        self.assertEqual(bank.subs["S004"]["status"], "cancelled")

    def test_missing_target_but_user_changes_topic(self):
        _, _, d = dlg([J("subscription_cancel", None), json.dumps({"intent": "balance"})])
        d.handle("帮我停掉")
        self.assertIn("12000.00", d.handle("我还剩多少钱").message)


if __name__ == "__main__":
    unittest.main()