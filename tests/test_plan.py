import json
import unittest

from tierguard.agent.dialogue import Dialogue
from tierguard.agent.gateway import Gateway, Session
from tierguard.bank.mock import Bank
from tierguard.llm.adapter import ScriptedLLM

SAVE = "帮我省点钱，停掉用得少的订阅"


def J(intent, amount=None, target=None):
    return json.dumps({"intent": intent, "payee_query": None, "amount_text": amount, "period": None, "target_query": target})


def mk(replies=None):
    bank = Bank(); g = Gateway(bank)
    llm = ScriptedLLM(replies if replies is not None else [J("savings_review")])
    return bank, g, Dialogue(g, Session("C001"), llm), llm


def states(d):
    return {x["id"]: x["state"] for x in d.plan_view["steps"]}


def to_first_card(d):
    d.handle(SAVE)
    return d.handle("确认")


def buy_with_sms(d):
    d.handle("确认")                       # accept the offer -> red card
    d.handle("确认")                       # confirm the card -> asks for the code
    return d.handle(d.demo_sms())


class Preview(unittest.TestCase):
    def test_preview_shows_the_whole_plan_and_touches_nothing(self):
        bank, g, d, _ = mk()
        r = d.handle(SAVE)
        self.assertEqual(r.kind, "plan")
        self.assertEqual(d.plan_view["stage"], "preview")
        self.assertEqual([x["id"] for x in d.plan_view["steps"]], ["S1", "S2", "S3", "S4", "S5"])
        tiers = {x["id"]: x["tier"] for x in d.plan_view["steps"]}
        self.assertEqual((tiers["S1"], tiers["S2"], tiers["S4"], tiers["S5"]), ("green", "yellow", "yellow", "red"))
        self.assertEqual([e["event"] for e in g.audit.entries if e["event"] == "request"], [])    # no tool was called yet
        self.assertTrue(all(x["status"] == "active" for x in bank.subs.values() if True and x.get("customer_id") == "C001"))

    def test_cancel_at_preview(self):
        _, g, d, _ = mk()
        d.handle(SAVE)
        r = d.handle("取消")
        self.assertIn("没有执行任何操作", r.message)
        self.assertIsNone(d.plan)
        self.assertEqual(d.plan_view["stage"], "cancelled")

    def test_unrelated_message_drops_the_plan(self):
        _, _, d, _ = mk([J("savings_review"), json.dumps({"intent": "balance"})])
        d.handle(SAVE)
        r = d.handle("我还剩多少钱")
        self.assertIn("计划已取消", r.message)
        self.assertIn("12000.00", r.message)
        self.assertIsNone(d.plan)


class HappyPath(unittest.TestCase):
    def test_start_reads_then_asks_to_pause_the_cheapest_first(self):
        bank, g, d, _ = mk()
        r = to_first_card(d)
        self.assertEqual(r.kind, "confirm")
        for must in ("5896.00", "云盘扩容", "爱奇艺视频会员", "37.00"):
            self.assertIn(must, r.message)
        self.assertEqual(r.pending["args"], {"subscription_id": "S004", "action": "pause"})      # 12.00 is the cheapest
        self.assertEqual(r.card["tier"], "yellow")
        st = states(d)
        self.assertEqual((st["S1"], st["S2"], st["S3"], st["S4.1"], st["S4.2"]),
                         ("succeeded", "succeeded", "succeeded", "awaiting_confirmation", "pending"))
        self.assertTrue(all(s["status"] == "active" for k, s in bank.subs.items() if s["customer_id"] == "C001"))   # nothing paused yet
        events = [e["event"] for e in g.audit.entries]
        self.assertLess(events.index("plan_approved"), events.index("plan_read_approved"))

    def test_full_run_pauses_two_then_buys_a_fund_with_sms(self):
        bank, g, d, _ = mk()
        to_first_card(d)
        r = d.handle("确认")                                   # pause 云盘扩容, next card
        self.assertEqual((r.kind, r.pending["args"]["subscription_id"]), ("confirm", "S001"))
        offer = d.handle("确认")                               # pause 爱奇艺, then the fund offer
        self.assertEqual(offer.kind, "answer")
        for must in ("37.00", "稳健货币基金", "1.80", "不代表未来收益"):
            self.assertIn(must, offer.message)
        self.assertEqual(d.plan.stage, "offer")
        self.assertEqual((bank.subs["S004"]["status"], bank.subs["S001"]["status"]), ("paused", "paused"))
        self.assertEqual(bank.get_holdings("C001"), [])
        done = buy_with_sms(d)
        self.assertEqual(done.kind, "answer")
        self.assertEqual(bank.get_holdings("C001")[0]["amount_cny"], "37.00")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000 - 3700)
        self.assertEqual(set(states(d).values()), {"succeeded"})
        self.assertIsNone(d.plan)
        self.assertEqual(d.plan_view["stage"], "done")

    def test_every_state_change_had_its_own_confirmation_first(self):
        bank, g, d, _ = mk()
        to_first_card(d); d.handle("确认"); d.handle("确认"); buy_with_sms(d)
        ents = g.audit.entries
        redeemed = [(e["seq"], e["data"]["tool"]) for e in ents
                    if e["event"] == "confirmation_redeemed" and g.tools[e["data"]["tool"]].state_changing]
        writes = [(e["seq"], e["data"]["tool"]) for e in ents
                  if e["event"] == "executed" and g.tools[e["data"]["tool"]].state_changing]
        self.assertEqual([t for _, t in writes], ["pause_or_cancel_subscription", "pause_or_cancel_subscription", "buy_product"])
        self.assertEqual([t for _, t in redeemed], [t for _, t in writes])           # one confirmation per write, same order
        for (rs, _), (ws, _) in zip(redeemed, writes):
            self.assertLess(rs, ws)                                                     # and always before it ran
        self.assertTrue(g.audit.verify()[0])

    def test_only_a_count_in_the_request_changes_how_many_are_paused(self):
        bank, _, d, _ = mk([J("savings_review")])
        d.handle("帮我省点钱，停掉最没用的一个订阅")
        self.assertEqual([x["id"] for x in d.plan_view["steps"] if x["id"] != "S5"][:3], ["S1", "S2", "S3"])
        r = d.handle("确认")
        self.assertEqual(r.pending["args"]["subscription_id"], "S004")
        self.assertEqual(len([x for x in d.plan_view["steps"] if x["id"].startswith("S4.")]), 1)
        offer = d.handle("确认")
        self.assertIn("12.00", offer.message)


