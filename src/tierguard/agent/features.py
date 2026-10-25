"""Flows for the extended scenarios. Same rules as the rest of the agent: the model only fills a small form; amounts, dates,
people and every number come from the USER'S OWN WORDS and from real records; each state change gets its own confirmation card."""
from __future__ import annotations

import re
from decimal import Decimal

from tierguard.agent.dates import is_recurring, parse_when, strip_dates
from tierguard.agent.nlu import TurnResult, _confirm_turn, _grounded, _pname, _say, transfer_flow
from tierguard.agent.zh_numbers import extract_amounts, parse_cn

KIND_ZH = {"debit": "借记卡", "virtual": "虚拟卡"}
STATUS_ZH = {"active": "正常", "frozen": "已冻结", "lost": "已挂失"}
SCHED_ZH = {"pending": "待执行", "executed": "已执行", "cancelled": "已取消", "failed": "未能执行"}
FAIL_ZH = {"INSUFFICIENT_FUNDS": "余额不足", "LIMIT_NEEDS_RECONFIRM": "当天合计超过1000元需重新确认", "NOT_FOUND": "收款人已不存在",
           "NOT_AUTHORISED": "缺少确认记录"}


def _err(lang, zh, en, **kw):
    return TurnResult("error", _say(lang, zh, en), **kw)


# ============================================================ cards
def card_apply(gateway, session, lang, notes):
    return _confirm_turn(gateway, session, "apply_virtual_card", {}, lang, notes)


def _card_label(c, lang):
    if lang == "zh":
        return f"{c['last4_masked']}（{KIND_ZH.get(c['kind'], c['kind'])}，{STATUS_ZH.get(c['status'], c['status'])}）"
    return f"{c['last4_masked']} ({c['kind']}, {c['status']})"


def card_action(gateway, session, lang, kind, card_id, amount, notes):
    tool = {"lost": "report_card_lost", "freeze": "freeze_card", "unfreeze": "unfreeze_card", "limit": "set_card_limit"}[kind]
    args = {"card_id": card_id}
    if kind == "limit":
        args["daily_limit"] = f"{amount:.2f}"
    return _confirm_turn(gateway, session, tool, args, lang, notes)


def card_flow(gateway, session, lang, kind, utterance, notes, amount=None):
    rc = gateway.call(session, "get_cards", {})
    if rc.status != "executed":
        return _err(lang, "暂时无法读取您的银行卡。", "I could not read your cards just now.", notes=notes)
    ok_status = {"lost": ("active", "frozen"), "freeze": ("active",), "unfreeze": ("frozen",), "limit": ("active", "frozen")}[kind]
    cand = [c for c in rc.data["cards"] if c["status"] in ok_status]
    digits = re.findall(r"(?<!\d)(\d{4})(?!\d)", strip_dates(utterance))
    if digits:
        narrowed = [c for c in cand if any(c["last4_masked"].endswith(d) for d in digits)]
        cand = narrowed or cand
    if not cand:
        zh = {"lost": "您没有可以挂失的卡。", "freeze": "没有可以冻结的卡（可能已经冻结）。", "unfreeze": "您没有被冻结的卡，无需解冻。",
              "limit": "没有可以调整限额的卡。"}[kind]
        en = {"lost": "You have no card that can be reported lost.", "freeze": "No card can be frozen (it may already be frozen).",
              "unfreeze": "You have no frozen card, so there is nothing to unfreeze.", "limit": "No card's limit can be changed."}[kind]
        return TurnResult("refuse", _say(lang, zh, en), notes=notes)
    if len(cand) > 1:
        opts = "；".join(f"{i}. {_card_label(c, lang)}" for i, c in enumerate(cand, 1)) if lang == "zh" else \
               "; ".join(f"{i}. {_card_label(c, lang)}" for i, c in enumerate(cand, 1))
        return TurnResult("clarify", _say(lang, f"您有多张卡，请选择（回复序号或卡号后四位）：{opts}",
                                         f"You have several cards, which one (reply with the number or last 4 digits)? {opts}"), notes=notes,
                          clarify_state={"type": "card_choice", "kind": kind, "cards": [{"card_id": c["card_id"], "last4_masked": c["last4_masked"]} for c in cand],
                                         "amount": str(amount) if amount is not None else None})
    return card_action(gateway, session, lang, kind, cand[0]["card_id"], amount, notes)


