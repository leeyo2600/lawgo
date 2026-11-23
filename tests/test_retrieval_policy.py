import copy
import json
import tempfile
import unittest
import requests
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import lawgo_core as core
import pipeline_core as pc
from elis_client import ElisClient, ElisUnavailable, parse_detail, parse_list
from matcher import HighRecallLawApi
from xlsx_input import XlsxReader
from retrieval_policy import ambiguous_variants, clean_fragments, lookup_spec, resolution_key


TITLE = "고성군 개인택시운송사업면허사무처리 규정"
DETAIL_HTML = '''<div class="title-area"><strong class="tit">고성군 개인택시운송사업면허사무처리 규정</strong>
<p>[시행 2024.10.18] (전부개정) 2024.10.18 훈령 제550호</p></div>
<div class="cont-area"><div class="txt" id="0009006"><p><strong>제9조(거주 요건)</strong>
<p>① 첫째 항의 실제 조문이다.</p><p>② 둘째 항의 실제 조문이다.</p></div>
<div class="txt" id="0010006"><p>제10조(다음 조문) 다음 조문은 포함하면 안 된다.</p></div>
<div class="txt mt50" id="BUC0001">제9조(부칙) 부칙은 본문과 혼동하면 안 된다.</div></div>'''


def spec(raw="사회복지사업법 제34조 5", category="law"):
    return {"raw": raw, "category": category, "slot": "법" if category == "law" else "훈령", "municipality": "강원 고성군"}


