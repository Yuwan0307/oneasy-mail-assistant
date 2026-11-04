import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from app.ai import CloudAI
from app.core import Mail,classify
from app.store import Store
from app.notifications import group_notice_text,group_item_text,item_text
from tests.test_core import at


class NoticeContextTest(unittest.TestCase):
    def mail(self,body):
        return Mail('operation@oneasy.cc','form',at('2026-11-04T13:48'),'官网咨询','form@example.com',body)

    def model(self,mail,source,evidence,internal=''):
        data=dict(category='action',summary='客户申请试用',reason='业务事项',confidence=.99,evidence='申请试用',
                  deadline_source=source,deadline_evidence=evidence,internal_followup_evidence=internal,hints='准备试用开通')
        with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(data)}}]}))) as client:
            return classify(mail,CloudAI({'api_key':'test-only'},client))

    def test_explicit_simulation_is_labeled_without_real_work_or_followup(self):
        mail=self.mail('申请试用。\n本邮件为测试模拟客户来信，仅用于验证邮件助手的分类和企业微信群提醒，无需实际开通或回复。\n后续行动建议：请于1个工作日内联系客户。')
        result=classify(mail,None)
        self.assertTrue(result.is_test); self.assertEqual(result.category,'info')
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory,patch('app.store.now',return_value=at('2026-11-04T14:00')):
            db=Store(Path(directory)/'test.db'); db.activate(at('2026-11-04T10:00'))
            key=db.ingest(mail,result); db.schedule(at('2026-11-04T18:00'))
            item=db.item(key); text=group_notice_text(item,'initial')
            self.assertIn('测试邮件',text); self.assertIn('无需实际开通或回复',text)
            self.assertNotIn('按需处理',text); self.assertNotIn('按真实需求',text)
            self.assertIn('测试邮件',item_text(item))
            self.assertEqual([r['kind'] for r in db.rows('SELECT kind FROM notices WHERE item_id=?',(key,))],['initial'])

    def test_test_company_name_does_not_suppress_real_customer(self):
        result=self.model(self.mail('test公司申请试用，请于今天17:00回复。'),'customer','请于今天17:00回复')
        self.assertFalse(result.is_test); self.assertEqual(result.category,'action'); self.assertIsNotNone(result.deadline)

    def test_internal_form_sla_cannot_become_customer_deadline_even_if_model_claims_customer(self):
        result=self.model(self.mail('申请试用。\n后续行动建议：请于今天17:00联系客户。'),'customer','请于今天17:00联系客户')
        self.assertIsNone(result.deadline)
        self.assertIn('今天17:00',result.internal_followup)
        self.assertNotIn('截止要求需人工确认',result.reason)

    def test_customer_deadline_and_internal_suggestion_are_kept_separate(self):
        mail=self.mail('申请试用，请于今天17:00回复。\n内部跟进建议：请于1个工作日内联系客户。')
        result=self.model(mail,'customer','请于今天17:00回复','请于1个工作日内联系客户')
        self.assertEqual(result.deadline.isoformat(),'2026-11-04T17:00:00+08:00')
        self.assertEqual(result.internal_followup,'请于1个工作日内联系客户')
        item=dict(subject=mail.subject,account=mail.account,sender=mail.sender,received=mail.received.isoformat(),summary=result.summary,reason=result.reason,
                  deadline=result.deadline.isoformat(),internal_followup=result.internal_followup,deadline_source=result.deadline_source)
        text=group_item_text(item)
        self.assertIn('客户要求的截止时间',text); self.assertIn('内部跟进建议',text)

    def test_unattributed_time_uses_neutral_wording_and_creates_no_deadline(self):
        result=self.model(self.mail('申请试用，请于今天17:00回复。'),'unknown','请于今天17:00回复')
        self.assertIsNone(result.deadline)
        item=dict(subject='咨询',account='operation@oneasy.cc',sender='client@example.com',received='2026-11-04T13:48:00+08:00',summary=result.summary,reason=result.reason)
        text=group_item_text(item)
        self.assertNotIn('客户提出了时间要求',text); self.assertIn('时间来源',text)

    def test_test_marker_does_not_bypass_sensitive_or_risk_protection(self):
        marker='本邮件为测试模拟客户来信，仅用于验证邮件助手。'
        self.assertEqual(classify(self.mail(marker+'API Key: private-value'),None).category,'human')
        self.assertEqual(classify(self.mail(marker+'异常登录'),None).category,'action')

    def test_negative_and_quoted_test_statements_do_not_hide_real_customer(self):
        for text in ('本邮件是正式业务请求，不是测试模拟，请开通试用。',
                     '申请试用。\n> 本邮件为测试模拟客户来信，仅用于验证邮件助手。'):
            result=self.model(self.mail(text),'none','')
            self.assertFalse(result.is_test); self.assertEqual(result.category,'action')

    def test_customer_section_after_internal_section_keeps_customer_deadline(self):
        result=self.model(self.mail('申请试用。\n内部跟进建议：先登记。\n客户要求：请于今天17:00回复。'),'customer','请于今天17:00回复')
        self.assertEqual(result.deadline.isoformat(),'2026-11-04T17:00:00+08:00')
        self.assertEqual(result.deadline_source,'customer')

    def test_simulated_html_form_followup_contains_no_markup(self):
        mail=self.mail('<p>本邮件为测试模拟客户来信，仅用于验证邮件助手，无需实际开通或回复。</p><h3>后续行动建议：</h3><li>请于1个工作日内联系客户;</li>')
        result=classify(mail,None)
        self.assertTrue(result.is_test)
        self.assertIn('1个工作日',result.internal_followup)
        self.assertNotIn('<',result.internal_followup)