def card_limit_flow(gateway, session, lang, utterance, notes):
    amounts = extract_amounts(strip_dates(utterance))
    if len(amounts) != 1:
        return TurnResult("clarify", _say(lang, "新的单日限额是多少元？（请只给一个金额）", "What should the new daily limit be? (one amount please)"),
                          notes=notes, clarify_state={"type": "need_limit", "utterance": utterance})
    return card_limit_amount(gateway, session, lang, utterance, amounts[0], notes)


def card_limit_amount(gateway, session, lang, utterance, amount, notes):
    cap = Decimal(gateway.bank.MAX_DAILY_LIMIT_FEN) / 100
    if amount <= 0 or amount > cap:
        return TurnResult("refuse", _say(lang, f"单日限额必须大于 0 且不超过 {cap:.2f} 元。", f"The daily limit must be above 0 and at most {cap:.2f} CNY."), notes=notes)
    return card_flow(gateway, session, lang, "limit", utterance, notes, amount=amount)


# ============================================================ scheduled transfers
def schedule_flow(gateway, session, lang, intent, utterance, notes):
    if is_recurring(utterance):
        return TurnResult("clarify", _say(lang, "目前只支持一次性的预约转账（指定一个未来日期）。要设置哪一天？",
                                         "I can only schedule a one-time transfer on a specific future date. Which date?"), notes=notes)
    today = gateway.bank.today
    dates = parse_when(utterance, today)
    if len(dates) != 1:
        return TurnResult("clarify", _say(lang, "请告诉我一个具体日期，例如“明天”“下周五”“10月28号”。",
                                         "Please give me one specific date, for example 'tomorrow', 'next Friday' or 'Oct 28'."), notes=notes)
    d = dates[0]
    if d <= today:
        return TurnResult("clarify", _say(lang, "预约需要明天或更晚的日期；今天的转账请直接说“转账”。",
                                         "A scheduled transfer needs tomorrow or a later date; for today just ask for the transfer."), notes=notes)
    if (d - today).days > 365:
        return TurnResult("clarify", _say(lang, "预约日期最多支持一年以内。", "I can schedule at most one year ahead."), notes=notes)
    amounts = extract_amounts(strip_dates(utterance))
    if len(amounts) != 1:
        return TurnResult("clarify", _say(lang, "请告诉我具体转账金额（只转一笔）。", "Please tell me one specific amount to transfer."), notes=notes,
                          clarify_state={"type": "need_amount", "payee_query": intent["payee_query"], "heard": utterance, "date": d.isoformat()}
                          if (not amounts and intent["payee_query"]) else None)
    if not intent["payee_query"]:
        return TurnResult("clarify", _say(lang, "要转给谁？", "Who should I send it to?"), notes=notes,
                          clarify_state={"type": "need_payee", "amount": str(amounts[0]), "date": d.isoformat()})
    return transfer_flow(gateway, session, lang, intent["payee_query"], amounts[0], notes, heard=utterance, date=d.isoformat())


