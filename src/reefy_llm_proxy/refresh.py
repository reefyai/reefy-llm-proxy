"""OAuth refresh: lazy, on 401 from upstream.

The proxy uses access_tokens as-is. When an upstream returns 401, we
acquire the per-provider lock, refresh (or skip if a concurrent
caller already refreshed within the last second), update the vault,
and the caller retries the original request once.

Both xAI and codex rotate refresh_tokens on every refresh and
implement reuse-detection. A `refresh_token_reused` response (or any
4xx on the token endpoint) is a permanent failure for that chain -
the user has to re-attach.
"""

import logging
import time

import httpx

from . import providers, retry
from .credentials import Credential, CredentialStore

log = logging.getLogger(__name__)


class RefreshError(Exception):
    """Raised when a refresh attempt fails. `permanent=True` means
    the refresh_token chain is dead and the user has to re-attach."""

    def __init__(self, message: str, *, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


async def refresh_credential(
    client: httpx.AsyncClient,
    store: CredentialStore,
    cred: Credential,
) -> Credential:
    spec = providers.get(cred.provider)
    if spec is None:
        raise RefreshError(
            f'unknown provider {cred.provider!r}', permanent=True)

    async def _post() -> httpx.Response:
        return await client.post(
            spec.token_url,
            data={
                'grant_type':    'refresh_token',
                'refresh_token': cred.refresh_token,
                'client_id':     spec.client_id,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )

    resp = await retry.request_with_retry(
        _post, label=f'refresh[{cred.provider}@{cred.label}]')

    if resp.status_code >= 500:
        # Retries exhausted - upstream still 5xx. Transient: not a
        # chain failure; caller can try again later.
        raise RefreshError(
            f'token endpoint {resp.status_code} after retries',
            permanent=False)
    if resp.status_code >= 400:
        # 4xx on the token endpoint: chain is dead. Distinguish
        # `refresh_token_reused` for clearer logging but treat all
        # 4xx as permanent.
        body = resp.text[:500]
        log.error('refresh[%s] failed: %d %s',
                  cred.provider, resp.status_code, body)
        raise RefreshError(
            f'token endpoint {resp.status_code}: {body}',
            permanent=True)

    body = resp.json()
    access_token = body.get('access_token')
    if not access_token:
        raise RefreshError(
            'refresh response missing access_token', permanent=True)

    # refresh_token rotates on every refresh for both providers.
    # Fall back to the old one only if the response omits it
    # (shouldn't happen for xAI/codex but defensive).
    new_refresh_token = body.get('refresh_token') or cred.refresh_token

    expires_in = int(body.get('expires_in', 3600))
    new_cred = Credential(
        provider=cred.provider,
        label=cred.label,
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_at=int(time.time()) + expires_in,
        scopes=cred.scopes,
        last_refreshed_at=int(time.time()),
    )
    store.update(new_cred)
    log.info('refresh[%s@%s] ok; expires in %ds',
             cred.provider, cred.label, expires_in)
    return new_cred
