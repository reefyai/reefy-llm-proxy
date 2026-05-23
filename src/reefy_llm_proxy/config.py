"""Runtime configuration sourced from env vars.

All values have sensible defaults that match the production container
deployment described in PLAN-reefy-llm-proxy.md. Override via env vars
for local development or alternate deployment targets.
"""

import os
from pathlib import Path


LISTEN_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9080'))

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
# Two-file model:
#   credentials.json         - reconciler-owned, overwritten on every
#                              state apply. Holds the "what the user
#                              attached" snapshot.
#   credentials.runtime.json - proxy-owned. Holds rotated tokens.
#                              The proxy prefers this if its mtime is
#                              >= credentials.json's mtime (i.e. we
#                              rotated AFTER the last attach); if
#                              credentials.json is newer, the user
#                              just re-attached and we drop the
#                              runtime copy.
CREDENTIALS_FILE = DATA_DIR / 'credentials.json'
CREDENTIALS_RUNTIME_FILE = DATA_DIR / 'credentials.runtime.json'
MODELS_CACHE_FILE = DATA_DIR / 'models-cache.json'
# Lifetime request + token counters. Re-loaded on startup so the
# /internal/stats endpoint (and the dashboard graph fed from it)
# stays cumulative across container restarts.
STATS_FILE = DATA_DIR / 'stats.json'

# How long a /v1/models pull is considered fresh before re-fetching.
# Provider model lists change on the order of weeks; 24h is plenty.
# First-time empty cache always fetches regardless.
MODELS_CACHE_TTL_S = int(os.environ.get('MODELS_CACHE_TTL_S', '86400'))

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
