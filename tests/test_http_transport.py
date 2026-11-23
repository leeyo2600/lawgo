import os
from pathlib import Path
import ssl
import subprocess
import sys
import unittest
from unittest.mock import patch

import requests
import http_transport as transport


class TransportTests(unittest.TestCase):
    def test_import_after_requests_does_not_break_existing_ssl_context(self):
        code = '''
import ssl, requests
original = ssl.SSLContext
old = getattr(requests.adapters, '_preloaded_ssl_context', None)
if old is None:
    old = ssl.create_default_context()
import web_app, llm_normalizer, lawgo_core
assert ssl.SSLContext is original
old.verify_mode = ssl.CERT_REQUIRED
print('OK')
'''
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(sys.path)
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(transport.TRUSTSTORE_ACTIVE, 'truststore not installed')
    def test_https_adapter_keeps_verification_and_system_context(self):
        with transport.create_session() as session:
            self.assertTrue(session.verify)
            adapter = session.get_adapter('https://example.invalid')
            ctx = adapter.ssl_context
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(adapter.max_retries.total, 0)
            self.assertIs(adapter.poolmanager.connection_pool_kw['ssl_context'], ctx)
            manager = adapter.proxy_manager_for('http://proxy.invalid:8080')
            self.assertIs(manager.connection_pool_kw['ssl_context'], ctx)
            if hasattr(requests.adapters.HTTPAdapter, 'build_connection_pool_key_attributes'):
                request = requests.Request('GET', 'https://example.invalid').prepare()
                _, kwargs = adapter.build_connection_pool_key_attributes(request, True)
                self.assertIs(kwargs['ssl_context'], ctx)
                self.assertEqual(kwargs['cert_reqs'], 'CERT_REQUIRED')

    def test_missing_truststore_uses_verified_requests_default(self):
        with patch.object(transport, 'TRUSTSTORE_ACTIVE', False):
            with transport.create_session() as session:
                self.assertTrue(session.verify)
                self.assertNotIsInstance(session.get_adapter('https://example.invalid'), transport.SystemTrustAdapter)


if __name__ == '__main__':
    unittest.main()
