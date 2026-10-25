"""Birthday cross-scenario plan: implemented in the next stage. Until then the agent says so honestly instead of guessing."""
from __future__ import annotations

from tierguard.agent.nlu import TurnResult, _say


def start_birthday_plan(gateway, session, lang, intent, utterance, notes):
    return TurnResult("clarify", _say(lang, "生日计划功能还没有开放。现在我可以帮您预约转账、AA 收款、管理银行卡和订阅，或者做“帮我省点钱”的计划。",
                                     "The birthday plan is not available yet. I can schedule transfers, split bills, manage cards and subscriptions, or run the savings plan."),
                      notes=notes)