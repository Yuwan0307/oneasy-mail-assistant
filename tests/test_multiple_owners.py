import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.core import Mail, Decision
from app.store import Store
from app.web import create_app
from app.worker import Worker
from tests.smtp_support import SMTPServer, SMTP_CONFIG
from tests.test_core import at


class MultipleOwnersTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.db = Store(Path(self.temp.name) / 'main.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan', '李涵', 'li@example.com', True)
        self.db.save_person('other', '同事', 'other@example.com')
        self.server = SMTPServer()
        mocked = patch('smtplib.SMTP_SSL', side_effect=self.server.connect)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.app = create_app(self.db, {'smtp': dict(SMTP_CONFIG)})
        self.client = TestClient(self.app, base_url='http://127.0.0.1', client=('127.0.0.1', 1))

    def add(self, mid='customer', category='action', account='service@oneasy.cc'):
        with patch('app.store.now', return_value=at('2026-10-09T12:00')):
            return self.db.ingest(Mail(account, mid, at('2026-10-09T12:00'), mid, 'client@example.com', '咨询'),
                                  Decision(category, '摘要', '客户来信', at('2026-10-11T17:00')))

    def test_each_owner_receives_once_and_unrelated_mailbox_is_excluded(self):
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        item = self.add()
        other_box = self.add('partner', account='partner@oneasy.cc')
        for i in range(5):
            with patch('app.worker.now', return_value=at('2026-10-09T12:01') + timedelta(seconds=5*i)):
                self.app.state.worker.sync()
        recipients = [str(m['To']) for m in self.server.messages]
        self.assertCountEqual(recipients, ['li@example.com', 'other@example.com', 'li@example.com'])
        self.assertEqual(len(self.db.rows("SELECT * FROM notices WHERE item_id=? AND state='sent'", (item,))), 2)
        self.assertEqual(len(self.db.rows('SELECT * FROM notices WHERE item_id=?', (other_box,))), 1)
        self.db.schedule(at('2026-10-11T16:00'))
        self.db.schedule(at('2026-10-11T16:00'))
        for kind in ('review', 'deadline'):
            self.assertCountEqual([n['owner'] for n in self.db.rows('SELECT * FROM notices WHERE item_id=? AND kind=?', (item, kind))], ['lihan', 'other'])

    def test_reports_follow_each_recipients_delivery_interval(self):
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        self.add('ad', 'digest')
        self.add('partner-only', 'digest', 'partner@oneasy.cc')
        self.db.schedule(at('2026-10-09T18:00'))
        self.assertEqual([i['subject'] for i in self.db.report('other', '2026-10-09')['items']], ['ad'])
        with self.db.tx() as db:
            db.execute("UPDATE notices SET state='sent' WHERE kind='report' AND owner='lihan'")
        self.db.schedule(at('2026-10-10T18:00'))
        self.assertEqual(self.db.report('lihan', '2026-10-10')['items'], [])
        self.assertEqual([i['subject'] for i in self.db.report('other', '2026-10-10')['items']], ['ad'])

    def test_removing_recipient_cancels_only_their_pending_delivery(self):
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        item = self.add()
        self.db.schedule(at('2026-10-09T18:00'))
        self.db.change_owners('service@oneasy.cc', ['lihan'], 'admin')
        self.assertEqual(self.db.rows("SELECT * FROM notices WHERE owner='other' AND state='pending'"), [])
        self.assertTrue(self.db.rows("SELECT * FROM notices WHERE item_id=? AND owner='lihan' AND state='pending'", (item,)))
        self.assertEqual(self.db.report('other', '2026-10-09')['items'], [])

    def test_corrected_classification_reminds_every_current_owner(self):
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        item = self.add('ad', 'digest')
        self.assertTrue(self.db.review(item, 1, 'other', 'needs_action'))
        notices = self.db.rows("SELECT owner,version FROM notices WHERE item_id=? AND state='pending'", (item,))
        self.assertCountEqual(notices, [{'owner':'lihan', 'version':2}, {'owner':'other', 'version':2}])

    def test_person_autosave_never_sends_and_delete_preserves_history(self):
        response = self.client.post('/api/people', json={'id':'new', 'name':'新同事', 'email':'new@example.com'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.server.messages, [])
        self.db.change_owners('service@oneasy.cc', ['lihan', 'new'], 'admin')
        self.add()
        response = self.client.post('/api/people/remove', json={'id':'new'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.owners('service@oneasy.cc'), ['lihan'])
        self.assertEqual(len(self.db.rows('SELECT * FROM items')), 1)
        self.assertTrue(self.db.rows("SELECT * FROM audit WHERE action='person_removed'"))
        reopened = Store(self.db.path)
        self.assertFalse(reopened.rows("SELECT active FROM people WHERE id='new'")[0]['active'])
        self.assertNotIn('new', [p['id'] for p in self.client.get('/api/state').json()['people']])

    def test_cannot_leave_mailbox_without_owner_or_save_duplicate_address(self):
        response = self.client.post('/api/people/remove', json={'id':'lihan'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.db.owners('service@oneasy.cc'), ['lihan'])
        response = self.client.post('/api/mailbox', json={'account':'service@oneasy.cc','owners':[]})
        self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post('/api/people', json={'id':'duplicate','name':'重复','email':'LI@example.com'})
        self.assertEqual(response.status_code, 400, response.text)

    def test_mailbox_validation_is_atomic_and_state_exposes_multiple_owners(self):
        response = self.client.post('/api/mailbox', json={'account':'service@oneasy.cc','owners':['other'],'folders':['!bad']})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.owners('service@oneasy.cc'), ['lihan'])
        response = self.client.post('/api/mailbox', json={'account':'service@oneasy.cc','owners':['lihan','other']})
        self.assertEqual(response.status_code, 200, response.text)
        mailbox = next(b for b in self.client.get('/api/state').json()['mailboxes'] if b['account']=='service@oneasy.cc')
        self.assertCountEqual(mailbox['owners'], ['lihan','other'])

    def test_migration_preserves_legacy_notices_without_resending(self):
        item = self.add()
        with self.db.tx() as db:
            db.execute("UPDATE notices SET id=?,state='sent' WHERE item_id=?", ('initial:'+item+':1', item))
            db.execute("INSERT INTO notices(id,item_id,version,owner,kind,payload,state,due) VALUES(?,?,1,'lihan','review','{}','sent',?)", ('review:'+item+':1', item, '2026-10-09T18:00:00+08:00'))
            db.execute('DROP TABLE mailbox_owners')
            db.execute('DROP TABLE item_owners')
            db.execute("DELETE FROM settings WHERE key='multiple_owners'")
        reopened = Store(self.db.path)
        reopened.schedule(at('2026-10-10T18:00'))
        self.assertEqual(reopened.owners('service@oneasy.cc'), ['lihan'])
        self.assertEqual(reopened.rows("SELECT * FROM notices WHERE item_id=? AND kind='review' AND state='pending'", (item,)), [])

    def test_system_alert_goes_to_current_owners(self):
        self.db.change_owners('service@oneasy.cc', ['other'], 'admin')
        self.app.state.worker.alert('mailbox_scan_failed', 'service@oneasy.cc')
        self.assertEqual([n['owner'] for n in self.db.rows("SELECT * FROM notices WHERE kind='system'")], ['other'])

    def test_readding_recipient_resumes_unsent_but_does_not_repeat_sent_notice(self):
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        pending = self.add('pending')
        sent = self.add('sent')
        with self.db.tx() as db:
            db.execute("UPDATE notices SET state='sent' WHERE item_id=? AND owner='other'", (sent,))
        self.db.change_owners('service@oneasy.cc', ['lihan'], 'admin')
        self.db.change_owners('service@oneasy.cc', ['lihan', 'other'], 'admin')
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE item_id=? AND owner='other'", (pending,))[0]['state'], 'pending')
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE item_id=? AND owner='other'", (sent,))[0]['state'], 'sent')


if __name__ == '__main__': unittest.main()
