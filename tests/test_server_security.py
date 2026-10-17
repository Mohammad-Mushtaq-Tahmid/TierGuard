import http.client
import json
import threading
import unittest

from tierguard.llm.adapter import ScriptedLLM
from tierguard.ui.server import RateLimiter, make_server


def J(intent, payee=None, amount=None):
    return json.dumps({"intent": intent, "payee_query": payee, "amount_text": amount, "period": None, "target_query": None})


class Srv:
    def __init__(self, replies=(), rate=(20, 0.5)):
        self.llm = ScriptedLLM(list(replies))
        self.srv = make_server(self.llm, port=0, mode="test", rate=rate)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"} if method == "POST" else {}
        h.update(headers or {})
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class WebSecurity(unittest.TestCase):
    def setUp(self):
        self.s = None

    def tearDown(self):
        if self.s:
            self.s.close()

    def test_dns_rebinding_host_is_refused(self):
        self.s = Srv()
        for m, p in (("GET", "/api/state"), ("GET", "/"), ("POST", "/api/chat")):
            st, _, _ = self.s.req(m, p, {"message": "hi"} if m == "POST" else None, {"Host": "evil.example"})
            self.assertEqual(st, 403, (m, p))
        st, _, _ = self.s.req("GET", "/api/state", headers={"Host": f"127.0.0.1.evil.example:{self.s.port}"})
        self.assertEqual(st, 403)

    def test_cross_origin_post_is_refused_and_does_nothing(self):
        self.s = Srv([J("transfer", "妈", "五百")])
        st, _, _ = self.s.req("POST", "/api/chat", {"message": "给我妈转五百"}, {"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertEqual(len(self.s.llm.replies), 1)                         # the model was never even called

    def test_classic_csrf_with_text_plain_is_refused(self):
        self.s = Srv([J("balance")])
        st, _, _ = self.s.req("POST", "/api/chat", b'{"message":"\xe4\xbd\x99\xe9\xa2\x9d"}', {"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        st, _, _ = self.s.req("POST", "/api/chat", b'message=x', {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 415)
        self.assertEqual(len(self.s.llm.replies), 1)                         # still unused

    def test_same_origin_post_is_accepted(self):
        self.s = Srv([J("balance")])
        st, _, body = self.s.req("POST", "/api/chat", {"message": "余额"}, {"Origin": f"http://localhost:{self.s.port}",
                                                                          "Host": f"localhost:{self.s.port}"})
        self.assertEqual(st, 200)

    def test_security_headers(self):
        self.s = Srv()
        _, h, _ = self.s.req("GET", "/")
        csp = h["Content-Security-Policy"]
        for part in ("default-src 'self'", "frame-ancestors 'none'", "form-action 'none'", "base-uri 'none'"):
            self.assertIn(part, csp)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["Referrer-Policy"], "no-referrer")
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", h)                   # no CORS: other sites cannot read responses

    def test_flood_gets_429(self):
        self.s = Srv(rate=(3, 0.0001))
        codes = [self.s.req("POST", "/api/chat", {"message": "  "})[0] for _ in range(5)]
        self.assertEqual(codes[:3], [400, 400, 400])
        self.assertEqual(codes[3:], [429, 429])

    def test_origin_check_is_case_insensitive_but_exact(self):
        self.s = Srv([J("balance")])
        ok = self.s.req("POST", "/api/chat", {"message": "余额"}, {"Origin": f"HTTP://LOCALHOST:{self.s.port}",
                                                                 "Host": f"localhost:{self.s.port}"})[0]
        self.assertEqual(ok, 200)
        for bad in ("null", f"http://localhost:{self.s.port}.evil.example", f"https://localhost:{self.s.port}", ""):
            self.assertEqual(self.s.req("POST", "/api/chat", {"message": "x"}, {"Origin": bad})[0], 403, bad)

    def test_server_refuses_public_bind(self):
        for host in ("0.0.0.0", "192.168.1.5", "8.8.8.8"):
            with self.assertRaises(ValueError):
                make_server(ScriptedLLM([]), host=host, port=0)
        make_server(ScriptedLLM([]), host="127.0.0.1", port=0).server_close()


class Limiter(unittest.TestCase):
    def test_refill_over_time(self):
        t = [0.0]
        rl = RateLimiter(2, 1.0, clock=lambda: t[0])
        self.assertTrue(rl.allow("a")); self.assertTrue(rl.allow("a")); self.assertFalse(rl.allow("a"))
        self.assertTrue(rl.allow("b"))                                       # other clients are independent
        t[0] += 1.0
        self.assertTrue(rl.allow("a")); self.assertFalse(rl.allow("a"))


if __name__ == "__main__":
    unittest.main()