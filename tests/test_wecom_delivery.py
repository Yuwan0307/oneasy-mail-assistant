import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.core import Mail, Decision, classify
from app.store import Store
from app.worker import Worker
from app.web import create_app
from app.config import load_config
from app.notifications import group_item_text
from tests.smtp_support import SMTPServer, SMTP_CONFIG
from tests.test_core import at


class GroupSender:
    def __init__(self, failure=None):
        self.messages=[]; self.failure=failure

    def send_text(self, text, userids, identity=''):
        self.messages.append((text, userids, identity))
        if self.failure: raise self.failure


class WeComDeliveryTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'test.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan','李涵','li@example.com',True)
        with self.db.tx() as db: db.execute("UPDATE people SET userid='LiHan' WHERE id='lihan'")
        self.config={'smtp':dict(SMTP_CONFIG)}
        self.server=SMTPServer()
        transport=patch('smtplib.SMTP_SSL',side_effect=self.server.connect)
        transport.start(); self.addCleanup(transport.stop)
        self.worker=Worker(self.db,self.config)
        self.group=GroupSender(); self.worker.wecom_sender=self.group

    def add(self):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            return self.db.ingest(Mail('service@oneasy.cc','mail',at('2026-10-09T12:00'),
                '客户开通咨询','client@example.cc','申请开通'),Decision('action','请为客户开通','客户请求'))

    def deliver(self, when='2026-10-09T12:01'):
        with patch('app.worker.now',return_value=at(when)): self.worker.sync()

    def second_owner(self, userid='Other'):
        self.db.save_person('other','同事','other@example.com')
        with self.db.tx() as db: db.execute('UPDATE people SET userid=? WHERE id=?',(userid,'other'))
        self.db.change_owners('service@oneasy.cc',['lihan','other'],'admin')

    def test_one_group_message_mentions_all_owners_without_email_or_repeat(self):
        self.second_owner(); self.add()
        self.deliver(); self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(set(self.group.messages[0][1]),{'LiHan','Other'})
        self.assertIn('客户开通咨询',self.group.messages[0][0])
        self.assertEqual(self.server.messages,[])
        self.assertEqual({n['state'] for n in self.db.rows('SELECT * FROM notices')},{'sent'})
        self.assertEqual({n['channel'] for n in self.db.rows('SELECT * FROM notices')},{'wecom'})

    def test_group_failure_falls_back_for_every_owner_and_stays_email_after_restart(self):
        self.second_owner(); self.add()
        self.group.failure=RuntimeError('private webhook key')
        self.deliver()
        self.worker=Worker(Store(self.db.path),self.config)
        self.worker.wecom_sender=self.group
        self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)
        self.assertCountEqual([m['To'] for m in self.server.messages],['li@example.com','other@example.com'])
        self.assertEqual({n['channel'] for n in self.db.rows('SELECT * FROM notices')},{'smtp'})
        self.assertNotIn('private webhook key',json.dumps(self.db.rows('SELECT * FROM audit')))

    def test_owner_without_member_id_receives_email_while_other_is_mentioned(self):
        self.second_owner(''); self.add()
        self.deliver(); self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(self.group.messages[0][1],['LiHan'])
        self.assertEqual([m['To'] for m in self.server.messages],['other@example.com'])

    def test_failed_email_fallback_retries_email_without_reposting_group(self):
        self.add(); self.group.failure=RuntimeError('group unavailable')
        self.server.failure=smtplib.SMTPDataError(451,b'try later')
        self.deliver()
        self.assertEqual(self.db.rows('SELECT state FROM notices')[0]['state'],'pending')
        self.server.failure=None; self.group.failure=None
        self.deliver('2026-10-09T12:04')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(len(self.server.messages),1)
        self.assertEqual(self.db.rows('SELECT state FROM notices')[0]['state'],'sent')

    def test_pause_blocks_both_channels_and_resume_sends_group(self):
        self.add(); self.worker.set_paused(True); self.deliver()
        self.assertEqual(self.group.messages,[]); self.assertEqual(self.server.messages,[])
        self.worker.set_paused(False); self.deliver('2026-10-09T12:02')
        self.assertEqual(len(self.group.messages),1)

    def test_sensitive_mail_notice_does_not_disclose_equipment_values(self):
        calls=[]
        mail=Mail('service@oneasy.cc','secret',at('2026-10-09T12:00'),
            '192.168.1.22 网络设置','client@example.cc','token=private')
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.db.ingest(mail,classify(mail,lambda value:calls.append(value)))
        self.deliver()
        text=self.group.messages[0][0]
        self.assertNotIn('192.168.1.22',text); self.assertNotIn('token=private',text)
        self.assertEqual(calls,[])

    def test_removed_owner_is_not_mentioned(self):
        self.second_owner(); self.add()
        self.db.change_owners('service@oneasy.cc',['lihan'],'admin')
        self.deliver()
        self.assertEqual(self.group.messages[0][1],['LiHan'])

    def test_review_notice_has_readable_time_and_hints_without_ai_reason(self):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.db.ingest(Mail('service@oneasy.cc','review-readable',at('2026-10-09T12:00'),
                '客户开通咨询','client@example.cc','申请开通'),Decision('action','客户希望开通试用账号',
                'raw-classifier-reason action confidence 0.99',hints="['确认所需资料', '说明试用期限']"))
        self.deliver()
        self.db.schedule(at('2026-10-09T18:00'))
        self.deliver('2026-10-09T18:01')
        text=self.group.messages[-1][0]
        self.assertIn('处理复核',text)
        self.assertIn('已处理可忽略',text)
        self.assertIn('2026-10-09 12:00',text)
        self.assertIn('• 确认所需资料',text)
        self.assertIn('• 说明试用期限',text)
        for raw in ('raw-classifier-reason',"['",'T12:00','+08:00','无需反馈'):
            self.assertNotIn(raw,text)

    def test_deadline_notice_names_the_customer_deadline(self):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.db.ingest(Mail('service@oneasy.cc','deadline-readable',at('2026-10-09T12:00'),
                '请在下午五点前协助','client@example.cc','请求'),Decision('action','请协助处理',
                '客户业务',deadline=at('2026-10-09T17:00')))
        self.deliver()
        self.db.schedule(at('2026-10-09T16:00'))
        self.deliver('2026-10-09T16:01')
        text=self.group.messages[-1][0]
        self.assertIn('截止前复核',text)
        self.assertIn('2026-10-09 17:00',text)

    def test_truncated_hint_list_keeps_complete_advice_and_flags_missing_tail(self):
        item={'subject':'开通咨询','account':'service@oneasy.cc','sender':'client@example.cc',
              'received':'2026-10-09T12:00:00+08:00','summary':'申请试用',
              'hints':''}
        for hints in (['确认所需资料','说明试用期限'*100],
                      ['确认所需资料','确认联系人','告知流程','说明试用期限'*100]):
            item['hints']=str(hints)[:500]
            for compact in (True,False):
                text=group_item_text(item,compact=compact)
                self.assertIn('• 确认所需资料',text)
                self.assertIn('部分建议内容不完整，请结合原邮件核对',text)
                self.assertNotIn("['",text)

    def test_ambiguous_deadline_keeps_manual_check_prompt_without_raw_reason(self):
        item={'subject':'开通咨询','account':'service@oneasy.cc','sender':'client@example.cc',
              'received':'2026-10-09T12:00:00+08:00','summary':'申请试用',
              'reason':'raw-classifier-reason；截止要求需人工确认','deadline':None}
        for compact in (True,False):
            text=group_item_text(item,compact=compact)
            self.assertIn('客户提出了时间要求',text)
            self.assertIn('具体截止时间请查看原邮件并人工确认',text)
            self.assertNotIn('raw-classifier-reason',text)

    def test_system_group_alert_translates_internal_error_code(self):
        self.worker.alert('mailbox_scan_failed','service@oneasy.cc')
        self.deliver('2099-10-09T12:01')
        text=self.group.messages[-1][0]
        self.assertIn('邮箱扫描未成功',text)
        self.assertNotIn('mailbox_scan_failed',text)

    def test_full_daily_review_stays_in_email(self):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.db.ingest(Mail('service@oneasy.cc','ad',at('2026-10-09T12:00'),'广告待复核',
                'ad@example.cc','广告'),Decision('digest','原始摘要','非业务邮件'))
        self.db.schedule(at('2026-10-09T18:00'))
        self.deliver('2026-10-09T18:01')
        self.assertEqual(self.group.messages,[])
        self.assertIn('广告待复核',self.server.messages[0].get_content())

    def test_member_id_update_is_optional_and_preserved_by_legacy_callers(self):
        self.db.save_person('lihan','李涵','li@example.com',userid='NewMember')
        self.db.save_person('lihan','李涵','li@example.com',True)
        self.assertEqual(self.db.rows("SELECT userid FROM people WHERE id='lihan'")[0]['userid'],'NewMember')
        with self.assertRaises(ValueError):
            self.db.save_person('lihan','李涵','li@example.com',userid='@all')

    def test_member_id_changes_through_settings_without_sending_messages(self):
        client=TestClient(create_app(self.db,self.config),base_url='http://127.0.0.1',client=('127.0.0.1',123))
        response=client.post('/api/people',json={'id':'lihan','name':'李涵','email':'li@example.com','userid':'NewMember'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(client.get('/api/state').json()['people'][0]['userid'],'NewMember')
        self.assertEqual(self.server.messages,[])

    def test_webhook_encrypted_saved_and_not_returned_or_logged(self):
        client=TestClient(create_app(self.db,self.config),base_url='http://127.0.0.1',client=('127.0.0.1',123))
        secret='local-secret-for-test'
        url='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='+secret
        response=client.post('/api/wecom',json={'enabled':True,'webhook':url})
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(client.app.state.worker.wecom_sender)
        self.assertEqual(client.get('/api/state').json()['wecom'],{'enabled':True,'webhook_saved':True})
        self.assertNotIn(secret,client.get('/api/state').text)
        self.assertNotIn(secret,json.dumps(self.db.rows('SELECT * FROM audit')))
        self.assertNotIn(secret,(self.db.path.parent/'config.json').read_text())
        self.assertEqual(load_config(self.db.path.parent/'config.json')['wecom']['webhook'],url)
        self.assertEqual(client.post('/api/wecom',json={'enabled':False,'webhook':''}).status_code,200)
        self.assertIsNone(client.app.state.worker.wecom_sender)
        self.assertEqual(load_config(self.db.path.parent/'config.json')['wecom']['webhook'],url)
        self.assertEqual(client.post('/api/wecom',json={'enabled':True,'webhook':'https://evil.example/?key='+secret}).status_code,400)
        self.assertTrue(self.config['wecom']['webhook']==url)
        self.assertEqual(self.server.messages,[])
