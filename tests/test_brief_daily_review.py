import json
import unittest
from unittest.mock import patch

from app.core import MAILBOXES
from app.notifications import group_notice_text
from app.worker import Worker
from tests.test_core import at


class BriefDailyReviewTest(unittest.TestCase):
    def setUp(self):
        from tests.test_group_reports import GroupReportsTest
        self.fixture=GroupReportsTest();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db

    def add_handling_checks(self):
        result=[]
        for number,account in enumerate(MAILBOXES,1):
            result.append(self.fixture.add(f'客户事项{number}',account,category='action'))
        with self.db.tx() as db:db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        return result

    def test_six_mailbox_checks_and_daily_review_are_one_group_checklist(self):
        keys=self.add_handling_checks();digest=self.fixture.add('普通系统通知')
        self.fixture.schedule();self.fixture.finish()
        self.assertEqual(len(self.fixture.group),1)
        text=self.fixture.group[0]['content']
        self.assertIn('18:00',text)
        for account in MAILBOXES:self.assertIn(account,text)
        for title in ['客户事项1','客户事项2','客户事项3','客户事项4','客户事项5','客户事项6','普通系统通知']:self.assertIn(title,text)
        self.assertEqual(sum(line.startswith(tuple(str(i)+'. ' for i in range(1,8))) for line in text.splitlines()),7)
        for detail in ['发件人：','收信时间：','建议处理','回复参考','分类依据','事项：']:self.assertNotIn(detail,text)
        self.assertEqual(self.fixture.group[0]['mentioned_list'],['LiHan'])
        self.assertEqual(len(self.fixture.emails),1)
        for key in keys:
            row=self.db.rows("SELECT * FROM notices WHERE item_id=? AND kind='review'",(key,))[0]
            self.assertEqual(row['state'],'sent');self.assertEqual(row['channel'],'wecom')
        self.assertFalse(self.db.rows('SELECT 1 FROM notices WHERE item_id=?',(digest,)))

    def test_long_content_is_brief_in_group_and_complete_in_email(self):
        self.fixture.add('定期系统通知',long=True);self.fixture.schedule();self.fixture.finish()
        self.assertEqual(len(self.fixture.group),1)
        text=self.fixture.group[0]['content']
        self.assertLess(len(text),450)
        self.assertNotIn('中文🙂说明'*200,text)
        self.assertIn('中文🙂说明'*200,self.fixture.emails[0][1])
        self.assertNotIn('mentioned_list',self.fixture.group[0])

    def test_checked_ads_tests_and_information_only_mail_are_not_review_tasks(self):
        checked=self.fixture.add('已复核的通知')
        self.fixture.add('忽略推广',category='marketing')
        test=self.fixture.add('已确认测试',category='info')
        self.fixture.add('仅供知悉',category='info')
        with self.db.tx() as db:
            db.execute('UPDATE items SET reviewed=1 WHERE id=?',(checked,))
            db.execute('UPDATE items SET is_test=1 WHERE id=?',(test,))
            db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        self.fixture.schedule();self.fixture.finish()
        self.assertEqual(len(self.fixture.group),1)
        text=self.fixture.group[0]['content']
        self.assertIn('今日无待复核邮件',text)
        for subject in ['已复核的通知','忽略推广','已确认测试','仅供知悉']:self.assertNotIn(subject,text)
        self.assertNotIn('mentioned_list',self.fixture.group[0])

    def test_group_failure_preserves_handling_checks_until_report_delivery(self):
        self.add_handling_checks();self.fixture.schedule();self.fixture.group_failure=True
        self.fixture.deliver(0)
        reviews=self.db.rows("SELECT * FROM notices WHERE kind='review'")
        self.assertTrue(reviews);self.assertTrue(all(row['state']=='pending' for row in reviews))
        self.assertEqual(self.fixture.group,[])
        self.fixture.worker=Worker(self.db,self.fixture.config);self.fixture.worker.wecom_sender=self.fixture.group_sender
        self.fixture.group_failure=False;self.fixture.finish(200)
        self.assertEqual(len(self.fixture.group),1)
        self.assertTrue(all(row['state']=='sent' for row in self.db.rows("SELECT * FROM notices WHERE kind='review'")))

    def test_disabled_group_keeps_original_email_review_fallback(self):
        key=self.fixture.add('需要处理',category='action')
        with self.db.tx() as db:db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        self.fixture.schedule();self.fixture.worker.wecom_sender=None;self.fixture.finish()
        self.assertEqual(self.fixture.group,[])
        self.assertTrue(any('处理复核' in email[2] and '需要处理' in email[1] for email in self.fixture.emails))
        self.assertEqual(self.db.rows("SELECT channel FROM notices WHERE item_id=? AND kind='review'",(key,))[0]['channel'],'smtp')

    def test_persistent_group_failure_completes_email_review_fallback_after_restart(self):
        self.add_handling_checks();self.fixture.schedule();self.fixture.group_failure=True
        self.fixture.deliver(0)
        self.fixture.worker=Worker(self.db,self.fixture.config);self.fixture.worker.wecom_sender=self.fixture.group_sender
        for seconds in range(4,45,4): self.fixture.deliver(seconds)
        reviews=self.db.rows("SELECT * FROM notices WHERE kind='review'")
        self.assertTrue(all(row['state']=='sent' and row['channel']=='smtp' for row in reviews))
        self.assertEqual(self.fixture.notice()['state'],'pending')
        self.assertEqual(self.fixture.notice('report')['state'],'sent')
        self.assertEqual(self.fixture.group,[])

    def test_partial_checklist_does_not_finish_reviews_until_all_parts_arrive(self):
        for index in range(18):self.fixture.add(f'处理事项{index:03d}',long=True,category='action')
        with self.db.tx() as db:db.execute("UPDATE notices SET state='sent' WHERE kind='initial'")
        self.fixture.schedule();self.fixture.deliver(0)
        self.assertEqual(len(self.fixture.group),1)
        self.assertTrue(all(row['state']=='pending' for row in self.db.rows("SELECT * FROM notices WHERE kind='review'")))
        self.fixture.finish(4)
        self.assertTrue(all(row['state']=='sent' for row in self.db.rows("SELECT * FROM notices WHERE kind='review'")))

    def test_owner_rename_does_not_change_accepted_fragment_boundaries(self):
        self.fixture.add_large_checklist();self.fixture.schedule();self.fixture.deliver(0);self.fixture.deliver(4)
        report=self.db.report('lihan','2026-10-09')
        from app.notifications import group_review_report_parts
        original=group_review_report_parts(report,'李涵')
        self.db.save_person('lihan','新负责人'*16,'li@example.com',userid='NewOwner')
        self.fixture.finish(8)
        self.assertEqual([message['content'] for message in self.fixture.group],original)

    def test_large_checklist_preserves_every_row_in_minimal_numbered_parts(self):
        for number in range(30):self.fixture.add(f'复核事项{number:03d}',long=True)
        self.fixture.schedule();self.fixture.finish()
        self.assertGreater(len(self.fixture.group),1)
        self.assertLess(len(self.fixture.group),10)
        text=''.join(message['content'] for message in self.fixture.group)
        for number in range(30):self.assertEqual(text.count(f'复核事项{number:03d}'),1)
        self.assertTrue(all(len(message['content'].encode('utf-8'))<=2048 for message in self.fixture.group))

    def test_initial_and_customer_deadline_notices_keep_their_details(self):
        key=self.fixture.add('正常即时业务',category='action')
        item=self.db.item(key)
        for kind in ['initial','deadline']:
            text=group_notice_text(item,kind)
            self.assertIn('发件人：',text);self.assertIn('收信时间：',text);self.assertIn('事项：',text)


if __name__=='__main__':unittest.main()
