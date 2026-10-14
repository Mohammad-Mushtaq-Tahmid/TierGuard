# PROGRESS

## Session 1 (Oct 12, 2026)
### Done (all verified by tests: 56 unit/property tests + 12 benchmark cases)
- Repo skeleton, mock bank (seeded, 2 customers, duplicate payee, new payee, injection-laden memo, anomaly).
- Tool registry (14 tools) with strict validation and canonical money formatting.
- Policy engine driven by tiers.yaml; official green/yellow/red + black; escalation never relaxes (tested).
- Confirmation tokens: single-use, 120s TTL, bound to SHA-256 of canonical action, simulated OTP for red.
- Gateway: default-deny, idempotent retries, circuit breaker (session lock), hash-chained audit log.
- Grounding verifier (numbers in replies must come from tool results).
- LLM adapter (OpenAI-compatible, stdlib) + record/replay cache + scripted test double.
- Mutation check: 3 deliberate security bugs were each caught by the tests.
- Benchmark v0: 12 cases, runner writes eval/reports/latest.md.

### Decisions
- Core is stdlib + PyYAML only. Pydantic/FastAPI/Hypothesis added later on the dev machine.
- Organiser questions NOT SENT as of Oct 12; proceeding on own mock bank + own LLM behind adapters.
- list_subscriptions = green, freeze_card = yellow, set_card_limit raise = red / lower = yellow: ASSUMPTIONS to confirm.
- Business errors (insufficient funds) do not count toward the circuit breaker; security failures do.
- A token is consumed on redemption; if the bank stays down after 3 internal retries the user must re-confirm.

### Not done yet
- LLM sign-ups (nobody has an API key yet), planner/extractor prompts, dialogue manager (zh numerals, relations).
- Wealth tools, scheduled transfers, AA split, cross-scenario saga/rollback, interruption + human takeover.
- Chat UI + inspector panel, IM adapter, red-team suite (0 of 200), benchmark 12 of 40.
- Docs: architecture, threat model, security self-assessment, deck, demo video.

### Risks
- No LLM access yet: blocks all utterance-level testing. Top priority next session.
- Evaluation interface unknown; adapters keep the swap cheap.

## Organiser replies (received Oct 13)
1. Bank API: NOT provided. Build our own simulated bank. (Confirms the mock-bank decision.)
2. LLM: no access or credits provided. Overseas models ARE allowed. We pay for or get our own access.
3. English can be used (assume docs and defence at least; language of scripted tasks and attack prompts still unclear, keep zh + en tests).
4. Tier ambiguities (subscription query, wealth redemption, card limits): NOT ANSWERED. The reply numbered 4 was about the video.
5. Face recognition and SMS may be simulated, no strict requirements.
6. Demo video is REQUIRED (not optional). Clear, direct demonstration preferred.
Still unknown: how scripted tasks are fed to the agent and how task completion is scored; runtime limits; self-assessment template.

### Decisions after replies
- list_subscriptions changed green -> yellow (literal reading of official "subscription query and cancellation").
  Rationale: tiers are never relaxed below the official text. Cost: one extra confirmation on queries; mitigate later by
  confirming a whole read-only plan once.
- Wealth redemption stays red, card limit raise red / lower yellow, freeze yellow: still our assumptions.
- Demo video moves from "nice to have" to a deliverable with its own slot in the schedule (record by Oct 29).
- Step-up stays simulated SMS; add a simulated face-check method for the demo (visible in the video).
