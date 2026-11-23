import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from lawgo_core import LawApi

class BodyRetryTests(unittest.TestCase):
    def test_recovers_and_only_caches_complete_response(self):
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep'):
            api=LawApi('secret-oc',Path(folder),delay=0)
            response=Mock();response.json.return_value={'ok':True}
            api.session.get=Mock(side_effect=[requests.exceptions.ChunkedEncodingError(),response])
            api.on_body_retry=Mock()
            self.assertEqual(api._get('https://example.invalid',{}),{'ok':True})
            self.assertEqual(api.session.get.call_count,2)
            self.assertEqual(api.body_retries,1)
            self.assertEqual(api._get('https://example.invalid',{}),{'ok':True})
            self.assertEqual(api.session.get.call_count,2)
            response.close.assert_called_once()

    def test_exhaustion_is_bounded_and_not_cached(self):
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep'):
            api=LawApi('oc',Path(folder),delay=0)
            api.session.get=Mock(side_effect=requests.exceptions.ChunkedEncodingError())
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):api._get('https://example.invalid',{})
            self.assertEqual(api.session.get.call_count,4)
            self.assertEqual(list(Path(folder).glob('*.json')),[])

    def test_cancel_during_backoff(self):
        class Cancel(BaseException):pass
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep'):
            api=LawApi('oc',Path(folder),delay=0)
            api.session.get=Mock(side_effect=requests.exceptions.ChunkedEncodingError())
            def on_retry(*args):api.check_cancel=Mock(side_effect=Cancel())
            api.on_body_retry=on_retry
            with self.assertRaises(Cancel):api._get('https://example.invalid',{})
            self.assertEqual(api.session.get.call_count,1)

    def test_http_auth_error_not_retried_by_body_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep'):
            api=LawApi('oc',Path(folder),delay=0)
            response=Mock();response.raise_for_status.side_effect=requests.exceptions.HTTPError()
            api.session.get=Mock(return_value=response)
            with self.assertRaises(requests.exceptions.HTTPError):api._get('https://example.invalid',{})
            self.assertEqual(api.session.get.call_count,1)

    def test_rate_limit_honors_retry_after(self):
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep') as sleep:
            api=LawApi('oc',Path(folder),delay=0)
            response=requests.Response();response.status_code=429;response.headers['Retry-After']='3'
            ok=Mock();ok.json.return_value={'ok':True}
            api.session.get=Mock(side_effect=[requests.exceptions.HTTPError(response=response),ok])
            self.assertEqual(api._get('https://example.invalid',{}),{'ok':True})
            self.assertEqual(api.session.get.call_count,2)
            self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list),3)

    def test_transport_error_is_not_missing_clause(self):
        from lawgo_core import extract_law_subunit
        api=Mock();api.law_subunit.side_effect=requests.exceptions.ReadTimeout()
        with self.assertRaises(requests.exceptions.ReadTimeout):
            extract_law_subunit(api,'1',{'article':1})

    def test_long_retry_after_stops_without_early_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch('lawgo_core.time.sleep'):
            api=LawApi('oc',Path(folder),delay=0)
            response=requests.Response();response.status_code=429;response.headers['Retry-After']='600'
            api.session.get=Mock(side_effect=requests.exceptions.HTTPError(response=response))
            with self.assertRaises(requests.exceptions.HTTPError):api._get('https://example.invalid',{})
            self.assertEqual(api.session.get.call_count,1)
