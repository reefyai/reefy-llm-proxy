"""Credential vault: read/write credentials.json on disk + in-memory
cache. Atomic write via tempfile + os.replace so a crash mid-write
can't leave a half-written file.

Two-file layout, see CredentialStore docstring for the chain-merge
semantics.
"""

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class Credential:
    provider: str
    label: str
    access_token: str
    refresh_token: str
    expires_at: int                      # unix seconds
    scopes: list[str] = field(default_factory=list)
    last_refreshed_at: int | None = None


@dataclass
class Vault:
    providers: dict[str, Credential]     # key: "<provider>" or "<provider>@<label>"


def _key(provider: str, label: str = 'default') -> str:
    return provider if label == 'default' else f'{provider}@{label}'


class CredentialStore:
    """Two-file credential store with content-based chain detection.

    Files:
      - `attach_path` (credentials.json): the "what the user attached"
        snapshot. The OUTER system (whatever's writing this file - a
        reconciler, an admin script, a `cp` invocation) owns it. The
        proxy NEVER writes here.
      - `runtime_path` (credentials.runtime.json): the proxy's rotated
        state. Written by the proxy after every successful OAuth
        refresh. Carries a `derived_from_attach` map recording which
        attach refresh_token each rotation chain started from.

    Why two files: the access_token expires (hours-days), so the
    proxy has to refresh and persist new tokens; the attach file
    holds the user-controlled origin (months-stable) and shouldn't
    be mutated by the proxy. Splitting writers means a re-write of
    the attach file by whatever controls it (e.g. routine config sync)
    doesn't clobber the proxy's rotated state.

    Chain-merge rule on load:

        For each provider slug present in attach:
          - If runtime has the slug AND
            runtime.derived_from_attach[slug] == attach.refresh_token:
              -> same chain (rotated successor of THIS attach token),
                 use runtime
          - Otherwise:
              -> chain mismatch (re-attach happened, or first run,
                 or runtime predates this schema) -> use attach

    We compare refresh_token VALUES, not file mtimes. A re-attach
    always mints a different refresh_token (OAuth server-side), so
    value-equality is the exact signal for "is the proxy's rotation
    chain still anchored to this attach token". File mtimes get
    bumped by any rewrite (even byte-identical), so they would lie.

    All writes go to `runtime_path` only.
    """

    def __init__(self, attach_path: Path, runtime_path: Path):
        self._attach_path = attach_path
        self._runtime_path = runtime_path
        self._vault = Vault(providers={})
        # Per-provider snapshot of "what attach refresh_token did this
        # rotation chain start from". Set on load; carried through
        # every _persist() so the file on disk always tells the next
        # loader how to detect a re-attach.
        self._chain_seed: dict[str, str] = {}
        # One asyncio.Lock per provider key. Concurrent 401s on the
        # same provider coalesce into a single refresh; different
        # providers refresh in parallel.
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._load()

    def _read_raw(self, path: Path) -> dict | None:
        """Read + parse JSON. Returns None on missing/invalid."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error('failed to read %s: %s', path, e)
            return None

    def _parse_providers(self, raw: dict, source: str) -> dict[str, Credential]:
        providers: dict[str, Credential] = {}
        for key, entry in (raw.get('providers') or {}).items():
            try:
                providers[key] = Credential(
                    provider=entry['provider'],
                    label=entry.get('label', 'default'),
                    access_token=entry['access_token'],
                    refresh_token=entry['refresh_token'],
                    expires_at=int(entry.get('expires_at', 0)),
                    scopes=list(entry.get('scopes', [])),
                    last_refreshed_at=entry.get('last_refreshed_at'),
                )
            except KeyError as e:
                log.error(
                    '%s entry %s missing field %s; skipped',
                    source, key, e)
        return providers

    def _load(self) -> None:
        attach_raw = self._read_raw(self._attach_path)
        runtime_raw = self._read_raw(self._runtime_path)

        attach_providers = (
            self._parse_providers(attach_raw, self._attach_path.name)
            if attach_raw else {}
        )
        runtime_providers = (
            self._parse_providers(runtime_raw, self._runtime_path.name)
            if runtime_raw else {}
        )
        runtime_seed = (
            runtime_raw.get('derived_from_attach') if runtime_raw else None
        )
        if not isinstance(runtime_seed, dict):
            runtime_seed = {}

        # Edge case: no attach file at all. Best-effort fallback to
        # runtime (operator may have deleted attach intentionally;
        # the proxy can still serve until the chain breaks).
        if not attach_providers:
            if runtime_providers:
                self._vault = Vault(providers=runtime_providers)
                self._chain_seed = {
                    k: v for k, v in runtime_seed.items()
                    if isinstance(v, str)
                }
                log.warning(
                    'no attach file at %s; using runtime as-is '
                    '(%d provider(s))',
                    self._attach_path, len(runtime_providers))
            else:
                self._vault = Vault(providers={})
                self._chain_seed = {}
                log.warning(
                    'no credentials at all (attach=%s, runtime=%s); '
                    'vault is empty',
                    self._attach_path, self._runtime_path)
            return

        # Per-provider chain check.
        merged: dict[str, Credential] = {}
        seed: dict[str, str] = {}
        from_runtime = 0
        re_attach: list[str] = []

        for slug, attach_cred in attach_providers.items():
            rt_cred = runtime_providers.get(slug)
            rt_seed = runtime_seed.get(slug)
            chain_alive = (
                rt_cred is not None
                and isinstance(rt_seed, str)
                and rt_seed == attach_cred.refresh_token
            )
            if chain_alive:
                merged[slug] = rt_cred
                seed[slug] = rt_seed
                from_runtime += 1
            else:
                merged[slug] = attach_cred
                seed[slug] = attach_cred.refresh_token
                # Distinguish "re-attach" (runtime has a different
                # seed for this slug) from "first run / no rotation
                # yet" (runtime has nothing for this slug).
                if rt_cred is not None:
                    re_attach.append(slug)

        self._vault = Vault(providers=merged)
        self._chain_seed = seed

        from_attach = len(merged) - from_runtime
        msg = f'loaded {len(merged)} credential(s)'
        parts = []
        if from_runtime:
            parts.append(f'{from_runtime} from runtime chain')
        if from_attach:
            parts.append(f'{from_attach} from attach')
        if parts:
            msg += ' (' + ', '.join(parts) + ')'
        if re_attach:
            msg += f' [re-attach detected: {", ".join(re_attach)}]'
        log.info(msg)

    def reload(self) -> None:
        """Re-read both files and re-merge. Called by the file watcher
        when credentials.json changes on disk (re-attach or operator
        edit), or manually if needed."""
        self._load()

    def list_keys(self) -> list[str]:
        return list(self._vault.providers.keys())

    def list_providers(self) -> Iterable[Credential]:
        return self._vault.providers.values()

    def get(self, provider: str, label: str = 'default') -> Credential | None:
        return self._vault.providers.get(_key(provider, label))

    def lock_for(self, provider: str, label: str = 'default') -> asyncio.Lock:
        key = _key(provider, label)
        if key not in self._refresh_locks:
            self._refresh_locks[key] = asyncio.Lock()
        return self._refresh_locks[key]

    def update(self, cred: Credential) -> None:
        """Replace a credential in-memory + persist rotated state to
        credentials.runtime.json (NOT credentials.json - that file
        belongs to the reconciler)."""
        self._vault.providers[_key(cred.provider, cred.label)] = cred
        self._persist()

    def _persist(self) -> None:
        # `derived_from_attach` lets the next loader detect whether
        # the attach refresh_token still matches the one this rotation
        # chain started from. Always written alongside the providers
        # dict so any rotation event re-anchors the breadcrumb.
        payload = {
            'providers': {
                k: asdict(v) for k, v in self._vault.providers.items()
            },
            'derived_from_attach': dict(self._chain_seed),
        }
        # Atomic write: temp file in same dir, fsync, rename.
        self._runtime_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix='credentials.runtime.', suffix='.tmp',
            dir=str(self._runtime_path.parent),
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._runtime_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
