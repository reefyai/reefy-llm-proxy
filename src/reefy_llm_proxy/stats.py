"""In-memory counters for /internal/stats.

A reefy-metrics-publisher collector will poll this endpoint and emit
the counts to the device metrics MQTT topic. v1 ships counters only;
latency added later if useful.

Tokens are split by direction (prompt vs completion). Cost analysis
needs the split (completion tokens cost 3-5x more than prompt
tokens across all major providers), and the upstream API returns
them separately, so aggregating loses information for free.
"""

import threading
from collections import defaultdict


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {(provider, model): count}
        self._requests: dict[tuple[str, str], int] = defaultdict(int)
        # {(provider, model, direction): count}, direction in
        # {"prompt", "completion"}
        self._tokens: dict[tuple[str, str, str], int] = defaultdict(int)

    def add_request(self, provider: str, model: str) -> None:
        with self._lock:
            self._requests[(provider, model)] += 1

    def add_tokens(
        self, provider: str, model: str, prompt: int, completion: int
    ) -> None:
        with self._lock:
            if prompt:
                self._tokens[(provider, model, 'prompt')] += prompt
            if completion:
                self._tokens[(provider, model, 'completion')] += completion

    def snapshot(self) -> dict:
        with self._lock:
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


stats = Stats()
