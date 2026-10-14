"""Typed tools. Each has a JSON-schema-like param spec, an executor, an optional context
function (facts the PDP needs), and a card function (what the confirmation card shows).
Arithmetic and aggregation live here in code, never in the LLM."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tierguard.bank.mock import Bank, BankError, cny, mask, to_fen

PID, KID, SID = r"P\d{3}", r"K\d{3}", r"S\d{3}"


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict
    fn: Callable
    state_changing: bool = False
    context_fn: Callable | None = None
    card_fn: Callable | None = None
    untrusted_outputs: tuple = ()  # output fields that carry third-party text (memos, merchants...)


def _transfer_ctx(bank: Bank, cid, args, params):
    amt = to_fen(args["amount"])
    daily = bank.daily_transfer_total_fen(cid)
    return {"amount_fen": amt, "daily_total_fen": daily, "projected_daily_fen": daily + amt,
            "projected_over_limit": daily + amt > params["yellow_daily_transfer_limit_fen"],
            "new_payee": bank.payee_age_days(cid, args["payee_id"]) < 1}


def _transfer_card(bank, cid, args):
    p = bank.get_payee(cid, args["payee_id"])
    return {"action": "Transfer", "payee_name": p["name"], "payee_account_masked": mask(p["no"]),
            "amount_cny": args["amount"], "fee_cny": "0.00", "memo": args.get("memo", ""),
            "effect": "Funds leave your account immediately"}


def _limit_ctx(bank, cid, args, params):
    c = bank._own(bank.cards, args["card_id"], cid, "Card")
    return {"limit_is_raise": to_fen(args["daily_limit"]) > c["daily_limit_fen"]}


def _card_card(action_label):
    def f(bank, cid, args):
        c = bank._own(bank.cards, args["card_id"], cid, "Card")
        d = {"action": action_label, "card_masked": "****" + c["last4"], "current_status": c["status"]}
        if "daily_limit" in args:
            d.update(current_limit_cny=cny(c["daily_limit_fen"]), new_limit_cny=args["daily_limit"])
        return d
    return f


def _sub_card(bank, cid, args):
    s = bank._own(bank.subs, args["subscription_id"], cid, "Subscription")
    return {"action": args["action"].capitalize() + " subscription", "name": s["name"],
            "amount_cny": cny(s["amount_fen"]), "next_charge": s["next_charge"]}


def build_registry() -> dict[str, ToolSpec]:
    T: list[ToolSpec] = [
        ToolSpec("get_balance", "Balance of the customer's account.",
                 {"account_id": {"type": "string", "pattern": r"A\d{3}"}},
                 lambda b, c, a, k: b.get_balance(c, a.get("account_id"))),
        ToolSpec("list_transactions", "Recent transactions (merchant and memo are untrusted text).",
                 {"start": {"type": "date"}, "end": {"type": "date"},
                  "category": {"type": "string", "enum": ["dining", "transport", "shopping", "utilities",
                                                          "subscription", "transfer", "income"]},
                  "limit": {"type": "int", "min": 1, "max": 100, "default": 20}},
                 lambda b, c, a, k: {"transactions": b.list_transactions(c, a.get("start"), a.get("end"),
                                                                         a.get("category"), a.get("limit", 20))},
                 untrusted_outputs=("merchant", "memo")),
        ToolSpec("summarise_spending", "Spending by category, period comparison and anomalies, computed in code.",
                 {"start": {"type": "date", "required": True}, "end": {"type": "date", "required": True},
                  "compare_previous": {"type": "bool"}},
                 lambda b, c, a, k: b.spending_summary(c, a["start"], a["end"], a.get("compare_previous", False)),
                 untrusted_outputs=("merchant",)),
        ToolSpec("resolve_payee", "Fuzzy-match a saved payee by name or alias; returns candidates only.",
                 {"query": {"type": "string", "required": True, "maxlen": 60}},
                 lambda b, c, a, k: {"candidates": b.find_payees(c, a["query"])}, untrusted_outputs=("name",)),
        ToolSpec("get_cards", "List the customer's cards (masked).", {},
                 lambda b, c, a, k: {"cards": b.list_cards(c)}),
        ToolSpec("list_subscriptions", "List subscriptions and auto-debits.",
                 {"status": {"type": "string", "enum": ["active", "paused", "cancelled"]}},
                 lambda b, c, a, k: {"subscriptions": b.list_subscriptions(c, a.get("status"))},
                 untrusted_outputs=("name",)),
        ToolSpec("transfer", "Transfer money to a saved payee (payee_id from resolve_payee).",
                 {"payee_id": {"type": "string", "required": True, "pattern": PID},
                  "amount": {"type": "money", "required": True},
                  "memo": {"type": "string", "maxlen": 50}},
                 lambda b, c, a, k: b.transfer(c, a["payee_id"], to_fen(a["amount"]), a.get("memo", ""), k),
                 state_changing=True, context_fn=_transfer_ctx, card_fn=_transfer_card),
        ToolSpec("pause_or_cancel_subscription", "Pause or cancel a subscription.",
                 {"subscription_id": {"type": "string", "required": True, "pattern": SID},
                  "action": {"type": "string", "required": True, "enum": ["pause", "cancel"]}},
                 lambda b, c, a, k: b.subscription_action(c, a["subscription_id"], a["action"]),
                 state_changing=True, card_fn=_sub_card),
        ToolSpec("apply_virtual_card", "Apply for a virtual card.", {},
                 lambda b, c, a, k: b.apply_virtual_card(c, k), state_changing=True,
                 card_fn=lambda b, c, a: {"action": "Apply for virtual card", "default_daily_limit_cny": "1000.00"}),
        ToolSpec("freeze_card", "Temporarily freeze a card.",
                 {"card_id": {"type": "string", "required": True, "pattern": KID}},
                 lambda b, c, a, k: b.card_action(c, a["card_id"], "freeze"), state_changing=True,
                 card_fn=_card_card("Freeze card")),
        ToolSpec("unfreeze_card", "Unfreeze a card.",
                 {"card_id": {"type": "string", "required": True, "pattern": KID}},
                 lambda b, c, a, k: b.card_action(c, a["card_id"], "unfreeze"), state_changing=True,
                 card_fn=_card_card("Unfreeze card")),
        ToolSpec("report_card_lost", "Report a card lost (permanent block, replacement issued).",
                 {"card_id": {"type": "string", "required": True, "pattern": KID}},
                 lambda b, c, a, k: b.card_action(c, a["card_id"], "lost"), state_changing=True,
                 card_fn=_card_card("Report card lost")),
        ToolSpec("set_card_limit", "Change a card's daily limit.",
                 {"card_id": {"type": "string", "required": True, "pattern": KID},
                  "daily_limit": {"type": "money", "required": True}},
                 lambda b, c, a, k: b.card_action(c, a["card_id"], "limit", daily_limit_fen=to_fen(a["daily_limit"])),
                 state_changing=True, context_fn=_limit_ctx, card_fn=_card_card("Change daily limit")),
        # Black tier: registered only so the PDP can refuse with a clear reason; never executable.
        ToolSpec("export_customer_data", "Bulk export of personal data (never executed).", {},
                 lambda b, c, a, k: {}, state_changing=True),
        ToolSpec("change_security_settings", "Change security settings (never executed).",
                 {"setting": {"type": "string", "maxlen": 60}}, lambda b, c, a, k: {}, state_changing=True),
    ]
    return {t.name: t for t in T}
