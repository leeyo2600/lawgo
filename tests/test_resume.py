import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pipeline_core as pc
import unittest
import test_web_app
from web_app import create_app
from xlsx_input import XlsxReader


class ResumeTests(unittest.TestCase):
    setUp = test_web_app.WebTests.setUp
    tearDown = test_web_app.WebTests.tearDown
    def wait_done(self, token):
        for _ in range(500):
            state = self.client.get(f'/api/jobs/{token}').json
            if state['status'] in ('error', 'done'):
                return state
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_resume_after_failure_and_restart(self):
        records = [{'규제명': str(i), '상위법령': {'법': f'건축법 제{i+1}조'}} for i in range(3)]
        token = self.client.post('/api/upload', data={'file': (io.BytesIO(json.dumps(records).encode()), 'input.json')}, headers=self.headers).json['id']
        original = pc.enrich_reference
        seen = []
        def fail_third(api, spec, norm, offline):
            seen.append(spec['raw'])
            if len(seen) == 3:
                raise RuntimeError('simulated connection failure')
            return original(api, spec, norm, offline)
        with patch('web_app.pc.enrich_reference', side_effect=fail_third):
            self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test-user'}, headers=self.headers)
            state = self.wait_done(token)
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['completed'], 2)
        self.assertEqual(state['progress'], 66)
        response = self.client.get(f'/api/jobs/{token}/download/partial')
        self.assertEqual(response.status_code, 200)
        path = Path(self.tmp.name)/'partial.xlsx'
        path.write_bytes(response.data)
        response.close()
        self.assertEqual(len(XlsxReader(path).read_sheet('규제목록').rows), 3)
        self.assertEqual(self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test-user', 'limit': 10}, headers=self.headers).status_code, 409)
        self.app.extensions['lawgo']['executor'].shutdown(wait=True)
        cookie = self.client.get_cookie('session').value
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-only', 'DATA_DIR': Path(self.tmp.name), 'OFFLINE': True})
        self.client = self.app.test_client()
        self.client.set_cookie('session', cookie)
        self.assertEqual(self.client.get(f'/api/jobs/{token}').json['completed'], 2)
        self.assertEqual(self.app.test_client().get(f'/api/jobs/{token}/download/partial').status_code, 404)
        with patch('web_app.pc.enrich_reference', wraps=original) as enrich, patch('web_app.RuleOnlyNormalizer.normalize_many') as normalize:
            self.assertEqual(self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test-user'}, headers=self.headers).status_code, 202)
            state = self.wait_done(token)
            self.assertEqual(state['status'], 'done', state)
            self.assertEqual(enrich.call_count, 1)
            self.assertEqual(enrich.call_args.args[1]['raw'], '건축법 제3조')
            normalize.assert_not_called()
        sheet = XlsxReader(Path(self.tmp.name)/token/'result.xlsx').read_sheet('규제목록')
        self.assertEqual(len(sheet.rows), 4)
        self.assertEqual(state['completed'], 3)

    def test_llm_failure_keeps_preceding_rows_and_retries_only_failed_reference(self):
        from llm_normalizer import RuleOnlyNormalizer
        records = [{'규제명': str(i), '상위법령': {'법': f'건축법 제{i+1}조'}} for i in range(3)]
        token = self.client.post('/api/upload', data={'file': (io.BytesIO(json.dumps(records).encode()), 'input.json')}, headers=self.headers).json['id']
        original = RuleOnlyNormalizer.normalize_many
        def fail_last(normalizer, items):
            out = original(normalizer, items)
            for item in items:
                if item['raw'] == '건축법 제3조':
                    out[item['id']] = {'id': item['id'], 'llm_error': True, 'review_reason': 'invalid response', 'error_code': 'LLM_INVALID_RESPONSE'}
            return out
        with patch.object(RuleOnlyNormalizer, 'normalize_many', fail_last):
            self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test-user'}, headers=self.headers)
            state = self.wait_done(token)
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['completed'], 2)
        diagnostic = json.loads((Path(self.tmp.name)/token/'error.json').read_text())
        self.assertEqual(diagnostic['source_row'], 3)
        self.assertEqual(len(list((Path(self.tmp.name)/token/'normalizations').glob('*.json'))), 2)
        seen=[]
        def recover(normalizer, items):
            seen.extend(x['raw'] for x in items)
            return original(normalizer, items)
        with patch.object(RuleOnlyNormalizer, 'normalize_many', recover):
            self.client.post(f'/api/jobs/{token}/start', json={'oc': 'test-user'}, headers=self.headers)
            state = self.wait_done(token)
        self.assertEqual(state['status'], 'done', state)
        self.assertEqual(seen, ['건축법 제3조'])

    def test_stop_preserves_rows_and_resume(self):
        import threading
        records=[{'규제명':str(i),'상위법령':{'법':f'건축법 제{i+1}조'}} for i in range(7)]
        token=self.client.post('/api/upload',data={'file':(io.BytesIO(json.dumps(records).encode()),'input.json')},headers=self.headers).json['id']
        entered=threading.Event(); release=threading.Event()
        original=pc.enrich_reference
        def slow(api,spec,norm,offline):
            if spec['raw']=='건축법 제6조':
                entered.set();release.wait(5)
            return original(api,spec,norm,offline)
        with patch('web_app.pc.enrich_reference',side_effect=slow):
            self.client.post(f'/api/jobs/{token}/start',json={'oc':'test-user'},headers=self.headers)
            try:
                self.assertTrue(entered.wait(5))
                state=self.client.get(f'/api/jobs/{token}').json
                self.assertIsNotNone(state['eta_seconds'])
                self.assertEqual(self.client.post(f'/api/jobs/{token}/stop',headers=self.headers).status_code,202)
                self.assertTrue(self.client.get(f'/api/jobs/{token}').json['stop_requested'])
            finally:release.set()
            for _ in range(500):
                state=self.client.get(f'/api/jobs/{token}').json
                if state['status']=='paused':break
                time.sleep(.01)
        self.assertEqual(state['status'],'paused',state)
        self.assertEqual(state['completed'],6)
        self.assertIsNone(state['eta_seconds'])
        self.assertEqual(self.client.post(f'/api/jobs/{token}/start',json={'oc':'test-user'},headers=self.headers).status_code,202)
        self.assertEqual(self.wait_done(token)['status'],'done')

    def test_api_cache_survives_new_client_without_network(self):
        from lawgo_core import LawApi
        from unittest.mock import Mock
        folder=Path(self.tmp.name)/'responses'
        first=LawApi('test-oc',folder,delay=0)
        response=Mock();response.json.return_value={'law':{'title':'example'}}
        first.session.get=Mock(return_value=response)
        first._get('https://example.invalid/search',{'query':'law'})
        second=LawApi('test-oc',folder,delay=0)
        second.session.get=Mock(side_effect=AssertionError('duplicate network call'))
        self.assertEqual(second._get('https://example.invalid/search',{'query':'law'}),{'law':{'title':'example'}})
        second.session.get.assert_not_called()
        self.assertEqual(second.cache_hits,1)
