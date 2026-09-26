# Vera challenge API

FastAPI service with deterministic, context-grounded messages and per-conversation reply handling. No external model or API key is needed. Context is held in memory for one judge run and cleared by `/v1/teardown`; run one worker and keep the service awake during the test.

## Run locally

```bash
python -m pip install -r requirements.txt
python -m uvicorn bot:app --host 0.0.0.0 --port 8080
```

Send versioned category, merchant, customer, and trigger payloads to `POST /v1/context`. Call `POST /v1/tick` with an ISO timestamp and active trigger IDs. Continue an action with `POST /v1/reply`. `GET /v1/healthz` and `GET /v1/metadata` provide operational information.

## Deploy

Connect this repository to Render as a Python web service. `render.yaml` provides the build, start, and health settings. Render assigns the public HTTPS base URL. Keep the service running with one worker throughout a judge run because state is in memory.

The main tradeoff is deterministic wording rather than LLM prose. Additional verified merchant ratings, offer prices, and trigger-specific source material would make messages more useful without fabricating details.
