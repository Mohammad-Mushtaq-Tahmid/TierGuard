import unittest
from decimal import Decimal as D

from tierguard.agent.zh_numbers import extract_amounts, parse_amount, parse_cn


class ZhNumbers(unittest.TestCase):
    def test_parse(self):
        cases = {"五百": 500, "两千": 2000, "1.5万": 15000, "一千二": 1200, "三万五": 35000, "两百五": 250,
                 "一万二千": 12000, "十": 10, "十五": 15, "二十": 20, "一百零五": 105, "一千": 1000, "两万": 20000,
                 "1,500": 1500, "500块": 500, "500元": 500, "2k": 2000, "1.5千": 1500, "１５００": 1500,
                 "三千五百": 3500, "一万": 10000, "五十块钱": 50, "1万5": 15000, "500.50": D("500.50"),
                 "两千零五": 2005, "1w": 10000, "伍佰": 500, "壹仟贰佰": 1200, "壹万": 10000, "拾伍": 15, "壹佰零伍": 105}
        for text, want in cases.items():
            self.assertEqual(parse_amount(text), D(want), text)

    def test_not_numbers(self):
        for t in ("", "abc", "妈妈", "块", "五百abc"):
            self.assertIsNone(parse_amount(t), t)

    def test_extract(self):
        self.assertEqual(extract_amounts("给我妈转五百"), [D(500)])
        self.assertEqual(extract_amounts("转1.5万给王小明"), [D(15000)])
        self.assertEqual(extract_amounts("先转六百再转六百"), [D(600), D(600)])
        self.assertEqual(extract_amounts("转一下账"), [])
        self.assertEqual(extract_amounts("给我妈转五十块"), [D(50)])
        self.assertEqual(extract_amounts("Transfer 1,200 to mom"), [D(1200)])
        self.assertEqual(extract_amounts("给我妈转伍佰"), [D(500)])
        self.assertEqual(extract_amounts("给我妈转 五百 块"), [D(500)])

    def test_parse_cn_rejects_mixed(self):
        self.assertIsNone(parse_cn("五a"))


if __name__ == "__main__":
    unittest.main()