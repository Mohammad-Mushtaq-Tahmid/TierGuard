"""One interface for every provider (OpenAI-compatible), plus record/replay so tests are
deterministic and offline. stdlib only. Verify base URLs and model names at sign-up time."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

PROVIDERS = {  # base_url values to be confirmed against each provider's current docs
    "zai": "https://api.z.ai/api/paas/v4",
    "modelscope": "https://api-inference.modelscope.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "local": "http://localhost:11434/v1",  # Ollama / llama.cpp server
}


class CacheMiss(Exception):
    pass


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120, retries: int = 4,
                 extra_body: dict | None = None, max_tokens: int | None = None, sleep=time.sleep):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.timeout, self.retries = timeout, retries
        self.extra_body, self.max_tokens, self._sleep = extra_body or {}, max_tokens, sleep

    @classmethod
    def from_env(cls):
        prov = os.environ.get("TG_PROVIDER", "local")
        extra = {}
        if prov == "zai" and os.environ.get("TG_THINKING", "off") != "on":
            extra["thinking"] = {"type": "disabled"}  # GLM thinks for ages otherwise; free tier = 1 request at a time
        return cls(os.environ.get("TG_BASE_URL", PROVIDERS[prov]), os.environ.get("TG_API_KEY", "none"),
                   os.environ.get("TG_MODEL", "qwen2.5:7b"), extra_body=extra,
                   max_tokens=int(os.environ.get("TG_MAX_TOKENS", "400")))

    def chat(self, messages: list[dict], *, temperature: float = 0.0, json_mode: bool = False) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature, **self.extra_body}
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]   # the provider's own explanation
                except Exception:
                    detail = ""
                last = RuntimeError(f"HTTP {e.code} {detail}".strip())
                if e.code == 429 or e.code in (500, 502, 503):
                    self._sleep([8, 20, 40, 60][min(attempt, 3)])  # free tier: wait for the open slot
                    continue
                raise last
            except (TimeoutError, urllib.error.URLError) as e:
                last = e
                self._sleep(20)  # the timed-out request may still hold the single free slot
        raise RuntimeError(f"LLM request failed after {self.retries} attempts: {last}")


class ReplayCache:
    """mode: 'replay' (miss = error, for CI), 'record' (hit else call+store), 'off'."""

    def __init__(self, inner, directory: str, mode: str = "replay"):
        self.inner, self.dir, self.mode = inner, Path(directory), mode
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key(self, messages, temperature, json_mode):
        model = getattr(self.inner, "model", "unknown")
        raw = json.dumps([model, messages, temperature, json_mode], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def chat(self, messages, *, temperature=0.0, json_mode=False) -> str:
        if self.mode == "off":
            return self.inner.chat(messages, temperature=temperature, json_mode=json_mode)
        f = self.dir / (self._key(messages, temperature, json_mode) + ".json")
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))["response"]
        if self.mode == "replay":
            raise CacheMiss(f"No recorded response for {f.name}")
        out = self.inner.chat(messages, temperature=temperature, json_mode=json_mode)
        f.write_text(json.dumps({"messages": messages, "response": out}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        return out


class ScriptedLLM:
    """Test double: returns canned replies in order."""
    model = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, temperature=0.0, json_mode=False):
        self.calls.append(messages)
        return self.replies.pop(0)