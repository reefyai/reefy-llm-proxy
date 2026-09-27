# Model discovery: empty Codex list and detached-provider cache

On Gputer on 2026-09-27, CC `/api/models` and proxy `/v1/models` returned 13 xAI models even though the reconciler-owned attachment file contained only Codex. The cache held 13 old xAI entries and a fresh empty Codex entry. Direct authenticated upstream discovery returned HTTP 200 with `models: []` using `client_version=0.21.0`; changing only that parameter to the installed official CLI version `0.155.0-alpha.9.2` returned ten model entries.

The original six real-HTTP E2Es produced **five failures, one pass** before the fix. The failures reproduced the obsolete-version response, stale xAI/empty Codex disk state, live detach, detach-all with rotated credentials, and account replacement while discovery was in flight. The positive cache/error-fallback control passed.

The fix:

- Pins Codex discovery and User-Agent to the verified client version. This is an explicit compatibility version, not automatic latest-version discovery.
- Binds cached lists to a hash of the attached credential chain and provider discovery configuration. Tokens themselves are never stored in model caches. Rotation within the same chain retains the binding; account reattachment or query-version changes invalidate it.
- Drops legacy/unbound cache entries and entries for detached providers. Both API listing and bare-name routing check the current binding.
- Discards discovery results if the attachment changes during the HTTP request.
- Treats an explicit empty attachment list as authoritative; runtime credentials cannot repopulate it. The existing missing/unreadable-file fallback is unchanged.
- Uses at most a 60-second TTL for successful empty lists. Provider failures retain last-good results only for the same attached account and back off for 30 seconds.

The expanded suite also checks short empty-cache recovery, discovery-profile changes and positive-cache persistence across proxy restart. It uses the real proxy route, lifespan, vault, watcher and cache, with a synthetic upstream HTTP server. No model list is hardcoded into the proxy. The synthetic version-sensitive response is based on the observed live failure, not a claim that the fixture is the upstream service.

HTTP evidence files contain only synthetic requests/model IDs. Do not attach real credential files to CI artifacts. Live verification uses only model-discovery GETs; successful discovery does not prove chat-completion or tool-call compatibility for every listed model.

## Verified results on Gputer

- Original six synthetic HTTP E2Es: 5 failures before the fix.
- Expanded suite against the fixed source and built image: 9 passed; the live test is skipped unless explicitly enabled.
- Identical live GET-only E2E: failed before with 13 stale `xai/...` entries; passed after with 10 `codex/...` entries and an identical set returned by CC `/api/models`.
- Captured results include `codex/gpt-6-astra`, `codex/gpt-6-sol`, `codex/gpt-6-luna`, `codex/gpt-reserve`, the three `gpt-5.6-*` models, `codex/gpt-daybreak-blue-latest`, `codex/gpt-5.5`, and `codex/codex-auto-review`. These are observed results, not a baked-in expected list.

Opt-in live check (GET requests only; requires access to the selected local endpoints):

```sh
PYTHONPATH=src PROXY_LIVE_DISCOVERY=1 PROXY_EXPECT_PROVIDER=codex \
  CC_LIVE_MODELS_URL=http://127.0.0.1:20025/api/models \
  E2E_ARTIFACT_DIR=artifacts/live \
  python -m unittest discover -s tests/e2e -p test_live_discovery.py -v
```

The active Gputer container received a backed-up source hotfix for `providers.py`, `credentials.py` and `registry.py`; the platform-wide image pin was deliberately not changed. The backup (source, state, and installed-source hashes) is `/mnt/reefy-data/state/llm-proxy-hotfix-backups/20260927-164512-models/` on Gputer. This hotfix survives container restart, but recreation restores the pinned image. A tested candidate image `reefy-llm-proxy:models-fix-20260927` exists locally. Publishing the repository fix and rolling out the corresponding managed image remain necessary for permanent deployment. The CC model selection was not changed automatically.
