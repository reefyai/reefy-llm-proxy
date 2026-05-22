"""Request forwarding to upstream providers.

Flow per inbound /v1/{path}:
  1. Read request body (FastAPI buffers up to the configured max).
  2. Parse JSON to extract `model` (used for routing).
  3. Resolve provider via the rules in the plan:
       - 0 providers configured -> 503
       - 1 provider             -> route everything there
       - >1 providers           -> prefix wins, else registry, else 503
  4. Strip "provider/" prefix from model before sending upstream
     (xAI doesn't know about "xai/grok-4", just "grok-4").
  5. Forward with Bearer access_token. Stream response back.
  6. On 401: acquire per-provider refresh lock, refresh, retry once.
  7. On 2xx: increment counters from the upstream's `usage` block.
"""

import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import providers
from .credentials import Credential, CredentialStore
from .refresh import RefreshError, refresh_credential
from .registry import ModelRegistry
from .retry import request_with_retry
from .stats import stats

log = logging.getLogger(__name__)


# Headers we strip from the inbound request before forwarding.
# `host` and `content-length` are recomputed by httpx; `authorization`
# is replaced with the provider's bearer token.
_HOP_BY_HOP = {
    'host', 'content-length', 'authorization', 'connection',
    'keep-alive', 'transfer-encoding', 'te', 'trailers',
    'proxy-authorization', 'proxy-authenticate', 'upgrade',
}

# Headers we drop from the upstream response before returning to the
# client. We always decompress upstream's response body before
# forwarding (see _stream_with_stats - uses aiter_bytes which decodes
# automatically), so content-encoding and content-length both have
# to be dropped (their values reference the compressed/original size,
# which our outgoing chunked stream no longer matches).
_RESPONSE_DROP = {
    'content-encoding', 'content-length',
    'transfer-encoding', 'connection',
    'keep-alive', 'te', 'trailer', 'upgrade',
    'proxy-authenticate', 'proxy-authorization',
}


def _filter_request_headers(headers: dict) -> dict:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _filter_response_headers(headers: dict) -> dict:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _RESPONSE_DROP
    }


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={'error': {'message': message, 'type': 'reefy_llm_proxy'}},
    )


def _strip_model_prefix(model: str) -> str:
    """xai/grok-4-fast -> grok-4-fast; xai@work/grok-4 -> grok-4."""
    if '/' in model:
        _, _, rest = model.partition('/')
        return rest
    return model


async def _resolve_provider(
    model: str,
    store: CredentialStore,
    registry: ModelRegistry,
) -> tuple[str | None, str | None]:
    """Returns (provider_slug, error_message). Exactly one is None."""
    attached_slugs = {c.provider for c in store.list_providers()}

    if not attached_slugs:
        return None, 'no llm provider configured on this device'

    if len(attached_slugs) == 1:
        # Single-provider device: honor user intent regardless of
        # what prefix they sent. Strip prefix on the forward side
        # so the upstream sees a name it knows.
        only = next(iter(attached_slugs))
        return only, None

    # Multi-provider: prefix wins.
    if '/' in model:
        slug, _, _ = model.partition('/')
        slug = slug.split('@', 1)[0]
        if slug in attached_slugs:
            return slug, None
        return None, f"provider '{slug}' not configured on this device"

    # Bare name: ask the registry (registry caches /v1/models per
    # provider; first unambiguous match wins).
    slug = await registry.resolve_provider(model)
    if slug is not None and slug in attached_slugs:
        return slug, None
    return None, (
        f"model {model!r} not recognized; prefix with provider "
        f"(e.g. 'xai/{model}') to disambiguate"
    )


def _update_token_stats_from_chunk(
    chunk: bytes, provider: str, model: str,
) -> None:
    """Best-effort: if the upstream response chunk contains a JSON
    body with usage info (non-streaming case), record tokens. For
    SSE streams the per-event payloads usually don't carry usage
    except at the end; we read those too. Silent on parse failure -
    counters are observability, not correctness."""
    try:
        # SSE lines look like "data: {...}\n\n"
        for line in chunk.split(b'\n'):
            line = line.strip()
            if line.startswith(b'data:'):
                line = line[5:].strip()
            if not line or line == b'[DONE]':
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            usage = obj.get('usage') if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                stats.add_tokens(
                    provider, model,
                    int(usage.get('prompt_tokens', 0) or 0),
                    int(usage.get('completion_tokens', 0) or 0),
                )
    except Exception:
        pass


