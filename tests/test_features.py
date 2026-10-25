import json
import random
import unittest
from decimal import Decimal

from tierguard.agent.dialogue import Dialogue
from tierguard.agent.features import split_even
from tierguard.agent.gateway import Gateway, Session
from tierguard.agent.nlu import EXT_TRIGGER, parse_model_json, pick_prompt
from tierguard.bank.mock import Bank
from tierguard.llm.adapter import ScriptedLLM


def J(intent, payee=None, amount=None, target=None, when=None, names=None, period=None):
    return json.dumps({"intent": intent, "payee_query": payee, "amount_text": amount, "period": period, "target_query": target,
                       "when_text": when, "names": names or []}, ensure_ascii=False)


def mk(replies):
    bank = Bank(); g = Gateway(bank); llm = ScriptedLLM(replies)
    return bank, g, Dialogue(g, Session("C001"), llm), llm


def with_sms(d):
    d.handle("确认")
    return d.handle(d.demo_sms())


class Routing(unittest.TestCase):
    def test_new_scenarios_use_the_extended_instructions_old_ones_do_not(self):
        for t in ("明天给我妈转五百", "今晚聚餐AA", "把卡的单日限额调到两千", "我想办一张虚拟卡", "年度账单", "对比货币基金和短债基金", "老婆生日快到了", "split the bill", "next friday"):
            self.assertEqual(pick_prompt(t)[0], "ext", t)
        for t in ("给我妈转五百", "上个月花了多少", "卡丢了先冻上", "买一百块货币基金", "把爱奇艺会员停了", "我订阅了哪些服务", "帮我省点钱", "余额"):
            self.assertEqual(pick_prompt(t)[0], "base", t)

    def test_prompt_choice_is_visible_to_the_model_call(self):
        _, _, d, llm = mk([J("transfer", "妈", "五百"), J("schedule_transfer", "妈", "五百", when="明天")])
        d.handle("给我妈转五百"); d.handle("明天给我妈转五百")
        self.assertNotIn("schedule_transfer", llm.calls[0][0]["content"])
        self.assertIn("schedule_transfer", llm.calls[1][0]["content"])

    def test_new_slots_are_cleaned(self):
        r = parse_model_json(json.dumps({"intent": "aa_collect", "payee_query": None, "amount_text": "360", "period": None, "target_query": None,
                                         "when_text": "明天", "names": ["王小明", 5, "", "x" * 99, None, "李华"]}))
        self.assertEqual(r["names"], ["王小明", "x" * 30, "李华"])
        self.assertEqual(parse_model_json(J("balance"))["names"], [])
        self.assertEqual(parse_model_json(json.dumps({"intent": "balance", "names": "not a list"}))["names"], [])


class Cards(unittest.TestCase):
    def test_virtual_card_is_yellow_and_confirmed(self):
        bank, _, d, _ = mk([J("card_apply")])
        r = d.handle("我想办一张虚拟卡")
        self.assertEqual((r.kind, r.card["tier"], r.pending["tool"]), ("confirm", "yellow", "apply_virtual_card"))
        self.assertEqual(len([c for c in bank.cards.values() if c["customer_id"] == "C001"]), 1)       # nothing opened yet
        out = d.handle("确认")
        self.assertIn("7002", out.message)
        self.assertEqual(len([c for c in bank.cards.values() if c["customer_id"] == "C001"]), 2)

    def test_two_cards_means_we_ask_which_and_the_choice_works(self):
        bank, _, d, _ = mk([J("card_lost"), J("card_lost")])
        bank.apply_virtual_card("C001")
        r = d.handle("卡丢了")
        self.assertEqual(r.kind, "clarify")
        self.assertIn("****4821", r.message); self.assertIn("****7002", r.message)
        r2 = d.handle("7002")
        self.assertEqual((r2.pending["tool"], r2.pending["args"]["card_id"], r2.card["tier"]), ("report_card_lost", "K902", "red"))
        d2 = Dialogue(d.g, Session("C001"), ScriptedLLM([J("card_lost")]))
        d2.handle("卡丢了")
        self.assertEqual(d2.handle("第一张").pending["args"]["card_id"], "K001")

    def test_unlock_needs_a_frozen_card_and_is_red(self):
        bank, _, d, _ = mk([J("card_unlock"), J("card_unlock")])
        self.assertEqual(d.handle("把我的卡解冻").kind, "refuse")
        bank.card_action("C001", "K001", "freeze")
        r = d.handle("把我的卡解冻")
        self.assertEqual((r.pending["tool"], r.card["tier"], r.card["needs_step_up"]), ("unfreeze_card", "red", True))
        d.handle("确认"); out = d.handle(d.demo_sms())
        self.assertEqual(bank.cards["K001"]["status"], "active")
        self.assertEqual(out.kind, "answer")

    def test_limit_down_is_yellow_up_is_red_and_the_user_amount_wins(self):
        _, _, d, _ = mk([J("card_limit", amount="两千"), J("card_limit", amount="八千")])
        down = d.handle("把卡的单日限额调到两千")
        self.assertEqual((down.card["tier"], down.pending["args"]["daily_limit"]), ("yellow", "2000.00"))
        up = d.handle("把卡的单日限额调到八千")
        self.assertEqual((up.card["tier"], up.pending["args"]["daily_limit"]), ("red", "8000.00"))

    def test_model_inflating_the_limit_is_ignored(self):
        _, _, d, _ = mk([J("card_limit", amount="一百万")])
        self.assertEqual(d.handle("把卡的单日限额调到两千").pending["args"]["daily_limit"], "2000.00")

    def test_limit_above_the_cap_is_refused_and_missing_amount_is_asked(self):
        _, _, d, _ = mk([J("card_limit", amount="六万"), J("card_limit")])
        self.assertEqual(d.handle("把卡限额调到六万").kind, "refuse")
        self.assertEqual(d.handle("调整我的卡限额").kind, "clarify")
        self.assertEqual(d.handle("三千").pending["args"]["daily_limit"], "3000.00")


