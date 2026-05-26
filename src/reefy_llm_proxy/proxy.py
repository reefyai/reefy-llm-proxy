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

import base64
import json
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import codex_translator, debug, providers
from .credentials import Credential, CredentialStore
from .refresh import RefreshError, refresh_credential
from .registry import ModelRegistry
from .retry import request_with_retry
from .stats import stats

log = logging.getLogger(__name__)


def _extract_chatgpt_account_id(access_token: str) -> str | None:
    """Pull `chatgpt_account_id` out of the codex JWT's claims.

    The codex backend requires a `ChatGPT-Account-ID` header to scope
    the request to the right ChatGPT account (a single OAuth client
    can have access to multiple). The codex-rs CLI extracts it from
    the JWT's `https://api.openai.com/auth.chatgpt_account_id` claim;
    we mirror that. Returns None on malformed tokens - caller treats
    None as "don't send the header" (request still proceeds, surfaces
    as a 401 if upstream actually needs it, instead of crashing here)."""
    try:
        parts = access_token.split('.')
        if len(parts) < 2:
            return None
        payload = parts[1] + '=' * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get('https://api.openai.com/auth') or {}
        acct = auth.get('chatgpt_account_id')
        if isinstance(acct, str) and acct:
            return acct
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _provider_headers(spec: providers.ProviderSpec, cred: Credential) -> dict:
    """Provider-specific headers to inject on outgoing requests.
    Combines the static `spec.extra_headers` with per-credential
    dynamic headers (currently just codex's ChatGPT-Account-ID
    extracted from the bearer JWT)."""
    out = dict(spec.extra_headers or {})
    if spec.slug == 'codex':
        acct = _extract_chatgpt_account_id(cred.access_token)
        if acct:
            out['ChatGPT-Account-ID'] = acct
    return out


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


def _extract_usage(obj: object) -> tuple[int, int] | None:
    """Pull (prompt, completion) token counts out of an upstream JSON
    body. Returns None if no usage block is present. Two upstream
    shapes are recognised:

      ChatCompletions (xAI, classic OpenAI):
        {"usage": {"prompt_tokens": N, "completion_tokens": M}}

      Responses API (codex SSE event payload):
        {"type": "response.completed",
         "response": {"usage": {"input_tokens": N, "output_tokens": M}}}

    The Responses API also emits `usage: null` on intermediate events
    (response.created, response.in_progress, ...) - only the final
    `response.completed` event carries non-null counts, so the recursion
    naturally records exactly once per request."""
    if not isinstance(obj, dict):
        return None
    usage = obj.get('usage')
    if isinstance(usage, dict):
        prompt = (
            usage.get('prompt_tokens')
            or usage.get('input_tokens')
            or 0
        )
        completion = (
            usage.get('completion_tokens')
            or usage.get('output_tokens')
            or 0
        )
        if prompt or completion:
            return int(prompt), int(completion)
    # Nested under .response for the Responses API event stream.
    return _extract_usage(obj.get('response'))


def _update_token_stats_from_chunk(
    chunk: bytes, provider: str, model: str,
) -> None:
    """Best-effort: if the upstream response chunk contains a JSON
    body with usage info, record tokens. Silent on parse failure -
    counters are observability, not correctness."""
    try:
        # SSE lines look like "data: {...}\n\n". For non-streaming
        # JSON bodies the whole chunk is the document.
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
            usage = _extract_usage(obj)
            if usage:
                p, c = usage
                stats.add_tokens(provider, model, p, c)
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


