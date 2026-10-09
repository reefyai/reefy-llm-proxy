"""Model discovery follows attachments and upstream catalog parameters."""

import json
import dataclasses
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from reefy_llm_proxy import providers
from reefy_llm_proxy.credentials import CredentialStore
from reefy_llm_proxy.registry import ModelRegistry


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.attach = self.root / 'credentials.json'
        self.cache = self.root / 'models-cache.json'
        self.requests = []
        self.attach_providers('codex')
        self.store = CredentialStore(self.attach, self.root / 'runtime.json')

    def attach_providers(self, *slugs):
        self.attach.write_text(json.dumps({'providers': {
            slug: {'provider': slug, 'access_token': 'test-access',
                   'refresh_token': 'test-refresh', 'expires_at': 9999999999}
            for slug in slugs
        }}))
        if hasattr(self, 'store'):
            self.store.reload()

    def seed(self, slug, models, *, fresh=True, current_params=True):
        raw = json.loads(self.cache.read_text()) if self.cache.exists() else {}
        entry = {'fetched_at': int(time.time()) if fresh else 0, 'models': models}
        if current_params:
            entry['context'] = ModelRegistry(self.cache, 86400, self.store, None)._context(slug)
        raw[slug] = entry
        self.cache.write_text(json.dumps(raw))

    async def registry(self, handler=None):
        def respond(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, json={'models': [{'slug': 'gpt-5.5'}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.aclose)
        return ModelRegistry(self.cache, 86400, self.store, client)

    async def test_restart_drops_disconnected_catalog_from_api_and_disk(self):
        self.seed('xai', [{'id': 'grok-4.5'}])
        self.seed('codex', [{'slug': 'gpt-5.5', 'context_window': 400000}])
        registry = await self.registry()
        models = await registry.list_for_api()
        self.assertEqual([m['id'] for m in models], ['codex/gpt-5.5'])
        self.assertEqual(models[0]['context_window'], 400000)
        self.assertNotIn('xai', json.loads(self.cache.read_text()))
        self.assertEqual(self.requests, [])

    async def test_live_detach_hides_models_and_bare_routes(self):
        self.attach_providers('xai', 'codex')
        self.seed('xai', [{'id': 'grok-4.5'}])
        self.seed('codex', [{'slug': 'gpt-5.5'}])
        registry = await self.registry()
        self.assertEqual(len(await registry.list_for_api()), 2)
        self.attach_providers('codex')
        self.assertIsNone(await registry.resolve_provider('grok-4.5'))
        self.assertEqual(await registry.resolve_provider('gpt-5.5'), 'codex')
        self.assertEqual([m['id'] for m in await registry.list_for_api()],
                         ['codex/gpt-5.5'])

    async def test_disconnected_catalog_does_not_make_active_route_ambiguous(self):
        self.seed('xai', [{'id': 'shared'}])
        self.seed('codex', [{'slug': 'shared'}])
        registry = await self.registry()
        self.assertEqual(await registry.resolve_provider('shared'), 'codex')

    async def test_no_attachments_returns_empty_even_with_disk_cache(self):
        self.attach_providers()
        self.seed('xai', [{'id': 'grok-4.5'}])
        registry = await self.registry()
        self.assertEqual(await registry.list_for_api(), [])
        self.assertIsNone(await registry.resolve_provider('grok-4.5'))
        self.assertEqual(self.requests, [])

    async def test_old_empty_codex_cache_refetches_without_waiting_for_ttl(self):
        self.seed('codex', [], current_params=False)
        registry = await self.registry()
        self.assertEqual([m['id'] for m in await registry.list_for_api()],
                         ['codex/gpt-5.5'])
        request = self.requests[0]
        self.assertEqual(request.url.params['client_version'], '0.155.0-alpha.9.2')
        self.assertEqual(request.headers['user-agent'],
                         'codex_cli_rs/0.155.0-alpha.9.2 (reefy-llm-proxy)')
        self.assertEqual(request.headers['originator'], 'codex_cli_rs')
        await registry.list_for_api()
        self.assertEqual(len(self.requests), 1)
        persisted = json.loads(self.cache.read_text())['codex']
        self.assertEqual(persisted['context'], registry._context('codex'))

    async def test_changed_query_parameters_invalidate_fresh_cache(self):
        self.seed('codex', [])
        spec = dataclasses.replace(providers.get('codex'),
                                   extra_query_params={'client_version': 'fixture-next'})
        with patch.dict(providers.PROVIDERS, {'codex': spec}):
            registry = await self.registry()
            self.assertEqual(len(await registry.list_for_api()), 1)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.requests[0].url.params['client_version'], 'fixture-next')

    async def test_upstream_failure_keeps_only_attached_fallback(self):
        self.seed('xai', [{'id': 'grok-4.5'}])
        self.seed('codex', [{'slug': 'gpt-5.5'}], fresh=False)
        registry = await self.registry(lambda _: httpx.Response(403))
        self.assertEqual([m['id'] for m in await registry.list_for_api()],
                         ['codex/gpt-5.5'])
        self.assertIsNone(await registry.resolve_provider('grok-4.5'))

    async def test_detach_during_fetch_does_not_restore_catalog(self):
        self.attach_providers('xai', 'codex')
        self.seed('xai', [{'id': 'grok-4.5'}], fresh=False)
        self.seed('codex', [{'slug': 'gpt-5.5'}])
        def detach(request):
            self.attach_providers('codex')
            return httpx.Response(200, json={'data': [{'id': 'grok-4.5'}]})
        registry = await self.registry(detach)
        self.assertEqual([m['id'] for m in await registry.list_for_api()],
                         ['codex/gpt-5.5'])
        self.assertNotIn('xai', json.loads(self.cache.read_text()))

    async def test_reattach_fetches_catalog_removed_on_detach(self):
        self.seed('xai', [{'id': 'grok-old'}])
        self.seed('codex', [{'slug': 'gpt-5.5'}])
        registry = await self.registry(
            lambda _: httpx.Response(200, json={'data': [{'id': 'grok-new'}]}))
        await registry.list_for_api()
        self.attach_providers('codex', 'xai')
        self.assertEqual({m['id'] for m in await registry.list_for_api()},
                         {'codex/gpt-5.5', 'xai/grok-new'})
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
