import json, os, urllib.request, urllib.error

body = {"model": "glm-4.7-flash",
        "messages": [{"role": "user", "content": "Say hello in one short sentence."}]}
req = urllib.request.Request(
    "https://api.z.ai/api/paas/v4/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["TG_API_KEY"]})
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        print(json.loads(r.read())["choices"][0]["message"])
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode("utf-8", "replace"))