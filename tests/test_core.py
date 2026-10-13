import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.core import Mail, Decision, classify, first_review, sensitive
from app.store import Store


def at(value):
    return datetime.fromisoformat(value + '+08:00')


class RulesTest(unittest.TestCase):
    def test_review_boundary_and_deadline_merge(self):
        self.assertEqual(first_review(at('2026-10-09T17:59')).isoformat(), '2026-10-09T18:00:00+08:00')
        self.assertEqual(first_review(at('2026-10-09T18:00')).isoformat(), '2026-10-10T18:00:00+08:00')

    def test_sensitive_values_and_unknown_attachment_never_reach_ai(self):
        calls = []
        ai = lambda mail: calls.append(mail) or Decision('action', '客户事项', '客户请求')
        for body, attachments in [('IP 192.168.1.3', []), ('token=abc123', []), ('您好', [('scan.png', b'png')]), ('子网掩码设置', [])]:
            result = classify(Mail('service@oneasy.cc', '1', at('2026-10-09T12:00'), '来信', 'a@b.cc', body, attachments), ai)
            self.assertEqual(result.category, 'human')
        self.assertEqual(calls, [])
        self.assertIn('IP 地址', sensitive('地址 2001:db8::1'))

    def test_ai_failure_and_information_mail(self):
        mail = Mail('legal@oneasy.cc', '1', at('2026-10-09T12:00'), '谢谢', 'new@a.cc', '收到，谢谢')
        self.assertEqual(classify(mail, lambda m: Decision('info', '收到确认', '业务确认')).category, 'info')
        def broken(m):
            raise RuntimeError('private secret')
        result = classify(mail, broken)
        self.assertEqual(result.category, 'human')
        self.assertNotIn('private', result.reason)

    def test_html_encoded_sensitive_value_is_blocked_before_ai(self):
        calls=[]
        mail=Mail('service@oneasy.cc','html',at('2026-10-09T12:00'),'来信','a@b.cc','IP：&#49;&#57;&#50;&#46;&#49;&#54;&#56;&#46;&#49;&#46;&#50;')
        result=classify(mail,lambda m:calls.append(m) or Decision('info','普通来信','确认'))
        self.assertEqual(result.category,'human')
        self.assertEqual(calls,[])


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name) / 'test.db')
        self.db.activate(at('2026-10-09T10:00'))

    def mail(self, mid='1', received='2026-10-09T12:00'):
        return Mail('service@oneasy.cc', mid, at(received), '客户申请开通', 'customer@example.cc', '请开通')

    def test_new_only_dedup_and_restart(self):
        self.assertIsNone(self.db.ingest(self.mail('old','2026-10-09T09:00'), Decision('action','开通','业务请求')))
        item = self.db.ingest(self.mail(), Decision('action','开通','业务请求'))
        self.assertEqual(self.db.ingest(self.mail(), Decision('action','开通','业务请求')), item)
        self.assertEqual(len(self.db.rows('select * from notices')), 1)
        reopened = Store(self.db.path)
        self.assertEqual(len(reopened.rows('select * from items')), 1)


    def test_daily_report_includes_all_nonurgent_and_unreviewed(self):
        with patch('app.store.now',return_value=at('2026-10-09T12:00')):
            for i in range(75):
                self.db.ingest(self.mail(str(i)), Decision('digest','广告内容','无业务关系'))
        self.db.schedule(at('2026-10-09T18:00'))
        report = self.db.report('lihan', '2026-10-09')
        self.assertEqual(len(report['review']), 75)
        self.db.schedule(at('2026-10-10T18:00'))
        self.assertEqual(len(self.db.report('lihan','2026-10-10')['review']), 75)

    def test_binding_change_cancels_old_delivery_and_transfers_open_work(self):
        item = self.db.ingest(self.mail(), Decision('action','开通','业务请求'))
        self.db.save_person('new', '新负责人', 'new@example.com', verified=True)
        self.db.change_owner('service@oneasy.cc', 'new', 'admin')
        self.assertEqual(self.db.item(item)['owner'], 'new')
        pending = self.db.rows("select owner from notices where state='pending' and item_id=?", (item,))
        self.assertEqual([n['owner'] for n in pending], ['new'])

    def test_sensitive_content_not_persisted_in_item_or_audit(self):
        mail = self.mail()
        mail.subject = '密码 token=abc123'
        mail.body = '192.168.1.2'
        item = self.db.ingest(mail, classify(mail, None))
        combined = str(self.db.item(item)) + str(self.db.rows('select * from audit'))
        self.assertNotIn('abc123', combined)
        self.assertNotIn('192.168', combined)

    def test_information_notice_follows_new_owner_before_delivery(self):
        item=self.db.ingest(self.mail('info'),Decision('info','确认收到','业务确认'))
        self.db.save_person('new','新负责人','new@example.com',True)
        self.db.change_owner('service@oneasy.cc','new','admin')
        pending=self.db.rows("select owner from notices where item_id=? and state='pending'",(item,))
        self.assertEqual([n['owner'] for n in pending],['new'])

    def test_latest_undelivered_report_covers_previous_operations(self):
        for day in ['09','10','11']:
            self.db.schedule(at(f'2026-10-{day}T18:00'))
        pending=self.db.rows("select * from notices where kind='report' and state='pending'")
        self.assertEqual(len(pending),1)
        self.assertEqual(self.db.report('lihan','2026-10-11')['since'],'2026-10-09T10:00:00+08:00')

    def test_recovery_does_not_queue_expired_deadline(self):
        self.db.ingest(self.mail('expired'),Decision('action','处理','业务请求',at('2026-10-09T17:00')))
        self.db.schedule(at('2026-10-11T18:00'))
        self.assertEqual(self.db.rows("select * from notices where kind='deadline' and state='pending'"),[])


if __name__ == '__main__':
    unittest.main()
