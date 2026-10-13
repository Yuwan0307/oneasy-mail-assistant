import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.config import load_config, save_config
from app.__main__ import configure
from app.core import now
from app.store import Store
from app.web import create_app


class SettingsTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'main.db')
        self.config={'alimail':{'client_id':'app-id','client_secret':'private-mail-value'},
                     'ai':{'base_url':'https://ai.example.com/v1','model':'test-model','api_key':'private-ai-value'}}
        save_config(self.db.path.parent/'config.json',self.config)
        self.app=create_app(self.db,self.config)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',12345))

    def test_local_dashboard_needs_no_password_and_never_returns_secrets(self):
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['connections']['client_secret_saved'])
        self.assertTrue(response.json()['connections']['api_key_saved'])
        self.assertNotIn('private-mail-value',response.text)
        self.assertNotIn('private-ai-value',response.text)
        self.assertNotIn('管理访问码',self.client.get('/').text)

    def test_remote_host_origin_and_fetch_requests_cannot_change_settings(self):
        for headers in [{'Host':'attacker.example'}, {'Origin':'https://attacker.example'},
                        {'Sec-Fetch-Site':'cross-site'}, {'Origin':'null'}]:
            result=self.client.post('/api/connections',json={},headers=headers)
            self.assertEqual(result.status_code,403,headers)
        remote=TestClient(self.app,base_url='http://127.0.0.1:8765',client=('192.0.2.10',12345))
        self.assertEqual(remote.get('/api/state').status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={'Host':'localhost:8765'}).status_code,200)

    def test_save_preserves_empty_secrets_and_activation_across_owner_change(self):
        self.db.activate(now()); original=self.db.get('activated')
        self.db.save_person('new-owner','新负责人','new@example.com',True)
        payload={'client_id':'app-id','client_secret':'','base_url':'https://ai.example.com/v1',
                 'model':'changed-model','api_key':''}
        response=self.client.post('/api/connections',json=payload,headers={'Origin':'http://127.0.0.1:8765'})
        self.assertEqual(response.status_code,200,response.text)
        saved=load_config(self.db.path.parent/'config.json')
        self.assertEqual(saved['alimail']['client_secret'],'private-mail-value')
        self.assertEqual(saved['ai']['api_key'],'private-ai-value')
        self.assertEqual(self.app.state.worker.ai.config['model'],'changed-model')
        response=self.client.post('/api/mailbox',json={'account':'service@oneasy.cc','owner':'new-owner'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(load_config(self.db.path.parent/'config.json'),saved)
        self.assertEqual(self.db.get('activated'),original)
        restarted=TestClient(create_app(self.db,saved),base_url='http://127.0.0.1',client=('127.0.0.1',1))
        self.assertTrue(restarted.get('/api/state').json()['configured']['ai'])
        audit=str(self.db.rows('SELECT * FROM audit'))
        self.assertNotIn('private-ai-value',audit)
        self.assertNotIn('private-mail-value',audit)

    def test_changed_destination_needs_new_secret_and_failed_save_keeps_old_config(self):
        for data in [{'client_id':'different','client_secret':''}, {'client_id':'different'},
                     {'base_url':'https://different.example/v1'},
                     {'base_url':'https://different.example/v1','api_key':''},
                     {'base_url':'http://example.com/v1','api_key':'new-key'},
                     {'base_url':'https://user:password@example.com/v1','api_key':'new-key'},
                     {'client_secret':['bad-type']}, []]:
            result=self.client.post('/api/connections',json=data)
            self.assertEqual(result.status_code,400,result.text)
        original=load_config(self.db.path.parent/'config.json')
        with patch('app.web.save_config',side_effect=OSError('private-ai-value')):
            result=self.client.post('/api/connections',json={'model':'new-model'})
        self.assertEqual(result.status_code,503)
        self.assertNotIn('private-ai-value',result.text)
        self.assertEqual(self.config,original)
        result=self.client.post('/api/connections',json={'client_id':'new-id','client_secret':'new-mail-value',
             'base_url':'https://new.example/v1','api_key':'new-ai-value'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.app.state.worker.mail_api.config['client_id'],'new-id')
        self.assertEqual(self.app.state.worker.ai.config['api_key'],'new-ai-value')

    def test_setup_has_no_prompts_or_access_code_and_retains_existing_credentials(self):
        path=self.db.path.parent/'config.json'
        save_config(path,{**self.config,'admin_token':'obsolete-access-code'})
        with patch('builtins.input',side_effect=AssertionError('setup should not ask for credentials')), patch('builtins.print') as output:
            configure(path)
        saved=load_config(path)
        self.assertNotIn('admin_token',saved)
        self.assertEqual(saved['alimail'],self.config['alimail'])
        self.assertNotIn('obsolete-access-code',str(output.call_args_list))
        new_path=self.db.path.parent/'fresh'/'config.json'
        with patch('builtins.input',side_effect=AssertionError('no prompts')), patch('builtins.print'):
            configure(new_path)
        self.assertEqual(load_config(new_path)['ai']['model'],'qwen-plus')


if __name__=='__main__': unittest.main()
