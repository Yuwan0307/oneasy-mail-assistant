import json
import smtplib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.core import Mail, Decision
from app.store import Store
from app.smtp import SMTPSender
from tests.smtp_support import SMTPServer, SMTP_CONFIG
from app.worker import Worker
from app.web import create_app
from tests.test_core import at


class NotificationTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'test.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan','李涵','li@example.com',True)
        self.config={'admin_token':'a'*32,'smtp':dict(SMTP_CONFIG)}
        self.server=SMTPServer(); self.sent=self.server.messages
        transport=patch('smtplib.SMTP_SSL',side_effect=self.server.connect)
        transport.start(); self.addCleanup(transport.stop)
        self.worker=Worker(self.db,self.config)

    def add(self,mid='mail',category='action',received='2026-10-09T12:00',deadline=None,created=None):
        with patch('app.store.now',return_value=at(created or received)):
            return self.db.ingest(Mail('service@oneasy.cc',mid,at(received),mid,'client@example.cc','申请'),
                Decision(category,'摘要'+mid,'业务请求' if category!='digest' else '非业务邮件',at(deadline) if deadline else None))

    def deliver(self,when='2026-10-09T12:01'):
        with patch('app.worker.now',return_value=at(when)):
            self.worker.sync()

    def test_plain_email_delivery_only_to_owner_and_does_not_repeat(self):
        self.add()
        self.deliver(); self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0].get_content_type(),'text/plain')
        self.assertEqual(self.sent[0]['To'],'li@example.com')
        self.assertIn('摘要mail',self.sent[0].get_content())
        self.assertNotIn('Reply-To',self.sent[0])
        self.assertEqual(self.db.rows('SELECT state FROM notices')[0]['state'],'sent')

    def test_first_review_only_once_and_deadline_on_later_day_not_merged(self):
        item=self.add(deadline='2026-10-11T19:00')
        for day in ['09','10','11','12']:
            self.db.schedule(at(f'2026-10-{day}T18:00'))
        scheduled=self.db.rows("SELECT kind FROM notices WHERE item_id=? AND kind!='initial'",(item,))
        self.assertCountEqual([n['kind'] for n in scheduled],['review','deadline'])

    def test_first_review_and_same_time_deadline_merge(self):
        item=self.add(deadline='2026-10-09T19:00')
        self.db.schedule(at('2026-10-09T18:00'))
        scheduled=self.db.rows("SELECT kind FROM notices WHERE item_id=? AND kind!='initial'",(item,))
        self.assertEqual([n['kind'] for n in scheduled],['review'])

    def test_after_six_review_waits_until_next_day_and_information_has_no_followup(self):
        item=self.add(received='2026-10-09T18:00')
        info=self.add('thanks','info')
        self.db.schedule(at('2026-10-09T18:00'))
        self.assertEqual(self.db.rows("SELECT * FROM notices WHERE kind='review'"),[])
        self.db.schedule(at('2026-10-10T18:00'))
        self.assertEqual([n['item_id'] for n in self.db.rows("SELECT * FROM notices WHERE kind='review'")],[item])
        self.assertEqual(len(self.db.rows('SELECT * FROM notices WHERE item_id=?',(info,))),1)

    def test_daily_report_does_not_require_confirmation_and_catches_late_scan(self):
        self.add('ad','digest'); self.add('request')
        self.db.schedule(at('2026-10-09T18:00'))
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE kind='report'")
        self.add('late','digest',created='2026-10-09T19:00')
        self.db.schedule(at('2026-10-10T18:00'))
        report=self.db.report('lihan','2026-10-10')
        self.assertEqual([i['subject'] for i in report['review']],['late'])
        self.assertEqual([i['subject'] for i in report['items']],['late'])
        self.assertNotIn('open',report)

    def test_full_report_utf8_chunks_resume_after_failure_and_restart(self):
        for i in range(75): self.add(f'广告{i:03d}','digest')
        with self.db.tx() as db: db.execute('UPDATE items SET summary=?',('完整说明'*250,))
        self.db.schedule(at('2026-10-09T18:00'))
        self.deliver('2026-10-09T18:01')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE kind='report'")[0]['state'],'pending')
        self.server.failure=smtplib.SMTPDataError(451,b'try later'); self.deliver('2026-10-09T18:02')
        self.server.failure=None
        self.worker=Worker(Store(self.db.path),self.config)
        for i in range(300):
            with patch('app.worker.now',return_value=at('2026-10-09T19:00')+timedelta(seconds=i*5)):
                self.worker.sync()
            if self.db.rows("SELECT state FROM notices WHERE kind='report'")[0]['state']=='sent': break
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE kind='report'")[0]['state'],'sent')
        text=''.join(m.get_content() for m in self.sent)
        for i in range(75): self.assertIn(f'广告{i:03d}',text)
        self.assertEqual(text.count('邮件每日汇总'),1)
        self.assertTrue(all(len(m.get_content().encode('utf-8'))<129000 for m in self.sent))
        self.assertTrue(self.db.rows("SELECT * FROM audit WHERE action='notification_failed'"))

    def test_rebind_preserves_pending_information_and_system_notice(self):
        self.add('thanks','info')
        with self.db.tx() as db: self.db.notice(db,'alert:test','lihan','system',payload={'text':'系统提示'},due=at('2026-10-09T12:00'))
        self.db.save_person('lihan','李涵','new@example.com',True)
        self.deliver(); self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.sent),2)
        self.assertEqual({str(m['To']) for m in self.sent},{'new@example.com'})

    def test_completion_and_confirmation_requests_are_rejected(self):
        item=self.add()
        client=TestClient(create_app(self.db,{**self.config,'demo':True}),base_url='http://127.0.0.1',client=('127.0.0.1',1))
        for action in ['done','confirm']:
            response=client.post('/api/action',json={'id':item,'version':1,'action':action},headers={'Authorization':'Bearer '+'a'*32})
            self.assertEqual(response.status_code,400)
        self.assertEqual(self.db.rows("SELECT * FROM audit WHERE action='completed'"),[])

    def test_owner_change_does_not_lose_unsent_digest_in_new_owners_report(self):
        self.db.save_person('new','新负责人','new@example.com',True)
        self.db.change_owner('partner@oneasy.cc','new','admin')
        self.add('转交广告','digest')
        self.db.schedule(at('2026-10-09T18:00'))
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE owner='new' AND kind='report'")
        with patch('app.store.now',return_value=at('2026-10-10T12:00')):
            self.db.change_owner('service@oneasy.cc','new','admin')
        self.db.schedule(at('2026-10-10T18:00'))
        self.assertEqual([i['subject'] for i in self.db.report('new','2026-10-10')['review']],['转交广告'])
        self.assertEqual(self.db.report('lihan','2026-10-09')['review'],[])

    def test_activate_without_gateway_or_callback_configuration(self):
        db=Store(Path(self.tmp.name)/'inactive.db')
        db.save_person('lihan','李涵','li@example.com',True)
        app=create_app(db,self.config)
        class MailAPI:
            def folders(self,account): return [{'id':'2'}]
        app.state.worker.mail_api=MailAPI()
        client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1))
        response=client.post('/api/activate',headers={'Authorization':'Bearer '+'a'*32})
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(db.get('activated'))

    def test_owner_change_transfers_unsent_operations_with_mail(self):
        self.db.save_person('new','新负责人','new@example.com',True)
        self.db.change_owner('partner@oneasy.cc','new','admin')
        item=self.add('unreported','digest')
        with patch('app.store.now',return_value=at('2026-10-09T13:00')):
            self.db.log('move_started',item,{'target':'review'})
        self.db.schedule(at('2026-10-09T18:00'))
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE owner='new' AND kind='report'")
        with patch('app.store.now',return_value=at('2026-10-10T12:00')):
            self.db.change_owner('service@oneasy.cc','new','admin')
        self.db.schedule(at('2026-10-10T18:00'))
        operations=[a['action'] for a in self.db.report('new','2026-10-10')['operations'] if a['item_id']==item]
        self.assertCountEqual(operations,['classified','instant_skipped','move_started'])

    def test_owner_change_does_not_repeat_already_reported_history(self):
        self.db.save_person('new','新负责人','new@example.com',True)
        self.db.change_owner('partner@oneasy.cc','new','admin')
        self.add('already-reported','digest')
        self.db.schedule(at('2026-10-09T18:00'))
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE kind='report'")
        with patch('app.store.now',return_value=at('2026-10-10T12:00')):
            self.db.change_owner('service@oneasy.cc','new','admin')
        self.db.schedule(at('2026-10-10T18:00'))
        self.assertEqual(self.db.report('new','2026-10-10')['items'],[])

    def test_queued_deadline_expires_before_delivery(self):
        item=self.add(deadline='2026-10-09T17:00')
        self.db.schedule(at('2026-10-09T16:00'))
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        self.deliver('2026-10-10T12:00')
        self.assertEqual(self.sent,[])
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE item_id=? AND kind='deadline'",(item,))[0]['state'],'cancelled')


if __name__=='__main__': unittest.main()
