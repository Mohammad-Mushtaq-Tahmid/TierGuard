"""Live check: real LLM + our kernel. Run from the project folder with TG_API_KEY etc. set.
Nothing is ever executed here: transfers stop at the confirmation card."""
import time

from tierguard.agent.gateway import Gateway, Session
from tierguard.agent.nlu import plan_turn
from tierguard.bank.mock import Bank
from tierguard.llm.adapter import OpenAICompatClient, ReplayCache

UTTERANCES = [
    ("给我妈转五百", "Transfer 500 to my mom"),
    ("转一千二给王小明", "Transfer 1200 to Wang Xiaoming (two payees share the name)"),
    ("上个月花了多少，有没有异常", "How much did I spend last month, anything unusual?"),
    ("卡丢了先冻上", "Card lost, freeze it first"),
    ("我还剩多少钱", "How much money do I have left?"),
    ("给李华转3万", "Transfer 30,000 to Li Hua"),
]

# 'record' mode: answers are saved in llm_cache/, so re-running only asks the model for what is still missing
llm = ReplayCache(OpenAICompatClient.from_env(), "llm_cache", "record")
for text, gloss in UTTERANCES:
    g, s = Gateway(Bank()), Session("C001")
    t0 = time.time()
    print("=" * 70)
    print(f"USER : {text}   ({gloss})")
    try:
        r = plan_turn(g, s, llm, text)
    except Exception as e:  # network / rate limit
        print("ERROR:", repr(e)[:300])
        time.sleep(30)  # let the single free slot clear before the next message
        continue
    model_out = g.audit.events("nlu")[0]["data"]["model_output"]
    print("MODEL:", model_out)
    print(f"BOT  : [{r.kind}] {r.message}")
    if r.card:
        print(f"CARD : tier={r.card['tier']} step_up={r.card['needs_step_up']}")
    if r.notes:
        print("NOTES:", r.notes)
    print(f"({time.time() - t0:.1f}s)")
    time.sleep(6)  # free tier: one request at a time