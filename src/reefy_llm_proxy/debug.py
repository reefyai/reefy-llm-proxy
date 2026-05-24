"""Runtime request/response capture toggle.

Off by default. Two surfaces:

  * Summary log line per proxied call (one short line to journal so
    you can grep without flooding it).
  * Full request + response dumped to a JSON file under `dump_dir`
    (one file per call, so binary / streaming bodies don't collide).

Flip on at runtime via `POST /internal/debug {"enabled": true}` -
no restart needed. Initial state seeds from `REEFY_LLM_PROXY_DEBUG=1`.
Dump dir from `REEFY_LLM_PROXY_DEBUG_DIR` (default /data/debug);
created on first use. Operator is responsible for cleanup - this is
ad-hoc debugging, not telemetry.

Sensitive request headers (Authorization, ChatGPT-Account-ID,
Cookie, ...) are redacted in BOTH the journal line and the dump
file. Request/response bodies are written verbatim.

Streams: response bodies for SSE are accumulated chunk-by-chunk via
wrap_response_stream() and flushed to disk when the generator ends.
The tee adds one bytes copy per chunk - cheap relative to the
upstream HTTP itself.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Mapping

logger = logging.getLogger('reefy_llm_proxy.debug')

_TRUE = ('1', 'true', 'yes', 'on')

enabled: bool = os.getenv('REEFY_LLM_PROXY_DEBUG', '').strip().lower() in _TRUE

dump_dir: Path = Path(os.getenv('REEFY_LLM_PROXY_DEBUG_DIR', '/data/debug'))

# Headers whose values must never reach the log. Lower-case match.
_SENSITIVE = frozenset({
    'authorization',
    'cookie',
    'x-api-key',
    'openai-organization',
    'chatgpt-account-id',
    'proxy-authorization',
})

# Summary-line preview cap. Full bodies always go to the dump file.
_SUMMARY_PREVIEW = 200


def _redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: ('<redacted>' if k.lower() in _SENSITIVE else v)
            for k, v in headers.items()}


def _decode(body: bytes | None) -> str:
    if not body:
        return ''
    try:
        return body.decode('utf-8')
    except UnicodeDecodeError:
        return f'<binary {len(body)} bytes>'


def _ts_for_filename() -> str:
    # Microsecond precision keeps filenames unique under bursts (we
    # also stitch a monotonic counter via the call site, but the ts
    # is enough for a human to scan a directory listing).
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def _ensure_dump_dir() -> Path | None:
    """Create dump_dir on first use. Returns None if creation fails
    so the proxy keeps serving even when the disk path is unwritable."""
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        return dump_dir
    except OSError as e:
        # Don't crash the request; just stop trying to dump for now.
        logger.warning('[debug] dump_dir %s unwritable: %s', dump_dir, e)
        return None


def _write_dump(filename: str, payload: dict) -> str | None:
    target_dir = _ensure_dump_dir()
    if target_dir is None:
        return None
    path = target_dir / filename
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning('[debug] write %s failed: %s', path, e)
        return None
    return str(path)


def log_summary(
    *,
    method: str,
    inbound_path: str,
    provider: str,
    upstream_url: str,
    upstream_status: int,
    inbound_body_len: int,
    response_body_len: int,
    dump_path: str | None,
    duration_ms: int,
) -> None:
    """Short single-line journal entry. Always called when enabled,
    after the upstream response status is known but before/while the
    stream is being tee'd."""
    logger.info(
        '[debug] %s %s -> %s %s (in=%dB out=%dB %dms) dump=%s',
        method, inbound_path, provider, upstream_status,
        inbound_body_len, response_body_len, duration_ms,
        dump_path or '<no-file>',
    )


def dump_request_response(
    *,
    method: str,
    inbound_path: str,
    inbound_body: bytes,
    provider: str,
    upstream_url: str,
    upstream_body: bytes,
    upstream_headers: Mapping[str, str],
    response_status: int,
    response_headers: Mapping[str, str],
    response_body: bytes,
    duration_ms: int,
) -> str | None:
    """Write a single JSON file capturing one proxied round-trip.
    Returns the dump path (or None if write failed). Body fields are
    decoded as utf-8 with a binary-size fallback."""
    filename = f'{_ts_for_filename()}.json'
    payload = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'duration_ms': duration_ms,
        'request': {
            'method': method,
            'inbound_path': inbound_path,
            'inbound_body': _decode(inbound_body),
            'provider': provider,
            'upstream_url': upstream_url,
            'upstream_headers': _redact_headers(upstream_headers),
            # Only included if translation rewrote the body; equal
            # bodies are de-duped in the call site.
            'upstream_body': _decode(upstream_body),
            'translated': inbound_body != upstream_body,
        },
        'response': {
            'status': response_status,
            'headers': _redact_headers(response_headers),
            'body': _decode(response_body),
            'body_bytes': len(response_body),
        },
    }
    return _write_dump(filename, payload)


async def wrap_response_stream(
    upstream_gen: AsyncIterator[bytes],
    *,
    method: str,
    inbound_path: str,
    inbound_body: bytes,
    provider: str,
    upstream_url: str,
    upstream_body: bytes,
    upstream_headers: Mapping[str, str],
    response_status: int,
    response_headers: Mapping[str, str],
) -> AsyncIterator[bytes]:
    """Tee `upstream_gen` to the client while accumulating chunks.
    On stream end (normal or exception) dump the full round-trip + log
    summary. Caller must check `enabled` BEFORE wrapping so off-mode
    pays zero per-chunk cost."""
    chunks: list[bytes] = []
    start = time.monotonic()
    try:
        async for chunk in upstream_gen:
            chunks.append(chunk)
            yield chunk
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        body = b''.join(chunks)
        dump_path = dump_request_response(
            method=method,
            inbound_path=inbound_path,
            inbound_body=inbound_body,
            provider=provider,
            upstream_url=upstream_url,
            upstream_body=upstream_body,
            upstream_headers=upstream_headers,
            response_status=response_status,
            response_headers=response_headers,
            response_body=body,
            duration_ms=duration_ms,
        )
        log_summary(
            method=method,
            inbound_path=inbound_path,
            provider=provider,
            upstream_url=upstream_url,
            upstream_status=response_status,
            inbound_body_len=len(inbound_body),
            response_body_len=len(body),
            dump_path=dump_path,
            duration_ms=duration_ms,
        )