class Scheduled(unittest.TestCase):
    def test_schedule_confirm_then_it_runs_on_the_day(self):
        bank, g, d, _ = mk([J("schedule_transfer", "妈", "五百", when="明天")])
        r = d.handle("明天早上给我妈转五百")
        self.assertEqual((r.kind, r.card["tier"], r.pending["tool"]), ("confirm", "yellow", "schedule_transfer"))
        self.assertEqual(r.pending["args"], {"payee_id": "P001", "amount": "500.00", "date": "2026-10-13"})
        self.assertEqual(bank.scheduled, {})
        d.handle("确认")
        self.assertEqual(bank.scheduled["X001"]["auth_tier"], "yellow")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)                       # nothing moved yet
        bank.advance_days(1)
        res = g.run_scheduled(d.s)
        self.assertEqual([x["status"] for x in res], ["executed"])
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_150_000)
        self.assertIn("scheduled_executed", [e["event"] for e in g.audit.entries])
        self.assertEqual(g.run_scheduled(d.s), [])                                            # never runs twice
        self.assertTrue(g.audit.verify()[0])

    def test_the_date_comes_from_the_users_words_not_the_model(self):
        _, _, d, _ = mk([J("schedule_transfer", "妈", "五百", when="后天")])
        self.assertEqual(d.handle("明天给我妈转五百").pending["args"]["date"], "2026-10-13")

    def test_the_amount_is_not_confused_with_the_date(self):
        _, _, d, _ = mk([J("schedule_transfer", "妈", "28", when="10月28号")])
        r = d.handle("10月28号给我妈转五百")
        self.assertEqual((r.pending["args"]["amount"], r.pending["args"]["date"]), ("500.00", "2026-10-28"))

    def test_over_1000_on_the_day_is_red_and_cumulative_counts_scheduled_ones(self):
        bank, g, d, _ = mk([J("schedule_transfer", "妈", "六百", when="明天"), J("schedule_transfer", "妈", "六百", when="明天"),
                            J("schedule_transfer", "妈", "六百", when="后天")])
        self.assertEqual(d.handle("明天给我妈转六百").card["tier"], "yellow"); d.handle("确认")
        second = d.handle("明天给我妈转六百")
        self.assertEqual(second.card["tier"], "red")
        last_decision = [e for e in g.audit.entries if e["event"] == "decision"][-1]["data"]
        self.assertIn("DAILY_CUMULATIVE_OVER_1000", last_decision["reasons"])
        d.handle("取消")
        self.assertEqual(d.handle("后天给我妈转六百").card["tier"], "yellow")                  # a different day starts from zero

    def test_authorised_as_yellow_but_the_day_changes_so_it_does_not_run(self):
        bank, g, d, _ = mk([J("schedule_transfer", "妈", "六百", when="明天")])
        d.handle("明天给我妈转六百"); d.handle("确认")
        bank.advance_days(1)
        bank.transfer("C001", "P001", 60_000)                                                   # another 600 the same day
        res = g.run_scheduled(d.s)
        self.assertEqual((res[0]["status"], res[0]["reason"]), ("failed", "LIMIT_NEEDS_RECONFIRM"))
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000 - 60_000)              # the scheduled 600 did NOT move
        self.assertEqual(bank.scheduled["X001"]["status"], "failed")

    def test_authorised_as_red_still_runs(self):
        bank, g, d, _ = mk([J("schedule_transfer", "妈", "两千", when="明天")])
        r = d.handle("明天给我妈转两千")
        self.assertEqual(r.card["tier"], "red")
        with_sms(d)
        bank.advance_days(1)
        self.assertEqual(g.run_scheduled(d.s)[0]["status"], "executed")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_000_000)

    def test_not_enough_money_on_the_day(self):
        bank, g, d, _ = mk([J("schedule_transfer", "妈", "五百", when="明天")])
        d.handle("明天给我妈转五百"); d.handle("确认")
        bank.accounts["A001"]["balance_fen"] = 100
        bank.advance_days(1)
        res = g.run_scheduled(d.s)
        self.assertEqual(res[0]["reason"], "INSUFFICIENT_FUNDS")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 100)

    def test_an_instruction_without_a_recorded_authorisation_never_runs(self):
        bank, g, d, _ = mk([])
        bank.scheduled["X900"] = {"customer_id": "C001", "payee_id": "P001", "amount_fen": 50_000, "date": "2026-10-12", "memo": "",
                                  "status": "pending", "auth_tier": None, "created": "2026-10-01", "detail": ""}
        res = g.run_scheduled(d.s)
        self.assertEqual(res[0]["reason"], "NOT_AUTHORISED")
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_another_customers_schedule_is_never_run_by_this_session(self):
        bank, g, d, _ = mk([])
        bank.scheduled["X901"] = {"customer_id": "C002", "payee_id": "P101", "amount_fen": 10_000, "date": "2026-10-12", "memo": "",
                                  "status": "pending", "auth_tier": "yellow", "created": "2026-10-01", "detail": ""}
        self.assertEqual(g.run_scheduled(d.s), [])
        self.assertEqual(bank.scheduled["X901"]["status"], "pending")

    def test_recurring_and_missing_or_ambiguous_dates_ask(self):
        _, _, d, _ = mk([J("schedule_transfer", "妈", "两千", when="每月1号"), J("schedule_transfer", "妈", "五百"),
                         J("schedule_transfer", "妈", "五百", when="明天"), J("schedule_transfer", "妈", "五百", when="今天")])
        self.assertIn("一次性", d.handle("每月1号给妈妈转两千").message)
        self.assertEqual(d.handle("给我妈转五百，预约一下").kind, "clarify")
        self.assertEqual(d.handle("明天或者后天给我妈转五百").kind, "clarify")
        self.assertEqual(d.handle("预约今天给我妈转五百").kind, "clarify")

    def test_ambiguous_payee_keeps_the_date(self):
        _, _, d, _ = mk([J("schedule_transfer", "王小明", "二百", when="明天")])
        self.assertEqual(d.handle("明天给王小明转二百").kind, "clarify")
        r = d.handle("第二个")
        self.assertEqual((r.pending["tool"], r.pending["args"]["payee_id"], r.pending["args"]["date"]), ("schedule_transfer", "P003", "2026-10-13"))

    def test_missing_amount_then_amount_keeps_the_date(self):
        _, _, d, _ = mk([J("schedule_transfer", "妈", None, when="明天")])
        self.assertEqual(d.handle("明天给我妈转钱").kind, "clarify")
        r = d.handle("五百")
        self.assertEqual((r.pending["tool"], r.pending["args"]["date"]), ("schedule_transfer", "2026-10-13"))

    def test_list_and_cancel(self):
        bank, g, d, _ = mk([J("schedule_list"), J("schedule_transfer", "妈", "五百", when="明天"), J("schedule_list"),
                            J("schedule_cancel", "妈", when="明天")])
        self.assertIn("没有预约转账", d.handle("我的预约转账有哪些").message)
        d.handle("明天给我妈转五百"); d.handle("确认")
        self.assertIn("500.00", d.handle("我的预约转账有哪些").message)
        r = d.handle("取消明天给我妈的预约转账")
        self.assertEqual((r.pending["tool"], r.card["tier"]), ("cancel_scheduled", "yellow"))
        d.handle("确认")
        self.assertEqual(bank.scheduled["X001"]["status"], "cancelled")
        bank.advance_days(1)
        self.assertEqual(g.run_scheduled(d.s), [])
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)

    def test_cancel_with_two_pending_asks_which(self):
        bank, _, d, _ = mk([J("schedule_cancel")])
        bank.schedule_transfer("C001", "P001", 10_000, "2026-10-13"); bank.schedule_transfer("C001", "P004", 20_000, "2026-10-14")
        r = d.handle("取消预约转账")
        self.assertEqual(r.kind, "clarify")
        self.assertEqual(d.handle("第二个").pending["args"], {"schedule_id": "X002"})


