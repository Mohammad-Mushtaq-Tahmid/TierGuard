"""Typed tools. Each has a JSON-schema-like param spec, an executor, an optional context
function (facts the PDP needs), and a card function (what the confirmation card shows).
Arithmetic and aggregation live here in code, never in the LLM."""
from __future__ import annotations

import hashlib
import datetime as dt
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
    bind_fn: Callable | None = None  # facts about the touched records, bound into the confirmation hash


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


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _transfer_bind(bank, cid, args):
    p = bank.get_payee(cid, args["payee_id"])
    return {"payee_account_fp": _fp(p["no"]), "payee_name": p["name"]}


def _card_bind(bank, cid, args):
    c = bank._own(bank.cards, args["card_id"], cid, "Card")
    return {"card_last4": c["last4"], "card_status": c["status"], "daily_limit_fen": c["daily_limit_fen"]}


def _sub_bind(bank, cid, args):
    x = bank._own(bank.subs, args["subscription_id"], cid, "Subscription")
    return {"name": x["name"], "amount_fen": x["amount_fen"], "status": x["status"]}


def _product_card(label):
    def f(bank, cid, args):
        p = bank.products.get(args["product_id"])
        if p is None:
            raise BankError("NOT_FOUND", "Product not found")
        d = {"action": label, "product": p["name"], "product_en": p["name_en"], "risk": f"R{p['risk']}",
             "amount_cny": args["amount"], "reference_yield_percent": p["ref_yield"],
             "note": "Past yield does not guarantee future returns"}
        if label == "Buy product":
            d["min_amount_cny"] = cny(p["min_fen"])
        return d
    return f


def _parse_shares(text: str) -> list[tuple[str, int]]:
    out = []
    for part in text.split(","):
        pid, amt = part.split(":")
        out.append((pid, to_fen(amt)))
    return out


def _schedule_ctx(bank, cid, args, params):
    when = dt.date.fromisoformat(args["date"])
    if when <= bank.today or (when - bank.today).days > 365:      # refuse BEFORE a confirmation card is shown
        raise BankError("BAD_DATE", "The scheduled date must be after today and within a year")
    amt = to_fen(args["amount"])
    planned = bank.sched_total_fen(cid, args["date"])
    return {"amount_fen": amt, "projected_daily_fen": planned + amt,
            "projected_over_limit": planned + amt > params["yellow_daily_transfer_limit_fen"],
            "new_payee": bank.payee_age_days(cid, args["payee_id"]) < 1}


def _schedule_card(bank, cid, args):
    p = bank.get_payee(cid, args["payee_id"])
    return {"action": "Schedule transfer", "payee_name": p["name"], "payee_account_masked": mask(p["no"]),
            "amount_cny": args["amount"], "date": args["date"], "memo": args.get("memo", ""),
            "effect": "Runs automatically on that date if funds and limits still allow"}


def _cancel_sched_card(bank, cid, args):
    x = bank._own(bank.scheduled, args["schedule_id"], cid, "Scheduled transfer")
    return {"action": "Cancel scheduled transfer", "payee_name": bank.payees[x["payee_id"]]["name"],
            "amount_cny": cny(x["amount_fen"]), "date": x["date"], "current_status": x["status"]}


def _cancel_sched_bind(bank, cid, args):
    x = bank._own(bank.scheduled, args["schedule_id"], cid, "Scheduled transfer")
    return {"status": x["status"], "amount_fen": x["amount_fen"], "date": x["date"], "payee_account_fp": _fp(bank.payees[x["payee_id"]]["no"])}


def _aa_ctx(bank, cid, args, params):
    shares = _parse_shares(args["shares"])
    if len({pid for pid, _ in shares}) != len(shares) or any(a <= 0 for _, a in shares) or sum(a for _, a in shares) > to_fen(args["total"]):
        raise BankError("BAD_SHARES", "The shares must be positive, belong to different people and not exceed the bill")
    for pid, _ in shares:
        bank.get_payee(cid, pid)                                  # every person must be one of the customer's own saved payees
    return {}


def _aa_card(bank, cid, args):
    shares = _parse_shares(args["shares"])
    people = ", ".join(f"{bank.get_payee(cid, pid)['name']} {cny(a)}" for pid, a in shares)
    return {"action": "Send AA payment requests", "total_cny": args["total"], "people": people, "note": args.get("note", ""),
            "effect": "Each person is asked for their share. No money leaves your account."}


