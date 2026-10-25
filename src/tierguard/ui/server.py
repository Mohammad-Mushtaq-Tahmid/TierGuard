"""Demo web server (stdlib only). Serves the chat page and a tiny JSON API.
Binds to 127.0.0.1. Every page string is inserted as text by the browser, never as HTML."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tierguard.agent.dialogue import Dialogue
from tierguard.agent.gateway import Gateway, Session
from tierguard.bank.mock import Bank, cny
from tierguard.llm.adapter import CacheMiss

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
ACTION_TEXT = {"confirm": ("确认", "confirm"), "cancel": ("取消", "cancel"), "skip": ("跳过", "skip"), "human": ("转人工", "human")}
ACTIONS = set(ACTION_TEXT) | {"advance"}
MAX_BODY, MAX_SESSIONS, MAX_MSG = 8192, 50, 500


class RateLimiter:
    """Token bucket per client address: protects the agent (and the free model quota) from floods."""
    def __init__(self, capacity=20, per_sec=0.5, clock=time.monotonic):
        self.capacity, self.per_sec, self.clock = capacity, per_sec, clock
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            tokens, last = self._b.get(key, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - last) * self.per_sec)
            ok = tokens >= 1.0
            self._b[key] = (tokens - 1.0 if ok else tokens, now)
            if len(self._b) > 1000:
                self._b.pop(next(iter(self._b)))
            return ok


class LockedLLM:
    """The free tier allows one request at a time: serialise model calls."""
    def __init__(self, inner):
        self.inner, self._lock = inner, threading.Lock()
        self.model = getattr(inner, "model", "unknown")

    def chat(self, *a, **kw):
        with self._lock:
            return self.inner.chat(*a, **kw)


class Ctx:
    def __init__(self, llm):
        self.bank = Bank()
        self.gateway = Gateway(self.bank)
        self.session = Session("C001")
        self.dialogue = Dialogue(self.gateway, self.session, llm)
        self.seen = 0
        self.lock = threading.Lock()


def _brief(v, n=70):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(e: dict) -> dict:
    d, ev = e["data"], e["event"]
    tone, txt = "info", ev
    if ev == "nlu":
        m = d["model_output"]
        bits = [f"intent={m['intent']}"] + [f"{k}={m[k]}" for k in ("payee_query", "amount_text", "period", "target_query") if m.get(k)]
        txt = "Model filled the form: " + ", ".join(bits) + (" | " + ", ".join(d["notes"]) if d.get("notes") else "")
    elif ev == "request":
        txt = f"Tool request: {d['tool']}({_brief(d['args'])})" + (f" taint={d['taint']}" if d.get("taint") else "") + (" [with confirmation token]" if d.get("has_token") else "")
    elif ev == "decision":
        tone = d["tier"]
        txt = f"Policy: {d['tier'].upper()} -> {d['verdict']}" + (f" ({', '.join(d['reasons'])})" if d["reasons"] else "")
    elif ev == "plan_approved":
        tone, txt = "yellow", f"Plan approved by you: {len(d['steps'])} steps, plan hash {d['plan_hash'][:12]}"
    elif ev == "plan_read_approved":
        tone, txt = "yellow", f"Plan approval covers this read-only step: {d['tool']} ({d['tier']})"
    elif ev == "plan_cancelled":
        tone, txt = "yellow", "Plan cancelled: remaining steps will not run"
    elif ev == "scheduled_executed":
        tone, txt = "green", f"Scheduled transfer {d['schedule_id']} executed (authorised as {d['tier']})"
    elif ev == "scheduled_failed":
        tone, txt = "red", f"Scheduled transfer {d['schedule_id']} NOT executed: {d['reason']}"
    elif ev == "human_handoff":
        tone, txt = "red", "Handed to a human: automatic actions paused"
    elif ev == "confirmation_issued":
        tone, txt = "yellow", f"Confirmation card issued, bound to action hash {d['action_hash'][:12]}" + (" + SMS step-up" if d["step_up"] else "")
    elif ev == "confirmation_redeemed":
        tone, txt = "green", f"Confirmation accepted: hash matches ({d['action_hash'][:12]})"
    elif ev == "confirmation_rejected":
        tone, txt = "red", f"Confirmation REJECTED: {d['result']}"
    elif ev == "executed":
        tone, txt = "green", f"Executed {d['tool']}"
    elif ev == "denied":
        tone, txt = "red", f"DENIED {d['tool']}: {d['code']}"
    elif ev == "error":
        tone, txt = "red", f"Error in {d['tool']}: {d['code']}"
    elif ev == "security_lock":
        tone, txt = "red", "SECURITY LOCK: session frozen after repeated suspicious failures"
    elif ev == "policy_loaded":
        tone, txt = "info", f"Policy loaded: tiers.yaml sha256 {d['sha256'][:12]}…, {len(d['tools'])} tools registered"
    elif ev == "input_sanitized":
        tone, txt = "yellow", f"Input sanitised: {d['invisible_chars']} invisible/control character(s) removed"
    elif ev == "rate_limited":
        tone, txt = "red", f"Rate limit hit ({d['limit']} messages per minute)"
    elif ev == "llm_error":
        tone, txt = "red", f"Language model unavailable ({d['type']})"
    return {"seq": e["seq"], "event": ev, "text": txt, "tone": tone}


def snapshot(c: Ctx) -> dict:
    b, cid = c.bank, c.session.customer_id
    return {"balance_cny": b.get_balance(cid)["balance_cny"], "daily_transfer_cny": cny(b.daily_transfer_total_fen(cid)),
            "yellow_limit_cny": "1000.00", "cards": b.list_cards(cid),
            "subscriptions": b.list_subscriptions(cid), "locked": c.session.locked,
            "audit_len": len(c.gateway.audit.entries), "audit_ok": c.gateway.audit.verify()[0],
            "date": b.today.isoformat(), "scheduled_pending": len([1 for x in b.scheduled.values() if x["customer_id"] == cid and x["status"] == "pending"])}


class App:
    def __init__(self, llm, mode="custom", demo_sms=True):
        self.llm, self.mode, self.demo_sms = LockedLLM(llm), mode, demo_sms
        self.allowed_hosts: set[str] = set()
        self.sessions: dict[str, Ctx] = {}
        self._lock = threading.Lock()

    def ctx(self, sid: str | None):
        with self._lock:
            if sid not in self.sessions:
                if len(self.sessions) >= MAX_SESSIONS:
                    self.sessions.pop(next(iter(self.sessions)))
                sid = secrets.token_hex(16)
                self.sessions[sid] = Ctx(self.llm)
            return sid, self.sessions[sid]

    def reset(self, sid):
        with self._lock:
            sid = sid if sid in self.sessions else secrets.token_hex(16)
            self.sessions[sid] = Ctx(self.llm)
            return sid, self.sessions[sid]

    def events_since(self, c: Ctx):
        ents = c.gateway.audit.entries[c.seen:]
        c.seen = len(c.gateway.audit.entries)
        return [summarize(e) for e in ents]

    def chat(self, sid, text=None, action=None):
        sid, c = self.ctx(sid)
        with c.lock:
            d = c.dialogue
            if action == "advance":               # demo clock: one day passes, due scheduled transfers are re-checked and run
                from tierguard.agent.features import advance_message
                c.bank.advance_days(1)
                results = c.gateway.run_scheduled(c.session)
                reply = {"kind": "answer", "message": advance_message(results, c.bank.today, d.lang), "card": None, "awaiting_otp": False}
                return {"sid": sid, "reply": reply, "plan": d.plan_view, "events": self.events_since(c), "state": snapshot(c),
                        "sms": None, "mode": self.mode}, 200
            if action in ACTION_TEXT:
                text = ACTION_TEXT[action][0 if d.lang == "zh" else 1]
            text = (text or "").strip()[:MAX_MSG]
            if not text:
                return {"sid": sid, "error": "empty message"}, 400
            try:
                r = d.handle(text)
            except Exception as ex:   # model unreachable / rate-limited: never crash the demo
                c.gateway.audit.append(c.session.session_id, "llm_error", {"type": type(ex).__name__})
                if isinstance(ex, CacheMiss):
                    msg = ("Offline replay mode: this sentence was not pre-recorded. Restart the server with TG_API_KEY set "
                           "to use the live model, or try one of the example buttons.")
                else:
                    msg = "The language model is unavailable right now (rate limit?). Please try again in a moment."
                reply = {"kind": "error", "message": msg, "card": None, "awaiting_otp": False}
            else:
                reply = {"kind": r.kind, "message": r.message, "card": r.card if r.kind == "confirm" else None,
                         "awaiting_otp": d.awaiting_otp}
            return {"sid": sid, "reply": reply, "plan": d.plan_view, "events": self.events_since(c), "state": snapshot(c),
                    "sms": d.demo_sms() if (d.awaiting_otp and self.demo_sms) else None, "mode": self.mode}, 200


def make_handler(app: App, limiter: RateLimiter):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, post=False) -> bool:
            """Reject DNS-rebinding (wrong Host), cross-site requests (foreign Origin), non-JSON posts and floods.
            Without this, any web page open in the browser could drive the local demo."""
            if (self.headers.get("Host") or "").lower() not in app.allowed_hosts:
                self._json({"error": "forbidden host"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in {"http://" + h for h in app.allowed_hosts}:
                self._json({"error": "forbidden origin"}, 403)
                return False
            if post:
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    self._json({"error": "content type must be application/json"}, 415)
                    return False
                if not limiter.allow(self.client_address[0]):
                    self._json({"error": "too many requests"}, 429)
                    return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._json({"error": "body too large"}, 413)
                return None
            try:
                obj = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(obj, dict):
                    raise ValueError
                return obj
            except ValueError:
                self._json({"error": "bad json"}, 400)
                return None

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            if u.path in FILES:
                name, ctype = FILES[u.path]
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            if u.path == "/api/state":
                sid, c = app.ctx((parse_qs(u.query).get("sid") or [None])[0])
                with c.lock:
                    return self._json({"sid": sid, "state": snapshot(c), "mode": app.mode, "events": app.events_since(c)})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._guard(post=True):
                return
            path = urlparse(self.path).path
            if path not in ("/api/chat", "/api/reset"):
                return self._json({"error": "not found"}, 404)
            body = self._body()
            if body is None:
                return
            sid = body.get("sid") if isinstance(body.get("sid"), str) else None
            if path == "/api/reset":
                sid, c = app.reset(sid)
                return self._json({"sid": sid, "state": snapshot(c), "mode": app.mode, "events": []})
            text = body.get("message") if isinstance(body.get("message"), str) else None
            action = body.get("action") if body.get("action") in ACTIONS else None
            out, code = app.chat(sid, text, action)
            self._json(out, code)
    return H


def make_server(llm, host="127.0.0.1", port=8000, mode="custom", demo_sms=True, rate=(20, 0.5)):
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise ValueError("refusing to bind a non-loopback address: this demo server has no authentication or TLS")
    app = App(llm, mode, demo_sms)
    srv = ThreadingHTTPServer((host, port), make_handler(app, RateLimiter(*rate)))
    srv.daemon_threads = True
    app.allowed_hosts = {f"127.0.0.1:{srv.server_port}", f"localhost:{srv.server_port}", f"[::1]:{srv.server_port}"}
    srv.app = app
    return srv


def build_llm():
    from tierguard.llm.adapter import OpenAICompatClient, ReplayCache
    root = Path(__file__).resolve().parents[3]
    mode = os.environ.get("TG_LLM_MODE") or ("record" if os.environ.get("TG_API_KEY") else "replay")
    os.environ.setdefault("TG_PROVIDER", "zai")
    os.environ.setdefault("TG_MODEL", "glm-4.7-flash")
    return ReplayCache(OpenAICompatClient.from_env(), str(root / "llm_cache"), mode), mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    llm, mode = build_llm()
    demo_sms = os.environ.get("TG_DEMO_SMS", "1") != "0"      # set TG_DEMO_SMS=0 to stop showing the code on screen
    srv = make_server(llm, port=a.port, mode=mode, demo_sms=demo_sms)
    print(f"TierGuard demo: http://127.0.0.1:{a.port}   (model mode: {mode})  Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()