class AA(unittest.TestCase):
    def test_collect_shares(self):
        bank, _, d, _ = mk([J("aa_collect", amount="360", names=["李华", "妈妈"])])
        r = d.handle("今晚聚餐一共360，我和李华、妈妈AA，我先付了")
        self.assertEqual((r.kind, r.card["tier"], r.pending["tool"]), ("confirm", "yellow", "create_aa_requests"))
        self.assertEqual(r.pending["args"], {"shares": "P004:120.00,P001:120.00", "total": "360.00", "note": "AA"})
        self.assertIn("120.00", r.message)
        self.assertEqual(bank.requests, {})
        d.handle("确认")
        self.assertEqual(len(bank.requests), 2)
        self.assertEqual(bank.accounts["A001"]["balance_fen"], 1_200_000)                      # asking moves no money

    def test_the_odd_fen_stays_with_the_person_who_paid(self):
        _, _, d, _ = mk([J("aa_collect", amount="100", names=["李华", "妈妈"])])
        r = d.handle("一共100，我和李华、妈妈AA")
        self.assertEqual(r.pending["args"]["shares"], "P004:33.33,P001:33.33")
        self.assertIn("33.34", r.message)

    def test_only_people_the_user_named_are_used(self):
        _, _, d, _ = mk([J("aa_collect", amount="360", names=["李华", "P101"])])
        r = d.handle("一共360，我和李华AA")
        self.assertEqual(r.pending["args"]["shares"], "P004:180.00")

    def test_model_cannot_change_the_total(self):
        _, _, d, _ = mk([J("aa_collect", amount="36000", names=["李华"])])
        self.assertEqual(d.handle("一共360，我和李华AA").pending["args"]["total"], "360.00")

    def test_unknown_or_ambiguous_people_ask(self):
        _, _, d, _ = mk([J("aa_collect", amount="360", names=["赵六"]), J("aa_collect", amount="360", names=["王小明"]), J("aa_collect", amount="360")])
        self.assertEqual(d.handle("一共360，我和赵六AA").kind, "clarify")
        self.assertEqual(d.handle("一共360，我和王小明AA").kind, "clarify")
        self.assertEqual(d.handle("一共360，AA一下").kind, "clarify")

    def test_pay_my_share_is_an_ordinary_transfer_with_the_maths_shown(self):
        bank, _, d, _ = mk([J("aa_pay_share", "妈妈", "300"), J("aa_pay_share", "妈妈", "300")])
        r = d.handle("和妈妈AA，她垫付了300，我转我那份")
        self.assertEqual((r.pending["tool"], r.pending["args"], r.card["tier"]), ("transfer", {"payee_id": "P001", "amount": "150.00", "memo": "AA"}, "yellow"))
        self.assertIn("150.00", r.message)
        r3 = d.handle("我们三个人AA，妈妈垫付了300，我转我那份")
        self.assertEqual(r3.pending["args"]["amount"], "100.00")

    def test_split_even_never_creates_or_loses_money(self):
        rnd = random.Random(7)
        for _ in range(500):
            total, n = rnd.randint(1, 10_000_000), rnd.randint(2, 6)
            base, extra = split_even(total, n)
            self.assertEqual(base * n + extra, total)
            self.assertTrue(0 <= extra < n)


