"""Credential vault: read/write credentials.json on disk + in-memory
cache. Atomic write via tempfile + os.replace so a crash mid-write
can't leave a half-written file.
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
    """Two-file credential store.

    - `attach_path` (credentials.json): reconciler-owned snapshot of
      what the user attached on the backend. Overwritten on every
      state apply. Never written by the proxy.
    - `runtime_path` (credentials.runtime.json): proxy-owned rotated
      state. Written by the proxy after each successful refresh.

    On load, we prefer `runtime_path` if its mtime >= `attach_path`'s
    mtime (meaning we rotated after the last attach). Otherwise the
    user just re-attached, so we use `attach_path` and delete the
    stale runtime file.

    All writes go to `runtime_path` only.
    """

    def __init__(self, attach_path: Path, runtime_path: Path):
        self._attach_path = attach_path
        self._runtime_path = runtime_path
        self._vault = Vault(providers={})
        # One asyncio.Lock per provider key. Concurrent 401s on the
        # same provider coalesce into a single refresh; different
        # providers refresh in parallel.
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._load()

    def _read_vault(self, path: Path) -> Vault | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error('failed to read %s: %s', path, e)
            return None
        providers = {}
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
                    path.name, key, e)
        return Vault(providers=providers)

    def _load(self) -> None:
        attach_mtime = (
            self._attach_path.stat().st_mtime
            if self._attach_path.exists() else None
        )
        runtime_mtime = (
            self._runtime_path.stat().st_mtime
            if self._runtime_path.exists() else None
        )

        use_runtime = (
            runtime_mtime is not None
            and (attach_mtime is None or runtime_mtime >= attach_mtime)
        )

        if use_runtime:
            vault = self._read_vault(self._runtime_path)
            source = 'runtime'
        else:
            # Either no runtime file, or attach is newer (re-attach).
            # Drop any stale runtime file so a future refresh writes
            # a fresh one.
            if runtime_mtime is not None and not use_runtime:
                try:
                    self._runtime_path.unlink()
                    log.info('dropped stale runtime credentials '
                             '(re-attach detected)')
                except OSError as e:
                    log.warning('failed to drop runtime creds: %s', e)
            vault = self._read_vault(self._attach_path)
            source = 'attach'

        if vault is None:
            log.warning(
                'no credentials available (attach=%s, runtime=%s); '
                'vault is empty',
                self._attach_path, self._runtime_path)
            self._vault = Vault(providers={})
            return

        self._vault = vault
        log.info('loaded %d credential(s) from %s store',
                 len(vault.providers), source)

    def reload(self) -> None:
        """Re-read both files and re-pick the winner. Called when
        desired-state apply replaces credentials.json (i.e. user
        re-attached)."""
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
        payload = {
            'providers': {
                k: asdict(v) for k, v in self._vault.providers.items()
            },
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
