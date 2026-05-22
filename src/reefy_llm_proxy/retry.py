"""Retry policy for upstream calls (LLM forward + OAuth refresh).

Schema:
  - connection errors / timeouts: up to 3 attempts, 100/200/400ms backoff
  - 5xx: up to 2 retries, same backoff
  - 429: 1 retry, honor Retry-After header
  - 401: caller-handled (refresh + retry once, not via this module)
  - other 4xx: no retry, return to caller as-is
"""

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)


CONNECT_BACKOFFS_S = (0.1, 0.2, 0.4)   # 3 attempts on connection errors
SERVER_BACKOFFS_S = (0.1, 0.2)         # 2 retries on 5xx
RATELIMIT_MAX_RETRY = 1


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    h = resp.headers.get('Retry-After')
    if not h:
        return None
    try:
        return float(h)
    except ValueError:
        return None     # ignore date-format Retry-After for v1


async def request_with_retry(
    call: Callable[[], Awaitable[httpx.Response]],
    *,
    label: str = 'upstream',
) -> httpx.Response:
    """Invoke `call`, retrying transient failures per the policy above.
    Returns the final response (which may still be non-2xx if the
    retries didn't help; caller decides what to do).
    """
    connect_attempt = 0
    server_attempt = 0
    ratelimit_attempt = 0

    while True:
        try:
            resp = await call()
        except (httpx.ConnectError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            if connect_attempt < len(CONNECT_BACKOFFS_S):
                backoff = CONNECT_BACKOFFS_S[connect_attempt]
                log.warning('%s: connection error (%s); retry %d in %.2fs',
                            label, e, connect_attempt + 1, backoff)
                await asyncio.sleep(backoff)
                connect_attempt += 1
                continue
            raise

        if 500 <= resp.status_code < 600:
            if server_attempt < len(SERVER_BACKOFFS_S):
                backoff = SERVER_BACKOFFS_S[server_attempt]
                log.warning('%s: %d; retry %d in %.2fs',
                            label, resp.status_code, server_attempt + 1, backoff)
                await asyncio.sleep(backoff)
                server_attempt += 1
                continue
            return resp

        if resp.status_code == 429 and ratelimit_attempt < RATELIMIT_MAX_RETRY:
            backoff = _retry_after_seconds(resp) or 1.0
            log.warning('%s: 429; retry in %.2fs (Retry-After)', label, backoff)
            await asyncio.sleep(backoff)
            ratelimit_attempt += 1
            continue

        return resp
