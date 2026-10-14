import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.runner.run_tasks import run_case  # noqa: E402


class SeedBenchmark(unittest.TestCase):
    def test_all_seed_cases_pass(self):
        cases = yaml.safe_load((ROOT / "eval/tasks/seed_cases.yaml").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 12)
        for c in cases:
            r = run_case(c)
            self.assertTrue(r["ok"], (c["id"], r["problems"]))
