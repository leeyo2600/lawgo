import tempfile
import json
import unittest
from unittest.mock import Mock
import requests

from llm_normalizer import LLMNormalizer, provider_error_detail


class NormalizerTests(unittest.TestCase):
    def test_403_records_reason_without_leaking_credentials_or_retrying(self):
        with tempfile.TemporaryDirectory() as folder:
            key = 'secret-short-key'
            normalizer = LLMNormalizer('https://example.invalid', 'model', api_key=key, cache_dir=folder)
            response = requests.Response(); response.status_code = 403
            response._content = json.dumps({'error': {'code': 'access_denied', 'message': f'Provider rejected request. API key={key} https://example.invalid?OC=private user@example.invalid'}, 'request': {'Authorization': key}}).encode()
            normalizer._post_batch = Mock(side_effect=requests.HTTPError(response=response))
            item = {'id':'one', 'raw':'주차장법 제19조 제4항'}
            result = normalizer.normalize_many([item])['one']
            diagnostic = result['error_detail']
            self.assertEqual(json.loads(diagnostic)['code'], 'access_denied')
            self.assertIn('Provider rejected request.', diagnostic)
            for secret in (key, 'OC=private', 'user@example.invalid'):
                self.assertNotIn(secret, diagnostic)
            self.assertNotIn('모델 사용 권한', result['review_reason'])
            self.assertEqual(normalizer._post_batch.call_count, 1)
            self.assertIsNone(normalizer._load_cache(item))

    def test_provider_html_error_body_is_not_logged(self):
        response = requests.Response(); response.status_code = 403
        response._content = b'<html>Authorization: hidden-secret</html>'
        detail = provider_error_detail(requests.HTTPError(response=response))
        self.assertEqual(json.loads(detail), {'http_status':403, 'response_format':'non_json'})

    def test_timeout_splits_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            n = LLMNormalizer("https://example.invalid", "model", cache_dir=folder)
            items = [{"id": str(i), "raw": "건축법 제42조"+str(i)} for i in range(2)]
            def post(batch):
                if len(batch)>1:
                    raise requests.exceptions.ReadTimeout()
                return {x["id"]: {"normalized":x["raw"]} for x in batch}
            n._post_batch = Mock(side_effect=post)
            self.assertEqual(len(n.normalize_many(items)),2)
            self.assertEqual(n.failures,0)
            self.assertEqual(n._post_batch.call_count,3)

    def test_missing_id_retried_and_auth_error_not_repeated(self):
        with tempfile.TemporaryDirectory() as folder:
            n = LLMNormalizer("https://example.invalid", "model", cache_dir=folder)
            items = [{"id": str(i), "raw": "건축법 제42조"+str(i)} for i in range(2)]
            n._post_batch = Mock(side_effect=[{"0":{"normalized":items[0]["raw"]}}, {"1":{"normalized":items[1]["raw"]}}])
            out=n.normalize_many(items)
            self.assertFalse(any(x.get("llm_error") for x in out.values()))
            response = requests.Response();response.status_code=401
            n._post_batch = Mock(side_effect=requests.exceptions.HTTPError(response=response))
            failed=n._recover_batch(items)
            self.assertEqual(n._post_batch.call_count,1)
            self.assertEqual(failed["0"]["error_code"],"LLM_HTTP_401")

    def test_provider_default_temperature_and_failure_cache_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            normalizer = LLMNormalizer("https://example.invalid", "model", cache_dir=folder)
            item = {"id": "one", "raw": "건축법 제42조"}
            normalizer._save_cache(item, {"normalized": item["raw"], "llm_error": True})
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": '{"results":[{"id":"0","normalized":"건축법 제42조","confidence":"HIGH","needs_review":false}]}'}}]}
            normalizer.session.post = Mock(return_value=response)
            result = normalizer.normalize_many([item])
            self.assertNotIn("temperature", normalizer.session.post.call_args.kwargs["json"])
            self.assertFalse(result["one"].get("llm_error", False))
            self.assertEqual(normalizer.cache_hits, 0)
            normalizer.normalize_many([item])
            self.assertEqual(normalizer.session.post.call_count, 1)
            self.assertEqual(normalizer.cache_hits, 1)

    def test_failed_call_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            normalizer = LLMNormalizer("https://example.invalid", "model", cache_dir=folder)
            normalizer.session.post = Mock(side_effect=ValueError("bad response"))
            item = {"id":"one", "raw":"건축법 제42조"}
            self.assertTrue(normalizer.normalize_many([item])["one"]["llm_error"])
            self.assertIsNone(normalizer._load_cache(item))
