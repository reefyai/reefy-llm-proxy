"""Per-provider configuration.

Each provider knows its upstream URLs and OAuth refresh endpoint /
client_id. Adding a new provider = "add an entry here + make sure
the refresh shape matches RFC 6749 grant_type=refresh_token."

`client_id` values are the upstream CLIs' public OAuth clients
(hermes/openclaw for xAI, OpenAI Codex CLI for codex). They are
public per RFC 6749 and ship in the upstream sources unchanged. The
proxy reuses them for the refresh leg so devices can refresh tokens
that were attached via Reefy's device-code OAuth flow (which itself
also uses these public client_ids - see oauth_proxy.py).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderSpec:
    slug: str            # internal name used in model prefixes ("xai/...", "codex/...")
    base_url: str        # upstream OpenAI-compat API root
    token_url: str       # OAuth refresh endpoint
    client_id: str       # OAuth client_id (public; not a secret)
    # Defaults match the OpenAI /v1 shape (xAI follows it). Codex's
    # backend-api is OpenAI-flavoured but not OpenAI-compatible: every
    # call requires a `?client_version=<semver>` query param, and the
    # /models response uses `{"models": [{"slug": "..."}]}` instead of
    # the spec's `{"data": [{"id": "..."}]}`.
    extra_query_params: dict[str, str] = field(default_factory=dict)
    models_list_key: str = 'data'
    model_id_key: str = 'id'
    # Headers injected on every forwarded request (after stripping the
    # client's hop-by-hop headers but before the proxy sets its own
    # Authorization). Used by the codex provider to advertise an
    # allowed Cloudflare originator and a codex-CLI-shaped User-Agent;
    # without these, chatgpt.com/backend-api/codex returns 403 with
    # `cf-mitigated: challenge` from non-residential IPs.
    extra_headers: dict[str, str] = field(default_factory=dict)


# Source for these constants:
#   xAI:   https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth.py
#          https://github.com/openclaw/openclaw/blob/main/extensions/xai/xai-oauth.ts
#          reefy-service app/routes/oauth_proxy.py PROVIDERS['xai-oauth']
#   codex: https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs
#          reefy-service app/routes/oauth_proxy.py PROVIDERS['openai-codex']
#
# Codex client_version: the value the upstream backend gates request
# shape on. Bump in lockstep with the official codex-cli release we
# track so response fields (e.g. prefer_websockets in /models) match
# what our parser expects.
PROVIDERS: dict[str, ProviderSpec] = {
    'xai': ProviderSpec(
        slug='xai',
        base_url='https://api.x.ai/v1',
        token_url='https://auth.x.ai/oauth2/token',
        client_id='b1a00492-073a-47ea-816f-4c329264a828',
    ),
    'codex': ProviderSpec(
        slug='codex',
        base_url='https://chatgpt.com/backend-api/codex',
        token_url='https://auth.openai.com/oauth/token',
        client_id='app_EMoamEEZ73f0CkXaXp7hrann',
        extra_query_params={'client_version': '0.21.0'},
        models_list_key='models',
        model_id_key='slug',
        # Cloudflare in front of chatgpt.com/backend-api/codex
        # whitelists a small set of first-party originators
        # (codex_cli_rs, codex_vscode, codex_sdk_ts, anything
        # starting with `Codex`). Requests from non-residential IPs
        # or without an allowed originator get 403 + cf-mitigated:
        # challenge regardless of bearer-token validity.
        #
        # Same headers + ChatGPT-Account-ID extraction that
        # hermes-agent and openclaw inject when they call the codex
        # backend directly. Their docs explicitly note these are NOT
        # added by the clients when they go through a generic
        # OpenAI-compatible proxy, so the responsibility for the
        # Cloudflare passthrough lands here. References:
        #   - hermes:   auxiliary_client.py::_codex_cloudflare_headers
        #   - openclaw: docs/concepts/model-providers.md (codex section)
        # ChatGPT-Account-ID is dynamic (extracted from the bearer
        # JWT's claims) and injected per-request in proxy.py.
        extra_headers={
            'User-Agent': 'codex_cli_rs/0.21.0 (reefy-llm-proxy)',
            'originator': 'codex_cli_rs',
        },
    ),
}


def known_slugs() -> list[str]:
    return list(PROVIDERS.keys())


def get(slug: str) -> ProviderSpec | None:
    return PROVIDERS.get(slug)
