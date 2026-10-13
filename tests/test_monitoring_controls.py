import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.core import Decision, Mail
from app.store import Store
from app.web import create_app
from app.worker import Worker
from tests.test_core import at


class MonitoringControlsTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name) / 'main.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan', '李涵', 'owner@example.com')
        self.config = {'demo': False}
        self.box = self.db.mailboxes()[0]

    def client(self, **kwargs):
        app = create_app(self.db, self.config, **kwargs)
        return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1)), app

    def test_paused_worker_makes_no_mail_calls_or_sends_and_keeps_pending_notice(self):
        class Offline:
            def folders(self, account): raise AssertionError('paused scanner used the network')
            def metadata(self, *args): raise AssertionError('paused move used the network')
            def send_text(self, *args): raise AssertionError('paused sender sent email')
        worker = Worker(self.db, self.config, mail_api=Offline())
        worker.sender = Offline()
        item = self.db.ingest(Mail(self.box['account'], 'new', at('2026-10-09T11:00'), '业务', 'client@example.com', '开通'), Decision('action', '开通', '客户请求'))
        with self.db.tx() as db:
            db.execute("UPDATE items SET move_state='restore_pending' WHERE id=?", (item,))
        worker.set_paused(True)
        worker.poll_mailbox(self.box)
        worker.moves()
        worker.tick()
        worker.sync()
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE item_id=?", (item,))[0]['state'], 'pending')
        self.assertEqual(self.db.rows('SELECT * FROM reports'), [])

    def test_resume_scans_mail_that_arrived_while_paused_without_resetting_activation(self):
        calls = []
        class Inbox:
            def folders(self, account):
                calls.append('folders')
                return [{'id': '2'}]
            def messages(self, account, folder):
                return [{'id': 'during-pause', 'receivedDateTime': '2026-10-09T05:00:00Z'}]
            def read(self, account, mid):
                return Mail(account, mid, at('2026-10-09T13:00'), '客户开通', 'client@example.com', '请开通')
        worker = Worker(self.db, self.config, mail_api=Inbox())
        original = self.db.get('activated')
        worker.set_paused(True)
        worker.poll_mailbox(self.box)
        self.assertEqual(calls, [])
        worker.set_paused(False)
        worker.poll_mailbox(self.box)
        worker.poll_mailbox(self.box)
        self.assertEqual(len(self.db.rows('SELECT * FROM items')), 1)
        self.assertEqual(len(self.db.rows("SELECT * FROM notices WHERE kind='initial'")), 1)
        self.assertEqual(self.db.get('activated'), original)

    def test_pause_during_read_does_not_mark_unprocessed_mail_seen(self):
        reads = []
        class Inbox:
            def folders(self, account): return [{'id': '2'}]
            def messages(self, account, folder): return [{'id': 'mid', 'receivedDateTime': '2026-10-09T05:00:00Z'}]
            def read(self, account, mid):
                reads.append(mid)
                if len(reads) == 1: worker.set_paused(True)
                return Mail(account, mid, at('2026-10-09T13:00'), '开通', 'client@example.com', '请开通')
        worker = Worker(self.db, self.config, mail_api=Inbox())
        worker.poll_mailbox(self.box)
        self.assertEqual(self.db.rows('SELECT * FROM seen'), [])
        self.assertEqual(self.db.rows('SELECT * FROM items'), [])
        worker.set_paused(False)
        worker.poll_mailbox(self.box)
        self.assertEqual(reads, ['mid', 'mid'])
        self.assertEqual(len(self.db.rows('SELECT * FROM items')), 1)

    def test_pause_waits_for_inflight_smtp_before_reporting_success(self):
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        class Sender:
            def message_id(self, *args): return '<test@example.com>'
            def send_text(self, *args):
                entered.set()
                if not release.wait(3): raise AssertionError('test release timed out')
        worker = Worker(self.db, self.config)
        worker.sender = Sender()
        self.db.ingest(Mail(self.box['account'], 'smtp', at('2026-10-09T11:00'), '业务', 'client@example.com', '开通'), Decision('action', '开通', '请求'))
        sending = threading.Thread(target=worker.sync)
        sending.start()
        self.assertTrue(entered.wait(2))
        pausing = threading.Thread(target=lambda: (worker.set_paused(True), paused.set()))
        pausing.start()
        try:
            self.assertFalse(paused.wait(.1))
        finally:
            release.set()
            sending.join(3)
            pausing.join(3)
        self.assertTrue(paused.is_set())
        self.assertTrue(worker.is_paused())

    def test_pause_state_persists_and_management_pages_stay_available(self):
        client, app = self.client()
        response = client.post('/api/monitoring', json={'paused': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['paused'])
        self.assertTrue(client.get('/api/state').json()['paused'])
        self.assertTrue(Worker(self.db, self.config).is_paused())
        self.assertEqual(client.get('/api/items').status_code, 200)
        self.assertEqual(client.post('/api/people', json={'id': 'new', 'name': '同事', 'email': 'new@example.com'}).status_code, 200)
        self.assertEqual(client.post('/api/monitoring', json={'paused': False}).status_code, 200)
        self.assertFalse(Worker(self.db, self.config).is_paused())
        actions = [row['action'] for row in self.db.rows('SELECT action FROM audit')]
        self.assertIn('monitoring_paused', actions)
        self.assertIn('monitoring_resumed', actions)

    def test_pause_waits_for_current_scan_stage_before_acknowledging(self):
        for stage in ('folders', 'page', 'read', 'classify'):
            with self.subTest(stage=stage):
                entered, release, paused = threading.Event(), threading.Event(), threading.Event()
                def block(name):
                    if stage == name:
                        entered.set()
                        if not release.wait(3): raise AssertionError('scan release timed out')
                class Inbox:
                    def folders(self, account):
                        block('folders')
                        return [{'id': '2'}]
                    def messages(self, account, folder):
                        block('page')
                        yield {'id': stage, 'receivedDateTime': '2026-10-09T05:00:00Z'}
                    def read(self, account, mid):
                        block('read')
                        return Mail(account, mid, at('2026-10-09T13:00'), '开通', 'client@example.com', '请开通')
                def inspect(mail, ai):
                    block('classify')
                    return Decision('action', '开通', '客户请求')
                worker = Worker(self.db, self.config, mail_api=Inbox())
                worker.set_paused(False)
                with patch('app.worker.classify', inspect):
                    scanning = threading.Thread(target=lambda: worker.poll_mailbox(self.box))
                    scanning.start()
                    self.assertTrue(entered.wait(2))
                    pausing = threading.Thread(target=lambda: (worker.set_paused(True), paused.set()))
                    pausing.start()
                    try:
                        self.assertFalse(paused.wait(.1))
                    finally:
                        release.set()
                        scanning.join(3)
                        pausing.join(3)
                    self.assertTrue(paused.is_set())

    def test_pause_prevents_fetching_next_mail_page(self):
        pages = []
        class Inbox:
            def folders(self, account): return [{'id': '2'}]
            def messages(self, account, folder):
                pages.append(1)
                yield {'id': 'already-seen', 'receivedDateTime': '2026-10-09T05:00:00Z'}
                pages.append(2)
                yield {'id': 'next-page', 'receivedDateTime': '2026-10-09T05:00:00Z'}
        worker = Worker(self.db, self.config, mail_api=Inbox())
        original_rows = self.db.rows
        def rows(sql, args=()):
            if 'SELECT 1 FROM seen' in sql:
                worker.set_paused(True)
                return [{'found': 1}]
            return original_rows(sql, args)
        with patch.object(self.db, 'rows', rows):
            worker.poll_mailbox(self.box)
        self.assertEqual(pages, [1])

    def test_monitoring_control_rejects_bad_values_and_cross_site_calls(self):
        client, app = self.client()
        for value in ['true', 1, None]:
            self.assertEqual(client.post('/api/monitoring', json={'paused': value}).status_code, 400)
        self.assertEqual(client.post('/api/monitoring', json={'paused': True}, headers={'Origin': 'https://unrelated.example'}).status_code, 403)
        self.config['demo'] = True
        self.assertEqual(client.post('/api/monitoring', json={'paused': True}).status_code, 409)

    def test_desktop_token_is_one_time_short_lived_and_contains_no_command(self):
        client, app = self.client(desktop_control=True, desktop_port=8765)
        self.assertTrue(client.get('/api/state').json()['desktop_available'])
        response = client.post('/api/desktop/request', json={'action': 'exit'})
        self.assertEqual(response.status_code, 200)
        uri = response.json()['url']
        self.assertRegex(uri, r'^oneasy-mail-assistant://control/8765/[A-Za-z0-9_-]+$')
        self.assertNotIn('exit', uri)
        token = uri.rsplit('/', 1)[1]
        self.assertEqual(client.post('/api/desktop/command', json={'token': token}).json(), {'action': 'exit'})
        self.assertEqual(client.post('/api/desktop/command', json={'token': token}).status_code, 409)
        with patch('app.web.time.monotonic', return_value=100):
            stale = client.post('/api/desktop/request', json={'action': 'hide'}).json()['url'].rsplit('/', 1)[1]
        with patch('app.web.time.monotonic', return_value=1000):
            self.assertEqual(client.post('/api/desktop/command', json={'token': stale}).status_code, 409)

    def test_desktop_control_rejects_unknown_actions_or_non_desktop_mode(self):
        client, app = self.client()
        self.assertFalse(client.get('/api/state').json()['desktop_available'])
        self.assertEqual(client.post('/api/desktop/request', json={'action': 'exit'}).status_code, 409)
        desktop, app = self.client(desktop_control=True)
        for action in ['arbitrary-command', [], None, 1]:
            self.assertEqual(desktop.post('/api/desktop/request', json={'action': action}).status_code, 400)
        self.assertEqual(desktop.post('/api/desktop/request', json={'action': 'exit'}, headers={'Origin': 'https://unrelated.example'}).status_code, 403)
        self.config['demo'] = True
        self.assertEqual(desktop.post('/api/desktop/request', json={'action': 'exit'}).status_code, 409)


if __name__ == '__main__':
    unittest.main()
