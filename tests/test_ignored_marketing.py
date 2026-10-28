import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient

from app.ai import CloudAI
from app.core import Mail,Decision,classify
from app.notifications import group_report_text,report_text
from app.store import Store
from app.web import create_app
from tests.test_core import at


class IgnoredMarketingTest(unittest.TestCase):
    def setUp(self):
        clock=patch('app.store.now',return_value=at('2026-10-28T12:00')); clock.start(); self.addCleanup(clock.stop)
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'mail.db'); self.db.activate(at('2026-10-28T10:00'))
        self.mail=Mail('support@oneasy.cc','offer',at('2026-10-28T12:00'),'第三方产品推广','sales@example.com','We offer a paid product diagnosis for CNY 99.')

    def decision(self,confidence=.99,evidence=None):
        return Decision('marketing','第三方产品推广','无具体客户事项',evidence=self.mail.body if evidence is None else evidence,confidence=confidence)

    def test_cloud_classifier_accepts_marketing_from_model(self):
        def reply(request):
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"category":"marketing","summary":"产品推广","reason":"无客户事项","confidence":0.99,"evidence":"We offer a paid product diagnosis for CNY 99."}'}}]})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            result=classify(self.mail,CloudAI({'api_key':'unit-test-only'},client))
        self.assertEqual(result.category,'marketing')

    def test_uncertain_or_unverifiable_marketing_cannot_be_ignored(self):
        for confidence,evidence in ((.94,self.mail.body),(.99,''),(.99,'不在原文的依据')):
            with self.subTest(confidence=confidence,evidence=evidence):
                self.assertEqual(classify(self.mail,lambda mail:self.decision(confidence,evidence)).category,'human')
        accepted=classify(self.mail,lambda mail:self.decision())
        self.assertEqual(accepted.category,'marketing')

    def test_marketing_cannot_hide_risk_or_sensitive_mail(self):
        for text,want in (('异常登录，请检查','action'),('API Key: company-value','human'),('设备地址 192.168.1.2','human')):
            with self.subTest(text=text):
                self.mail.body=text
                result=classify(self.mail,lambda mail:self.fail('Risk or sensitive mail must be inspected locally'))
                self.assertEqual(result.category,want)

    def test_ignored_marketing_stays_out_of_business_and_review_but_keeps_history(self):
        key=self.db.ingest(self.mail,self.decision())
        with TestClient(create_app(self.db,{'demo':True}),base_url='http://127.0.0.1',client=('127.0.0.1',1)) as client:
            self.assertEqual(client.get('/api/items?view=business').json()['total'],0)
            self.assertEqual(client.get('/api/items?view=review').json()['total'],0)
            self.assertEqual(client.get('/api/items?view=all').json()['items'][0]['id'],key)
            self.assertEqual(client.get('/api/state').json()['pending_review'],0)
        self.assertEqual(self.db.rows('SELECT * FROM notices WHERE item_id=?',(key,)),[])

    def test_marketing_only_report_retains_records_without_processing_prompts(self):
        key=self.db.ingest(self.mail,self.decision())
        self.db.schedule(at('2026-10-28T18:00'))
        report=self.db.report('lihan','2026-10-28')
        self.assertEqual(report['review'],[])
        self.assertEqual([row['id'] for row in report['items']],[key])
        for text in (report_text(report),group_report_text(report,'李涵')):
            self.assertIn('已忽略推广',text)
            self.assertIn('第三方产品推广',text)
            self.assertNotIn('请在原邮箱处理业务',text)
            self.assertNotIn('请人工检查是否遗漏',text)
            self.assertNotIn('未即时提醒的邮件请人工复核',text)
        self.assertIn('业务来信 0 封',group_report_text(report,'李涵'))

    def test_ignored_mail_can_still_be_corrected_to_business(self):
        key=self.db.ingest(self.mail,self.decision())
        self.assertEqual(self.db.rows('SELECT * FROM notices WHERE item_id=?',(key,)),[])
        self.assertTrue(self.db.review(key,1,'lihan','needs_action'))
        self.assertEqual(self.db.item(key)['category'],'action')
        self.assertEqual(len(self.db.rows("SELECT * FROM notices WHERE item_id=? AND kind='initial'",(key,))),1)