def schedule_list(gateway, session, lang, notes):
    r = gateway.call(session, "list_scheduled", {})
    if r.status != "executed":
        return _err(lang, "暂时无法读取预约转账。", "I could not read your scheduled transfers.", notes=notes)
    items = r.data["scheduled"]
    if not items:
        return TurnResult("answer", _say(lang, "您目前没有预约转账。", "You have no scheduled transfers."), notes=notes)
    if lang == "zh":
        lines = [f"· {x['date']}：转给 {x['payee_name']} {x['amount_cny']} 元（{SCHED_ZH.get(x['status'], x['status'])}" +
                 (f"，原因：{FAIL_ZH.get(x['detail'], x['detail'])}" if x["status"] == "failed" else "") + "）" for x in items]
        msg = f"您共有 {len(items)} 笔预约转账：\n" + "\n".join(lines)
    else:
        lines = [f"- {x['date']}: {x['amount_cny']} CNY to {x['payee_name']} ({x['status']})" for x in items]
        msg = f"You have {len(items)} scheduled transfers:\n" + "\n".join(lines)
    return TurnResult("answer", _grounded(msg, [r.data], "", notes, derived=[len(items)]), notes=notes)


def cancel_scheduled_card(gateway, session, lang, schedule_id, notes):
    return _confirm_turn(gateway, session, "cancel_scheduled", {"schedule_id": schedule_id}, lang, notes)


def schedule_cancel_flow(gateway, session, lang, utterance, notes):
    r = gateway.call(session, "list_scheduled", {"status": "pending"})
    items = (r.data or {}).get("scheduled", []) if r.status == "executed" else []
    if not items:
        return TurnResult("refuse", _say(lang, "您没有待执行的预约转账。", "You have no pending scheduled transfers."), notes=notes)
    pick = items
    dates = {d.isoformat() for d in parse_when(utterance, gateway.bank.today)}
    if dates:
        pick = [x for x in pick if x["date"] in dates] or pick
    named = [x for x in pick if x["payee_name"].casefold() in utterance.casefold()]
    pick = named or pick
    if len(pick) == 1:
        return cancel_scheduled_card(gateway, session, lang, pick[0]["schedule_id"], notes)
    opts = "；".join(f"{i}. {x['date']} {x['payee_name']} {x['amount_cny']} 元" for i, x in enumerate(pick, 1)) if lang == "zh" else \
           "; ".join(f"{i}. {x['date']} {x['payee_name']} {x['amount_cny']} CNY" for i, x in enumerate(pick, 1))
    return TurnResult("clarify", _say(lang, f"有多笔预约转账，要取消哪一笔？（回复序号）{opts}", f"Several scheduled transfers: which one to cancel? (reply with the number) {opts}"),
                      notes=notes, clarify_state={"type": "sched_choice", "items": [x["schedule_id"] for x in pick]})


def advance_message(results, today, lang):
    """The reply after the demo clock moves one day (no model involved)."""
    head = _say(lang, f"已进入 {today.isoformat()}。", f"It is now {today.isoformat()}. ")
    if not results:
        return head + _say(lang, "今天没有到期的预约转账。", "No scheduled transfer was due today.")
    parts = []
    for x in results:
        if x["status"] == "executed":
            parts.append(_say(lang, f"已执行：转给 {x['payee_name']} {x['amount_cny']} 元", f"Executed: {x['amount_cny']} CNY to {x['payee_name']}"))
        else:
            parts.append(_say(lang, f"未执行（{FAIL_ZH.get(x['reason'], x['reason'])}）：{x['amount_fen'] / 100:.2f} 元，需要您重新确认",
                              f"Not executed ({x['reason']}): {x['amount_fen'] / 100:.2f} CNY, please confirm again"))
    return head + ("；" if lang == "zh" else "; ").join(parts) + "。"


# ============================================================ AA splitting
_GROUP = re.compile(r"(\d{1,2}|[二两三四五六])\s*个?\s*(?:人|位)")


def _group_size(text):
    m = _GROUP.search(text)
    if not m:
        return None
    tok = m.group(1)
    n = int(tok) if tok.isdigit() else (int(parse_cn(tok)) if parse_cn(tok) else None)
    return n if n and 2 <= n <= 6 else None


def split_even(total_fen: int, n: int) -> tuple[int, int]:
    """(share for each ordinary participant, extra fen carried by the person who paid). base*n + extra == total."""
    base = total_fen // n
    return base, total_fen - base * n


