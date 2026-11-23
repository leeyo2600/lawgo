import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import llm_connection as lc
from web_app import create_app


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {'llm_provider':'openai', 'llm_model':'gpt-5.6-luna', 'llm_api_key':'openai-test-secret',
                    'bizrouter_optional': {'api_url':'https://router.example/v1/chat/completions', 'model':'router-model', 'api_key':'router-test-secret'}}
        self.path = self.root/'config.json'
        lc.save_configuration(self.path, self.cfg)
        self.app = create_app({'TESTING':True, 'SECRET_KEY':'test', 'DATA_DIR':self.root/'jobs', 'CONFIG_PATH':self.path})
        self.client = self.app.test_client()
        page = self.client.get('/')
        self.headers = {'X-CSRF-Token':re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()}

    def tearDown(self):
        self.app.extensions['lawgo']['executor'].shutdown(wait=True)
        self.tmp.cleanup()

    def test_status_distinguishes_configuration_from_response_and_hides_keys(self):
        r = self.client.get('/api/llm')
        self.assertEqual(r.json['provider'], 'openai')
        self.assertEqual(r.json['check'], {})
        self.assertTrue(r.json['key_configured'])
        self.assertNotIn(b'test-secret', r.data)
        self.assertNotIn(b'fingerprint', r.data)

    def test_switch_roundtrip_preserves_provider_specific_keys(self):
        for provider, model in [('bizrouter','router-new'), ('openai','gpt-5.6-luna')]:
            r = self.client.post('/api/llm', json={'provider':provider,'model':model}, headers=self.headers)
            self.assertEqual(r.status_code,200,r.data)
            self.assertEqual(r.json['provider'],provider)
            self.assertEqual(r.json['model'],model)
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved['llm_api_key'],self.cfg['llm_api_key'])
        self.assertEqual(saved['bizrouter_optional']['api_key'],self.cfg['bizrouter_optional']['api_key'])
        self.assertIn('\n',self.path.read_text())

    def test_remote_mutations_and_csrf_are_blocked(self):
        for route in ['/api/llm','/api/llm/check']:
            self.assertEqual(self.client.post(route,json={},headers=self.headers,environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).status_code,403)
            self.assertEqual(self.client.post(route,json={}).status_code,403)

    def test_busy_job_blocks_changes(self):
        self.app.extensions['lawgo']['jobs']['test']={'status':'running','llm_connection':{'provider':'openai','model':'old'}}
        r=self.client.post('/api/llm',json={'provider':'bizrouter','model':'new'},headers=self.headers)
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.client.get('/api/llm').json['active_jobs'][0]['model'],'old')

    def test_check_cache_invalidated_when_key_changes(self):
        cfg=lc.resolve_provider(self.cfg)
        report={'fingerprint':lc.fingerprint(cfg),'ok':True,'message':'test response','response_model':'actual-model'}
        self.assertTrue(lc.status(self.cfg,report)['check']['ok'])
        self.assertEqual(lc.status({**self.cfg,'llm_api_key':'changed'},report)['check'],{})

    def test_empty_key_prevents_paid_call(self):
        with patch.object(lc,'LLMNormalizer') as n:
            with self.assertRaises(ValueError):lc.probe({**self.cfg,'llm_api_key':''})
            n.assert_not_called()

    def test_five_samples_are_one_real_uncached_batch(self):
        samples=[{'id':str(i),'raw':f'시험법 제{i+1}조','category':'law','slot':'법'} for i in range(5)]
        with patch.object(lc,'LLMNormalizer') as cls:
            n=cls.return_value
            n.calls=1;n.last_response_model='gpt-5.6-luna';n.token_usage={}
            n._post_batch.return_value={s['id']:{'normalized':s['raw']} for s in samples}
            result=lc.probe(self.cfg,samples)
            self.assertTrue(result['ok']);self.assertEqual(result['sample_count'],5)
            n._post_batch.assert_called_once_with(samples)
            self.assertEqual(result['http_calls'],1)
            n.session.close.assert_called_once()

    def test_check_endpoint_returns_safe_status(self):
        report={'fingerprint':lc.fingerprint(lc.resolve_provider(self.cfg)),'ok':True,'message':'actual response','results':{'hidden':'body'},'http_calls':1}
        with patch.object(lc,'probe',return_value=report):
            r=self.client.post('/api/llm/check',headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['check']['ok'])
        self.assertNotIn(b'fingerprint',r.data)
        self.assertNotIn(b'hidden',r.data)

    def test_model_input_cannot_change_endpoint_or_key(self):
        r=self.client.post('/api/llm',json={'provider':'openai','model':'openai/gpt-5.6-luna','llm_api_key':'injected'},headers=self.headers)
        self.assertEqual(r.status_code,400)
        self.assertEqual(json.loads(self.path.read_text())['llm_api_key'],self.cfg['llm_api_key'])

    def test_resume_cannot_mix_old_and_new_models(self):
        folder=self.root/'previous';(folder/'rows').mkdir(parents=True)
        (folder/'rows/00000000.json').write_text(json.dumps({'provenance':{'llm_model':'old-model'}}))
        with self.client.session_transaction() as s: owner=s['owner']
        job={'status':'paused','completed':1,'owner':owner,'folder':folder}
        self.app.extensions['lawgo']['jobs']['old']=job
        for previous in [None, {'provider':'bizrouter','model':'old-model'}]:
            if previous:job['llm_connection']=previous
            response=self.client.post('/api/jobs/old/start',json={'oc':'test','limit':10},headers=self.headers)
            self.assertEqual(response.status_code,409,response.data)
            self.assertIn('모델',response.json['error'])


if __name__=='__main__':unittest.main()
