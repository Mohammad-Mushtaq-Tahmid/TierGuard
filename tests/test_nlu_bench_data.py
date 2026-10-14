import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.runner.run_nlu import run_nlu_case  # noqa: E402
from tierguard.agent.zh_numbers import extract_amounts  # noqa: E402
from tierguard.llm.adapter import ScriptedLLM  # noqa: E402

CASES = yaml.safe_load((ROOT / "eval/tasks/utterances.yaml").read_text(encoding="utf-8"))


class BenchData(unittest.TestCase):
    def test_ids_unique_and_fields_present(self):
        self.assertEqual(len({c["id"] for c in CASES}), len(CASES))
        for c in CASES:
            for k in ("id", "group", "utterance", "gloss", "expect"):
                self.assertIn(k, c)

    def test_expected_amounts_match_the_deterministic_parser(self):
        """Independent of any LLM: the code must extract exactly the amount each case expects."""
        for c in CASES:
            want = c["expect"].get("amount")
            if want:
                self.assertEqual(extract_amounts(c["utterance"]), [Decimal(want)], c["id"])

    def test_runner_with_perfect_fake_model(self):
        c = next(x for x in CASES if x["id"] == "N05")
        fake = ScriptedLLM([json.dumps({"intent": "transfer", "payee_query": "妈", "amount_text": "五百", "period": None})])
        res, problems, g = run_nlu_case(c, fake)
        self.assertEqual(problems, [])

    def test_runner_flags_wrong_behaviour(self):
        c = next(x for x in CASES if x["id"] == "N06")      # expects red
        fake = ScriptedLLM([json.dumps({"intent": "balance"})])
        _, problems, _ = run_nlu_case(c, fake)
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()