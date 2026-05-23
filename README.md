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

## Acceptable use & disclaimer

> **USE AT YOUR OWN RISK.** This software is provided "AS IS",
> without warranty of any kind, express or implied. The authors and
> contributors accept no liability for any consequence of running it
> — including, but not limited to, suspension or termination of
> accounts with upstream LLM providers, loss of subscription credit,
> rate-limit lockouts, or data loss. **You are solely responsible
> for ensuring your use complies with the Terms of Service of every
> upstream provider whose credentials you attach.** Provider
> policies change without notice; review them periodically.

### What this proxy is (and is permitted)

- A **single-user** proxy running on a device you own, forwarding
  requests from apps you also own/run, all on the same machine or
  same private bridge network.
- Authentication via OAuth tokens minted by the providers' own
  device-code flows (`auth.openai.com/oauth/token`,
  `auth.x.ai/oauth2/token`), explicitly built for headless /
  terminal / developer-tool consumption.
- Backend endpoints exposed for exactly this use case:
  `chatgpt.com/backend-api/codex` and `api.x.ai/v1`. **Not** the
  consumer web UIs (`chatgpt.com`, `grok.com`), no cookie scraping,
  no headless-browser automation.
- Cloudflare passthrough headers (`originator`, `User-Agent`)
  shaped to match the upstream CLIs — the same set hermes-agent
  and openclaw inject when calling codex directly. The traffic
  looks exactly like a first-party CLI doing what a first-party
  CLI does.

Both upstream clients we mirror treat this use case as supported.
The cleanest public statement is openclaw's codex provider page,
quoted verbatim: *"Policy note: OpenAI Codex OAuth is explicitly
supported for external tools/workflows like OpenClaw."* See:
[github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)
`docs/concepts/model-providers.md`. xAI's OAuth tier is similarly
documented as a first-class developer-facing surface.

### What would break that and is NOT permitted

- **Multi-tenant proxying.** Routing requests from people who are
  not the account owner through one OAuth pool. ToS violation
  across every provider, and triggers their abuse detection within
  hours.
- **Account sharing.** Letting multiple individuals consume one
  subscription via this proxy.
- **Commercial reselling.** Charging third parties for access to
  your account's capacity.
- **Tying this to consumer web-session credentials** (e.g. SSO
  cookies extracted from chatgpt.com or grok.com). The proxy
  doesn't support that path and adding it would put you in scraper
  territory regardless of what wrapping you put on top.
- **Unbounded automation loops** that burn through subscription
  fast-mode quotas at machine speed. Both OpenAI and xAI flag
  velocity anomalies; an agent in a runaway loop can cost you the
  account, not just the daily quota.

### Provider policy can change

Subscription-via-OAuth has been allowed for years, but the field
is in motion: in early 2026 Anthropic narrowed its position around
third-party-CLI reuse of Claude subscriptions, forcing some users
back to paid API keys. OpenAI and xAI have not made that change;
they could. If a provider publishes a new policy, this proxy stops
being the right fit for that provider and the operator should fall
back to an official billable API key (`console.x.ai` /
`platform.openai.com`).

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

## Security model

`LISTEN_HOST=0.0.0.0` inside the container is intentional - the
proxy is designed to be reached via a private docker bridge network
(e.g. `reefy-llm`) where every member is a trusted app you wired in.
**There is no auth on any route**, including `/v1/*` and
`/internal/stats`. The bridge network IS the access boundary.

If you publish the container port on the host (e.g.
`-p 127.0.0.1:9080:9080`), every local process on that host can use
your subscription LLM credentials with no authentication.

## Credential lifecycle

The proxy uses **two files** with disjoint writers:

| File | Writer | Role |
|---|---|---|
| `$DATA_DIR/credentials.json` | The user (or any external system you point at it) | "What was attached" snapshot. Holds the OAuth pair the user obtained via the provider's device-code flow. Proxy NEVER writes here. |
| `$DATA_DIR/credentials.runtime.json` | The proxy | Rotated state. Written after every successful OAuth refresh. Includes a `derived_from_attach` map (see below). |
| `$DATA_DIR/stats.json` | The proxy | Lifetime request + token counters surfaced via `/internal/stats`. Loaded on startup so the counters survive container restarts. Written atomically after every increment. |

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

## Per-provider quirks

Each provider speaks an OpenAI-ish API but with subtle differences
the proxy normalises. Defaults in `ProviderSpec` track the OpenAI
shape; per-provider entries override what diverges.

### xAI (`api.x.ai/v1`)

Vanilla OpenAI shape. `/models` returns `{data: [{id: ...}]}`, chat
completions accept the standard ChatCompletions request body, no
extra headers required. The OAuth bearer is enough; xAI doesn't
enforce User-Agent / originator gating. (Confirmed by inspecting
hermes-agent's xAI aux client — it uses a stock `OpenAI(api_key,
base_url=api.x.ai/v1)` with no `default_headers`.)

### codex (`chatgpt.com/backend-api/codex`)

Not OpenAI-compatible despite the surface similarity. Three
overrides:

1. **`?client_version=<semver>` query param on every call.**
   Upstream rejects without it. Pinned to a value matching a real
   codex-rs CLI release; bump if the response shape we depend on
   drifts.
2. **`/models` response uses `{models: [{slug: ...}]}`** instead of
   `{data: [{id: ...}]}`. Registry maps via `models_list_key` +
   `model_id_key` overrides.
3. **Cloudflare originator + User-Agent + ChatGPT-Account-ID
   headers.** chatgpt.com/backend-api is behind a Cloudflare layer
   that whitelists a small set of first-party originators
   (`codex_cli_rs`, `codex_vscode`, `codex_sdk_ts`, anything
   starting with `Codex`). Requests from non-residential IPs (VPS,
   server-hosted runners) or without an allowed originator get
   `HTTP 403` with `cf-mitigated: challenge`, regardless of bearer
   validity. We send:

   ```
   User-Agent:           codex_cli_rs/0.21.0 (reefy-llm-proxy)
   originator:           codex_cli_rs
   ChatGPT-Account-ID:   <extracted from JWT claim>
   ```

   `ChatGPT-Account-ID` is extracted per-request from the bearer
   JWT's `https://api.openai.com/auth.chatgpt_account_id` claim
   (same path the codex-rs CLI uses). On a malformed token we drop
   the header rather than fail — the request still goes through and
   any auth issue surfaces as a clean 401.

   This mirrors what hermes-agent and openclaw inject when they
   call codex directly. Both of their docs explicitly state these
   headers are NOT added on their generic OpenAI-compatible proxy
   path, so the responsibility for the Cloudflare passthrough lands
   on the proxy in front of codex. Sources (all open):

   - codex-rs CLI (upstream we shape against):
     [github.com/openai/codex](https://github.com/openai/codex) —
     authoritative for `User-Agent` / `originator` / account-ID
     handling.
   - hermes-agent's direct-codex header builder:
     `auxiliary_client.py::_codex_cloudflare_headers` in
     [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
     — docstring there is the source for the Cloudflare originator
     whitelist explanation copied above.
   - openclaw's docs page on the codex provider:
     [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)
     `docs/concepts/model-providers.md` — explicitly states the
     hidden attribution headers are not added on
     OpenAI-compatible-proxy paths.

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
