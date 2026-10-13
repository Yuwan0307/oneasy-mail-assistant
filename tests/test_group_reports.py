import json
import smtplib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core import Mail, Decision
from app.notifications import message_parts, report_text, group_report_parts
from app.smtp import SMTPSender
from app.store import Store
from app.wecom import WeComSender
from app.worker import Worker
from tests.smtp_support import SMTP_CONFIG
from tests.test_core import at


class GroupReportsTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'test.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan','李涵','li@example.com',True,userid='LiHan')
        self.config={'smtp':dict(SMTP_CONFIG)}
        self.worker=Worker(self.db,self.config)
        self.emails=[]; self.group=[]; self.requests=[]
        self.smtp_failure=None; self.group_failure=False
        self.smtp_patch=patch.object(SMTPSender,'send_text',self.send_email)
        self.smtp_patch.start(); self.addCleanup(self.smtp_patch.stop)
        client=httpx.Client(transport=httpx.MockTransport(self.send_group))
        self.addCleanup(client.close)
        self.group_sender=WeComSender({'enabled':True,'webhook':
            'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=unit-test-only'},client)
        self.worker.wecom_sender=self.group_sender

    def send_email(self,recipient,text,subject='',identity=''):
        if self.smtp_failure: raise self.smtp_failure
        self.emails.append((recipient,text,subject,identity))

    def send_group(self,request):
        content=json.loads(request.content)['text']
        self.requests.append(content)
        if self.group_failure:
            raise httpx.ConnectError('private webhook key=unit-test-only',request=request)
        self.group.append(content)
        return httpx.Response(200,json={'errcode':0,'errmsg':'ok'})

    def add(self,subject='待复核广告',account='service@oneasy.cc',long=False,category='digest'):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            item=self.db.ingest(Mail(account,subject,at('2026-10-09T12:00'),subject,
                'client@example.com','广告'),Decision(category,'中文🙂说明'*200 if long else '完整摘要','非业务邮件'))
        return item

    def schedule(self,when='2026-10-09T18:00'):
        self.db.schedule(at(when),group_reports=True)

    def deliver(self,seconds=0):
        with patch('app.worker.now',return_value=at('2026-10-09T18:01')+timedelta(seconds=seconds)):
            self.worker.sync()

    def notice(self,kind='report_group',owner='lihan',day='2026-10-09'):
        return self.db.rows('SELECT * FROM notices WHERE id=?',(f'{kind}:{owner}:{day}',))[0]

    def finish(self,start=0):
        for second in range(start,start+400,4):
            self.deliver(second)
            if not self.db.rows("SELECT 1 FROM notices WHERE state='pending'"): return
        self.fail('report delivery did not finish')

    # A missing group schedule or lost SMTP task must fail this test.
    def test_daily_tick_queues_both_channels_and_each_owner_is_mentioned_separately(self):
        self.db.save_person('other','同事','other@example.com',userid='Other')
        self.db.change_owners('service@oneasy.cc',['lihan','other'],'admin')
        self.add('共有业务',category='info'); self.add('仅李涵业务','partner@oneasy.cc',category='info')
        with self.db.tx() as db: db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        with patch('app.worker.now',return_value=at('2026-10-09T18:00')):
            self.worker.tick()
        self.finish()
        self.assertCountEqual([email[0] for email in self.emails],['li@example.com','other@example.com'])
        self.assertTrue(all('共有业务' in email[1] for email in self.emails))
        self.assertEqual(len(self.group),2)
        by_member={part['mentioned_list'][0]:part['content'] for part in self.group}
        self.assertIn('负责人：李涵',by_member['LiHan'])
        self.assertIn('负责人：同事',by_member['Other'])
        self.assertIn('仅李涵业务',by_member['LiHan'])
        self.assertNotIn('仅李涵业务',by_member['Other'])
        self.assertTrue(all('请在原邮箱处理业务' in text for text in by_member.values()))
        self.assertEqual(self.notice()['channel'],'wecom')
        self.assertTrue(self.db.rows("SELECT 1 FROM audit WHERE action='notification_sent' AND detail LIKE '%wecom%'"))
        self.deliver(500)
        self.assertEqual(len(self.group),2)

    # A truncating transport call, dropped character, or missing prefix budget must fail.
    def test_complete_group_report_is_numbered_and_limited_including_utf8_prefix(self):
        self.add(long=True); self.schedule()
        self.finish()
        self.assertGreater(len(self.group),1)
        bodies=[]
        for index,part in enumerate(self.group,1):
            content=part['content']
            self.assertLessEqual(len(content.encode('utf-8')),2048)
            self.assertNotIn('mentioned_list',part)
            prefix,body=content.split('\n',1)
            self.assertIn(f'第{index}/{len(self.group)}条',prefix)
            self.assertIn('2026-10-09',prefix)
            self.assertNotIn('内容较长，请登录',content)
            bodies.append(body)
        text=''.join(bodies)
        self.assertIn('中文🙂说明'*200,text)
        self.assertIn('待复核广告',text)
        self.assertIn('负责人：李涵',text)
        self.assertEqual(len(self.emails),1)

    def test_report_omits_operations_and_keeps_every_email_without_raw_logs(self):
        self.add('业务复核一'); self.add('业务复核二','partner@oneasy.cc')
        with patch('app.store.now',return_value=at('2026-10-09T13:00')):
            self.db.log('notification_sent',detail={'notice':'internal-notice-id','channel':'wecom','userid':'LiHan'})
            self.db.log('wecom_changed',detail={'enabled':True,'webhook_saved':True})
        self.schedule(); self.finish()
        text=''.join(part['content'].split('\n',1)[1] for part in self.group)
        for expected in ('业务复核一','业务复核二','待人工复核','2026-10-09 18:00','原邮箱'):
            self.assertIn(expected,text)
        for raw in ('系统操作摘要','完成邮件分类','notification_sent','classified','internal-notice-id','webhook_saved','source_id','T18:00','+08:00'):
            self.assertNotIn(raw,text)
        self.assertNotIn('操作记录（',self.emails[0][1])
        self.assertNotIn('internal-notice-id',self.emails[0][1])
        self.assertTrue(self.db.report('lihan','2026-10-09')['operations'])
        self.assertTrue(self.db.rows("SELECT 1 FROM audit WHERE action='notification_sent' AND detail LIKE '%internal-notice-id%'"))

    def test_old_partially_sent_report_keeps_its_existing_fragment_boundaries(self):
        self.add(long=True); self.schedule(); self.deliver(0)
        report=self.db.report('lihan','2026-10-09')
        legacy=message_parts('负责人：李涵\n'+report_text(report,legacy=True),'report_group:lihan:2026-10-09:0',limit=2048)
        with self.db.tx() as db:
            db.execute("UPDATE notices SET part=1 WHERE kind='report_group'")
        self.deliver(4)
        self.assertEqual(self.group[0]['content'],legacy[1])

    # Losing part/retry_at or resetting them on restart would repeat an accepted fragment.
    def test_failure_retries_current_fragment_after_restart_without_repeating_accepted_fragment(self):
        self.add(long=True); self.schedule()
        self.deliver(0)  # Original SMTP report remains independent.
        self.deliver(4)
        self.assertEqual(self.notice()['part'],1)
        self.group_failure=True; self.deliver(8)
        failed=self.notice()
        self.assertEqual((failed['state'],failed['part'],failed['channel']),('pending',1,'wecom'))
        self.assertGreater(at(failed['retry_at'][:16]),at('2026-10-09T18:01'))
        self.assertNotIn('unit-test-only',json.dumps(self.db.rows('SELECT * FROM audit'))+failed['last_error'])
        attempted=self.requests[-1]['content']
        self.worker=Worker(Store(self.db.path),self.config); self.worker.wecom_sender=self.group_sender
        self.group_failure=False
        before=len(self.requests); self.deliver(12)
        self.assertEqual(len(self.requests),before)
        self.finish(200)
        self.assertEqual(self.group[1]['content'],attempted)
        self.assertEqual(sum(part['content']==self.group[0]['content'] for part in self.group),1)
        self.assertEqual(len(self.emails),1)
        self.assertEqual(self.notice()['state'],'sent')

    # Replacing a group retry with SMTP or advancing SMTP's window from group success must fail.
    def test_group_success_does_not_advance_failed_mail_window(self):
        self.add(); self.schedule()
        self.smtp_failure=smtplib.SMTPDataError(451,b'try later')
        self.deliver(0); self.deliver(4)
        self.assertEqual(self.notice()['state'],'sent')
        self.assertEqual(self.notice('report')['state'],'pending')
        self.schedule('2026-10-10T18:00')
        self.assertEqual([i['subject'] for i in self.db.report('lihan','2026-10-10')['items']],['待复核广告'])

    def test_group_failure_preserves_old_pending_task_when_next_day_report_is_created(self):
        self.add(); self.schedule(); self.deliver(0)
        self.group_failure=True; self.deliver(4)
        self.schedule('2026-10-10T18:00')
        self.assertEqual(self.notice()['state'],'pending')
        self.assertEqual(self.notice('report')['state'],'sent')
        self.assertEqual(self.db.report('lihan','2026-10-10')['items'],[])

    def test_email_address_update_does_not_repeat_accepted_group_fragments(self):
        self.add(long=True); self.schedule(); self.deliver(0); self.deliver(4)
        accepted=self.group[0]['content']
        self.db.save_person('lihan','李涵','new@example.com')
        self.assertEqual(self.notice()['part'],1)
        self.finish(8)
        self.assertEqual(sum(part['content']==accepted for part in self.group),1)

    # Ignoring global pacing would allow two accepted group fragments less than four seconds apart.
    def test_group_fragments_share_global_pacing_and_pause_controls(self):
        self.add(long=True); self.schedule(); self.deliver(0)
        self.deliver(3); self.assertEqual(self.group,[])
        self.deliver(4); self.assertEqual(len(self.group),1)
        self.deliver(7); self.assertEqual(len(self.group),1)
        self.worker.set_paused(True); self.deliver(8)
        self.assertEqual(len(self.group),1)
        self.worker.set_paused(False); self.deliver(12)
        self.assertEqual(len(self.group),2)

    # Regenerating from the now-advanced email window would erase remaining report contents.
    def test_owner_change_rebuilds_pending_group_after_smtp_sent_using_original_interval(self):
        self.add('转交业务',long=True); self.add('保留业务','partner@oneasy.cc',long=True)
        self.schedule(); self.deliver(0); self.deliver(4)
        previous=self.group[0]['content'].split('\n',1)[0]
        self.db.save_person('other','同事','other@example.com',userid='Other')
        with patch('app.store.now',return_value=at('2026-10-09T18:02')):
            self.db.change_owners('service@oneasy.cc',['other'],'admin')
        self.assertEqual(self.notice('report')['state'],'sent')
        self.assertEqual(self.notice()['part'],0)
        report=self.db.report('lihan','2026-10-09')
        self.assertEqual([i['subject'] for i in report['items']],['保留业务'])
        self.assertEqual(report['since'],'2026-10-09T10:00:00+08:00')
        self.group.clear(); self.finish(200)
        rebuilt=''.join(part['content'].split('\n',1)[1] for part in self.group)
        self.assertNotIn('转交业务',rebuilt); self.assertIn('保留业务',rebuilt)
        self.assertNotEqual(self.group[0]['content'].split('\n',1)[0],previous)

    def test_removed_owner_group_is_cancelled(self):
        self.db.save_person('other','同事','other@example.com',userid='Other')
        self.db.change_owners('service@oneasy.cc',['lihan','other'],'admin')
        self.add(); self.schedule()
        self.db.change_owners('service@oneasy.cc',['lihan'],'admin')
        self.assertEqual(self.notice(owner='other')['state'],'cancelled')
        self.finish()
        self.assertTrue(all(not part.get('mentioned_list') for part in self.group))

    def test_daily_report_with_only_system_records_has_no_mentions(self):
        self.schedule(); self.finish()
        self.assertTrue(self.group)
        self.assertTrue(all('mentioned_list' not in part for part in self.group))
        text=''.join(part['content'] for part in self.group)
        for value in ('本期新增 0 封','2026-10-09','统计时段'):
            self.assertIn(value,text)
        for value in ('系统操作摘要','请在原邮箱处理业务','完整操作记录','以下邮件未即时提醒'):
            self.assertNotIn(value,text)
        self.assertEqual(len(self.emails),1)
        for value in ('操作记录（','请人工检查是否遗漏','本系统不记录工作完成状态'):
            self.assertNotIn(value,self.emails[0][1])
        self.assertIn('新增邮件 0 封',self.emails[0][1])

    def test_inflight_readable_report_keeps_pre_upgrade_boundaries(self):
        self.add(long=True); self.schedule(); self.deliver(0)
        report=self.db.report('lihan','2026-10-09')
        old_parts=group_report_parts(report,'李涵',legacy=True)
        with self.db.tx() as db:
            db.execute("UPDATE notices SET part=1,payload=? WHERE kind='report_group'",
                       (json.dumps({'report_id':'lihan:2026-10-09','format':'readable'}),))
        self.finish(4)
        self.assertEqual([part['content'] for part in self.group],old_parts[1:])

    def test_disabled_group_or_removed_member_cancels_group_and_preserves_original_email(self):
        for case in ('disabled','missing_member'):
            with self.subTest(case=case):
                self.add(); self.schedule()
                if case=='disabled': self.worker.wecom_sender=None
                else: self.db.save_person('lihan','李涵','li@example.com',userid='')
                self.finish()
                self.assertEqual(self.group,[])
                self.assertEqual(self.notice()['state'],'cancelled')
                self.assertEqual(self.notice('report')['state'],'sent')
                self.assertEqual(len(self.emails),1)
                # Both cases use a fresh next-day report with a restored member.
                if case=='disabled':
                    self.worker.wecom_sender=self.group_sender
                    with self.db.tx() as db:
                        db.execute('DELETE FROM notices'); db.execute('DELETE FROM reports')
                    self.emails.clear()

    def test_missing_member_is_not_scheduled_and_existing_report_is_not_backfilled(self):
        self.add(); self.db.schedule(at('2026-10-09T18:00'))
        self.schedule()
        self.assertEqual(self.db.rows("SELECT * FROM notices WHERE kind='report_group'"),[])
        self.db.save_person('lihan','李涵','li@example.com',userid='')
        self.schedule('2026-10-10T18:00')
        self.assertEqual(self.db.rows("SELECT * FROM notices WHERE kind='report_group'"),[])

    def test_small_numbered_chunks_preserve_content_when_count_grows_to_two_digits(self):
        original='中文🙂abcdefgh'*200
        parts=message_parts(original,'stable:revision:2',limit=100)
        self.assertGreater(len(parts),10)
        self.assertTrue(all(len(part.encode('utf-8'))<=100 for part in parts))
        self.assertEqual(''.join(part.split('\n',1)[1] for part in parts),original)


if __name__=='__main__': unittest.main()
