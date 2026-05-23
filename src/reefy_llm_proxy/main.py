"""FastAPI app composition + entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from watchfiles import Change, awatch

from . import config
from .credentials import CredentialStore
from .proxy import forward
from .registry import ModelRegistry
from .stats import stats


logging.basicConfig(
    level=config.LOG_LEVEL,
    format='%(asctime)s %(levelname)-5s %(name)s: %(message)s',
)
log = logging.getLogger('reefy-llm-proxy')


async def _watch_attach_file(
    store: CredentialStore, attach_path: Path,
) -> None:
    """Reload `store` whenever credentials.json is modified, added,
    or replaced. Watches the parent directory because atomic writers
    (tempfile + rename) replace the inode, which would invalidate a
    file-level inotify watch."""
    target = attach_path.resolve()
    parent = target.parent
    log.info('credentials watcher armed on %s', target)
    try:
        async for changes in awatch(str(parent), recursive=False):
            relevant = any(
                kind in (Change.added, Change.modified)
                and Path(path).resolve() == target
                for kind, path in changes
            )
            if not relevant:
                continue
            log.info('credentials.json changed on disk; reloading')
            try:
                store.reload()
            except Exception as e:
                # Log + keep the watcher alive. A bad write event
                # shouldn't take the proxy down; the next valid
                # event will reload again.
                log.error('credential reload failed: %s', e)
    except asyncio.CancelledError:
        log.info('credentials watcher stopped')
        raise


# Singletons. Built in `lifespan`, attached to app.state so handlers
# can reach them without globals.
@asynccontextmanager
async def lifespan(app: FastAPI):
    store = CredentialStore(
        attach_path=config.CREDENTIALS_FILE,
        runtime_path=config.CREDENTIALS_RUNTIME_FILE,
    )
    stats.enable_persistence(config.STATS_FILE)
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    registry = ModelRegistry(
        cache_file=config.MODELS_CACHE_FILE,
        ttl_s=config.MODELS_CACHE_TTL_S,
        store=store,
        client=client,
    )
    app.state.store = store
    app.state.client = client
    app.state.registry = registry
    log.info('reefy-llm-proxy ready; %d credential(s) loaded',
             len(store.list_keys()))
    watcher = asyncio.create_task(
        _watch_attach_file(store, config.CREDENTIALS_FILE),
        name='credentials-watcher',
    )
    try:
        yield
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        await client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get('/healthz')
async def healthz() -> dict:
    return {'ok': True}


@app.get('/internal/stats')
async def internal_stats() -> dict:
    """Polled by reefy-metrics-publisher collector. No auth - the
    collector lives on the same Docker network as the proxy and the
    network is the access boundary."""
    return stats.snapshot()


@app.get('/v1/models')
async def list_models(request: Request) -> dict:
    """Union of /v1/models from each attached provider, prefixed.
    See registry.list_for_api() for the dynamic-pull-and-cache logic."""
    data = await request.app.state.registry.list_for_api()
    return {'object': 'list', 'data': data}


# Forward everything else under /v1/* to the matched upstream.
@app.api_route(
    '/v1/{path:path}',
    methods=['POST', 'PUT', 'PATCH', 'DELETE', 'GET'],
)
async def forward_v1(request: Request, path: str):
    if path == 'models':
        # FastAPI prefers the literal route above, but be defensive.
        return await list_models(request)
    return await forward(
        request, path,
        store=request.app.state.store,
        registry=request.app.state.registry,
        client=request.app.state.client,
    )


@app.exception_handler(404)
async def not_found(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={'error': {
            'message': f'unknown path: {request.url.path}',
            'type': 'reefy_llm_proxy',
        }},
    )


def main() -> None:
    import uvicorn
    uvicorn.run(
        'reefy_llm_proxy.main:app',
        host=config.LISTEN_HOST,
        port=config.LISTEN_PORT,
        log_level=config.LOG_LEVEL.lower(),
        # 30MB request body limit - covers multimodal inputs
        # (a couple of high-res images, or a short PDF).
        h11_max_incomplete_event_size=30 * 1024 * 1024,
    )


if __name__ == '__main__':
    main()