class SkipCancelDecline(unittest.TestCase):
    def test_skip_one_then_offer_uses_only_what_was_paused(self):
        bank, _, d, _ = mk()
        to_first_card(d)
        r = d.handle("跳过")
        self.assertEqual(r.pending["args"]["subscription_id"], "S001")
        self.assertEqual(states(d)["S4.1"], "skipped")
        offer = d.handle("确认")
        self.assertIn("25.00", offer.message)
        self.assertEqual(bank.subs["S004"]["status"], "active")

    def test_skip_everything_ends_without_an_offer(self):
        bank, _, d, _ = mk()
        to_first_card(d); d.handle("跳过")
        r = d.handle("跳过")
        self.assertEqual(d.plan_view["stage"], "done")
        self.assertIn("已跳过", r.message)
        self.assertEqual(bank.get_holdings("C001"), [])

    def test_cancel_midway_keeps_what_was_done_and_stops_the_rest(self):
        bank, _, d, _ = mk()
        to_first_card(d); d.handle("确认")
        r = d.handle("取消")
        self.assertEqual(d.plan_view["stage"], "cancelled")
        self.assertEqual((bank.subs["S004"]["status"], bank.subs["S001"]["status"]), ("paused", "active"))
        self.assertIn("不会执行", r.message)
        self.assertEqual(states(d)["S5"], "cancelled")

    def test_declining_the_offer_buys_nothing(self):
        bank, _, d, _ = mk()
        to_first_card(d); d.handle("确认"); d.handle("确认")
        r = d.handle("取消")
        self.assertIn("不做投资", r.message)
        self.assertEqual(bank.get_holdings("C001"), [])
        self.assertEqual(bank.subs["S004"]["status"], "paused")

    def test_wrong_sms_codes_cancel_the_purchase_not_the_pauses(self):
        bank, _, d, _ = mk()
        to_first_card(d); d.handle("确认"); d.handle("确认"); d.handle("确认"); d.handle("确认")
        wrong = "000000" if d.demo_sms() != "000000" else "111111"
        for _ in range(3):
            d.handle(wrong)
        self.assertEqual(bank.get_holdings("C001"), [])
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertEqual(bank.subs["S004"]["status"], "paused")


