from tierguard.agent.gateway import Gateway, Session
from tierguard.bank.mock import Bank


def fresh():
    bank = Bank()
    g = Gateway(bank)
    return bank, g, Session("C001")


def confirm(g, s, tool, args, **kw):
    """Run the full ask -> confirm (-> OTP) flow; returns (first_result, final_result)."""
    r1 = g.call(s, tool, args, **kw)
    assert r1.status == "needs_confirmation", r1
    otp = None
    if r1.card["needs_step_up"]:
        otp = [m["otp"] for m in g.conf.sms_outbox if m["token"] == r1.token][0]
    return r1, g.call(s, tool, args, token=r1.token, otp=otp, **kw)
