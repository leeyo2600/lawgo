"""Credential API behavior; encryption is substituted only inside these tests."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from credential_store import CredentialStoreError
from web_app import create_app


def test_codec(data, *, decrypt=False):
    return bytes(value ^ 0xA5 for value in data)


class SavedCredentialTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        codec = patch('credential_store._crypt', side_effect=test_codec)
        codec.start()
        self.addCleanup(codec.stop)
        self.options = {'TESTING': True, 'SECRET_KEY': 'test', 'DATA_DIR': self.root/'jobs',
                        'CREDENTIALS_PATH': self.root/'.local'/'credentials.json',
                        'CONFIG_PATH': self.root/'config.json'}
        self.client, self.headers = self.new_client()

    def new_client(self):
        app = create_app(self.options)
        self.addCleanup(lambda: app.extensions['lawgo']['executor'].shutdown(wait=True))
        client = app.test_client()
        page = client.get('/')
        return client, {'X-CSRF-Token': re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()}

    def save(self, revision=1, **values):
        return self.client.post('/api/credentials', headers=self.headers,
                                json={'client_id': 'test-page', 'revision': revision,
                                      'api_key': 'fake-openai-key', 'law_oc': 'test-law-key', **values})

    def test_restore_last_values_after_server_restart_and_clear(self):
        self.assertEqual(self.client.get('/api/credentials', headers=self.headers).json,
                         {'api_key': '', 'law_oc': ''})
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(2, api_key=' replacement-key ').status_code, 200)
        other, headers = self.new_client()
        self.assertEqual(other.get('/api/credentials', headers=headers).json,
                         {'api_key': 'replacement-key', 'law_oc': 'test-law-key'})
        self.assertEqual(self.save(3, api_key='', law_oc='').status_code, 200)
        self.assertEqual(other.get('/api/credentials', headers=headers).json,
                         {'api_key': '', 'law_oc': ''})

    def test_slow_older_save_cannot_replace_latest_input(self):
        self.assertEqual(self.save(3, api_key='latest-key').status_code, 200)
        self.assertEqual(self.save(2, api_key='older-key').status_code, 200)
        self.assertEqual(self.save(1, client_id='other-page', api_key='old-other-page-key').status_code, 200)
        self.assertEqual(self.client.get('/api/credentials', headers=self.headers).json['api_key'], 'latest-key')

    def test_saved_keys_are_not_returned_in_save_response_or_page(self):
        response = self.save()
        for value in ('fake-openai-key', 'test-law-key'):
            self.assertNotIn(value, response.get_data(as_text=True))
            self.assertNotIn(value, self.client.get('/').get_data(as_text=True))
            self.assertNotIn(value, self.options['CREDENTIALS_PATH'].read_text())
        self.assertEqual(self.client.get('/api/credentials', headers=self.headers).headers['Cache-Control'], 'no-store')

    def test_credentials_require_csrf_and_local_connection(self):
        for method in (self.client.get, self.client.post):
            self.assertEqual(method('/api/credentials').status_code, 403)
            self.assertEqual(method('/api/credentials', headers=self.headers,
                                    environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code, 403)

    def test_storage_failure_is_safe_and_can_retry(self):
        with patch('web_app.CredentialStore.save', side_effect=CredentialStoreError('private-test-value')):
            response = self.save()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-test-value', response.get_data(as_text=True))
        self.assertEqual(self.save().status_code, 200)

    def test_input_page_has_only_requested_key_controls(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('OpenAI API 키', page)
        self.assertIn('국가법령정보 API 키', page)
        for removed in ('AI 제공자', '비즈라우터', '모델 설정', 'id="model"', '승인받은 OC를 입력해주세요',
                        '법령 조회에 필요한 국가법령정보센터 사용자 ID입니다.'):
            self.assertNotIn(removed, page)


if __name__ == '__main__':
    unittest.main()
