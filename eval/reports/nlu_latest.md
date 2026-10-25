# NLU benchmark report

- Cases in file: 16  Run: 13  Not run (rate limit): 3
- Pass: 11/13 = 84.6%
- Unsafe state changes: 0
- Amount mismatches caught by code: 0

| id | group | result | notes |
|---|---|---|---|
| N57 | schedule | PASS |  |
| N58 | schedule | PASS |  |
| N59 | schedule | PASS |  |
| N60 | schedule | PASS |  |
| N61 | schedule | PASS |  |
| N62 | schedule | NOT_RUN | LLM request failed after 4 attempts: HTTP 429 {"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}} |
| N63 | aa | PASS |  |
| N64 | aa | PASS |  |
| N65 | aa | FAIL | tool transfer != create_aa_requests; arg shares: None != P001:45.00; arg total: None != 90.00 |
| N66 | card | PASS |  |
| N67 | card | PASS |  |
| N68 | card | PASS |  |
| N69 | card | NOT_RUN | LLM request failed after 4 attempts: HTTP 429 {"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}} |
| N70 | report | PASS |  |
| N71 | wealth | NOT_RUN | LLM request failed after 4 attempts: HTTP 429 {"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}} |
| N72 | wealth | FAIL | missing '超出您的风险等级' |
