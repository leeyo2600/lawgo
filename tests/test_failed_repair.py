import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import repair_failed as rf
from llm_providers import resolve_provider, request_options
from llm_normalizer import LLMNormalizer
from xlsx_input import XlsxReader, MAIN_NS


def fixture(path, rows, prefix="x:"):
    """Minimal OOXML test fixture; no user workbook is authored by tests."""
    data = []
    for n, row in enumerate([rf.HEADERS] + rows, 1):
        cells = ''.join(f'<{prefix}c r="{chr(65+i)}{n}" s="0" t="str"><{prefix}v>{escape(s)}</{prefix}v></{prefix}c>' for i, s in enumerate(row))
        data.append(f'<{prefix}row r="{n}" ht="90" customHeight="1">{cells}</{prefix}row>')
    ns = 'xmlns:x' if prefix else 'xmlns'
    sheet = f'<{prefix}worksheet {ns}="{MAIN_NS}"><{prefix}sheetData>{"".join(data)}</{prefix}sheetData></{prefix}worksheet>'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{MAIN_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="전체 처리결과" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
        z.writestr('xl/styles.xml', '<styles unchanged="true"/>')


def verified(task):
    fragments = [{'참조': {'label': label}, 'status': 'OK', '조문내용': label + '(내용) 검증된 실제 본문입니다.'} for label in task['expected_labels']]
    result = {'status': 'OK', '조문': fragments, '별표': []}
    return {'verified': True, 'status': 'VERIFIED', 'result': result,
            'body': rf.pc.flatten_content(result), 'reason': 'CURRENT_EXACT', 'normalized': task['raw']}


class ProviderTests(unittest.TestCase):
    def test_explicit_openai_never_uses_bizrouter_key(self):
        cfg = resolve_provider({'llm_provider': 'openai', 'bizrouter_optional': {'api_key': 'old'}})
        self.assertEqual(cfg['llm_api_key'], '')
        self.assertEqual(cfg['llm_model'], 'gpt-5.6-luna')
        self.assertEqual(cfg['llm_api_url'], 'https://api.openai.com/v1/chat/completions')

    def test_bizrouter_is_explicit_opt_in(self):
        cfg = resolve_provider({'llm_provider': 'bizrouter', 'llm_api_key': 'new', 'bizrouter_optional': {'api_key': 'old', 'api_url': 'https://example.test', 'model': 'router-model'}})
        self.assertEqual(cfg['llm_api_key'], 'old')
        self.assertEqual(request_options(cfg['llm_api_url']), {})

    def test_openai_payload_uses_native_model_and_limits(self):
        with tempfile.TemporaryDirectory() as d:
            client = LLMNormalizer('https://api.openai.com/v1/chat/completions', 'gpt-5.6-luna', 'test', cache_dir=d)
            response = Mock()
            response.json.return_value = {'choices': [{'message': {'content': '{"results":[{"id":"0","normalized":"시험법 제1조","confidence":"HIGH","corrections":[]}]}'}, 'finish_reason': 'stop'}]}
            client.session.post = Mock(return_value=response)
            client._post_batch([{'id': 'x', 'raw': '시험법 제1조'}])
            payload = client.session.post.call_args.kwargs['json']
            self.assertEqual(payload['model'], 'gpt-5.6-luna')
            self.assertEqual(payload['reasoning_effort'], 'low')
            self.assertEqual(payload['max_completion_tokens'], 4096)
            self.assertNotIn('temperature', payload)
            client.session.close()

    def test_budget_counts_retries_and_blocks_before_network(self):
        post = Mock()
        client = SimpleNamespace(session=SimpleNamespace(post=post))
        count = rf.attach_budget(client, 2)
        client.session.post('a'); client.session.post('a')
        with self.assertRaises(rf.BudgetStop):
            client.session.post('a')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(count['attempts'], 2)

    def test_budget_stop_is_not_retried_or_recorded_as_a_paid_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            client = LLMNormalizer('https://api.openai.com/v1/chat/completions', 'gpt-5.6-luna', 'test-only', cache_dir=d)
            post = Mock()
            client.session.post = post
            rf.attach_budget(client, 0)
            with self.assertRaises(rf.BudgetStop):
                client.normalize_many([{'id': 'a', 'raw': '시험법 제1조'}])
            post.assert_not_called()
            self.assertEqual(rf.llm_metrics(client)['llm_usage_unreported_requests'], 0)
            client.session.close()


class FailedRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.xlsx'
        self.rows = [
            ['부처', '[법] 시험법 제1조', '[법] [제1조] 제1조(목적) 정상 본문', '강원 고성군', '[훈령] 고성군 규정 제9조', '', '원래 주석'],
            ['부처', '[법] 시험법', '', '강원 고성군', '', '', ''],
            ['부처', '[법] 시험법 제2조\n\n[시행령] 시험법 시행령 제3조', '[법] [제2조] 제2조 정상 본문', '', '', '', ''],
        ]
        fixture(self.path, self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_audit_excludes_good_and_blank_without_network(self):
        with patch.object(rf, 'HighRecallLawApi') as api, patch.object(rf, 'LLMNormalizer') as llm:
            plan = rf.audit(self.path)
        self.assertEqual([(t['row'], t['slot']) for t in plan['tasks']], [(2, '훈령'), (4, '시행령')])
        api.assert_not_called(); llm.assert_not_called()

    def test_ranges_preserve_both_paragraphs(self):
        case = rf.classify('담배사업법 시행령 제7조 제2~3항', '[제7조제3항] 제7조(제목) ③ 세 번째 항', 'upper')
        self.assertEqual(case['expected_labels'], ['제7조제2항', '제7조제3항'])
        self.assertEqual(case['missing_labels'], ['제7조제2항'])

    def test_annex_link_and_ambiguous_raw_flagged(self):
        case = rf.classify('시험법 시행령 제56조 별표1의2 제2호 마목', '[제56조] 제56조(기준) 별표 1의2와 같다.', 'upper')
        self.assertIn('별표 세부내용 확인 필요', case['reasons'])
        case = rf.classify('사회복지사업법 제34조 5', '[제34조제5항] 제34조 ⑤ 내용', 'upper')
        self.assertTrue(case['ambiguous'])

    def test_amendment_brackets_are_not_html(self):
        self.assertIsNone(rf.classify('시험법 제1조', '[제1조] 제1조(제목) 본문 <개정 2020.1.1.>', 'upper'))
        self.assertIn('HTML·이미지 태그 잔존', rf.classify('시험법 제1조', '[제1조] 제1조 <img src="x">', 'upper')['reasons'])

    def test_blank_key_refused_without_constructing_clients(self):
        plan = rf.audit(self.path)
        with patch.object(rf, 'HighRecallLawApi') as api, patch.object(rf, 'LLMNormalizer') as llm:
            with self.assertRaisesRegex(ValueError, 'llm_api_key'):
                rf.run_repairs(plan, {'llm_provider': 'openai', 'bizrouter_optional': {'api_key': 'old'}}, Path(self.tmp.name)/'state.json')
        api.assert_not_called(); llm.assert_not_called()

    def state(self, plan):
        return {'source_sha256': plan['source_sha256'], 'plan_sha256': rf.digest(plan), 'model': 'gpt-5.6-luna',
                'results': {t['id']: verified(t) for t in plan['tasks']}}

    def test_merge_preserves_originals_good_results_styles_and_backup(self):
        plan = rf.audit(self.path)
        old_bytes = self.path.read_bytes()
        result = rf.merge(plan, self.state(plan))
        self.assertEqual(Path(result['backup']).read_bytes(), old_bytes)
        actual = XlsxReader(self.path).read_sheet().rows
        self.assertEqual(actual[0], rf.HEADERS)
        for i, before in enumerate(self.rows, 1):
            for c in [0, 1, 3, 4]:
                self.assertEqual(actual[i][c], before[c])
        self.assertEqual(actual[1][2], self.rows[0][2])
        self.assertEqual(actual[2], self.rows[1])
        self.assertTrue(actual[3][2].startswith(self.rows[2][2]))
        self.assertIn('원래 주석', actual[1][6])
        with zipfile.ZipFile(self.path) as z:
            self.assertEqual(z.read('xl/styles.xml'), b'<styles unchanged="true"/>')

    def test_failed_retry_cannot_erase_previous_partial_contents(self):
        plan = rf.audit(self.path); state = self.state(plan)
        for item in state['results'].values():
            item.update(verified=False, status='REVIEW', body='')
        before = self.path.read_bytes()
        self.assertEqual(rf.merge(plan, state)['changed_cells'], 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_modified_source_and_wrong_plan_refused(self):
        plan = rf.audit(self.path); state = self.state(plan)
        other = copy.deepcopy(plan); other['tasks'][0]['raw'] += ' changed'
        with self.assertRaises(ValueError):
            rf.merge(other, state)
        fixture(self.path, self.rows + [self.rows[1]])
        with self.assertRaises(ValueError):
            rf.merge(plan, state)

    def test_retrieval_status_ok_requires_all_targets(self):
        task = {'ambiguous': False, 'expected_labels': ['제7조제2항', '제7조제3항']}
        r = {'status': 'OK', '조문': [{'status': 'OK', '참조': {'label': '제7조제3항'}, '조문내용': '③ 실제 본문'}]}
        self.assertFalse(rf.verified_result(task, r))

    def test_patch_missing_cells_and_default_namespace(self):
        xml = f'<worksheet xmlns="{MAIN_NS}"><sheetData><row r="2"><c r="A2" t="str"><v>원본</v></c><c r="G2"/></row></sheetData></worksheet>'.encode()
        changed = rf.patch_sheet(xml, {'F2': '내용 & 본문', 'G2': '주석'})
        self.assertIn(b'&amp;', changed)
        self.assertLess(changed.index(b'r="F2"'), changed.index(b'r="G2"'))
        with self.assertRaises(ValueError):
            rf.patch_sheet(xml, {'B2': '원본을 바꾸면 안 됨'})

    def test_corrupt_or_oversized_changes_leave_source_unchanged(self):
        plan = rf.audit(self.path); state = self.state(plan)
        next(iter(state['results'].values()))['body'] = 'x' * 32768
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            rf.merge(plan, state)
        self.assertEqual(self.path.read_bytes(), before)

    def test_resume_only_processes_remaining_tasks(self):
        plan = rf.audit(self.path)
        state_path = Path(self.tmp.name) / 'state.json'
        cfg = {'llm_provider': 'openai', 'llm_model': 'gpt-5.6-luna', 'llm_api_key': 'test-only', 'law_go_oc': 'test-only'}
        with patch.object(rf, 'ROOT', Path(self.tmp.name)), patch.object(rf, 'HighRecallLawApi'), patch.object(rf, 'repair_one', side_effect=lambda t, a, n: verified(t)) as repair:
            rf.run_repairs(plan, cfg, state_path, limit=1)
            self.assertEqual(repair.call_count, 1)
            rf.run_repairs(plan, cfg, state_path)
            self.assertEqual(repair.call_count, 2)
            state = rf.run_repairs(plan, cfg, state_path)
            self.assertEqual(repair.call_count, 2)
            self.assertEqual(state['last_run_http_attempts'], 0)

    def test_failure_saved_and_retried_without_losing_completed_results(self):
        plan = rf.audit(self.path)
        state_path = Path(self.tmp.name) / 'state.json'
        cfg = {'llm_provider': 'openai', 'llm_model': 'gpt-5.6-luna', 'llm_api_key': 'test-only', 'law_go_oc': 'test-only'}
        failed = {'status': 'RETRY', 'verified': False, 'reason': 'LLM_HTTP_403', 'body': ''}
        with patch.object(rf, 'ROOT', Path(self.tmp.name)), patch.object(rf, 'HighRecallLawApi'), patch.object(rf, 'repair_one', side_effect=[verified(plan['tasks'][0]), failed, verified(plan['tasks'][1])]) as repair:
            state = rf.run_repairs(plan, cfg, state_path)
            self.assertEqual(len(state['results']), 2)
            self.assertEqual(state['stop_reason'], 'RETRYABLE_ERROR')
            state = rf.run_repairs(plan, cfg, state_path)
            self.assertEqual(repair.call_count, 3)
            self.assertTrue(all(r['verified'] for r in state['results'].values()))

    def test_whole_annex_requires_exact_metadata_and_actual_body(self):
        ann = {'kind': '별표', 'number': 1, 'branch': 2, 'label': '별표 1의2'}
        good = {'별표번호': '0001', '별표가지번호': '02', '별표내용': '실제 별표의 기준과 내용을 포함한 본문'}
        self.assertTrue(rf.strict_annex([good], ann)['source_identity_verified'])
        for candidate in [{**good, '별표가지번호': '03'}, {'별표내용': good['별표내용']}, {**good, '별표내용': ''}, {**good, '별표내용': '<img src="x"/>'}]:
            self.assertNotEqual(rf.strict_annex([candidate], ann)['status'], 'OK')
        self.assertNotEqual(rf.strict_annex([good], {**ann, 'item': 2})['status'], 'OK')

    def test_ambiguous_lower_body_is_not_its_own_evidence(self):
        task = rf.audit(self.path)['tasks'][0]
        task['record']['자치법규']['훈령'] = '고성군 규정 제9조 2'
        task['local_context'] = '[훈령] [제9조제2항] 예전에 잘못 해석했을 수 있는 본문'
        self.assertEqual(rf.task_spec(task)['_context']['조회한_자치법규'], [])

    def test_invalid_zero_article_does_not_waste_llm_call(self):
        task = rf.audit(self.path)['tasks'][0]
        task.update(raw='시험법 제0조', **rf.classify('시험법 제0조', '', 'upper'))
        api, llm = Mock(), Mock()
        result = rf.repair_one(task, api, llm)
        self.assertEqual(result['status'], 'REVIEW')
        llm.normalize_many.assert_not_called()

    def test_second_law_is_not_silently_resolved_against_first_law(self):
        for raw in ['시험법 제1조; 다른법 제2조', '시험법 제1조 + 시행령 제2조']:
            case = rf.classify(raw, '[제1조] 내용\n\n[제2조] 다른 내용', 'upper')
            self.assertTrue(case['manual_reason'])


if __name__ == '__main__':
    unittest.main()