async def _stream_codex_chat_translation(
    response: httpx.Response,
    provider: str,
    model: str,
) -> AsyncIterator[bytes]:
    """Codex Responses SSE -> ChatCompletions SSE, with the same
    stats-update side effect as `_stream_with_stats`. The translator
    emits ChatCompletions chunks whose final entry carries a
    `usage: {prompt_tokens, completion_tokens, ...}` block; the
    existing chunk parser already recognises that shape, so token
    counting works without a separate codex path."""
    try:
        translated = codex_translator.responses_sse_to_chat_sse(
            response.aiter_bytes(), model)
        async for chunk in translated:
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
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Open a streaming request to upstream and return the response
    object (caller is responsible for closing). Uses retry.py for
    transient errors (connection / 5xx / 429)."""
    hdrs = {**headers, 'Authorization': f'Bearer {cred.access_token}'}

    async def _do() -> httpx.Response:
        req = client.build_request(
            method, upstream_url, headers=hdrs, content=body,
            params=params or None)
        return await client.send(req, stream=True)

    return await request_with_retry(
        _do, label=f'forward[{cred.provider}]')


# Refresh access_token if it expires within this many seconds. Covers
# clock skew between proxy and provider + the in-flight request RTT.
# Too small: requests in the last few seconds of validity still 401/403.
# Too large: pointless early-rotation; fine to err on the safe side.
_TOKEN_EXPIRY_SKEW_S = 60


async def _ensure_fresh(
    client: httpx.AsyncClient,
    store: 'CredentialStore',
    cred: Credential,
) -> Credential:
    """Return a credential whose access_token won't expire within the
    next _TOKEN_EXPIRY_SKEW_S seconds. If the input is already fresh,
    returns it unchanged (one int compare). Otherwise grabs the per-
    (provider, label) lock, double-checks under the lock (in case
    another coroutine just rotated), and runs `refresh_credential`.

    On refresh error, returns the original (stale) cred and lets the
    caller's upstream call surface the auth failure - that matches
    the existing behaviour for refresh-failed-in-flight (caller gets
    a 401/403 and the reactive path either retries or bubbles up).
    """
    if cred.expires_at - time.time() > _TOKEN_EXPIRY_SKEW_S:
        return cred
    lock = store.lock_for(cred.provider, cred.label)
    async with lock:
        current = store.get(cred.provider, cred.label) or cred
        # Another coroutine may have refreshed while we waited.
        if current.expires_at - time.time() > _TOKEN_EXPIRY_SKEW_S:
            return current
        try:
            return await refresh_credential(client, store, current)
        except RefreshError as e:
            log.warning(
                'proactive refresh failed for %s: %s; passing through '
                'with stale token (reactive 401/403 path may still recover)',
                current.provider, e)
            return current


async def forward(
    request: Request,
    path: str,
    *,
    store: CredentialStore,
    registry: ModelRegistry,
    client: httpx.AsyncClient,
) -> JSONResponse | StreamingResponse:
    body = await request.body()
    # Snapshot the raw inbound body BEFORE codex/ChatCompletions
    # translation rewrites `body` - the debug dump needs both shapes.
    inbound_body_for_debug = body
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
    # doesn't know about our prefixes), and force `stream_options.
    # include_usage = true` on streaming chat-completions so the
    # upstream emits a final usage chunk we can count tokens from.
    # Without this opt-in, OpenAI-shape providers (xAI, openai) omit
    # usage entirely from streaming responses and our /internal/stats
    # never increments token counters for the request - even though
    # the request itself succeeded. Codex's Responses API is exempt
    # (always emits usage on response.completed), but the inject is
    # cheap and harmless to spec it everywhere it applies.
    # Codex backend speaks only the Responses API; ChatCompletions
    # callers (openclaw default, claude-code, anything OPENAI_BASE_URL-
    # wired) get translated here. After translation the rest of the
    # forwarding logic is the same as a Responses-native client; the
    # response side gets re-translated back to ChatCompletions chunks
    # below so the client sees what it expected.
    translate_codex_response = False
    if (provider_slug == 'codex'
            and path.endswith('chat/completions')
            and parsed is not None):
        # Strip prefix BEFORE translating so the codex spec sees the
        # bare model.
        if model:
            parsed['model'] = _strip_model_prefix(model)
        parsed = codex_translator.chat_to_responses(parsed)
        body = json.dumps(parsed).encode('utf-8')
        path = 'responses'
        translate_codex_response = True
    elif parsed is not None:
        changed = False
        if model:
            bare = _strip_model_prefix(model)
            if bare != model:
                parsed['model'] = bare
                changed = True
        if (
            path.endswith('chat/completions')
            and parsed.get('stream') is True
        ):
            opts = parsed.get('stream_options')
            if not isinstance(opts, dict):
                opts = {}
            if opts.get('include_usage') is not True:
                opts['include_usage'] = True
                parsed['stream_options'] = opts
                changed = True
        # Codex /responses backend rejects requests without stream:true
        # or with store!=false (400 Bad Request). Inject both - clients
        # that already set them are no-ops; clients that didn't (hermes
        # in some flows, openclaw before its codex adapter wires this
        # in) now succeed.
        if (
            provider_slug == 'codex'
            and path.endswith('responses')
        ):
            if parsed.get('stream') is not True:
                parsed['stream'] = True
                changed = True
            if parsed.get('store') is not False:
                parsed['store'] = False
                changed = True
        if changed:
            body = json.dumps(parsed).encode('utf-8')

    upstream_url = f'{spec.base_url.rstrip("/")}/{path.lstrip("/")}'

    # Proactive refresh: if the access_token is at/past expiry (with a
    # 60s skew for clock drift + the request RTT itself), refresh it
    # in-place BEFORE the upstream call. Cheap when not stale (one
    # int compare); critical correctness when stale (xAI returns 403
    # not 401 for expired tokens, so the reactive path below is the
    # only catch for codex - and we'd rather not even depend on it).
    # Caught 2026-05-26 when an xai token sat 18h expired in the
    # store without a single refresh attempt; OAuth2 best practice
    # is "refresh ahead of expiry", not "wait for an error".
    cred = await _ensure_fresh(client, store, cred)

    fwd_headers = _filter_request_headers(dict(request.headers))
    # Provider-specific headers override anything the client passed.
    # For codex this carries the Cloudflare-required originator + a
    # codex-CLI-shaped User-Agent + ChatGPT-Account-ID. For xAI and
    # any new OpenAI-shape provider this is a no-op (empty dict).
    fwd_headers.update(_provider_headers(spec, cred))

    # First attempt.
    response = await _forward_once(
        client, request.method, upstream_url, fwd_headers, body, cred,
        params=spec.extra_query_params or None)

    # Reactive safety net: 401 (codex shape) OR 403 (xAI shape) ->
    # refresh + retry once. Anti-flood guard: if we JUST refreshed
    # (proactive or last reactive call), don't loop; the upstream is
    # rejecting for a non-auth reason (model unavailable, account
    # suspended, rate limit) and a fresh token won't help.
    if response.status_code in (401, 403):
        just_refreshed = (cred.last_refreshed_at is not None
                          and time.time() - cred.last_refreshed_at < 30)
        if not just_refreshed:
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
            # Recompute provider headers (codex's ChatGPT-Account-ID
            # is derived from the JWT, which changes on refresh).
            fwd_headers.update(_provider_headers(spec, cred))
            response = await _forward_once(
                client, request.method, upstream_url, fwd_headers, body, cred,
                params=spec.extra_query_params or None)

    bare_model = _strip_model_prefix(model) if model else ''
    if response.status_code < 400 and bare_model:
        stats.add_request(provider_slug, bare_model)

    resp_headers = _filter_response_headers(dict(response.headers))
    # Only run the codex chat-completions translator on successful
    # responses. On a 4xx/5xx upstream we want the raw error body to
    # reach the client - translating an error response into ChatCompletions
    # chunk shape would hide the real failure cause.
    if translate_codex_response and response.status_code < 400:
        stream_gen = _stream_codex_chat_translation(
            response, provider_slug, bare_model)
        # Translator output is ChatCompletions-style SSE (data: ...\n\n);
        # advertise the right media type regardless of what upstream
        # set on the codex /responses reply.
        media_type = 'text/event-stream'
    else:
        stream_gen = _stream_with_stats(response, provider_slug, bare_model)
        media_type = resp_headers.get('content-type')

    # Tee the response stream into the debug capture file when the
    # flag is on. Cost in off-mode is one boolean check; on-mode adds
    # one list.append + b''.join per request - cheap relative to the
    # upstream HTTP. Toggle at runtime via /internal/debug.
    if debug.enabled:
        stream_gen = debug.wrap_response_stream(
            stream_gen,
            method=request.method,
            inbound_path=request.url.path,
            inbound_body=inbound_body_for_debug,
            provider=provider_slug,
            upstream_url=upstream_url,
            upstream_body=body,
            upstream_headers=fwd_headers,
            response_status=response.status_code,
            response_headers=resp_headers,
        )

    return StreamingResponse(
        stream_gen,
        status_code=response.status_code,
        headers=resp_headers,
        media_type=media_type,
    )