def _resolve_names(gateway, session, lang, names, notes):
    """Each name must resolve to exactly one saved payee. Returns (payees, None) or (None, clarify TurnResult)."""
    out = []
    for nm in names:
        rp = gateway.call(session, "resolve_payee", {"query": nm})
        cands = (rp.data or {}).get("candidates", []) if rp.status == "executed" else []
        if not cands:
            return None, TurnResult("clarify", _say(lang, f"没有找到联系人“{nm}”，请确认姓名。", f"I could not find '{nm}' in your saved payees."), notes=notes)
        if len(cands) > 1 and cands[0]["score"] == cands[1]["score"]:
            return None, TurnResult("clarify", _say(lang, f"“{nm}”有多位匹配，请说得更具体一些（例如带上尾号）。",
                                                  f"Several payees match '{nm}'. Please be more specific (for example add the last 4 digits)."), notes=notes)
        if cands[0]["payee_id"] not in [p["payee_id"] for p in out]:
            out.append(cands[0])
    return out, None


def aa_flow(gateway, session, lang, it, intent, utterance, notes):
    amounts = extract_amounts(strip_dates(utterance))
    if len(amounts) != 1:
        return TurnResult("clarify", _say(lang, "这笔账一共多少钱？（请只给一个总额）", "What was the bill total? (one amount please)"), notes=notes)
    total = amounts[0]
    total_fen = int(total * 100)
    low = utterance.casefold()
    names = [n for n in intent.get("names", []) if n.casefold() in low]          # only people the user actually named
    if it == "aa_collect":
        if not names:
            return TurnResult("clarify", _say(lang, "要向谁收款？请告诉我他们的名字。", "Who should I ask for their share? Please give me their names."), notes=notes)
        people, bad = _resolve_names(gateway, session, lang, names, notes)
        if bad:
            return bad
        n = 1 + len(people)
        if n > 6:
            return TurnResult("clarify", _say(lang, "最多支持 6 人（含您自己）。", "At most 6 people including you."), notes=notes)
        base, extra = split_even(total_fen, n)
        if base < 1:
            return TurnResult("clarify", _say(lang, "金额太小，无法分摊。", "The amount is too small to split."), notes=notes)
        shares = ",".join(f"{p['payee_id']}:{base / 100:.2f}" for p in people)
        info = _say(lang, f"总额 {total:.2f} 元，{n} 人均摊，每位朋友 {base / 100:.2f} 元（您自己的一份 {(base + extra) / 100:.2f} 元）。",
                    f"Bill {total:.2f} CNY split {n} ways: each friend owes {base / 100:.2f} CNY (your own share is {(base + extra) / 100:.2f} CNY).")
        r = _confirm_turn(gateway, session, "create_aa_requests", {"shares": shares, "total": f"{total:.2f}", "note": "AA"}, lang, notes)
        if r.kind == "confirm":
            r.message = _grounded(info, [], utterance, notes, derived=[f"{total:.2f}", n, f"{base / 100:.2f}", f"{(base + extra) / 100:.2f}"]) + "\n" + r.message
        return r
    # aa_pay_share: someone else paid; the user pays their own share to that person
    payer = intent.get("payee_query")
    if not payer or payer.casefold() not in low:
        return TurnResult("clarify", _say(lang, "是谁先垫付的？", "Who paid the bill?"), notes=notes)
    others, bad = _resolve_names(gateway, session, lang, [n for n in names if n.casefold() != payer.casefold()], notes)
    if bad:
        return bad
    n = _group_size(utterance) or (2 + len(others))
    base, _extra = split_even(total_fen, n)
    if base < 1:
        return TurnResult("clarify", _say(lang, "金额太小，无法分摊。", "The amount is too small to split."), notes=notes)
    info = _say(lang, f"总额 {total:.2f} 元，{n} 人均摊，您这一份是 {base / 100:.2f} 元。", f"Bill {total:.2f} CNY split {n} ways: your share is {base / 100:.2f} CNY.")
    r = transfer_flow(gateway, session, lang, payer, Decimal(base) / 100, notes, heard=utterance, memo="AA")
    if r.kind in ("confirm", "clarify"):
        r.message = _grounded(info, [], utterance, notes, derived=[f"{total:.2f}", n, f"{base / 100:.2f}"]) + "\n" + r.message
    return r