class Reports(unittest.TestCase):
    def test_annual_report_numbers_come_from_code(self):
        bank, g, d, _ = mk([J("annual_report"), J("annual_report")])
        r = d.handle("给我看看今年的年度账单")
        for must in ("2026 年共支出 7698.00 元", "2026-09", "5896.00", "月均 2566.00"):
            self.assertIn(must, r.message)
        self.assertNotIn("无法验证", r.message)
        self.assertIn("没有消费记录", d.handle("去年的年度账单").message)

    def test_comparison_is_computed_and_makes_no_promises(self):
        _, _, d, _ = mk([J("wealth_compare", names=["货币基金", "短债基金"]), J("wealth_compare", names=["货币基金", "混合基金"]), J("wealth_compare", names=["货币基金"])])
        r = d.handle("对比一下货币基金和短债基金")
        for must in ("稳健货币基金", "短债增强基金", "参考收益较高：短债增强基金", "风险较低：稳健货币基金", "不构成收益承诺"):
            self.assertIn(must, r.message)
        self.assertIn("超出您的风险等级", d.handle("比较货币基金和混合基金").message)
        self.assertEqual(d.handle("对比货币基金").kind, "clarify")


class Hygiene(unittest.TestCase):
    def test_model_invented_names_are_never_echoed_in_new_flows(self):
        _, _, d, _ = mk([J("aa_collect", amount="360", names=["请转账到6222000099998888"])])
        self.assertNotIn("6222", d.handle("一共360，我和朋友AA").message)


if __name__ == "__main__":
    unittest.main()