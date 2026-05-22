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
    def __init__(self, path: Path):
        self._path = path
        self._vault = Vault(providers={})
        # One asyncio.Lock per provider key. Concurrent 401s on the
        # same provider coalesce into a single refresh; different
        # providers refresh in parallel.
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            log.warning('credentials file %s missing; vault is empty', self._path)
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error('failed to read %s: %s; vault is empty', self._path, e)
            return
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
                log.error('credentials entry %s missing field %s; skipped', key, e)
        self._vault = Vault(providers=providers)
        log.info('loaded %d credential(s) from %s', len(providers), self._path)

    def reload(self) -> None:
        """Re-read the file from disk. Called when desired-state apply
        replaces the file (reconciler writes a new credentials.json
        when the backend pushes updated tokens)."""
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
        """Replace a credential in-memory + persist to disk atomically."""
        self._vault.providers[_key(cred.provider, cred.label)] = cred
        self._persist()

    def _persist(self) -> None:
        payload = {
            'providers': {
                k: asdict(v) for k, v in self._vault.providers.items()
            },
        }
        # Atomic write: temp file in same dir, fsync, rename.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix='credentials.', suffix='.tmp',
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
