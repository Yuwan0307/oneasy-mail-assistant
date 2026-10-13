import json
import smtplib
import sqlite3
import ssl
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.config import load_config
from app.core import Mail, Decision
from app.store import Store
from app.web import create_app
from app.worker import Worker
from tests.smtp_support import SMTPServer, SMTP_CONFIG
from tests.test_core import at


class SMTPTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name)/'main.db')
        self.config = {'admin_token': 'a'*32, 'smtp': dict(SMTP_CONFIG)}
        self.server = SMTPServer()
        self.ssl_patch = patch('smtplib.SMTP_SSL', side_effect=self.server.connect)
        self.ssl_patch.start(); self.addCleanup(self.ssl_patch.stop)
        self.app = create_app(self.db, self.config)
        self.client = TestClient(self.app,base_url='http://127.0.0.1',client=('127.0.0.1',1))
        self.headers = {}

    def sender(self):
        sender = self.app.state.worker.sender
        self.assertIsNotNone(sender, 'SMTP configuration must create the notification sender')
        return sender

    def test_tls_mail_has_one_recipient_complete_content_and_stable_identity(self):
        sender = self.sender()
        sender.send_text('owner@example.com', '完整摘要'*2000, '需要处理', 'notice-1')
        sender.send_text('owner@example.com', '完整摘要'*2000, '需要处理', 'notice-1')
        first, second = self.server.messages
        self.assertEqual(first['To'], 'owner@example.com')
        self.assertEqual(first['From'].addresses[0].addr_spec, 'notify@example.com')
        self.assertEqual(first.get_content().strip(), '完整摘要'*2000)
        self.assertEqual(first['Message-ID'], second['Message-ID'])
        self.assertEqual(first['Auto-Submitted'], 'auto-generated')
        self.assertNotIn('Bcc', first)
        self.assertIn(('envelope', 'notify@example.com', ['owner@example.com']), self.server.calls)
        context = self.server.calls[0][3]['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_starttls_happens_before_login_and_header_injection_is_rejected(self):
        config = {**SMTP_CONFIG, 'security': 'starttls', 'port': 587}
        with patch('smtplib.SMTP', side_effect=self.server.connect):
            sender = Worker(self.db, {'smtp': config}).sender
            self.assertIsNotNone(sender)
            sender.send_text('owner@example.com', '内容', '标题', 'id')
        names = [c[0] for c in self.server.calls]
        self.assertLess(names.index('starttls'), names.index('login'))
        count = len(self.server.messages)
        for recipient in ['owner@example.com,customer@example.com', 'owner@example.com\r\nBcc: bad@example.com', 'LiHan']:
            with self.assertRaises(ValueError): sender.send_text(recipient, '内容', '标题', 'id')
        self.assertEqual(len(self.server.messages), count)

    def test_smtp_save_is_local_encrypted_and_has_no_secret_response(self):
        self.assertEqual(self.client.post('/api/smtp', json=SMTP_CONFIG, headers={'Origin':'https://other.example'}).status_code, 403)
        result = self.client.post('/api/smtp', json=SMTP_CONFIG, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        path = self.db.path.parent/'config.json'
        self.assertEqual(load_config(path)['smtp']['password'], 'local-test-secret')
        state = self.client.get('/api/state', headers=self.headers)
        self.assertNotIn('local-test-secret', state.text+str(self.db.rows('SELECT * FROM audit'))+path.read_text())
        self.assertEqual(state.json()['smtp']['address'], 'notify@example.com')
        self.assertEqual(self.server.messages, [])
        changed = {**SMTP_CONFIG, 'host': 'other.example.com', 'password': ''}
        self.assertEqual(self.client.post('/api/smtp', json=changed, headers=self.headers).status_code, 400)
        self.assertEqual(load_config(path)['smtp']['host'], 'smtp.example.com')

    def test_binding_sends_only_to_entered_owner_and_failure_keeps_old_binding(self):
        self.db.save_person('lihan', '李涵', 'old@example.com', True)
        data = {'id': 'lihan', 'name': '李涵', 'email': 'owner@example.com'}
        self.server.failure = smtplib.SMTPAuthenticationError(535, b'private server secret')
        failed = self.client.post('/api/person', json=data, headers=self.headers)
        self.assertEqual(failed.status_code, 400)
        self.assertNotIn('private server secret', failed.text+str(self.db.rows('SELECT * FROM audit')))
        self.assertEqual(self.db.rows('SELECT * FROM people')[0].get('email'), 'old@example.com')
        self.server.failure = None
        passed = self.client.post('/api/person', json=data, headers=self.headers)
        self.assertEqual(passed.status_code, 200, passed.text)
        self.assertEqual(self.server.messages[0]['To'], 'owner@example.com')
        self.assertEqual(self.db.rows('SELECT * FROM people')[0]['email'], 'owner@example.com')
        self.assertTrue(self.db.rows('SELECT * FROM people')[0]['verified'])

    def test_own_notice_is_logged_and_skipped_before_body_or_ai_read(self):
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan', '李涵', 'owner@example.com', True)
        with patch('app.store.now', return_value=at('2026-10-09T12:00')):
            self.db.ingest(Mail('service@oneasy.cc','customer',at('2026-10-09T12:00'),'咨询','customer@example.com','开通'), Decision('action','开通','客户事项'))
        with patch('app.worker.now', return_value=at('2026-10-09T12:01')):
            self.app.state.worker.sync()
        self.assertEqual(len(self.server.messages), 1)
        own_id = self.server.messages[0]['Message-ID']
        class MailAPI:
            def folders(self, account): return [{'id':'2'}]
            def messages(self, account, folder):
                return [{'id':'notification-copy','receivedDateTime':'2026-10-09T04:02:00Z','internetMessageId':own_id}]
            def read(self, *args): raise AssertionError('Must not classify own notifications')
        worker = self.app.state.worker; worker.mail_api = MailAPI()
        worker.poll_mailbox(self.db.rows("SELECT * FROM mailboxes WHERE account='service@oneasy.cc'")[0])
        self.assertEqual(len(self.db.rows('SELECT * FROM items')), 1)
        self.assertEqual(len(self.db.rows("SELECT * FROM audit WHERE action='own_notification_skipped'")), 1)

    def test_old_wecom_binding_is_not_treated_as_a_verified_email(self):
        path = self.db.path.parent/'old.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE people(id TEXT PRIMARY KEY,name TEXT NOT NULL,userid TEXT NOT NULL DEFAULT '',verified INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1)")
            db.execute("INSERT INTO people VALUES('lihan','李涵','LiHan',1,1)")
            db.commit()
        migrated = Store(path)
        person = migrated.rows('SELECT * FROM people')[0]
        self.assertEqual(person.get('email'), '')
        self.assertFalse(person['verified'])
        self.assertEqual(person['userid'], 'LiHan')
        self.assertIsNone(Worker(migrated, {'wecom': {'corp_id':'old','agent_id':1,'secret':'old'}}).sender)


if __name__ == '__main__': unittest.main()
