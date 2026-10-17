import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tierguard.agent.grounding import verify_reply
from tierguard.llm.adapter import CacheMiss, OpenAICompatClient, ReplayCache, ScriptedLLM


class Grounding(unittest.TestCase):
    R = [{"balance_cny": "12000.00", "account_masked": "****2222"}]

    def test_grounded_reply_passes(self):
        self.assertEqual(verify_reply("Your balance is 12,000.00 CNY on account ****2222.", self.R)[0], True)

    def test_invented_number_blocked(self):
        ok, missing = verify_reply("Your balance is 13,500 CNY.", self.R)
        self.assertFalse(ok); self.assertEqual(missing, ["13500"])

    def test_user_supplied_numbers_allowed(self):
        self.assertTrue(verify_reply("I will transfer 500 to mom.", [], "给我妈转500")[0])

    def test_no_numbers_ok(self):
        self.assertTrue(verify_reply("Done.", [])[0])


class _Stub(BaseHTTPRequestHandler):
    hits = 0
    last_body = None
    def do_POST(self):
        _Stub.hits += 1
        _Stub.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass


class LLMAdapter(unittest.TestCase):
    def test_client_and_record_replay(self):
        srv = HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            client = OpenAICompatClient(f"http://127.0.0.1:{srv.server_port}/v1", "k", "m")
            with tempfile.TemporaryDirectory() as d:
                msgs = [{"role": "user", "content": "hi"}]
                with self.assertRaises(CacheMiss):
                    ReplayCache(client, d, "replay").chat(msgs)
                self.assertEqual(ReplayCache(client, d, "record").chat(msgs), "hello")
                before = _Stub.hits
                self.assertEqual(ReplayCache(client, d, "replay").chat(msgs), "hello")
                self.assertEqual(_Stub.hits, before)  # served from cache, no network
        finally:
            srv.shutdown()

    def test_zai_env_disables_thinking_and_limits_tokens(self):
        import os
        srv = HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = dict(os.environ)
        try:
            os.environ.update(TG_PROVIDER="zai", TG_BASE_URL=f"http://127.0.0.1:{srv.server_port}/v1",
                              TG_API_KEY="k", TG_MODEL="glm-4.7-flash")
            os.environ.pop("TG_THINKING", None)
            OpenAICompatClient.from_env().chat([{"role": "user", "content": "hi"}])
            self.assertEqual(_Stub.last_body["thinking"], {"type": "disabled"})
            self.assertEqual(_Stub.last_body["max_tokens"], 400)
        finally:
            os.environ.clear(); os.environ.update(old); srv.shutdown()

    def test_retries_on_429_then_succeeds(self):
        class Flaky(BaseHTTPRequestHandler):
            n = 0
            def do_POST(self):
                Flaky.n += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                if Flaky.n < 3:
                    self.send_response(429); self.send_header("Content-Length", "0"); self.end_headers(); return
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        waits = []
        try:
            c = OpenAICompatClient(f"http://127.0.0.1:{srv.server_port}/v1", "k", "m", sleep=waits.append)
            self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "ok")
            self.assertEqual(waits, [8, 20])
        finally:
            srv.shutdown()

    def _status_server(self, code, body):
        class H(BaseHTTPRequestHandler):
            hits = 0
            def do_POST(self):
                H.hits += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                b = json.dumps(body).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, H

    def test_rejected_key_fails_fast_with_the_providers_message(self):
        srv, H = self._status_server(401, {"error": {"message": "invalid api key"}})
        waits = []
        try:
            c = OpenAICompatClient(f"http://127.0.0.1:{srv.server_port}/v1", "k", "m", sleep=waits.append)
            with self.assertRaises(RuntimeError) as cm:
                c.chat([{"role": "user", "content": "x"}])
            self.assertIn("HTTP 401", str(cm.exception))
            self.assertIn("invalid api key", str(cm.exception))
            self.assertEqual((H.hits, waits), (1, []))                          # not retried, no waiting
        finally:
            srv.shutdown()

    def test_persistent_429_reports_the_reason_after_backing_off(self):
        srv, H = self._status_server(429, {"error": {"message": "daily quota used up"}})
        waits = []
        try:
            c = OpenAICompatClient(f"http://127.0.0.1:{srv.server_port}/v1", "k", "m", sleep=waits.append)
            with self.assertRaises(RuntimeError) as cm:
                c.chat([{"role": "user", "content": "x"}])
            self.assertIn("HTTP 429", str(cm.exception))
            self.assertIn("daily quota used up", str(cm.exception))
            self.assertEqual(waits, [8, 20, 40, 60])
        finally:
            srv.shutdown()

    def test_scripted(self):
        self.assertEqual(ScriptedLLM(["a", "b"]).chat([]), "a")


if __name__ == "__main__":
    unittest.main()