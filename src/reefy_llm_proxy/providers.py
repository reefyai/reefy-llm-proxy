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

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    slug: str            # internal name used in model prefixes ("xai/...", "codex/...")
    base_url: str        # upstream OpenAI-compat API root
    token_url: str       # OAuth refresh endpoint
    client_id: str       # OAuth client_id (public; not a secret)


# Source for these constants:
#   xAI:   https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth.py
#          https://github.com/openclaw/openclaw/blob/main/extensions/xai/xai-oauth.ts
#          reefy-service app/routes/oauth_proxy.py PROVIDERS['xai-oauth']
#   codex: https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs
#          reefy-service app/routes/oauth_proxy.py PROVIDERS['openai-codex']
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
    ),
}


def known_slugs() -> list[str]:
    return list(PROVIDERS.keys())


def get(slug: str) -> ProviderSpec | None:
    return PROVIDERS.get(slug)
