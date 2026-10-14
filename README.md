# TierGuard: AI Banking Agent (FinTechathon 2026, Domestic AI Track)

Security-first banking agent. The LLM proposes tool calls; a deterministic gateway
(validator -> policy engine -> confirmation -> executor -> audit) decides what actually runs.

## Status
Security kernel and mock bank are done and tested. The LLM planner, dialogue manager and UI are next
(see PROGRESS.md).

## Run (clean machine, Python 3.11+)
    pip install -r requirements.txt
    make test          # or: PYTHONPATH=src:tests python -m unittest discover -s tests -v
    make eval          # or: PYTHONPATH=src python eval/runner/run_tasks.py   -> eval/reports/latest.md
Windows PowerShell:  `$env:PYTHONPATH="src;tests"; python -m unittest discover -s tests -v`

## Layout
    src/tierguard/bank/     mock bank, seeded and deterministic, per-customer scoping enforced here
    src/tierguard/tools/    typed tools + strict argument validation (rejects smuggled customer_id, zero-width chars)
    src/tierguard/policy/   tiers.yaml (data), pdp.py (decision point), confirm.py (single-use hash-bound tokens + OTP)
    src/tierguard/agent/    gateway.py (only path to the bank, circuit breaker), grounding.py (hallucination check)
    src/tierguard/audit/    hash-chained tamper-evident log
    src/tierguard/llm/      OpenAI-compatible adapter + record/replay cache
    eval/                   benchmark cases (YAML) + runner + reports

## Tiers (official baseline; we only escalate, never relax)
| Tier | Verdict | Examples |
|---|---|---|
| Green | automatic | balance, transactions, bill analysis |
| Yellow | confirmation card | transfer with daily total <= 1,000 CNY, cancel subscription, virtual card |
| Red | confirmation + OTP step-up | daily total > 1,000, card loss, unfreeze, purchase |
| Black (ours) | always refused | data export, security settings |
Dynamic escalation to red: tainted (untrusted-origin) argument, repeated failures, card limit raise.

## LLM providers
Set `TG_PROVIDER` (zai | modelscope | siliconflow | openrouter | local), `TG_API_KEY`, `TG_MODEL`.
Confirm base URLs and model names at sign-up. Use synthetic data only.