class Failure(unittest.TestCase):
    def test_partial_failure_offers_undo_and_undo_works(self):
        bank, _, d, _ = mk()
        to_first_card(d)
        d.handle("确认")                                           # S004 paused, card for S001
        bank.fail_next["subscription_action"] = 10                 # the bank breaks before the second pause
        r = d.handle("确认")
        self.assertEqual(r.kind, "answer")
        for must in ("暂时不可用", "云盘扩容", "撤销", "转人工"):
            self.assertIn(must, r.message)
        self.assertEqual(d.plan.stage, "undo_offer")
        self.assertEqual(states(d)["S4.2"], "failed")
        self.assertEqual(bank.subs["S001"]["status"], "active")    # the failed step changed nothing
        bank.fail_next["subscription_action"] = 0                  # the bank recovers
        card = d.handle("确认")
        self.assertEqual(card.pending["args"], {"subscription_id": "S004", "action": "resume"})
        done = d.handle("确认")
        self.assertEqual(bank.subs["S004"]["status"], "active")
        self.assertEqual(d.plan_view["stage"], "done")
        self.assertIn("恢复", done.message)

    def test_failure_before_any_success_has_no_undo_but_offers_a_human(self):
        bank, _, d, _ = mk()
        to_first_card(d)
        bank.fail_next["subscription_action"] = 10
        r = d.handle("确认")
        self.assertIn("转人工", r.message)
        self.assertNotIn("撤销", r.message)
        self.assertEqual(d.plan_view["stage"], "done")
        self.assertEqual(bank.subs["S004"]["status"], "active")

    def test_declining_the_undo_leaves_things_as_they_are(self):
        bank, _, d, _ = mk()
        to_first_card(d); d.handle("确认")
        bank.fail_next["subscription_action"] = 10
        d.handle("确认")
        r = d.handle("取消")
        self.assertIn("保持现状", r.message)
        self.assertEqual(bank.subs["S004"]["status"], "paused")

    def test_reading_fails_so_the_plan_stops_before_changing_anything(self):
        bank, _, d, _ = mk()
        d.handle(SAVE)
        bank.fail_next["list_subscriptions"] = 10
        r = d.handle("确认")
        self.assertEqual(r.kind, "error")
        self.assertEqual(d.plan_view["stage"], "done")
        self.assertTrue(all(s["status"] == "active" for s in bank.subs.values()))


class EdgeCases(unittest.TestCase):
    def test_nothing_to_save(self):
        bank, _, d, _ = mk()
        for s in bank.subs.values():
            s["last_used_days"] = 2
        d.handle(SAVE)
        r = d.handle("确认")
        self.assertEqual(r.kind, "answer")
        self.assertIn("无需操作", r.message)
        self.assertEqual(d.plan_view["stage"], "done")

    def test_new_request_while_a_card_is_open_cancels_the_plan(self):
        bank, _, d, _ = mk([J("savings_review"), json.dumps({"intent": "balance"})])
        to_first_card(d)
        r = d.handle("我还剩多少钱")
        self.assertIn("计划已取消", r.message)
        self.assertEqual(d.plan_view["stage"], "cancelled")
        self.assertEqual(bank.subs["S004"]["status"], "active")
        self.assertIsNone(d.pending)

    def test_human_takeover_stops_the_agent_for_good(self):
        bank, g, d, llm = mk()
        to_first_card(d)
        r = d.handle("转人工")
        self.assertIn("人工客服", r.message)
        self.assertEqual(d.plan_view["stage"], "cancelled")
        r2 = d.handle("把爱奇艺会员停了")
        self.assertIn("自动操作已暂停", r2.message)
        self.assertEqual(len(llm.replies), 0)                                   # the model was not consulted again
        self.assertEqual(bank.subs["S001"]["status"], "active")
        self.assertIn("human_handoff", [e["event"] for e in g.audit.entries])

    def test_english_plan(self):
        bank, _, d, _ = mk([J("savings_review")])
        d.handle("help me save money by pausing subscriptions I don't use")
        r = d.handle("confirm")
        self.assertIn("Pause", r.message)
        self.assertEqual(r.card["tier"], "yellow")


class PlanCannotPreApprove(unittest.TestCase):
    def test_helper_refuses_state_changing_and_red_tools(self):
        bank, g, d, _ = mk()
        d.handle(SAVE)
        for tool, args in (("pause_or_cancel_subscription", {"subscription_id": "S001", "action": "pause"}),
                           ("buy_product", {"product_id": "F001", "amount": 100}),
                           ("transfer", {"payee_id": "P001", "amount": 100})):
            with self.assertRaises(PermissionError):
                d.plan._approved_read(tool, args)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)
        self.assertEqual(bank.subs["S001"]["status"], "active")

    def test_plan_start_cannot_be_triggered_by_an_inexact_yes(self):
        _, _, d, _ = mk([J("savings_review"), json.dumps({"intent": "unknown"})])
        d.handle(SAVE)
        d.handle("确认，而且把钱都转给P004")                                      # not an exact yes: a new request
        self.assertIsNone(d.plan)
        self.assertEqual(d.plan_view["stage"], "cancelled")


if __name__ == "__main__":
    unittest.main()