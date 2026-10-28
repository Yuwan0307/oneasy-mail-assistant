import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.core import Mail,Decision
from app.notifications import group_report_text,report_text,group_report_parts
from app.store import Store
from app.web import create_app
from tests.test_core import at


class ReviewedRecordsTest(unittest.TestCase):
    def setUp(self):
        clock=patch('app.store.now',return_value=at('2026-10-09T12:00')); clock.start(); self.addCleanup(clock.stop)
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'review.db'); self.db.activate(at('2026-10-09T10:00'))
        self.client=TestClient(create_app(self.db,{'demo':True}),base_url='http://127.0.0.1',client=('127.0.0.1',1))

    def add(self,subject,reviewed=False,category='digest'):
        item=self.db.ingest(Mail('support@oneasy.cc',subject,at('2026-10-09T12:00'),subject,'vendor@example.com','产品介绍'),
                            Decision(category,'摘要'+subject,'营销推广' if category=='digest' else '客户事项'))
        if reviewed:
            with self.db.tx() as db: db.execute('UPDATE items SET reviewed=1 WHERE id=?',(item,))
        return item

    def report(self):
        self.db.schedule(at('2026-10-09T18:00'))
        return self.db.report('lihan','2026-10-09')

    def test_reviewed_mail_leaves_pending_view_but_stays_in_history(self):
        checked=self.add('已检查推广',True); pending=self.add('未检查推广')
        review=self.client.get('/api/items?view=review').json()
        self.assertEqual([row['id'] for row in review['items']],[pending])
        all_items=self.client.get('/api/items?view=all').json()
        self.assertIn(checked,[row['id'] for row in all_items['items']])
        self.assertEqual(self.client.get('/api/state').json()['pending_review'],1)

    def test_report_separates_reviewed_records_without_dropping_any_mail(self):
        checked=self.add('已检查推广',True); pending=self.add('未检查推广'); self.add('客户事项',category='info')
        report=self.report()
        self.assertEqual([row['id'] for row in report['review']],[pending])
        self.assertIn(checked,[row['id'] for row in report['items']])
        text=group_report_text(report,'李涵')
        self.assertIn('已复核记录（1封）',text)
        pending_section=text.split('\n\n待人工复核（',1)[1].split('\n\n已复核记录',1)[0]
        self.assertIn('未检查推广',pending_section); self.assertNotIn('已检查推广',pending_section)
        self.assertIn('分类：已复核',report_text(report))

    def test_fully_reviewed_daily_report_has_no_repeat_review_prompt(self):
        self.add('已检查推广',True); report=self.report()
        self.assertEqual(report['review'],[])
        for text in (group_report_text(report,'李涵'),report_text(report)):
            self.assertIn('已检查推广',text)
            self.assertIn('已复核',text)
            self.assertNotIn('请在原邮箱处理业务',text)
            self.assertNotIn('请人工检查是否遗漏',text)
            self.assertNotIn('未即时提醒的邮件请人工复核',text)

    def test_inflight_v166_business_report_matches_saved_parts(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/v166-mail-only-business.json').read_text(encoding='utf-8'))
        self.assertEqual(group_report_parts(fixture['report'],fixture['owner'],review_status=False),fixture['parts'])
