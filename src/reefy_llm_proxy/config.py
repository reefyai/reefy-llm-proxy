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
CREDENTIALS_FILE = DATA_DIR / 'credentials.json'
MODELS_CACHE_FILE = DATA_DIR / 'models-cache.json'

# How long a /v1/models pull is considered fresh before re-fetching.
# Provider model lists change on the order of weeks; 24h is plenty.
# First-time empty cache always fetches regardless.
MODELS_CACHE_TTL_S = int(os.environ.get('MODELS_CACHE_TTL_S', '86400'))

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
