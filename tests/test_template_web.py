import io
import json
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import xlsxwriter

import pipeline_core as pc
from job_connection import run_config
from template_export import patch_cells, write_template_result
from web_app import create_app, convert_workbook
from xlsx_input import NS, XlsxReader

HEADERS = ['소관부처', '법(+조항)', '법(+조항) 내용', '시행령(+조항)', '시행령(+조항) 내용',
           '시행규칙(+조항)', '시행규칙(+조항) 내용', '자치단체', '조례', '조례 내용',
           '규칙', '규칙 내용', '훈령', '훈령 내용', '예규', '예규 내용', '고시 등', '고시 등 내용',
           '수정사항', '규제등록번호', '규제명', '변경사유', '공포일', '시행일', '사무특성', '부문별', '유형별', '등록일']


def fixture(rows=2):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {'in_memory': True})
    ws = wb.add_worksheet('인구감소관심지역')
    fmt = wb.add_format({'font_name': '맑은 고딕', 'font_size': 10, 'text_wrap': True, 'border': 1})
    header = wb.add_format({'bg_color': '#DDEBF7', 'bold': True})
    ws.write_row(0, 0, HEADERS, header)
    ws.set_column('A:AB', 17)
    ws.set_column('C:C', 24.75)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, rows, 27)
    for index in range(rows):
        ws.set_row(index+1, 54)
        values = [''] * 28
        values[0] = '국토교통부'
        values[1] = f'건축법 제{index+1}조'
        values[7] = '시험시'
        values[8] = '시험시 조례 제1조'
        values[9] = '기존 본문 유지'
        values[18] = '담당자 기존 메모'
        values[19] = f'ID-{index}'
        values[20] = f'규제 {index}'
        ws.write_row(index+1, 0, values, fmt)
    other = wb.add_worksheet('참고')
    other.merge_range('A1:C1', '그대로 보존')
    other.write_formula('A2', '=1+2', None, 3)
    wb.close()
    return buf.getvalue()


