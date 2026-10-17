"""Safe key check. Prints the SHAPE of TG_API_KEY (lengths only, never the key), then makes one tiny request.
Run from the project folder:  python scripts\\check_llm.py"""
import json
import os
import sys
import urllib.error
import urllib.request

key = os.environ.get("TG_API_KEY", "")
id_part, _, secret = key.partition(".")
print(f"key length: {len(key)}   (a Z.ai key is usually 49)")
print(f"id part: {len(id_part)}   (usually 32)    secret part: {len(secret)}   (usually 16)")

if not key:
    print("\nTG_API_KEY is empty. Copy the new key with its copy button, then run:")
    print('    $env:TG_API_KEY = (Get-Clipboard).Trim()')
    sys.exit(1)
if len(key) < 30 or "." not in key:
    print("\nThe key is too short or has no dot, so it is incomplete.")
    print("Make a new key on the API Keys page, click the COPY button in the box that shows it once")
    print("(the table only shows a shortened version), then run:")
    print('    $env:TG_API_KEY = (Get-Clipboard).Trim()')
    sys.exit(1)
if len(key) != 49:
    print("\nWarning: the length is unusual. Continuing anyway.")

base = os.environ.get("TG_BASE_URL", "https://api.z.ai/api/paas/v4").rstrip("/")
body = {"model": os.environ.get("TG_MODEL", "glm-4.7-flash"),
        "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
        "thinking": {"type": "disabled"}, "max_tokens": 50}
req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        print("\nOK, the key works. The model said:", json.loads(r.read())["choices"][0]["message"]["content"])
except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", "replace")[:300]
    print(f"\nHTTP {e.code}  {detail}")
    if e.code in (400, 401, 403):
        print("-> The provider did not accept the key. Make a new key and copy it with the copy button.")
    elif e.code == 429:
        print("-> Too many requests or daily quota used. Wait 10-15 minutes and try again.")
except Exception as e:
    print("\nCould not reach the provider:", type(e).__name__, str(e)[:200])