async def _stream_with_stats(
    response: httpx.Response,
    provider: str,
    model: str,
) -> AsyncIterator[bytes]:
    """Iterate over the upstream response chunks, decoded (httpx
    aiter_bytes handles gzip/br/zstd transparently), mirror them to
    the client and increment token counters opportunistically."""
    try:
        async for chunk in response.aiter_bytes():
            _update_token_stats_from_chunk(chunk, provider, model)
            yield chunk
    finally:
        await response.aclose()


async def _forward_once(
    client: httpx.AsyncClient,
    method: str,
    upstream_url: str,
    headers: dict,
    body: bytes,
    cred: Credential,
) -> httpx.Response:
    """Open a streaming request to upstream and return the response
    object (caller is responsible for closing). Uses retry.py for
    transient errors (connection / 5xx / 429)."""
    hdrs = {**headers, 'Authorization': f'Bearer {cred.access_token}'}

    async def _do() -> httpx.Response:
        req = client.build_request(
            method, upstream_url, headers=hdrs, content=body)
        return await client.send(req, stream=True)

    return await request_with_retry(
        _do, label=f'forward[{cred.provider}]')


async def forward(
    request: Request,
    path: str,
    *,
    store: CredentialStore,
    registry: ModelRegistry,
    client: httpx.AsyncClient,
) -> JSONResponse | StreamingResponse:
    body = await request.body()
    model = ''
    parsed: dict | None = None
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                model = str(parsed.get('model', ''))
        except (ValueError, json.JSONDecodeError):
            parsed = None    # forward as-is

    provider_slug, error = await _resolve_provider(model, store, registry)
    if provider_slug is None:
        return _err(503, error or 'no provider')

    cred = store.get(provider_slug)
    if cred is None:
        return _err(503, f"provider '{provider_slug}' has no credentials")

    spec = providers.get(provider_slug)
    if spec is None:
        return _err(500, f"provider '{provider_slug}' not supported")

    # Rewrite the model in the body to its bare form (upstream
    # doesn't know about our prefixes).
    if parsed is not None and model:
        bare = _strip_model_prefix(model)
        if bare != model:
            parsed['model'] = bare
            body = json.dumps(parsed).encode('utf-8')

    upstream_url = f'{spec.base_url.rstrip("/")}/{path.lstrip("/")}'
    fwd_headers = _filter_request_headers(dict(request.headers))

    # First attempt.
    response = await _forward_once(
        client, request.method, upstream_url, fwd_headers, body, cred)

    # 401 -> refresh + retry once.
    if response.status_code == 401:
        await response.aclose()
        lock = store.lock_for(cred.provider, cred.label)
        async with lock:
            current = store.get(cred.provider, cred.label) or cred
            if current.access_token == cred.access_token:
                try:
                    cred = await refresh_credential(client, store, current)
                except RefreshError as e:
                    log.error('refresh failed for %s: %s', cred.provider, e)
                    return _err(
                        401,
                        f"refresh failed for '{cred.provider}': "
                        f"{'re-attach required' if e.permanent else 'transient; try again'}"
                    )
            else:
                cred = current     # someone else refreshed while we waited
        response = await _forward_once(
            client, request.method, upstream_url, fwd_headers, body, cred)

    bare_model = _strip_model_prefix(model) if model else ''
    if response.status_code < 400 and bare_model:
        stats.add_request(provider_slug, bare_model)

    resp_headers = _filter_response_headers(dict(response.headers))
    return StreamingResponse(
        _stream_with_stats(response, provider_slug, bare_model),
        status_code=response.status_code,
        headers=resp_headers,
        media_type=resp_headers.get('content-type'),
    )
