import tempfile
import unittest
from unittest.mock import Mock, patch

import pipeline_core as pc
from embedding_normalizer import EmbeddingRoutedNormalizer
from pre_llm_audit import format_score


class EmbeddingRoutingTests(unittest.TestCase):
    def test_exact_one_skips_llm_but_point_99_is_corrected_and_parsed(self):
        items=[{"id":"same","raw":"건축법 제42조","slot":"법","category":"law"}, {"id":"range","raw":"담배사업법 시행령 제7조 제2~3항","slot":"시행령","category":"law"}]
        audits=[{"score":1.0,"flag":False,"method":"dragonkue/BGE-m3-ko","comparison":items[0]["raw"],"reason":"동일"}, {"score":.99,"flag":True,"method":"dragonkue/BGE-m3-ko","comparison":"담배사업법 시행령 제7조제3항","reason":"범위 표기 차이"}]
        corrected={"normalized":"담배사업법 시행령 제7조제2항부터 제3항까지","confidence":"HIGH","needs_review":False,"corrections":[{"type":"RANGE","before":"제2~3항","after":"제2항부터 제3항까지","reason":"생략된 항 표시를 보완"}]}
        with tempfile.TemporaryDirectory() as folder, patch("embedding_normalizer.embedding_audits",return_value=audits):
            normalizer=EmbeddingRoutedNormalizer("https://example.invalid","model",cache_dir=folder)
            normalizer._post_batch=Mock(return_value={"range":corrected})
            results=normalizer.normalize_many(items)
            sent=normalizer._post_batch.call_args.args[0]
            self.assertEqual([x["id"] for x in sent],["range"])
            self.assertEqual(results["same"]["route"],"EMBEDDING_EXACT_SKIP_LLM")
            self.assertEqual(normalizer.direct_count,1)
            self.assertEqual(normalizer.flagged_count,1)
            res=pc.enrich_reference(None,items[1],results["range"],offline_api=True)
            self.assertEqual([t["label"] for t in res["파싱"]["targets"]],["제7조제2항","제7조제3항"])
            detail=pc.detail_from_result(1,{},items[1],res)
            out=pc.row_output([detail])
            self.assertEqual(out["LLM전_유사도_FLAG"],"Y")
            self.assertIn("0.99000000",out["LLM전_유사도_주석"])
            self.assertIn("생략된 항 표시를 보완",out["변경사유_주석"])
            self.assertIn("제7조제2항",out["LLM후_정규식해석"])
            normalizer.session.close()

    def test_no_rounding_bypass_and_empty_skipped(self):
        tiny=0.9999999999999999
        items=[{"id":"almost","raw":"건축법 제42 조"},{"id":"empty","raw":" "}]
        audits=[{"score":tiny,"flag":True,"comparison":"건축법 제42조","reason":"띄어쓰기"},{"score":None,"flag":False}]
        with tempfile.TemporaryDirectory() as folder, patch("embedding_normalizer.embedding_audits",return_value=audits):
            normalizer=EmbeddingRoutedNormalizer("https://example.invalid","model",cache_dir=folder)
            normalizer._post_batch=Mock(return_value={"almost":{"normalized":"건축법 제42조"}})
            results=normalizer.normalize_many(items)
            self.assertEqual(normalizer.direct_count,0)
            self.assertEqual(normalizer._post_batch.call_count,1)
            self.assertEqual(results["empty"]["route"],"EMPTY_SKIPPED")
            self.assertNotEqual(format_score(tiny),"1.00000000")
            normalizer.session.close()
