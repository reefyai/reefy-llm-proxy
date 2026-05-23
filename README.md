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

## Credential lifecycle

The proxy uses **two files** with disjoint writers:

| File | Writer | Role |
|---|---|---|
| `$DATA_DIR/credentials.json` | The user (or any external system you point at it) | "What was attached" snapshot. Holds the OAuth pair the user obtained via the provider's device-code flow. Proxy NEVER writes here. |
| `$DATA_DIR/credentials.runtime.json` | The proxy | Rotated state. Written after every successful OAuth refresh. Includes a `derived_from_attach` map (see below). |

Both files have mode `0600` and contain the same `providers` shape:

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

`credentials.runtime.json` carries one additional top-level field:

```json
{
  "providers": { ... },
  "derived_from_attach": {
    "xai":   "rt_originalAttachRefreshTokenForXai...",
    "codex": "rt_originalAttachRefreshTokenForCodex..."
  }
}
```

`derived_from_attach[slug]` records the `refresh_token` that was in
`credentials.json` at the moment this rotation chain began. The
proxy stamps it on every persist; it stays stable across multiple
rotations within the same chain.

### Why two files

The access_token expires (hours to days), so the proxy has to
refresh and persist new tokens. The attach file holds the
user-controlled origin (months-stable) and shouldn't be mutated by
the proxy. Splitting writers means any rewrite of the attach file
(e.g. routine config sync, an idempotent re-deploy of an
infrastructure script) does NOT clobber the proxy's rotated chain.

### Chain-merge rule on load

For each provider slug present in `credentials.json`:

| Runtime has slug? | `runtime.derived_from_attach[slug] == attach.refresh_token`? | Action |
|---|---|---|
| no | n/a | use attach (first run, or rotation has never happened yet) |
| yes | yes | use runtime (rotation chain is alive, anchored to this very attach token) |
| yes | no | use attach, treat runtime entry as stale (user re-attached: attach now holds a different `refresh_token` than the chain was started from) |

Comparison is on `refresh_token` **values**, not file mtimes. A real
re-attach always mints a server-side-fresh refresh_token, so
value-equality is the exact "is this still the same chain" signal.
File mtimes lie - any rewrite of `credentials.json` bumps mtime even
when the bytes are unchanged.

### Re-loading on attach-file change

The proxy watches `credentials.json` via inotify (through
`watchfiles`). When the file is modified, added, or atomically
replaced (tempfile + rename), the proxy re-runs the merge above and
swaps its in-memory vault. No restart required.

Practical effect:

- Update `credentials.json` with a freshly-obtained OAuth pair →
  proxy notices within ~milliseconds → next request uses the new
  pair → no container restart, no dropped connections.
- Same file written with identical bytes → merge re-runs, picks the
  same winners → no observable change.

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