def resolved(api, spec, normal, offline):
    return {'원문': spec['raw'], 'LLM': {**normal, 'corrections': [{'type': 'SPACING', 'before': '전', 'after': '후', 'reason': '표기 정리'}]},
            'LLM정규화': spec['raw'], '파싱': pc.core.parse_reference(spec['raw']), 'status': 'OK',
            '현행메타': {'법령명': '건축법', '매칭방식': 'CURRENT_EXACT'},
            '조문': [{'참조': {'label': '제1조'}, 'status': 'OK', '조문내용': '=본문 <안전> & 줄바꿈\n확인된 조문'}]}


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app({'TESTING': True, 'OFFLINE': True, 'SECRET_KEY': 'test',
                               'DATA_DIR': self.root/'jobs', 'CONFIG_PATH': self.root/'missing.json'})
        self.client = self.app.test_client()
        page = self.client.get('/')
        self.headers = {'X-CSRF-Token': re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()}

    def tearDown(self):
        self.app.extensions['lawgo']['executor'].shutdown(wait=True)
        self.tmp.cleanup()

    def upload(self, source):
        response = self.client.post('/api/upload', data={'file': (io.BytesIO(source), '입력.xlsx')}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.json['template_output'])
        return response.json['id']

    def finish(self, token):
        for _ in range(300):
            response = self.client.get(f'/api/jobs/{token}').json
            if response['status'] in ('done', 'error', 'paused'):
                return response
            time.sleep(.01)
        self.fail('worker timeout')

    def test_fill_only_blanks_preserves_all_formatting_and_other_sheets(self):
        source = fixture()
        token = self.upload(source)
        with patch('web_app.pc.enrich_reference', side_effect=resolved) as enrich:
            response = self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test',
                'api_key': 'run-only-secret'}, headers=self.headers)
            self.assertEqual(response.status_code, 202, response.data)
            state = self.finish(token)
            self.assertEqual(state['status'], 'done', state)
            self.assertEqual(enrich.call_count, 2)  # Already populated ordinance not fetched.
            self.assertTrue(all(call.args[1]['slot'] == '법' for call in enrich.call_args_list))
        response = self.client.get(f'/api/jobs/{token}/download/xlsx')
        data = response.data
        response.close()
        with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(io.BytesIO(data)) as after:
            self.assertEqual(before.namelist(), after.namelist())
            for name in before.namelist():
                if name != 'xl/worksheets/sheet1.xml':
                    self.assertEqual(before.read(name), after.read(name), name)
            a = ET.fromstring(before.read('xl/worksheets/sheet1.xml'))
            b = ET.fromstring(after.read('xl/worksheets/sheet1.xml'))
            for old, new in zip(a, b):
                if old.tag.endswith('sheetData'):
                    for row_a, row_b in zip(old, new):
                        self.assertEqual(row_a.attrib, row_b.attrib)
                        for ca, cb in zip(row_a, row_b):
                            self.assertEqual(ca.get('s'), cb.get('s'))
                            if ca.get('r') not in ('C2', 'C3', 'S2', 'S3'):
                                self.assertEqual(ET.tostring(ca), ET.tostring(cb))
                else:
                    self.assertEqual(ET.tostring(old), ET.tostring(new))
        result = self.root/'result.xlsx'
        result.write_bytes(data)
        sheet = XlsxReader(result).read_sheet('인구감소관심지역')
        self.assertEqual(sheet.rows[0], HEADERS)
        self.assertEqual(len(sheet.rows), 3)
        self.assertIn('=본문 <안전> &', sheet.rows[1][2])
        self.assertEqual(sheet.rows[1][9], '기존 본문 유지')
        self.assertIn('담당자 기존 메모', sheet.rows[1][18])
        self.assertIn('SPACING', sheet.rows[1][18])
        for path in (self.root/'jobs'/token).rglob('*.json'):
            self.assertNotIn('run-only-secret', path.read_text(encoding='utf-8'))
        self.assertFalse((self.root/'missing.json').exists())
        self.assertNotIn('run-only-secret', json.dumps(state))

    def test_partial_download_and_resume_keep_original_rows(self):
        source = fixture(3)
        token = self.upload(source)
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('simulated interruption')
            return resolved(*args)
        with patch('web_app.pc.enrich_reference', side_effect=interrupted):
            self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test'}, headers=self.headers)
            state = self.finish(token)
        self.assertEqual(state['completed'], 1)
        response = self.client.get(f'/api/jobs/{token}/download/partial')
        partial = self.root/'partial.xlsx'
        partial.write_bytes(response.data)
        response.close()
        sheet = XlsxReader(partial).read_sheet()
        self.assertEqual(len(sheet.rows), 4)
        self.assertTrue(sheet.rows[1][2])
        self.assertFalse(sheet.rows[2][2])
        with patch('web_app.pc.enrich_reference', side_effect=resolved) as enrich:
            self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test'}, headers=self.headers)
            self.assertEqual(self.finish(token)['status'], 'done')
            self.assertEqual(enrich.call_count, 2)

    def test_missing_result_columns_are_appended_without_moving_originals(self):
        cases = [
            [h for h in HEADERS if h != '수정사항'],
            ['자치단체', '법(+조항)', '조례', '규제명'],
            ['조례 내용', '지자체', '조례', '법', '수정사항'],
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                buf = io.BytesIO()
                wb = xlsxwriter.Workbook(buf, {'in_memory': True})
                ws = wb.add_worksheet('규제목록')
                fmt = wb.add_format({'text_wrap': True, 'border': 1})
                ws.write_row(2, 0, headers, fmt)
                values = {'자치단체': '시험시', '지자체': '시험시', '법(+조항)': '건축법 제1조',
                          '법': '건축법 제1조', '조례': '시험시 조례 제1조', '조례 내용': '기존 내용',
                          '규제명': '규제', '수정사항': '기존 메모'}
                ws.write_row(3, 0, [values.get(h, '') for h in headers], fmt)
                ws.write_blank(100, 0, None, fmt)  # Formatted blank rows aren't records.
                wb.close()
                source = buf.getvalue()
                token = self.upload(source)
                with patch('web_app.pc.enrich_reference', side_effect=resolved) as enrich:
                    self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test'}, headers=self.headers)
                    state = self.finish(token)
                    self.assertEqual(state['status'], 'done', state)
                    self.assertEqual(state['total'], 1)
                    self.assertEqual(enrich.call_count, 1 if '조례 내용' in headers else 2)
                response = self.client.get(f'/api/jobs/{token}/download/xlsx')
                path = self.root/'appended.xlsx'
                path.write_bytes(response.data)
                response.close()
                sheet = XlsxReader(path).read_sheet()
                self.assertEqual(len(sheet.rows), 101)
                self.assertEqual(sheet.rows[2][:len(headers)], headers)
                for index, header in enumerate(headers):
                    if header not in ('법(+조항) 내용', '수정사항'):
                        self.assertEqual(sheet.rows[3][index], values.get(header, ''))
                self.assertIn('확인된 조문', sheet.rows[3][sheet.rows[2].index('법(+조항) 내용')])
                self.assertIn('SPACING', sheet.rows[3][sheet.rows[2].index('수정사항')])
                self.assertEqual(sheet.rows[2].count('수정사항'), 1)
                with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(path) as after:
                    for name in before.namelist():
                        if name != 'xl/worksheets/sheet1.xml':
                            self.assertEqual(before.read(name), after.read(name))
                    root = ET.fromstring(after.read('xl/worksheets/sheet1.xml'))
                    cells = {c.get('r'): c for c in root.findall('.//m:c', NS)}
                    for c in root.findall('.//m:row[@r="3"]/m:c', NS):
                        self.assertEqual(c.get('s'), cells['A3'].get('s'))

    def test_missing_dimension_and_sparse_cells_are_supported(self):
        xml = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="2" ht="54"><c r="B2" s="7"/><c r="AA2" s="9"/></row></sheetData></worksheet>'
        result = patch_cells(xml, {'C2': '<test>', 'S2': '=literal'})
        root = ET.fromstring(result)
        self.assertEqual([c.get('r') for c in root.findall('.//m:c', NS)], ['B2', 'C2', 'S2', 'AA2'])
        self.assertNotIn(b'<f>', result)
        self.assertIn(b'ht="54"', result)
        with self.assertRaises(ValueError):
            patch_cells(xml, {'C2': 'x' * 32768})

    def test_elapsed_includes_time_inside_current_request(self):
        token = self.upload(fixture())
        job = self.app.extensions['lawgo']['jobs'][token]
        job.update(status='running', run_started=time.time()-12, run_elapsed_start=5,
                   active_elapsed=8, run_completed_start=0, completed=1, total=2)
        elapsed = self.client.get(f'/api/jobs/{token}').json['active_elapsed']
        self.assertGreaterEqual(elapsed, 17)
        self.assertLess(elapsed, 19)

    def test_web_run_with_only_key_uses_official_openai_url(self):
        self.assertNotIn(b'id="router-url"', self.client.get('/').data)
        token = self.upload(fixture(1))
        with patch('web_app.pc.enrich_reference', side_effect=resolved):
            response = self.client.post(f'/api/jobs/{token}/start', json={
                'oc': 'test', 'api_key': 'test-key'}, headers=self.headers)
            self.assertEqual(response.status_code, 202, response.data)
            state = self.finish(token)
        self.assertEqual(state['status'], 'done', state)
        self.assertEqual(state['llm_connection']['endpoint'], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(state['llm_connection']['provider'], 'openai')


class CredentialTests(unittest.TestCase):
    def test_openai_is_used_even_with_legacy_router_config(self):
        for config in ({}, {'llm_provider': 'bizrouter', 'llm_api_url': 'https://router.example',
                            'bizrouter_optional': {'api_key': 'router-only-secret'}}):
            original = json.loads(json.dumps(config))
            result = run_config(config, {'api_key': 'test-key'})
            self.assertEqual(result['llm_api_url'], 'https://api.openai.com/v1/chat/completions')
            self.assertEqual(result['llm_model'], 'gpt-6-luna')
            self.assertEqual(result['llm_api_key'], 'test-key')
            self.assertNotIn('bizrouter_optional', result)
            self.assertEqual(config, original)

    def test_input_key_is_isolated_and_submitted_model_is_ignored(self):
        original = {'llm_provider': 'openai', 'llm_api_key': 'old-openai',
                    'llm_model': 'configured-model',
                    'bizrouter_optional': {'api_key': 'old-router', 'api_url': 'https://router.example/v1/chat/completions'}}
        result = run_config(original, {'model': 'ignored-model', 'api_key': 'entered'})
        self.assertEqual(result['llm_api_key'], 'entered')
        self.assertEqual(result['llm_model'], 'configured-model')
        self.assertEqual(original['llm_api_key'], 'old-openai')
        self.assertEqual(original['bizrouter_optional']['api_key'], 'old-router')

    def test_endpoint_and_key_validation_before_network(self):
        base = {'api_key': 'test-key'}
        for url in ('http://router.example', 'https://user:secret@router.example', 'https://router.example?key=secret'):
            with self.assertRaises(ValueError):
                run_config({}, {**base, 'api_url': url}, True)
        with self.assertRaises(ValueError):
            run_config({}, {**base, 'api_url': 'https://router.example/v1/chat/completions'}, False)
        with self.assertRaises(ValueError):
            run_config({}, {'provider': 'bizrouter', 'api_key': 'test-key'})
        for key in ('', 'space inside', '비밀키', None):
            with self.assertRaises(ValueError):
                run_config({}, {'api_key': key})


if __name__ == '__main__':
    unittest.main()