# ============================================================ annual report
def annual_flow(gateway, session, lang, utterance, notes):
    today = gateway.bank.today
    low = utterance.casefold()
    m = re.search(r"(20\d{2})\s*年?", low)
    year = int(m.group(1)) if m else today.year - 1 if ("去年" in low or "last year" in low) else today.year
    r = gateway.call(session, "annual_report", {"year": year})
    if r.status != "executed":
        return _err(lang, "暂时无法生成年度账单。", "I could not build the annual report just now.", notes=notes)
    d = r.data
    if not d["months"]:
        return TurnResult("answer", _say(lang, f"{year} 年没有消费记录。", f"There are no spending records for {year}."), notes=notes)
    top = list(d["by_category"].items())[:3]
    cat = lambda k: {"dining": "餐饮", "transport": "交通", "shopping": "购物", "utilities": "水电煤", "subscription": "订阅"}.get(k, k)
    if lang == "zh":
        lines = [f"· {x['month']}：{x['total_cny']} 元（{x['count']} 笔）" for x in d["months"]]
        msg = (f"{year} 年共支出 {d['total_cny']} 元（{d['months_with_data']} 个月有记录，月均 {d['average_month_cny']} 元）。"
               f"支出最多的是 {d['busiest_month']['month']}（{d['busiest_month']['total_cny']} 元）。主要类别：" +
               "、".join(f"{cat(k)} {v['total_cny']} 元" for k, v in top) + "。\n" + "\n".join(lines) +
               (f"\n发现 {len(d['anomalies'])} 笔异常大额支出（例如 {d['anomalies'][0]['date']} {cat(d['anomalies'][0]['category'])} {d['anomalies'][0]['amount_cny']} 元）。" if d["anomalies"] else ""))
    else:
        lines = [f"- {x['month']}: {x['total_cny']} CNY ({x['count']} payments)" for x in d["months"]]
        msg = (f"In {year} you spent {d['total_cny']} CNY ({d['months_with_data']} months with data, {d['average_month_cny']} CNY a month on average). "
               f"The busiest month was {d['busiest_month']['month']} ({d['busiest_month']['total_cny']} CNY). Top categories: " +
               ", ".join(f"{k} {v['total_cny']} CNY" for k, v in top) + ".\n" + "\n".join(lines) +
               (f"\n{len(d['anomalies'])} unusually large payment(s) found (for example {d['anomalies'][0]['date']}, {d['anomalies'][0]['amount_cny']} CNY)." if d["anomalies"] else ""))
    return TurnResult("answer", _grounded(msg, [d], "", notes, derived=[year, d["months_with_data"], len(d["anomalies"])]), notes=notes)


