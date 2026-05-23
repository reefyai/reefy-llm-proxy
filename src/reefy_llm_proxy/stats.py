"""Lifetime counters for /internal/stats, persisted across restarts.

A reefy-metrics-publisher collector polls /internal/stats and emits
the counts to the device metrics MQTT topic. Counters are kept in a
JSON sidecar (default `$DATA_DIR/stats.json`) so a container or host
restart doesn't reset them - the graph stays meaningfully cumulative
("tokens spent on codex this lifetime"), not "tokens since the proxy
last started", which is misleading when the user can't see restarts.

Tokens are split by direction (prompt vs completion). Cost analysis
needs the split (completion tokens cost 3-5x more than prompt
tokens across all major providers), and the upstream API returns
them separately, so aggregating loses information for free.
"""

import json
import logging
import os
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {(provider, model): count}
        self._requests: dict[tuple[str, str], int] = defaultdict(int)
        # {(provider, model, direction): count}, direction in
        # {"prompt", "completion"}
        self._tokens: dict[tuple[str, str, str], int] = defaultdict(int)
        # Disk path for persistence; None = ephemeral mode (no-op writes).
        # enable_persistence() turns it on and seeds counters from disk.
        self._persist_path: Path | None = None

    def enable_persistence(self, path: Path) -> None:
        """Bind to a JSON file at `path`. Existing contents are loaded
        into the in-memory counters; subsequent increments flush back
        to the same file atomically.

        Safe to call once at startup. Loading a missing or malformed
        file is non-fatal - we just start from zero and the next
        increment overwrites it cleanly."""
        with self._lock:
            self._persist_path = path
            if not path.exists():
                log.info('stats: starting fresh at %s (no prior file)', path)
                return
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning(
                    'stats: failed to load %s (%s); starting fresh', path, e)
                return
            for entry in raw.get('requests_total') or []:
                try:
                    self._requests[(entry['provider'], entry['model'])] = \
                        int(entry['value'])
                except (KeyError, TypeError, ValueError):
                    pass
            for entry in raw.get('tokens_total') or []:
                try:
                    key = (entry['provider'], entry['model'], entry['direction'])
                    self._tokens[key] = int(entry['value'])
                except (KeyError, TypeError, ValueError):
                    pass
            log.info(
                'stats: loaded %d request-counter(s) + %d token-counter(s) from %s',
                len(self._requests), len(self._tokens), path)

    def add_request(self, provider: str, model: str) -> None:
        with self._lock:
            self._requests[(provider, model)] += 1
            self._persist_unlocked()

    def add_tokens(
        self, provider: str, model: str, prompt: int, completion: int
    ) -> None:
        with self._lock:
            if prompt:
                self._tokens[(provider, model, 'prompt')] += prompt
            if completion:
                self._tokens[(provider, model, 'completion')] += completion
            if prompt or completion:
                self._persist_unlocked()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict:
        """Build the snapshot payload. CALLER MUST HOLD self._lock."""
        return {
            'requests_total': [
                {'provider': p, 'model': m, 'value': v}
                for (p, m), v in self._requests.items()
            ],
            'tokens_total': [
                {'provider': p, 'model': m, 'direction': d, 'value': v}
                for (p, m, d), v in self._tokens.items()
            ],
        }

    def _persist_unlocked(self) -> None:
        """Atomic write of current counters to disk. CALLER MUST HOLD
        self._lock. No-op when persistence isn't enabled."""
        if self._persist_path is None:
            return
        path = self._persist_path
        payload = self._snapshot_unlocked()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix='stats.', suffix='.tmp', dir=str(path.parent))
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            # Disk full, permission denied, etc. - log and keep going;
            # counters stay correct in memory, just won't survive a
            # restart until the next successful write.
            log.warning('stats: persist failed (%s); in-memory still OK', e)


stats = Stats()