def norm(raw):
    return {"normalized": raw, "confidence": "HIGH", "route": "EMBEDDING_EXACT_SKIP_LLM"}


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = HighRecallLawApi("test-only", Path(self.tmp.name))

    def tearDown(self):
        self.api.session.close()
        if hasattr(self.api, "elis"):
            self.api.elis.close()
        self.tmp.cleanup()

    def test_old_name_does_not_bridge_history_or_fetch_other_body(self):
        self.api._law_candidates = Mock(return_value=[{"법령ID": "1", "법령명한글": "현행복지법"}])
        self.api.law_subunit = Mock()
        s = spec("옛복지법 제1조")
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "CURRENT_NAME_NOT_FOUND")
        self.api._law_candidates.assert_called_once_with("옛복지법", "3")
        self.api.law_subunit.assert_not_called()
        self.assertIn("옛 이름·다른 이름", pc.flatten_content(r))

    def test_llm_title_replacement_is_blocked_before_any_api_call(self):
        self.api._law_candidates = Mock()
        s = spec("옛복지법 제1조")
        r = pc.enrich_reference(self.api, s, norm("현행복지법 제1조"))
        self.assertEqual(r["status"], "CURRENT_TITLE_MISMATCH")
        self.api._law_candidates.assert_not_called()

    def test_old_ordinance_name_is_not_fuzzy_replaced(self):
        self.api.current_ordinance_catalog = Mock(return_value=[{"자치법규ID": "1", "자치법규명": "고성군 군계획 조례", "자치법규종류": "조례"}])
        self.api._paged_ordin = Mock(return_value=[])
        self.api.current_ordinance_body = Mock()
        self.api.elis = Mock()
        self.api.elis.find_current.return_value = (None, "ELIS_CURRENT_NAME_NOT_FOUND")
        s = {**spec("고성군 도시계획 조례 제1조", "ordinance"), "slot": "조례"}
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "CURRENT_NAME_NOT_FOUND")
        self.assertTrue(all(c.args[1] == 1 for c in self.api._paged_ordin.call_args_list))
        self.api.current_ordinance_body.assert_not_called()

    def test_empty_input_never_calls_either_source(self):
        self.api._law_candidates = Mock()
        self.api.elis = Mock()
        r = pc.enrich_reference(self.api, spec("", "ordinance"), norm(""))
        self.assertEqual(r["status"], "EMPTY_REFERENCE")
        self.assertEqual(pc.flatten_content(r), "")
        self.api.elis.find_current.assert_not_called()
        self.api._law_candidates.assert_not_called()

    def test_image_and_encoded_image_are_explanations_in_content_cells(self):
        for text in ['제9조(제목) <img src="body.png">', '제9조(제목) &lt;IMG src="body.png"&gt;']:
            self.api._law_candidates = Mock(return_value=[{"법령ID": "1", "법령명한글": "시험법"}])
            self.api.law_subunit = Mock(return_value={"조문내용": text})
            s = spec("시험법 제9조")
            r = pc.enrich_reference(self.api, s, norm(s["raw"]))
            d = pc.detail_from_result(1, {}, s, r)
            out = pc.row_output([d])
            self.assertIn("제목(헤더)과 이미지 태그", out["상위법령_최신조문내용"])
            self.assertNotIn("body.png", out["상위법령_최신조문내용"])
            self.assertEqual(out["검토필요"], "Y")
            self.assertNotEqual(r["status"], "OK")

    def test_exported_workbook_contains_reason_in_body_column_and_keeps_original(self):
        self.api._law_candidates = Mock(return_value=[])
        raw = "옛복지법  제1조"
        s = spec(raw)
        r = pc.enrich_reference(self.api, s, norm(raw))
        d = pc.detail_from_result(2, {}, s, r)
        output = pc.row_output([d])
        sheet = SimpleNamespace(name="결과", rows=[["원본"], [raw]], max_cols=1, max_rows=2)
        path = Path(self.tmp.name)/"test-result.xlsx"
        pc.write_output(sheet, path, {2: output}, [], {}, header_row_num=1)
        rows = XlsxReader(path).read_sheet("결과").rows
        self.assertEqual(rows[1][0], raw)
        self.assertIn("정확한 내용을 불러올 수 없음", rows[1][rows[0].index("상위법령_최신조문내용")])
        self.assertEqual(rows[1][rows[0].index("검토필요")], "Y")

    def test_missing_paragraph_of_image_body_keeps_image_reason(self):
        body = {"조문": [{"조내용": '제9조(제목) <img src="p.png">'}]}
        target = core.parse_reference("시험법 제9조제2항")["targets"][0]
        r = clean_fragments({"status": "OK", "조문": [core.extract_ordin_article(body, target)]})
        self.assertIn("이미지 태그", pc.flatten_content(r))

    def test_header_only_not_accepted_and_good_second_reference_is_preserved(self):
        r = clean_fragments({"status": "OK", "조문": [{"status": "OK", "참조": {"label": "제1조"}, "조문내용": "<p>제1조(목적)</p>"},
                                                             {"status": "OK", "참조": {"label": "제2조"}, "조문내용": "제2조(범위) 정상 본문을 그대로 보존한다."}]})
        content = pc.flatten_content(r)
        self.assertIn("제목(헤더)만", content)
        self.assertIn("정상 본문을 그대로 보존한다", content)
        self.assertEqual(r["status"], "PARTIAL")

    def test_elis_exact_fallback_and_article_boundaries(self):
        self.api.current_ordinance_catalog = Mock(return_value=[])
        self.api._paged_ordin = Mock(return_value=[])
        self.api.elis = Mock()
        found = parse_detail(DETAIL_HTML)
        found["url"] = "https://www.elis.go.kr/alrpop/alrDtlsPop?alrNo=1&histNo=006"
        self.api.elis.find_current.return_value = (found, "ELIS_CURRENT_EXACT")
        s = spec(TITLE+" 제9조제2항", "ordinance")
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "OK")
        self.assertIn("② 둘째 항", pc.flatten_content(r))
        self.assertNotIn("①", pc.flatten_content(r))
        self.assertNotIn("제10조", pc.flatten_content(r))
        self.assertNotIn("부칙", pc.flatten_content(r))
        self.assertIn("elis.go.kr", r["조회출처"])

    def prepare_comparison(self, verdict):
        self.api._law_candidates = Mock(return_value=[{"법령ID": "1", "법령명한글": "사회복지사업법"}])
        def subunit(lid, target):
            if target.get("paragraph") == 5:
                return {"조문내용": "⑤ 사회복지시설 운영의 위탁 기준을 정한다."}
            return {"조문내용": "제34조의5(다른 업무) 보험 가입에 관한 별도의 기준을 정한다."}
        self.api.law_subunit = Mock(side_effect=subunit)
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(verdict, ensure_ascii=False)}}]}
        cm = Mock()
        cm.__enter__ = Mock(return_value=response)
        cm.__exit__ = Mock(return_value=False)
        normalizer = SimpleNamespace(session=Mock(), model="test-model", calls=0, url="https://example.invalid", timeout=1)
        normalizer.session.post.return_value = response
        self.api.normalizer = normalizer
        return lookup_spec(spec(), {"규제명": "사회복지시설 운영의 위탁 기준", "자치법규": {}}, [])

    def test_two_actual_candidate_texts_are_sent_and_selected_with_quotes(self):
        verdict = {"selected": "A", "confidence": "HIGH", "reason": "위탁 기준이 규제명과 일치하고 B는 보험 규정이다.",
                   "context_quote": "사회복지시설 운영의 위탁 기준", "candidate_quote": "사회복지시설 운영의 위탁 기준"}
        s = self.prepare_comparison(verdict)
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["파싱"]["targets"][0]["paragraph"], 5)
        self.assertEqual(self.api.law_subunit.call_count, 2)
        sent = json.loads(self.api.normalizer.session.post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertIn("위탁 기준", sent["candidates"][0]["text"])
        self.assertIn("보험 가입", sent["candidates"][1]["text"])
        self.assertIn("조회판단주석", r)
        d = pc.detail_from_result(1, {}, s, r)
        self.assertIn("CONTENT_COMPARE_LLM", d["처리경로"])

    def test_abstention_and_fabricated_quotes_leave_short_reason_in_cell(self):
        for verdict in [{"selected": None}, {"selected": "A", "confidence": "HIGH", "reason": "추정", "context_quote": "실제 존재하지 않는 문맥", "candidate_quote": "실제 존재하지 않는 본문"}]:
            s = self.prepare_comparison(verdict)
            r = pc.enrich_reference(self.api, s, norm(s["raw"]))
            self.assertEqual(r["status"], "AMBIGUOUS_REFERENCE")
            self.assertEqual(r["조문"], [])
            self.assertIn("확정할 근거 부족", pc.flatten_content(r))
            self.assertEqual(pc.row_output([pc.detail_from_result(1, {}, s, r)])["검토필요"], "Y")

    def test_missing_candidate_is_not_evidence_for_the_other(self):
        s = self.prepare_comparison({"selected": "A"})
        self.api.law_subunit.side_effect = [{"조문내용": "⑤ 위탁에 관한 정상 조문 본문이다."}, {}]
        # The missing subunit now checks the full body too. Keep this test offline.
        self.api.current_law_body = Mock(return_value={})
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "AMBIGUOUS_REFERENCE")
        self.api.normalizer.session.post.assert_not_called()

    def test_unresolved_level_keeps_other_explicit_clause(self):
        s = self.prepare_comparison({"selected": None})
        s["raw"] = "사회복지사업법 제1조, 제34조 5"
        previous = self.api.law_subunit.side_effect
        self.api.law_subunit.side_effect = lambda lid, t: {"조문내용": "제1조(목적) 확인된 목적 조문이다."} if t["article"] == 1 else previous(lid, t)
        r = pc.enrich_reference(self.api, s, norm(s["raw"]))
        self.assertEqual(r["status"], "AMBIGUOUS_REFERENCE")
        self.assertIn("확인된 목적 조문", pc.flatten_content(r))
        self.assertIn("확정할 근거 부족", pc.flatten_content(r))
        payload = json.loads(self.api.normalizer.session.post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertTrue(all("확인된 목적 조문" not in c["text"] for c in payload["candidates"]))

    def test_clear_reference_skips_content_llm_and_spacing_is_allowed(self):
        s = self.prepare_comparison({})
        s["raw"] = "사회복지사업법 제34조 제5항"
        r = pc.enrich_reference(self.api, s, norm("사회복지사업법 제34조제5항"))
        self.assertEqual(r["status"], "OK")
        self.api.normalizer.session.post.assert_not_called()

    def test_ambiguity_cache_depends_on_context_model_and_normalization(self):
        a = lookup_spec(spec(), {"규제명": "위탁"}, [])
        b = lookup_spec(spec(), {"규제명": "보험"}, [])
        self.assertNotEqual(resolution_key(a, norm(a["raw"]), "model-a"), resolution_key(b, norm(b["raw"]), "model-a"))
        self.assertNotEqual(resolution_key(a, norm(a["raw"]), "model-a"), resolution_key(a, norm(a["raw"]), "model-b"))
        clear = spec("사회복지사업법 제34조제5항")
        self.assertEqual(resolution_key(lookup_spec(clear, {"규제명": "위탁"}, []), norm(clear["raw"])), resolution_key(lookup_spec(clear, {"규제명": "보험"}, []), norm(clear["raw"])))

    def test_multiple_references_preserved_when_single_bare_level_expanded(self):
        variants = ambiguous_variants("시험법 제1조, 제34조 5", "시험법 제1조, 제34조제5항")
        self.assertTrue(all(len(core.parse_reference(v)["targets"]) == 2 for v in variants))
        self.assertIsNone(ambiguous_variants("시험법 제34조의5", "시험법 제34조의5"))
        self.assertIsNotNone(ambiguous_variants("시험법 제34조5", "시험법 제34조5"))

    def test_explicit_ranges_and_lists_are_not_branch_ambiguities(self):
        for raw in ["시험법 제7조 제2~3항", "시험법 제7조 2,3항", "시험법 제7조 제2항부터 제3항까지"]:
            self.assertIsNone(ambiguous_variants(raw, "시험법 제7조제2항, 제7조제3항"))
        self.assertEqual(ambiguous_variants("시험법 제34조 5.", "시험법 제34조제5항"), [])

    def test_empty_success_response_is_not_marked_ok(self):
        result = clean_fragments({"status": "OK", "조문": [{"status": "OK", "조문내용": ""}]})
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("확인되지 않음", pc.flatten_content(result))


class ElisTests(unittest.TestCase):
    def test_transient_response_failure_retries_and_success_is_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ElisClient(folder)
            try:
                response = Mock()
                response.text = "successful public response"
                cm = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
                client.session.request = Mock(side_effect=[requests.exceptions.ChunkedEncodingError(), cm])
                with patch("elis_client.time.sleep"):
                    self.assertEqual(client.request("/example"), "successful public response")
                self.assertEqual(client.request("/example"), "successful public response")
                self.assertEqual(client.session.request.call_count, 2)
                self.assertEqual(client.cache_hits, 1)
            finally:
                client.close()

    def test_auth_failure_does_not_retry_or_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ElisClient(folder)
            try:
                response = requests.Response()
                response.status_code = 403
                client.session.request = Mock(side_effect=requests.HTTPError(response=response))
                with self.assertRaises(requests.HTTPError):
                    client.request("/example")
                self.assertEqual(client.session.request.call_count, 1)
                self.assertEqual(list(Path(folder).iterdir()), [])
            finally:
                client.close()

    def test_stop_check_interrupts_before_request(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ElisClient(folder, check_cancel=Mock(side_effect=KeyboardInterrupt))
            try:
                client.session.request = Mock()
                with self.assertRaises(KeyboardInterrupt):
                    client.request("/example")
                client.session.request.assert_not_called()
            finally:
                client.close()

    def test_list_and_detail_structure_must_be_verified(self):
        rows = parse_list('''<a onclick="lawPopup('','','','42820119254005','006'); return false;">고성군 규정</a>''')
        self.assertEqual(rows[0]["histNo"], "006")
        with self.assertRaises(ElisUnavailable):
            parse_detail("<html>maintenance</html>")

    def test_same_named_district_requires_explicit_province_and_official_codes(self):
        page = '''<meta name="_csrf" content="test"><meta name="_csrf_header" content="X-CSRF-TOKEN">
        <select id="cboCtpvCd"><option value="42">강원</option><option value="48">경남</option></select>'''
        with tempfile.TemporaryDirectory() as folder:
            client = ElisClient(folder)
            try:
                client.request = Mock(side_effect=[page, json.dumps([{"sggCd": "820", "sggNm": "고성군"}])])
                self.assertEqual(client.region("강원특별자치도 고성군"), {"ctpvCd": "42", "sggCd": "820"})
                self.assertEqual(client.request.call_args.kwargs["data"], {"ctpvCd": "42"})
                with self.assertRaises(ElisUnavailable):
                    client.region("고성군")
            finally:
                client.close()

    def test_detail_type_mismatch_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ElisClient(folder)
            try:
                client.region = Mock(return_value={"ctpvCd": "42", "sggCd": "820"})
                listing = f'''<a onclick="lawPopup('','','','123','006')">{TITLE}</a>'''
                client.request = Mock(side_effect=[listing, DETAIL_HTML.replace("훈령 제550", "규칙 제550")])
                found, method = client.find_current(TITLE, "강원 고성군", "훈령")
                self.assertIsNone(found)
                self.assertEqual(method, "ELIS_TITLE_OR_KIND_MISMATCH")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
