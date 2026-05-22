# reefy-llm-proxy

Device-side OpenAI-compatible gateway that lets apps use **subscription-based
LLM accounts** (xAI Grok, OpenAI Codex) - fixed monthly cost, no
per-token billing surprises - through standard OpenAI client SDKs.

The proxy handles the OAuth subscription dance under the hood: it
stores credentials produced by a device-code OAuth flow, refreshes
access_tokens lazily when upstream returns 401, persists rotated
refresh_tokens to disk atomically, and exposes everything as a
vanilla OpenAI-compatible HTTP API. Apps don't see OAuth, refresh,
or rotation - they just point at `OPENAI_BASE_URL=http://...:9080/v1`
and use the OpenAI SDK they'd use anywhere else.

## Endpoints

- `POST /v1/chat/completions` (and any other `/v1/*` path) - forward
  to the upstream provider matched by the request's `model` field.
- `GET  /v1/models` - union of `/v1/models` from each attached
  provider, prefixed with the provider slug (e.g. `xai/grok-4`,
  `codex/gpt-4o`). Cached with a 24h TTL; falls back to the last
  successful fetch if upstream is down.
- `GET  /healthz` - liveness probe.
- `GET  /internal/stats` - request + token counters (per
  provider/model, prompt vs completion split), for polling
  collectors.

## Routing

| Providers configured | Request   | Result |
|---|---|---|
| 0 | (any) | 503 - no provider configured |
| 1 | (any) | route to the one provider |
| >1 | `xai/grok-4` | route to xAI by prefix |
| >1 | `gpt-4o` (bare) | resolved via dynamic registry |
| >1 | `unknown` (bare, unmatched) | 503 - prefix required |

## Configuration

All via env vars.

| Var | Default | Purpose |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Bind address |
| `LISTEN_PORT` | `9080` | Bind port |
| `DATA_DIR` | `/data` | credentials.json + models-cache.json location |
| `MODELS_CACHE_TTL_S` | `86400` | How long /v1/models cache is fresh |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Credentials file shape

`$DATA_DIR/credentials.json`, mode 0600. The proxy reads it on
startup and re-reads it after each refresh:

```json
{
  "providers": {
    "xai": {
      "provider": "xai",
      "label": "default",
      "access_token": "...",
      "refresh_token": "...",
      "expires_at": 1747000000,
      "scopes": ["openid", "..."],
      "last_refreshed_at": null
    },
    "codex": { ... }
  }
}
```

When upstream rotates the refresh_token (xAI and codex both do), the
new chain replaces the old one on disk before the retry fires.

## Local development

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p /tmp/reefy-llm-proxy-data
# place a credentials.json with valid tokens here for live testing

DATA_DIR=/tmp/reefy-llm-proxy-data \
LOG_LEVEL=DEBUG \
  python -m reefy_llm_proxy.main

# in another terminal:
curl http://localhost:9080/healthz
curl http://localhost:9080/v1/models
curl -X POST http://localhost:9080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "xai/grok-4-fast", "messages": [{"role":"user","content":"hi"}]}'
```

## Build

CI builds on push to `main` via `.github/workflows/build.yml`,
publishing `ghcr.io/reefyai/reefy-llm-proxy:sha-<short-sha>` and
`:latest`.
