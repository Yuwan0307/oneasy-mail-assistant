import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core import MAILBOXES
from app.store import Store
from app.worker import Worker
from tests.smtp_support import SMTPServer, SMTP_CONFIG
from tests.test_core import at
from tests.test_wecom_delivery import GroupSender


class SystemAlertTest(unittest.TestCase):
    def setUp(self):
        clock=patch('app.store.now',return_value=at('2026-10-09T12:00'))
        clock.start(); self.addCleanup(clock.stop)
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'test.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan','李涵','li@example.com',userid='LiHan')
        self.config={'smtp':dict(SMTP_CONFIG)}
        self.worker=Worker(self.db,self.config)
        self.group=GroupSender(); self.worker.wecom_sender=self.group
        self.server=SMTPServer()
        transport=patch('smtplib.SMTP_SSL',side_effect=self.server.connect)
        transport.start(); self.addCleanup(transport.stop)

    def alerts(self,accounts=None):
        with patch('app.worker.now',return_value=at('2026-10-09T12:00')):
            for account in accounts or MAILBOXES: self.worker.alert('mailbox_scan_failed',account)

    def sync(self,time='2026-10-09T12:01'):
        with patch('app.worker.now',return_value=at(time)): self.worker.sync()

    def test_all_mailboxes_one_message_and_daily_dedup_survives_restart(self):
        self.alerts(); self.sync()
        self.assertEqual(len(self.group.messages),1)
        text,userids,_=self.group.messages[0]
        self.assertEqual(userids,[])
        for account in MAILBOXES: self.assertEqual(text.count(account),1)
        self.assertIn('新邮件提醒可能延迟',text)
        self.assertNotIn('mailbox_scan_failed',text)
        self.assertLessEqual(len(text.encode('utf-8')),2048)
        self.assertEqual({n['state'] for n in self.db.rows("SELECT * FROM notices")},{'sent'})
        self.worker=Worker(Store(self.db.path),self.config); self.worker.wecom_sender=self.group
        self.alerts(); self.sync('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)

    def test_scan_round_waits_for_all_failures_and_records_safe_error_type(self):
        self.worker.mail_api=object()  # poll_mailbox is replaced below; a connection is configured.
        def scan(box):
            self.worker.sync()
            self.assertEqual(self.group.messages,[])
            raise httpx.ConnectError('private-url-and-secret')
        with patch('app.worker.now',return_value=at('2026-10-09T12:00')), \
             patch.object(self.worker,'poll_mailbox',side_effect=scan), \
             patch.object(self.worker,'moves'), patch.object(self.db,'schedule'), \
             patch.object(self.db,'backup'), patch.object(self.db,'archive'):
            self.worker.tick()
        self.sync()
        self.assertEqual(len(self.group.messages),0)
        records=self.db.rows("SELECT detail FROM audit WHERE action='mailbox_scan_failed'")
        self.assertEqual(len(records),6)
        self.assertTrue(all(json.loads(r['detail']).get('failure_type')=='connection' for r in records))
        self.assertNotIn('private-url-and-secret',json.dumps(records))

    def test_group_failure_falls_back_to_one_email_and_retries_without_group_spam(self):
        self.alerts(); self.group.failure=RuntimeError('group unavailable')
        self.server.failure=smtplib.SMTPDataError(451,b'try later')
        self.sync()
        self.assertEqual(len(self.group.messages),1)
        self.server.failure=None; self.group.failure=None
        self.worker=Worker(Store(self.db.path),self.config); self.worker.wecom_sender=self.group
        self.sync('2026-10-09T12:04'); self.sync('2026-10-09T12:05')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(len(self.server.messages),1)
        for account in MAILBOXES: self.assertIn(account,self.server.messages[0].get_content())
        self.assertEqual({n['state'] for n in self.db.rows("SELECT * FROM notices")},{'sent'})

    def test_system_group_never_mentions_owners_and_needs_no_member_id(self):
        self.db.save_person('other','同事','other@example.com',userid='Other')
        self.db.save_person('email','邮件同事','email@example.com')
        self.db.change_owners('service@oneasy.cc',['lihan','other','email'],'admin')
        self.db.change_owners('legal@oneasy.cc',['lihan','other','email'],'admin')
        self.alerts()
        self.db.change_owners('legal@oneasy.cc',['lihan'],'admin')
        for minute in range(1,8): self.sync(f'2026-10-09T12:0{minute}')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(self.group.messages[0][1],[])
        self.assertEqual(len(self.server.messages),0)
        self.assertTrue(self.db.rows("SELECT 1 FROM notices WHERE owner='email' AND state='cancelled'"))

    def test_legacy_pending_notices_are_merged_and_pause_still_blocks_send(self):
        self.alerts()
        with self.db.tx() as db:
            for row in db.execute('SELECT * FROM notices').fetchall():
                payload=json.loads(row['payload']); payload.pop('code',None)
                db.execute('UPDATE notices SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
        self.worker.set_paused(True); self.sync(); self.assertEqual(self.group.messages,[])
        self.worker.set_paused(False); self.sync('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual({n['state'] for n in self.db.rows("SELECT * FROM notices")},{'sent'})

    def test_different_fault_types_remain_separate_and_email_only_is_merged(self):
        self.worker.wecom_sender=None
        self.alerts(['service@oneasy.cc','legal@oneasy.cc'])
        with patch('app.worker.now',return_value=at('2026-10-09T12:00')):
            self.worker.alert('move_or_restore_failed','support@oneasy.cc')
        self.sync(); self.sync('2026-10-09T12:02'); self.sync('2026-10-09T12:03')
        self.assertEqual(len(self.server.messages),2)
        text=self.server.messages[0].get_content()
        self.assertIn('service@oneasy.cc',text); self.assertIn('legal@oneasy.cc',text)
        self.assertNotIn('support@oneasy.cc',text)
