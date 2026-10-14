import json
import threading
import unittest
import urllib.error
import urllib.request

from tierguard.llm.adapter import ScriptedLLM
from tierguard.ui.server import make_server


def J(intent, payee=None, amount=None, period=None):
    return json.dumps({"intent": intent, "payee_query": payee, "amount_text": amount, "period": period})


class Srv:
    def __init__(self, replies):
        self.llm = ScriptedLLM(replies)
        self.srv = make_server(self.llm, port=0, mode="test")
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, dict(r.headers), r.read()

    def post(self, path, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.s = None

    def tearDown(self):
        if self.s:
            self.s.close()

    def test_static_files_and_security_headers(self):
        self.s = Srv([])
        st, h, body = self.s.get("/")
        self.assertEqual(st, 200)
        self.assertIn(b"TierGuard", body)
        self.assertEqual(h["Content-Security-Policy"], "default-src 'self'")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("javascript", self.s.get("/app.js")[1]["Content-Type"])
        self.assertIn("text/css", self.s.get("/style.css")[1]["Content-Type"])

    def test_unknown_and_traversal_paths_404(self):
        self.s = Srv([])
        for p in ("/nope", "/../server.py", "/static/../server.py", "/%2e%2e/server.py", "/api/other"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.s.get(p)
            self.assertEqual(cm.exception.code, 404, p)

    def test_state_creates_session(self):
        self.s = Srv([])
        _, _, body = self.s.get("/api/state")
        d = json.loads(body)
        self.assertTrue(d["sid"])
        self.assertEqual(d["state"]["balance_cny"], "12000.00")
        self.assertTrue(d["state"]["audit_ok"])

    def test_yellow_flow_over_http(self):
        self.s = Srv([J("transfer", "妈", "五百")])
        st, d = self.s.post("/api/chat", {"message": "给我妈转五百"})
        sid = d["sid"]
        self.assertEqual((st, d["reply"]["kind"], d["reply"]["card"]["tier"]), (200, "confirm", "yellow"))
        self.assertEqual(d["state"]["balance_cny"], "12000.00")                 # nothing moved yet
        tones = [e["tone"] for e in d["events"]]
        self.assertIn("yellow", tones)
        st, d2 = self.s.post("/api/chat", {"sid": sid, "action": "confirm"})
        self.assertEqual(d2["reply"]["kind"], "answer")
        self.assertIn("已转账", d2["reply"]["message"])                          # language kept: still Chinese
        self.assertEqual(d2["state"]["balance_cny"], "11500.00")
        self.assertTrue(d2["state"]["audit_ok"])
        self.assertTrue(any("[with confirmation token]" in e["text"] for e in d2["events"]))
        seqs = [e["seq"] for e in d["events"]] + [e["seq"] for e in d2["events"]]
        self.assertEqual(seqs, sorted(set(seqs)))                                # no duplicates, in order

    def test_red_flow_with_sms_over_http(self):
        self.s = Srv([J("transfer", "妈", "两千")])
        _, d = self.s.post("/api/chat", {"message": "转两千给我妈"})
        sid = d["sid"]
        self.assertEqual(d["reply"]["card"]["tier"], "red")
        self.assertIsNone(d["sms"])                                              # no code before the user confirms
        _, d2 = self.s.post("/api/chat", {"sid": sid, "action": "confirm"})
        self.assertTrue(d2["reply"]["awaiting_otp"])
        self.assertRegex(d2["sms"], r"^\d{6}$")
        self.assertEqual(d2["state"]["balance_cny"], "12000.00")
        _, d3 = self.s.post("/api/chat", {"sid": sid, "message": d2["sms"]})
        self.assertEqual(d3["reply"]["kind"], "answer")
        self.assertEqual(d3["state"]["balance_cny"], "10000.00")

    def test_cancel_action(self):
        self.s = Srv([J("transfer", "mom", "300")])
        _, d = self.s.post("/api/chat", {"message": "send 300 to mom"})
        _, d2 = self.s.post("/api/chat", {"sid": d["sid"], "action": "cancel"})
        self.assertIn("Cancelled", d2["reply"]["message"])
        self.assertEqual(d2["state"]["balance_cny"], "12000.00")

    def test_model_failure_does_not_crash_the_server(self):
        self.s = Srv([])                                                         # scripted LLM has no replies -> raises
        st, d = self.s.post("/api/chat", {"message": "查余额"})
        self.assertEqual((st, d["reply"]["kind"]), (200, "error"))
        self.assertIn("llm_error", [e["event"] for e in d["events"]])
        self.assertEqual(self.s.get("/api/state")[0], 200)

    def test_bad_requests(self):
        self.s = Srv([])
        self.assertEqual(self.s.post("/api/chat", raw=b"{not json")[0], 400)
        self.assertEqual(self.s.post("/api/chat", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.s.post("/api/chat", {"message": "   "})[0], 400)
        self.assertEqual(self.s.post("/api/chat", {"message": 123})[0], 400)
        self.assertEqual(self.s.post("/api/chat", {"action": "drop_tables"})[0], 400)
        st, _ = self.s.post("/api/chat", raw=b"x" * 9000)
        self.assertEqual(st, 413)

    def test_reset_and_session_isolation(self):
        self.s = Srv([J("transfer", "妈", "五百")])
        _, a = self.s.post("/api/chat", {"message": "给我妈转五百"})
        self.s.post("/api/chat", {"sid": a["sid"], "action": "confirm"})
        _, b = self.s.post("/api/reset", {})                                     # different session, fresh bank
        self.assertNotEqual(a["sid"], b["sid"])
        self.assertEqual(b["state"]["balance_cny"], "12000.00")
        _, a2 = self.s.post("/api/reset", {"sid": a["sid"]})
        self.assertEqual((a2["sid"], a2["state"]["balance_cny"], a2["events"]), (a["sid"], "12000.00", []))

    def test_unknown_session_id_gets_a_new_one(self):
        self.s = Srv([J("balance")])
        _, d = self.s.post("/api/chat", {"sid": "forged-id", "message": "余额"})
        self.assertNotEqual(d["sid"], "forged-id")


if __name__ == "__main__":
    unittest.main()