"""Run benchmark cases against a fresh seeded bank; write a markdown report. Usage: make eval"""
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from tierguard.agent.gateway import Gateway, Session  # noqa: E402
from tierguard.bank.mock import Bank  # noqa: E402


def run_case(case):
    bank = Bank(); g = Gateway(bank); s = Session("C001")
    problems, turns = [], 0
    for st in case["steps"]:
        turns += 1
        r = g.call(s, st["tool"], st["args"], taint=set(st.get("taint", [])))
        ex = st["expect"]
        if r.status != ex["status"]:
            problems.append(f"{st['tool']}: status {r.status} != {ex['status']}")
            continue
        tier = (r.decision or {}).get("tier") or (r.card or {}).get("tier")
        if "tier" in ex and tier != ex["tier"]:
            problems.append(f"{st['tool']}: tier {tier} != {ex['tier']}")
        if "error_code" in ex and r.error_code != ex["error_code"] and "final" not in ex:
            problems.append(f"{st['tool']}: error {r.error_code} != {ex['error_code']}")
        if "step_up" in ex and (r.card or {}).get("needs_step_up") != ex["step_up"]:
            problems.append(f"{st['tool']}: step_up mismatch")
        if "candidates" in ex and len(r.data["candidates"]) != ex["candidates"]:
            problems.append(f"{st['tool']}: {len(r.data['candidates'])} candidates != {ex['candidates']}")
        if "total_cny" in ex and r.data["total_cny"] != ex["total_cny"]:
            problems.append(f"{st['tool']}: total {r.data['total_cny']} != {ex['total_cny']}")
        if "anomalies" in ex and len(r.data["anomalies"]) != ex["anomalies"]:
            problems.append(f"{st['tool']}: anomalies {len(r.data['anomalies'])} != {ex['anomalies']}")
        if st.get("confirm") and r.status == "needs_confirmation":
            turns += 1
            otp = next((m["otp"] for m in g.conf.sms_outbox if m["token"] == r.token), None)
            r2 = g.call(s, st["tool"], st["args"], taint=set(st.get("taint", [])), token=r.token, otp=otp)
            if r2.status != ex["final"]:
                problems.append(f"{st['tool']}: final {r2.status} != {ex['final']}")
            if "error_code" in ex and r2.error_code != ex["error_code"]:
                problems.append(f"{st['tool']}: final error {r2.error_code} != {ex['error_code']}")
    # unsafe action = state change executed without a redeemed confirmation
    redeemed = len(g.audit.events("confirmation_redeemed"))
    changed = sum(1 for e in g.audit.events("executed") if g.tools[e["data"]["tool"]].state_changing)
    unsafe = changed > redeemed
    return {"id": case["id"], "scenario": case["scenario"], "ok": not problems and not unsafe,
            "problems": problems, "unsafe": unsafe, "turns": turns, "audit_ok": g.audit.verify()[0]}


def main():
    cases = yaml.safe_load((ROOT / "eval/tasks/seed_cases.yaml").read_text(encoding="utf-8"))
    t0 = time.time()
    res = [run_case(c) for c in cases]
    n_ok = sum(r["ok"] for r in res)
    lines = ["# Benchmark report (gateway level)", "",
             f"- Cases: {len(res)}  Passed: {n_ok}  Success rate: {100 * n_ok / len(res):.1f}%",
             f"- Unsafe state-changing actions: {sum(r['unsafe'] for r in res)}",
             f"- Audit chains valid: {all(r['audit_ok'] for r in res)}",
             f"- Runtime: {time.time() - t0:.2f}s", "", "| id | scenario | result | tool-turns | notes |", "|---|---|---|---|---|"]
    for r in res:
        lines.append(f"| {r['id']} | {r['scenario']} | {'PASS' if r['ok'] else 'FAIL'} | {r['turns']} | {'; '.join(r['problems'])} |")
    out = ROOT / "eval/reports/latest.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:6]))
    return 0 if n_ok == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
