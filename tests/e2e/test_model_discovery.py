"""Wire-level proxy E2E: real HTTP servers, lifespan, vault, watcher and disk cache.
Only the upstream provider is simulated. No production credentials or LLM calls.
"""
import asyncio
import dataclasses
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest

import httpx
import uvicorn
from fastapi import FastAPI, Request
from reefy_llm_proxy import config, main, providers

SUPPORTED_VERSION = '0.155.0-alpha.9.2'  # Observed live discovery succeeds; 0.21.0 returns [].


class DiscoveryE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = []
        self.wire = []
        self.servers = []
        self.original_specs = dict(providers.PROVIDERS)
        self.original_config = {key: getattr(config, key) for key in (
            'CREDENTIALS_FILE', 'CREDENTIALS_RUNTIME_FILE', 'MODELS_CACHE_FILE', 'STATS_FILE')}
        for key, filename in [('CREDENTIALS_FILE', 'credentials.json'), ('CREDENTIALS_RUNTIME_FILE', 'credentials.runtime.json'), ('MODELS_CACHE_FILE', 'models-cache.json'), ('STATS_FILE', 'stats.json')]:
            setattr(config, key, self.root / filename)
        self.gate_version = False
        self.empty = False
        self.unavailable = False
        self.hold = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        upstream = FastAPI()

        @upstream.get('/{provider}/models')
        async def models(provider: str, request: Request):
            version = request.query_params.get('client_version')
            self.requests.append({'provider': provider, 'client_version': version})
            self.entered.set()
            if self.hold:
                await self.release.wait()
            if self.unavailable:
                from fastapi.responses import JSONResponse
                return JSONResponse({'error': 'fixture outage'}, status_code=400)
            if provider == 'codex':
                token = request.headers.get('authorization', '')
                name = 'gpt-fixture-other-account' if token.endswith('-other') else 'gpt-fixture'
                return {'models': [] if self.empty or (self.gate_version and version != SUPPORTED_VERSION) else [{'slug': name, 'context_window': 123456}]}
            return {'data': [{'id': 'grok-fixture'}]}

        base = await self.start_server(upstream)
        for slug, spec in self.original_specs.items():
            providers.PROVIDERS[slug] = dataclasses.replace(spec, base_url=f'{base}/{slug}')
        self.client = httpx.AsyncClient(timeout=10)

    async def start_server(self, app):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        self.servers.append((server, task))
        for _ in range(200):
            if server.started:
                return f'http://127.0.0.1:{port}'
            if task.done():
                await task
            await asyncio.sleep(.01)
        self.fail('server did not start')

    def attach(self, slugs, suffix=''):
        entries = {slug: {'provider': slug, 'access_token': f'fake-access-{slug}{suffix}', 'refresh_token': f'fake-refresh-{slug}{suffix}', 'expires_at': int(time.time()) + 86400} for slug in slugs}
        temp = self.root / 'attach.tmp'
        temp.write_text(json.dumps({'providers': entries}))
        temp.replace(config.CREDENTIALS_FILE)
        return entries

    async def proxy(self):
        self.base = await self.start_server(main.app)
        # Let the real inotify watcher arm before modifying credentials.
        await asyncio.sleep(.15)

    async def models(self):
        response = await self.client.get(self.base + '/v1/models')
        body = response.json()
        self.wire.append({'status': response.status_code, 'body': body})
        self.assertEqual(response.status_code, 200)
        return [m['id'] for m in body['data']]

    async def wait_attachment(self, keys):
        for _ in range(200):
            if set(main.app.state.store.list_keys()) == set(keys):
                return
            await asyncio.sleep(.02)
        self.fail(f'watcher did not apply attachment keys {keys}')

    async def asyncTearDown(self):
        self.release.set()
        for server, task in reversed(self.servers):
            server.should_exit = True
            await asyncio.wait_for(task, 5)
        await self.client.aclose()
        artifact = os.environ.get('E2E_ARTIFACT_DIR')
        if artifact:
            out = Path(artifact)
            out.mkdir(parents=True, exist_ok=True)
            (out / (self._testMethodName + '.json')).write_text(json.dumps({'requests_to_upstream': self.requests, 'proxy_responses': self.wire}, indent=2))
        providers.PROVIDERS.clear()
        providers.PROVIDERS.update(self.original_specs)
        for key, value in self.original_config.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    async def test_codex_discovery_uses_supported_client_version(self):
        self.gate_version = True
        self.attach(['codex'])
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(self.requests[0]['client_version'], SUPPORTED_VERSION)

    async def test_migrates_actual_stale_xai_and_empty_codex_cache(self):
        self.attach(['codex'])
        config.MODELS_CACHE_FILE.write_text(json.dumps({
            'xai': {'fetched_at': int(time.time()), 'models': [{'id': 'grok-fixture'}]},
            'codex': {'fetched_at': int(time.time()), 'models': []}}))
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertNotIn('xai', json.loads(config.MODELS_CACHE_FILE.read_text()))

    async def test_detach_filters_listing_and_bare_name_routing(self):
        self.attach(['xai', 'codex'])
        await self.proxy()
        self.assertEqual(set(await self.models()), {'xai/grok-fixture', 'codex/gpt-fixture'})
        self.attach(['codex'])
        await self.wait_attachment(['codex'])
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertIsNone(await main.app.state.registry.resolve_provider('grok-fixture'))

    async def test_detach_all_does_not_resurrect_rotated_credentials(self):
        entries = self.attach(['xai'])
        config.CREDENTIALS_RUNTIME_FILE.write_text(json.dumps({'providers': entries, 'derived_from_attach': {'xai': entries['xai']['refresh_token']}}))
        await self.proxy()
        self.assertEqual(await self.models(), ['xai/grok-fixture'])
        self.attach([])
        await asyncio.sleep(.5)
        self.assertEqual(await self.models(), [])
        self.assertEqual(main.app.state.store.list_keys(), [])

    async def test_reattach_during_discovery_cannot_publish_old_account_result(self):
        self.attach(['codex'])
        await self.proxy()
        self.hold = True
        request = asyncio.create_task(self.models())
        await asyncio.wait_for(self.entered.wait(), 2)
        self.attach(['codex'], suffix='-other')
        for _ in range(200):
            if main.app.state.store.get('codex').access_token.endswith('-other'):
                break
            await asyncio.sleep(.02)
        self.release.set()
        self.assertNotIn('codex/gpt-fixture', await request)
        self.hold = False
        self.assertEqual(await self.models(), ['codex/gpt-fixture-other-account'])

    async def test_cache_reused_and_last_good_list_survives_upstream_error(self):
        self.attach(['codex'])
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(len(self.requests), 1)
        main.app.state.registry._cache['codex']['fetched_at'] = 0
        self.unavailable = True
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(len(self.requests), 2)

    async def test_empty_list_retries_after_short_ttl(self):
        self.attach(['codex'])
        self.empty = True
        await self.proxy()
        self.assertEqual(await self.models(), [])
        self.empty = False
        main.app.state.registry._cache['codex']['fetched_at'] -= 61
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(len(self.requests), 2)

    async def test_discovery_profile_change_invalidates_fresh_cache(self):
        self.attach(['codex'])
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        spec = providers.PROVIDERS['codex']
        providers.PROVIDERS['codex'] = dataclasses.replace(spec, extra_query_params={'client_version': 'fixture-next'})
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(self.requests[-1]['client_version'], 'fixture-next')

    async def test_positive_cache_survives_proxy_restart(self):
        self.attach(['codex'])
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        server, task = self.servers.pop()
        server.should_exit = True
        await task
        await self.proxy()
        self.assertEqual(await self.models(), ['codex/gpt-fixture'])
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
