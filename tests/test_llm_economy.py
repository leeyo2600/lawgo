import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from embedding_normalizer import EmbeddingRoutedNormalizer
from llm_normalizer import LLMNormalizer
from llm_runtime import llm_metrics, request_json
from upper_law_suggestions import evidence_sources, suggest_upper_laws


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.n = LLMNormalizer("https://example.invalid", "model-a", cache_dir=self.tmp.name)

    def tearDown(self):
        self.n.session.close()
        self.tmp.cleanup()

    def item(self, id, municipality="강원 고성군", category="law"):
        return {"id": id, "raw": "건축법 제42조 제1항", "category": category, "slot": "법", "municipality": municipality}

    def test_national_duplicates_coalesce_and_cached_ids_are_rebound(self):
        self.n._post_batch = Mock(side_effect=lambda batch: {i["id"]: {"normalized": "건축법 제42조제1항"} for i in batch})
        items = [self.item("a"), self.item("b", "경남 고성군")]
        out = self.n.normalize_many(items)
        self.assertEqual(len(self.n._post_batch.call_args.args[0]), 1)
        self.assertEqual(out["a"]["id"], "a")
        self.assertEqual(out["b"]["id"], "b")
        again = self.n.normalize_many([self.item("c", "경기 수원시")])
        self.assertEqual(again["c"]["id"], "c")
        self.assertEqual(self.n._post_batch.call_count, 1)
        out["a"]["corrections"].append({"reason": "test"})
        self.assertEqual(out["b"]["corrections"], [])

    def test_local_references_remain_region_specific(self):
        self.n._post_batch = Mock(side_effect=lambda batch: {i["id"]: {"normalized": i["raw"]} for i in batch})
        self.n.normalize_many([self.item("a", category="ordinance"), self.item("b", "경남 고성군", "ordinance")])
        self.assertEqual(len(self.n._post_batch.call_args.args[0]), 2)

    def test_short_wire_ids_and_usage_do_not_change_original_ids(self):
        def post(url, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            self.assertEqual([i["id"] for i in payload["items"]], ["0", "1"])
            self.assertNotIn("municipality", payload["items"][0])
            self.assertIn("municipality", payload["items"][1])
            response = Mock()
            response.json.return_value = {"usage": {"prompt_tokens": 110, "completion_tokens": 20},
                "choices": [{"message": {"content": __import__("json").dumps({"results": [{"id": i["id"], "normalized": i["raw"]} for i in payload["items"]]})}}]}
            return response
        self.n.session.post = Mock(side_effect=post)
        result = self.n.normalize_many([self.item("a"*40), self.item("b"*40, category="ordinance")])
        self.assertEqual(result["a"*40]["id"], "a"*40)
        metrics = llm_metrics(self.n)
        self.assertEqual(metrics["llm_reported_input_tokens"], 110)
        self.assertEqual(metrics["llm_reported_output_tokens"], 20)
        self.assertEqual(metrics["llm_usage_unreported_requests"], 0)
        usage = list(Path(self.tmp.name).glob("usage_*.json"))
        self.assertEqual(len(usage), 1)
        self.assertNotIn("Authorization", usage[0].read_text())

    def test_timeout_tree_is_bounded_and_server_error_does_not_split(self):
        batch = [self.item(str(i)) for i in range(10)]
        self.n._post_batch = Mock(side_effect=requests.ReadTimeout())
        with patch("llm_normalizer.time.sleep"):
            result = self.n._recover_batch(batch)
        self.assertEqual(self.n._post_batch.call_count, 5)
        self.assertTrue(all(r["llm_error"] for r in result.values()))
        for status in (429, 503):
            response = requests.Response()
            response.status_code = status
            self.n._post_batch = Mock(side_effect=requests.HTTPError(response=response))
            with patch("llm_normalizer.time.sleep"):
                self.n._recover_batch(batch)
            self.assertEqual(self.n._post_batch.call_count, 2)
            self.assertTrue(all(len(c.args[0]) == 10 for c in self.n._post_batch.call_args_list))

    def test_missing_results_retry_as_one_group_without_resending_success(self):
        batch = [self.item(str(i)) for i in range(4)]
        self.n._post_batch = Mock(side_effect=[{"0": {"normalized": "건축법 제42조제1항"}},
                                              {str(i): {"normalized": "건축법 제42조제1항"} for i in range(1, 4)}])
        out = self.n._recover_batch(batch)
        self.assertEqual(set(out), {"0", "1", "2", "3"})
        self.assertEqual(self.n._post_batch.call_count, 2)
        self.assertEqual([i["id"] for i in self.n._post_batch.call_args.args[0]], ["1", "2", "3"])

    def test_provider_denial_stops_later_batches_and_is_not_persisted(self):
        self.n.batch_size = 1
        response = requests.Response()
        response.status_code = 403
        self.n._post_batch = Mock(side_effect=requests.HTTPError(response=response))
        items = [{**self.item(str(i)), "raw": f"건축법 제{i+1}조"} for i in range(4)]
        result = self.n.normalize_many(items)
        self.assertTrue(all(r["llm_error"] for r in result.values()))
        self.n.normalize_many([{**self.item("last"), "raw": "건축법 제9조"}])
        self.assertEqual(self.n._post_batch.call_count, 1)
        fresh = LLMNormalizer("https://example.invalid", "model-a", cache_dir=self.tmp.name)
        try:
            fresh._post_batch = Mock(return_value={"0": {"normalized": "건축법 제1조"}})
            self.assertFalse(fresh.normalize_many(items[:1])["0"].get("llm_error", False))
            fresh._post_batch.assert_called_once()
        finally:
            fresh.session.close()

    def test_ambiguity_and_missing_location_skip_only_the_normalization_call(self):
        items = [self.item("amb"), self.item("missing"), self.item("range")]
        for i, raw in zip(items, ["사회복지사업법 제34조 5", "건축법", "시험법 제7조 제2~3항"]):
            i["raw"] = raw
        audits = [{"score": .99, "flag": True, "comparison": i["raw"], "reason": "표기 차이"} for i in items]
        n = EmbeddingRoutedNormalizer("https://example.invalid", "model", cache_dir=self.tmp.name)
        try:
            n._post_batch = Mock(return_value={"range": {"normalized": "시험법 제7조제2항부터 제3항까지"}})
            with patch("embedding_normalizer.embedding_audits", return_value=audits):
                out = n.normalize_many(items)
            self.assertEqual([i["id"] for i in n._post_batch.call_args.args[0]], ["range"])
            self.assertEqual(out["amb"]["normalized"], items[0]["raw"])
            self.assertEqual(out["amb"]["pre_llm_audit"]["score"], .99)
            self.assertTrue(out["amb"]["pre_llm_audit"]["flag"])
            self.assertEqual(n.preflight_skipped, 2)
        finally:
            n.session.close()

    def test_only_actual_llm_candidates_fill_configured_batches(self):
        items = [{**self.item(str(i)), "raw": f"시험법 제{i+1}조"} for i in range(20)]
        audits = [{"score": .99 if i % 2 else 1.0, "flag": bool(i % 2), "comparison": item["raw"], "reason": "표기 차이"} for i, item in enumerate(items)]
        n = EmbeddingRoutedNormalizer("https://example.invalid", "model", batch_size=5, cache_dir=self.tmp.name)
        try:
            n._post_batch = Mock(side_effect=lambda batch: {i["id"]: {"normalized": i["raw"]} for i in batch})
            with patch("embedding_normalizer.embedding_audits", return_value=audits):
                n.normalize_many(items)
            self.assertEqual([len(c.args[0]) for c in n._post_batch.call_args_list], [5, 5])
        finally:
            n.session.close()

    def test_aux_cache_includes_model_prompt_context_and_records_unknown_usage(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"candidates":[]}'}}]}
        self.n.session.post = Mock(return_value=response)
        valid = lambda x: isinstance(x, dict) and isinstance(x.get("candidates"), list)
        self.assertFalse(request_json(self.n, "suggestions", "prompt", {"text": "본문"}, valid)[1])
        self.assertTrue(request_json(self.n, "suggestions", "prompt", {"text": "본문"}, valid)[1])
        self.assertEqual(self.n.session.post.call_count, 1)
        self.assertEqual(llm_metrics(self.n)["llm_usage_unreported_requests"], 1)
        request_json(self.n, "suggestions", "prompt", {"text": "다른 본문"}, valid)
        request_json(self.n, "suggestions", "different prompt", {"text": "본문"}, valid)
        self.assertEqual(self.n.session.post.call_count, 3)
        other = LLMNormalizer("https://example.invalid", "model-b", cache_dir=self.tmp.name)
        try:
            other.session.post = Mock(return_value=response)
            self.assertFalse(request_json(other, "suggestions", "prompt", {"text": "본문"}, valid)[1])
        finally:
            other.session.close()

    def test_aux_http_errors_are_not_cached(self):
        self.n.session.post = Mock(side_effect=requests.ReadTimeout())
        for _ in range(2):
            with self.assertRaises(requests.ReadTimeout):
                request_json(self.n, "comparison", "prompt", {}, lambda _: True)
        self.assertEqual(self.n.session.post.call_count, 2)
        self.assertFalse((Path(self.tmp.name)/"aux").exists())

    def test_suggestions_skip_no_citation_and_remove_duplicate_irrelevant_paragraphs(self):
        detail = {"구분": "자치법규", "최종상태": "OK", "현행매칭명": "시험 조례", "추출내용": "군수는 교육을 실시한다."}
        result = suggest_upper_laws({"상위법령": {}}, [detail], self.n, Mock())
        self.assertFalse(result[2])
        self.assertEqual(self.n.calls, 0)
        detail["추출내용"] = "안내\n"*100 + "건축법 제42조에 따른 사항이다.\n끝"
        sources = evidence_sources([detail, detail], ["법"])
        self.assertEqual(len(sources), 1)
        self.assertIn("건축법 제42조", sources[0]["text"])
        self.assertLess(len(sources[0]["text"]), 100)
        self.assertEqual(evidence_sources([detail], ["시행규칙"]), [])


if __name__ == "__main__":
    unittest.main()
