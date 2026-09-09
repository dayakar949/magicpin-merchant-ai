# magicpin Vera-style Merchant Engagement Bot

## Approach
A stateful FastAPI bot implementing the challenge contract. It stores pushed Category,
Merchant, Customer and Trigger contexts, routes by **trigger family**, composes a concise
context-grounded message, and applies post-composition validation. The public
`compose(category, merchant, trigger, customer=None)` function also matches the challenge
composer contract.

The design is generalized: it does not contain T01–T30 response strings. Instead, trigger
payload fields drive reusable handlers for planning, research, compliance, competitor,
seasonal, curiosity, performance, milestones, GBP, matches, renewals, customer recall,
win-back, and other families. Missing fields are acknowledged rather than guessed.

When `OPENAI_API_KEY` is available, conversational responses can use an LLM at temperature
0, constrained by the same context and validation rules. Without a key, deterministic
composition keeps the service runnable and reproducible.

## Judge-focused behavior
The composer prioritizes the five rubric dimensions: specificity, category fit, merchant
fit, trigger relevance, and engagement compulsion. It uses concrete numbers/dates/headlines
when supplied, category-appropriate tone, merchant language preferences, one clear CTA,
and effort-externalizing next steps. It never invents missing facts.

The replay layer additionally handles:
- repeated identical WhatsApp canned auto-replies;
- explicit "join/proceed/go ahead" intent by switching immediately to action mode;
- hard "not interested"/STOP exits;
- off-topic requests without abandoning the merchant-growth mission;
- exact-message anti-repetition.

## Run
```bash
pip install -r requirements.txt
export OPENAI_API_KEY="YOUR_KEY"
export OPENAI_MODEL="gpt-5-mini"
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then:
```bash
curl http://localhost:8080/v1/healthz
```

## Dataset
The project bundles the challenge seed data plus generated local data:
- 5 categories
- 50 merchants
- 200 customers
- 100 triggers
- 30 canonical test pairs

`build_submission.py` deterministically rebuilds `submission.jsonl` from the generated
dataset.

## Submission artifacts
- `bot.py`
- `submission.jsonl` — 30 JSONL records
- `dataset/` — source and generated challenge data
- `build_submission.py` — reproducible submission builder
- `README.md`
