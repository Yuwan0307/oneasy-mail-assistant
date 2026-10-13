import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from app.config import save_config, load_config
from app.store import Store
from app.web import create_app


class DesktopTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_service_identity_is_local_and_rejects_invalid_ports(self):
        from app.deployment import service_settings
        self.assertEqual(service_settings(self.root)['port'],8765)
        (self.root/'deployment.json').write_text(json.dumps({'service_name':'OneasyMailAssistantTest','port':18765}))
        self.assertEqual(service_settings(self.root)['service_name'],'OneasyMailAssistantTest')
        for data in [{'port':0},{'port':'8765'},{'service_name':'OtherService'},{'service_name':'OneasyMailAssistant /bad'}]:
            (self.root/'deployment.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError): service_settings(self.root)

    def test_backup_is_consistent_preserves_credentials_and_refuses_overwrite(self):
        from app.deployment import copy_data
        source=self.root/'source'; source.mkdir()
        db=Store(source/'main.db'); db.set('activated','2026-10-10T12:00:00+08:00')
        save_config(source/'config.json',{'alimail':{'client_secret':'test-secret'}})
        destination=self.root/'backup'
        copy_data(source,destination)
        self.assertEqual(load_config(destination/'config.json'),load_config(source/'config.json'))
        self.assertEqual(Store(destination/'main.db').get('activated'),db.get('activated'))
        with self.assertRaises(ValueError): copy_data(source,destination)
        self.assertTrue((source/'main.db').exists())

    def test_health_identifies_application_without_runtime_configuration(self):
        app=create_app(Store(self.root/'main.db'),{'demo':True,'alimail':{'client_secret':'test-secret'}})
        client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1))
        response=client.get('/api/health')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['app'],'oneasy-mail-assistant')
        self.assertNotIn('test-secret',response.text)
        self.assertEqual(client.get('/api/health',headers={'Host':'foreign.example'}).status_code,403)


if __name__=='__main__': unittest.main()
