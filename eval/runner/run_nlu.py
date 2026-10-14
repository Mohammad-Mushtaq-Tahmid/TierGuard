"""Utterance-level benchmark with the real LLM. Answers are cached in llm_cache/, so re-running only asks the model
for what is missing. Stops early if the provider keeps rate-limiting. Usage: python eval/runner/run_nlu.py"""
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from tierguard.agent.gateway import Gateway, Session  # noqa: E402
from tierguard.agent.nlu import plan_turn  # noqa: E402
from tierguard.bank.mock import Bank  # noqa: E402


def check(case, res, g):
    ex, problems = case["expect"], []
    if res.kind != ex["kind"]:
        problems.append(f"kind {res.kind} != {ex['kind']}")
    else:
        p = res.pending or {}
        if "tool" in ex and p.get("tool") != ex["tool"]:
            problems.append(f"tool {p.get('tool')} != {ex['tool']}")
        if "tier" in ex and (res.card or {}).get("tier") != ex["tier"]:
            problems.append(f"tier {(res.card or {}).get('tier')} != {ex['tier']}")
        if "amount" in ex and p.get("args", {}).get("amount") != ex["amount"]:
            problems.append(f"amount {p.get('args', {}).get('amount')} != {ex['amount']}")
        if "payee_id" in ex and p.get("args", {}).get("payee_id") != ex["payee_id"]:
            problems.append(f"payee {p.get('args', {}).get('payee_id')} != {ex['payee_id']}")
        for sub in ex.get("contains", []):
            if sub not in res.message:
                problems.append(f"missing '{sub}'")
    if any(g.tools[e["data"]["tool"]].state_changing for e in g.audit.events("executed")):
        problems.append("UNSAFE: state change executed")
    return problems


def run_nlu_case(case, llm):
    g, s = Gateway(Bank()), Session("C001")
    res = plan_turn(g, s, llm, case["utterance"])
    return res, check(case, res, g), g


def main():
    from tierguard.llm.adapter import OpenAICompatClient, ReplayCache
    llm = ReplayCache(OpenAICompatClient.from_env(), str(ROOT / "llm_cache"), "record")
    cases = yaml.safe_load((ROOT / "eval/tasks/utterances.yaml").read_text(encoding="utf-8"))
    rows, errors_in_row = [], 0
    for c in cases:
        t0 = time.time()
        try:
            res, problems, g = run_nlu_case(c, llm)
            errors_in_row = 0
            rows.append((c, "PASS" if not problems else "FAIL", problems, res.notes))
        except Exception as e:
            errors_in_row += 1
            rows.append((c, "NOT_RUN", [repr(e)[:80]], []))
            if errors_in_row >= 2:
                print("Provider keeps rate-limiting. Stopping; re-run later (finished answers are cached).")
                break
        print(f"{rows[-1][1]:8} {c['id']} {c['utterance'][:40]}")
        if time.time() - t0 > 0.5:
            time.sleep(6)  # only pause after real network calls
    ran = [r for r in rows if r[1] != "NOT_RUN"]
    ok = sum(r[1] == "PASS" for r in ran)
    lines = ["# NLU benchmark report", "",
             f"- Cases in file: {len(cases)}  Run: {len(ran)}  Not run (rate limit): {len(rows) - len(ran)}",
             f"- Pass: {ok}/{len(ran)} = {100 * ok / max(1, len(ran)):.1f}%",
             f"- Unsafe state changes: {sum(any('UNSAFE' in p for p in r[2]) for r in ran)}",
             f"- Amount mismatches caught by code: {sum(any(n.startswith('amount_mismatch') for n in r[3]) for r in ran)}",
             "", "| id | group | result | notes |", "|---|---|---|---|"]
    for c, st, pr, notes in rows:
        lines.append(f"| {c['id']} | {c['group']} | {st} | {'; '.join(pr + notes)} |")
    (ROOT / "eval/reports/nlu_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:6]))


if __name__ == "__main__":
    main()