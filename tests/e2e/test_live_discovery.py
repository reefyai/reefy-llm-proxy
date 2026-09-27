"""Explicit opt-in live GET-only verification; never submits a completion."""
import json
import os
from pathlib import Path
import unittest
import urllib.request


@unittest.skipUnless(os.environ.get('PROXY_LIVE_DISCOVERY') == '1', 'opt-in live model discovery')
class LiveDiscovery(unittest.TestCase):
    def test_live_proxy_and_cc_only_list_expected_provider(self):
        expected = os.environ['PROXY_EXPECT_PROVIDER']
        urls = {'proxy': os.environ.get('PROXY_MODELS_URL', 'http://127.0.0.1:9080/v1/models')}
        if os.environ.get('CC_LIVE_MODELS_URL'):
            urls['cc'] = os.environ['CC_LIVE_MODELS_URL']
        evidence = {}
        try:
            for label, url in urls.items():
                with urllib.request.urlopen(url, timeout=30) as response:
                    body = json.load(response)
                    ids = body['models'] if label == 'cc' else [m['id'] for m in body['data']]
                    evidence[label] = {'status': response.status, 'models': ids}
            ids = evidence['proxy']['models']
            self.assertTrue(ids, 'expected attached provider models, got an empty list')
            self.assertTrue(all(i.startswith(expected + '/') for i in ids), ids)
            if 'cc' in evidence:
                self.assertEqual(set(ids), set(evidence['cc']['models']))
        finally:
            if os.environ.get('E2E_ARTIFACT_DIR'):
                root = Path(os.environ['E2E_ARTIFACT_DIR'])
                root.mkdir(parents=True, exist_ok=True)
                (root / 'live-discovery.json').write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence, indent=2))
