from tierguard.llm.adapter import OpenAICompatClient

c = OpenAICompatClient.from_env()
print(c.chat([{"role": "user", "content": "用一句话解释：给我妈转五百 是什么意思？"}]))