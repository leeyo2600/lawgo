import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import lawgo_core as core
import pipeline_core as pc
import repair_failed as rf
from retrieval_policy import clean_fragments
from test_failed_repair import fixture
from xlsx_input import XlsxReader


def target(raw):
    return core.parse_reference('시험법 ' + raw)['targets'][0]


class PartialFallbackTests(unittest.TestCase):
    def test_exact_item_is_preferred(self):
        r = core.extract_text_target('제2조(제목) ① 내용이다. 1. 첫째 내용 2. 둘째 내용 ② 다른 항이다.', target('제2조제1항제2호'))
        self.assertEqual(r['status'], 'OK')
        self.assertIn('둘째', r['조문내용'])
        self.assertNotIn('첫째', r['조문내용'])

    def test_missing_item_provides_only_its_parent_with_reason(self):
        r = core.extract_text_target('제2조(제목) ① 요청한 항이다. ② 다른 항이다.', target('제2조제1항제2호'))
        self.assertEqual(r['status'], 'PARENT_FALLBACK')
        self.assertEqual(r['대체참조']['label'], '제2조제1항')
        text = pc.flatten_content(clean_fragments({'조문': [r]}))
        self.assertIn('까지만 제공', text)
        self.assertNotIn('다른 항이다', text)
        self.assertFalse(rf.usable(text))
        self.assertIsNotNone(rf.classify('시험법 제2조제1항제2호', text, 'upper'))

    def test_missing_paragraph_does_not_use_whole_article(self):
        r = core.extract_text_target('제2조(제목) ① 첫째 항이다.', target('제2조제3항제1호'))
        self.assertEqual(r['status'], 'PARAGRAPH_NOT_FOUND')
        self.assertIsNone(r['조문내용'])

    def test_branch_item_cannot_masquerade_as_plain_item(self):
        r = core.extract_text_target('제2조(제목) ① 다음과 같다. 2. 일반 호의 내용', target('제2조제1항제2호의2'))
        self.assertEqual(r['status'], 'PARENT_FALLBACK')
        self.assertEqual(r['대체참조']['label'], '제2조제1항')

    def test_empty_national_subunit_checks_exact_article_in_current_body(self):
        api = Mock()
        api.law_subunit.return_value = {}
        api.current_law_body.return_value = {'법령': {'조문': {'조문단위': [
            {'조문번호': '2', '조문가지번호': '1', '조문내용': '제2조의1(오답) 다른 조문이다.'},
            {'조문번호': '2', '조문내용': '제2조(제목)', '항': [{'항내용': '① 실제 본문이다.'}]}]},
            '부칙': {'조문번호': '2', '조문내용': '제2조(부칙) 혼동하면 안 됨'}}}
        r = core.extract_law_subunit(api, '123', target('제2조제1항'))
        self.assertEqual(r['status'], 'OK')
        self.assertIn('실제 본문', r['조문내용'])
        self.assertNotIn('부칙', r['조문내용'])
        api.current_law_body.assert_called_once_with('123')

    def test_missing_branch_cannot_use_main_article(self):
        api = Mock()
        api.law_subunit.return_value = {}
        api.current_law_body.return_value = {'법령': {'조문': {'조문번호': '2', '조문내용': '제2조(제목) 잘못된 본문'}}}
        self.assertEqual(core.extract_law_subunit(api, '123', target('제2조의1'))['status'], 'EMPTY_RESULT')

    def test_deleted_article_is_not_a_recovered_first_paragraph(self):
        api = Mock()
        api.law_subunit.return_value = {}
        api.current_law_body.return_value = {'법령': {'조문': {'조문번호': '2', '조문내용': '제2조 삭제 <2017.12.29>'}}}
        r = core.extract_law_subunit(api, '123', target('제2조제1항'))
        self.assertEqual(r['status'], 'ARTICLE_DELETED')
        self.assertIsNone(r['조문내용'])
        self.assertIn('현행 조문이 삭제', r['조회불가사유'])

    def test_image_cleanup_needs_no_result_and_preserves_text(self):
        t = {'old_body': '[제2조] 읽을 수 있는 본문 <img src="secret.png">', 'expected_labels': ['제2조']}
        cleaned = rf.partial_body(t, {})
        self.assertNotIn('<img', cleaned)
        self.assertNotIn('secret.png', cleaned)
        self.assertIn('읽을 수 있는 본문', cleaned)
        self.assertIn('이미지에 포함된 본문은 확인 불가', cleaned)

    def test_partial_merge_preserves_good_sibling_and_diagnoses_missing_annex(self):
        t = {'old_body': '[제2조] 이미 확인한 본문', 'expected_labels': ['제2조', '제3조', '별표 1']}
        r = clean_fragments({'status': 'PARTIAL', '조문': [
            {'참조': {'label': '제2조'}, 'status': 'EMPTY_RESULT', '조문내용': None},
            {'참조': {'label': '제3조'}, 'status': 'OK', '조문내용': '제3조(제목) 새로 찾은 본문'}],
            '별표': [{'참조': {'label': '별표 1'}, 'status': 'ANNEX_UNAVAILABLE', '내용': None}]})
        body = rf.partial_body(t, {'result': r, 'body': pc.flatten_content(r)})
        self.assertIn('이미 확인한 본문', body)
        self.assertIn('새로 찾은 본문', body)
        self.assertIn('별표 원문·세부내용 미확인', body)

    def test_failed_image_recheck_does_not_discard_readable_old_text(self):
        t = {'old_body': '[제2조] 제2조(제목) 읽을 수 있는 본문 <img src="image.png">', 'expected_labels': ['제2조']}
        r = clean_fragments({'status': 'OK', '조문': [{'참조': {'label': '제2조'}, 'status': 'OK', '조문내용': '제2조(제목) <img src="new.png">'}]})
        body = rf.partial_body(t, {'result': r, 'body': pc.flatten_content(r)})
        self.assertIn('읽을 수 있는 본문', body)
        self.assertIn('이미지에 포함된 본문은 확인 불가', body)
        self.assertNotIn('<img', body)

    def test_new_workbook_keeps_original_and_other_law_block(self):
        with tempfile.TemporaryDirectory() as d:
            source, out = Path(d)/'source.xlsx', Path(d)/'new.xlsx'
            row = ['부처', '[법] 시험법 제2조\n[시행령] 시험법 시행령 제3조', '[법] [제2조] 보존할 정상 내용', '지역', '', '', '기존 주석']
            fixture(source, [row])
            before = source.read_bytes()
            plan = rf.audit(source)
            t = plan['tasks'][0]
            r = clean_fragments({'status': 'PARTIAL', '조문': [{'참조': target('제3조'), 'status': 'EMPTY_RESULT', '조문내용': None}]})
            state = {'source_sha256': plan['source_sha256'], 'plan_sha256': rf.digest(plan), 'model': 'test',
                     'results': {t['id']: {'status': 'REVIEW', 'verified': False, 'result': r, 'body': pc.flatten_content(r)}}}
            rf.merge(plan, state, out)
            self.assertEqual(source.read_bytes(), before)
            after = XlsxReader(out).read_sheet().rows[1]
            self.assertTrue(after[2].startswith(row[2]))
            self.assertIn('[시행령] [제3조]', after[2])
            self.assertIn('정확한 내용을 불러올 수 없음', after[2])
            for c in (0, 1, 3, 4):
                self.assertEqual(after[c], row[c])

    def test_bare_number_interpretation_unchanged(self):
        self.assertEqual(target('제23조 제1항 2')['label'], '제23조제1항')

    def test_manual_input_failure_gets_reason_without_fabricating_body(self):
        t = {'old_body': '', 'manual_reason': '유효한 조문 번호가 없음', 'expected_labels': []}
        self.assertIn('유효한 조문 번호가 없음', rf.partial_body(t, {'status': 'REVIEW'}))
        self.assertEqual(rf.partial_body(t, {}), '')


if __name__ == '__main__':
    unittest.main()
