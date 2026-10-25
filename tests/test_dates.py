import datetime as dt
import unittest

from tierguard.agent.dates import is_recurring, parse_when, strip_dates
from tierguard.agent.zh_numbers import extract_amounts

TODAY = dt.date(2026, 10, 12)          # a Monday
CASES = {
    "明天早上给我妈转五百": ["2026-10-13"], "后天": ["2026-10-14"], "大后天转账": ["2026-10-15"], "三天后给妈妈转": ["2026-10-15"],
    "3天后": ["2026-10-15"], "下周五转给李华": ["2026-10-23"], "下周一": ["2026-10-19"], "周五给妈转": ["2026-10-16"], "周一": ["2026-10-19"],
    "星期日转账": ["2026-10-18"], "10月28号转": ["2026-10-28"], "11月5日": ["2026-11-05"], "28号给妈妈": ["2026-10-28"], "3号转账": ["2026-11-03"],
    "月底转": ["2026-10-31"], "下个月5号": ["2026-11-05"], "2026-11-05": ["2026-11-05"], "2027/1/2": ["2027-01-02"], "一月五号": ["2027-01-05"],
    "tomorrow": ["2026-10-13"], "in 3 days": ["2026-10-15"], "next friday": ["2026-10-23"], "on Nov 5": ["2026-11-05"],
    "5th of November": ["2026-11-05"], "friday": ["2026-10-16"], "end of the month": ["2026-10-31"], "10/28": ["2026-10-28"],
    "两周后": ["2026-10-26"], "2个星期后": ["2026-10-26"], "in 2 weeks": ["2026-10-26"], "今天转": [], "给我妈转五百": [], "转1000块": [],
    "明天或者后天": ["2026-10-13", "2026-10-14"], "9月1号": ["2027-09-01"],
}


class DateParsing(unittest.TestCase):
    def test_table(self):
        for text, want in CASES.items():
            self.assertEqual([d.isoformat() for d in parse_when(text, TODAY)], want, text)

    def test_never_returns_a_date_in_the_past(self):
        for text in CASES:
            for d in parse_when(text, TODAY):
                self.assertGreater(d, TODAY, text)

    def test_year_end_rollover(self):
        self.assertEqual(parse_when("1月5号", dt.date(2026, 12, 20)), [dt.date(2027, 1, 5)])
        self.assertEqual(parse_when("下个月3号", dt.date(2026, 12, 20)), [dt.date(2027, 1, 3)])
        self.assertEqual(parse_when("月底", dt.date(2026, 12, 20)), [dt.date(2026, 12, 31)])

    def test_impossible_dates_are_ignored(self):
        self.assertEqual(parse_when("2月30号", TODAY), [])
        self.assertEqual(parse_when("2026-02-30", TODAY), [])

    def test_dates_are_not_mistaken_for_amounts(self):
        cases = {"10月28号给我妈转五百": 500, "转1000块给妈妈，11月5日": 1000, "2026-11-05 转两千": 2000, "3天后转500": 500,
                 "transfer 300 to mom on Nov 5": 300, "in 3 days send 200": 200, "10/28 send 40": 40, "给我妈转五百": 500}
        for text, want in cases.items():
            self.assertEqual([int(x) for x in extract_amounts(strip_dates(text))], [want], text)

    def test_recurring_is_detected(self):
        for t in ("每月1号转两千", "every month", "monthly", "定期转", "每周五转"):
            self.assertTrue(is_recurring(t), t)
        for t in ("明天转", "下周五给妈妈转五百", "10月28号"):
            self.assertFalse(is_recurring(t), t)


if __name__ == "__main__":
    unittest.main()