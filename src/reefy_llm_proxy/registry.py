"""Dynamic model registry.

Pulls /v1/models from each configured provider with a TTL cache (24h
by default - provider model lists change on the order of weeks).
Persists last-successful fetch to disk so the proxy can serve
/v1/models immediately on startup without waiting for an upstream
round-trip. Fallback when upstream is unreachable: serve the
last-successful list. If never fetched, return empty - never a
stale baked-in list.

The registry serves two purposes:
  1. /v1/models endpoint - apps probe to discover models.
  2. Routing of bare model names (no "provider/" prefix) to a
     provider. Built from the union of all dynamically-pulled lists.

First-call behavior: a provider with no cache entry has fetched_at=0,
which is always older than TTL, so it fetches on first access.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx

from . import providers, retry
from .credentials import CredentialStore

log = logging.getLogger(__name__)


class ModelRegistry:
    def __init__(
        self,
        cache_file: Path,
        ttl_s: int,
        store: CredentialStore,
        client: httpx.AsyncClient,
    ):
        self._cache_file = cache_file
        self._ttl_s = ttl_s
        self._store = store
        self._client = client
        # {provider: {"fetched_at": int, "models": [str, ...]}}
        self._cache: dict[str, dict] = {}
        self._fetch_lock = asyncio.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._cache_file.exists():
            return
        try:
            raw = json.loads(self._cache_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning('models cache %s unreadable: %s', self._cache_file, e)
            return
        if isinstance(raw, dict):
            self._cache = raw
            log.info('seeded models cache with %d provider(s) from disk',
                     len(raw))

    def _persist_to_disk(self) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix='models-cache.', suffix='.tmp',
            dir=str(self._cache_file.parent),
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self._cache, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._cache_file)
        except Exception as e:
            log.warning('failed to persist models cache: %s', e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    async def _fetch_one(self, slug: str) -> list[str] | None:
        spec = providers.get(slug)
        if spec is None:
            return None
        cred = self._store.get(slug)
        if cred is None:
            return None

        async def _get() -> httpx.Response:
            return await self._client.get(
                f'{spec.base_url}/models',
                headers={'Authorization': f'Bearer {cred.access_token}'},
            )

        try:
            resp = await retry.request_with_retry(
                _get, label=f'models[{slug}]')
        except httpx.HTTPError as e:
            log.warning('models[%s]: network error %s', slug, e)
            return None

        if resp.status_code != 200:
            log.warning('models[%s]: %d (not refreshing list)',
                        slug, resp.status_code)
            return None

        try:
            body = resp.json()
        except ValueError:
            log.warning('models[%s]: invalid json', slug)
            return None

        data = body.get('data')
        if not isinstance(data, list):
            return None
        ids = [m.get('id') for m in data if isinstance(m, dict)]
        return [m for m in ids if isinstance(m, str)]

    async def _maybe_refresh(self) -> None:
        """Refresh stale provider entries (>TTL old) in-place. Holds
        one lock; concurrent callers wait."""
        async with self._fetch_lock:
            now = int(time.time())
            stale = [
                slug for slug in providers.known_slugs()
                if (self._store.get(slug) is not None
                    and now - int(self._cache.get(slug, {}).get('fetched_at', 0))
                        > self._ttl_s)
            ]
            if not stale:
                return
            results = await asyncio.gather(
                *(self._fetch_one(slug) for slug in stale),
                return_exceptions=True,
            )
            dirty = False
            for slug, result in zip(stale, results):
                if isinstance(result, list):
                    self._cache[slug] = {
                        'fetched_at': now,
                        'models': result,
                    }
                    dirty = True
                # On failure: keep whatever's already in self._cache[slug]
                # (may be stale or absent). Honest.
            if dirty:
                self._persist_to_disk()

    async def list_for_api(self) -> list[dict]:
        """Return the union of all providers' models, formatted for
        /v1/models response. Each entry is prefixed with the provider
        slug."""
        await self._maybe_refresh()
        out: list[dict] = []
        for slug, entry in self._cache.items():
            for model_id in entry.get('models', []):
                out.append({
                    'id':       f'{slug}/{model_id}',
                    'object':   'model',
                    'owned_by': slug,
                })
        return out

    async def resolve_provider(self, model: str) -> str | None:
        """Map an inbound `model` string to a provider slug.

        - "provider/name" prefix takes precedence and bypasses the
          registry. "xai@work/grok-4" also works (strips @label).
        - Bare names are looked up in the cached lists; first
          unambiguous match wins.

        Returns the provider slug or None if unresolved (caller 503s).
        """
        if '/' in model:
            slug, _, _ = model.partition('/')
            slug = slug.split('@', 1)[0]
            return slug if providers.get(slug) is not None else None
        await self._maybe_refresh()
        matches = [
            slug for slug, entry in self._cache.items()
            if model in entry.get('models', [])
        ]
        if len(matches) == 1:
            return matches[0]
        return None