def _aa_bind(bank, cid, args):
    return {"payees": [(pid, _fp(bank.get_payee(cid, pid)["no"])) for pid, _ in _parse_shares(args["shares"])]}


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
                 untrusted_outputs=("name",),
                 card_fn=lambda b, c, a: {"action": "View subscriptions",
                                          "effect": "Read-only: lists your subscriptions, amounts and next charge dates"}),
        ToolSpec("resolve_subscription", "Fuzzy-match one of the customer's subscriptions by name; ids and names only.",
                 {"query": {"type": "string", "required": True, "maxlen": 60},
                  "action": {"type": "string", "enum": ["pause", "cancel", "resume"]}},
                 lambda b, c, a, k: {"candidates": b.find_subscriptions(c, a["query"], a.get("action"))},
                 untrusted_outputs=("name",)),
        ToolSpec("list_products", "Wealth product catalogue with the customer's risk profile and suitability flags.", {},
                 lambda b, c, a, k: b.list_products(c)),
        ToolSpec("resolve_product", "Fuzzy-match a wealth product by name; ids and names only.",
                 {"query": {"type": "string", "required": True, "maxlen": 60}},
                 lambda b, c, a, k: {"candidates": b.find_products(c, a["query"])}),
        ToolSpec("get_holdings", "The customer's wealth holdings.", {},
                 lambda b, c, a, k: {"holdings": b.get_holdings(c)}),
        ToolSpec("buy_product", "Buy a wealth product (amount from the user's own message).",
                 {"product_id": {"type": "string", "required": True, "pattern": r"F\d{3}"},
                  "amount": {"type": "money", "required": True}},
                 lambda b, c, a, k: b.buy_product(c, a["product_id"], to_fen(a["amount"]), k),
                 state_changing=True, card_fn=_product_card("Buy product")),
        ToolSpec("redeem_product", "Redeem (sell) part or all of a wealth holding.",
                 {"product_id": {"type": "string", "required": True, "pattern": r"F\d{3}"},
                  "amount": {"type": "money", "required": True}},
                 lambda b, c, a, k: b.redeem_product(c, a["product_id"], to_fen(a["amount"]), k),
                 state_changing=True, card_fn=_product_card("Redeem product")),
        ToolSpec("schedule_transfer", "Schedule a transfer to a saved payee on a future date (one-time).",
                 {"payee_id": {"type": "string", "required": True, "pattern": PID},
                  "amount": {"type": "money", "required": True},
                  "date": {"type": "date", "required": True},
                  "memo": {"type": "string", "maxlen": 50}},
                 lambda b, c, a, k: b.schedule_transfer(c, a["payee_id"], to_fen(a["amount"]), a["date"], a.get("memo", ""), k),
                 state_changing=True, context_fn=_schedule_ctx, card_fn=_schedule_card, bind_fn=_transfer_bind),
        ToolSpec("list_scheduled", "List the customer's scheduled transfers.", {"status": {"type": "string", "enum": ["pending", "executed", "cancelled", "failed"]}},
                 lambda b, c, a, k: {"scheduled": b.list_scheduled(c, a.get("status"))}, untrusted_outputs=("payee_name",)),
        ToolSpec("cancel_scheduled", "Cancel a pending scheduled transfer.",
                 {"schedule_id": {"type": "string", "required": True, "pattern": r"X\d{3}"}},
                 lambda b, c, a, k: b.cancel_scheduled(c, a["schedule_id"], k), state_changing=True,
                 card_fn=_cancel_sched_card, bind_fn=_cancel_sched_bind),
        ToolSpec("create_aa_requests", "Ask friends for their share of a bill (shares like 'P002:120.00,P004:120.00').",
                 {"shares": {"type": "string", "required": True, "pattern": r"P\d{3}:\d{1,7}\.\d{2}(,P\d{3}:\d{1,7}\.\d{2}){0,4}", "maxlen": 120},
                  "total": {"type": "money", "required": True},
                  "note": {"type": "string", "maxlen": 50}},
                 lambda b, c, a, k: b.create_aa_requests(c, _parse_shares(a["shares"]), to_fen(a["total"]), a.get("note", ""), k),
                 state_changing=True, context_fn=_aa_ctx, card_fn=_aa_card, bind_fn=_aa_bind),
        ToolSpec("annual_report", "Spending by month and category for a calendar year, computed in code.",
                 {"year": {"type": "int", "required": True, "min": 2000, "max": 2100}},
                 lambda b, c, a, k: b.annual_summary(c, a["year"]), untrusted_outputs=("merchant",)),
        ToolSpec("transfer", "Transfer money to a saved payee (payee_id from resolve_payee).",
                 {"payee_id": {"type": "string", "required": True, "pattern": PID},
                  "amount": {"type": "money", "required": True},
                  "memo": {"type": "string", "maxlen": 50}},
                 lambda b, c, a, k: b.transfer(c, a["payee_id"], to_fen(a["amount"]), a.get("memo", ""), k),
                 state_changing=True, context_fn=_transfer_ctx, card_fn=_transfer_card),
        ToolSpec("pause_or_cancel_subscription", "Pause or cancel a subscription.",
                 {"subscription_id": {"type": "string", "required": True, "pattern": SID},
                  "action": {"type": "string", "required": True, "enum": ["pause", "cancel", "resume"]}},
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
    reg = {t.name: t for t in T}
    reg["transfer"].bind_fn = _transfer_bind
    reg["pause_or_cancel_subscription"].bind_fn = _sub_bind
    for name in ("freeze_card", "unfreeze_card", "report_card_lost", "set_card_limit"):
        reg[name].bind_fn = _card_bind
    return reg