# ============================================================ wealth product comparison
def compare_flow(gateway, session, lang, intent, utterance, notes):
    low = utterance.casefold()
    names = [n for n in intent.get("names", []) if n.casefold() in low]
    if len(names) < 2:
        return TurnResult("clarify", _say(lang, "您想对比哪几个产品？（例如：货币基金和短债基金）", "Which products should I compare? (for example: money market and short bond)"), notes=notes)
    ids = []
    for nm in names:
        rp = gateway.call(session, "resolve_product", {"query": nm})
        cands = (rp.data or {}).get("candidates", []) if rp.status == "executed" else []
        if not cands:
            return TurnResult("clarify", _say(lang, f"没有找到产品“{nm}”。", f"I could not find a product matching '{nm}'."), notes=notes)
        if len(cands) > 1 and cands[0]["score"] == cands[1]["score"]:
            return TurnResult("clarify", _say(lang, f"“{nm}”有多个匹配，请说得更具体一些。", f"'{nm}' matches several products; please be more specific."), notes=notes)
        if cands[0]["product_id"] not in ids:
            ids.append(cands[0]["product_id"])
    if len(ids) < 2:
        return TurnResult("clarify", _say(lang, "请给出两个不同的产品来对比。", "Please name two different products."), notes=notes)
    lp = gateway.call(session, "list_products", {})
    if lp.status != "executed":
        return _err(lang, "暂时无法读取产品信息。", "I could not read the product list.", notes=notes)
    prods = [p for p in lp.data["products"] if p["product_id"] in ids]
    y = lambda p: Decimal(p["ref_yield_percent"])
    mn = lambda p: Decimal(p["min_amount_cny"])
    hi_y = max(prods, key=y)
    lo_r = min(prods, key=lambda p: int(p["risk"][1:]))
    lo_m = min(prods, key=mn)
    ok = [p for p in prods if p["suitable"]]
    if lang == "zh":
        lines = [f"· {p['name']}：{p['risk_zh']} {p['risk']}，起购 {p['min_amount_cny']} 元，参考七日年化 {p['ref_yield_percent']}%（历史数据），{p['redemption']} 赎回，"
                 f"{'适合您' if p['suitable'] else '超出您的风险等级'}" for p in prods]
        notes_ = [f"参考收益较高：{hi_y['name']}（{hi_y['ref_yield_percent']}%）", f"风险较低：{lo_r['name']}（{lo_r['risk']}）", f"起购较低：{lo_m['name']}（{lo_m['min_amount_cny']} 元）"]
        msg = f"为您对比（您的风险承受能力：{lp.data['profile_zh']} {lp.data['profile']}）：\n" + "\n".join(lines) + "\n" + "；".join(notes_) + \
              "。一般来说，参考收益更高的产品风险也更高。以上仅供参考，不构成收益承诺。"
    else:
        lines = [f"- {p['name_en']}: {p['risk_en']} {p['risk']}, minimum {p['min_amount_cny']} CNY, reference yield {p['ref_yield_percent']}% (historical), {p['redemption']} redemption, "
                 f"{'suits you' if p['suitable'] else 'above your risk level'}" for p in prods]
        msg = (f"Comparison (your risk profile: {lp.data['profile_en']} {lp.data['profile']}):\n" + "\n".join(lines) +
               f"\nHigher reference yield: {hi_y['name_en']} ({hi_y['ref_yield_percent']}%); lower risk: {lo_r['name_en']} ({lo_r['risk']}); "
               f"lower minimum: {lo_m['name_en']} ({lo_m['min_amount_cny']} CNY). Higher reference yield usually comes with higher risk. For information only: returns are not promised.")
    return TurnResult("answer", _grounded(msg, [lp.data], "", notes), notes=notes)


# ============================================================ dispatcher used by nlu.plan_turn
def dispatch(gateway, session, lang, it, intent, utterance, notes):
    if it == "card_apply":
        return card_apply(gateway, session, lang, notes)
    if it == "card_unlock":
        return card_flow(gateway, session, lang, "unfreeze", utterance, notes)
    if it == "card_limit":
        return card_limit_flow(gateway, session, lang, utterance, notes)
    if it == "schedule_transfer":
        return schedule_flow(gateway, session, lang, intent, utterance, notes)
    if it == "schedule_list":
        return schedule_list(gateway, session, lang, notes)
    if it == "schedule_cancel":
        return schedule_cancel_flow(gateway, session, lang, utterance, notes)
    if it in ("aa_collect", "aa_pay_share"):
        return aa_flow(gateway, session, lang, it, intent, utterance, notes)
    if it == "annual_report":
        return annual_flow(gateway, session, lang, utterance, notes)
    if it == "wealth_compare":
        return compare_flow(gateway, session, lang, intent, utterance, notes)
    return None