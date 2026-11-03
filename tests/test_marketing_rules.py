import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import Mail,Decision,classify
from app.store import Store
from tests.test_core import at


class MarketingRulesTest(unittest.TestCase):
    def mail(self,body,subject='How does AI describe your product?',attachments=None):
        return Mail('support@oneasy.cc','marketing',at('2026-10-28T11:42'),subject,
                    'market@example.com',body,attachments or [])

    def test_unsubscribe_token_is_removed_before_marketing_reaches_ai(self):
        calls=[]; offer='We offer a paid AI visibility diagnosis.'
        mail=self.mail(offer+'\nUnsubscribe: https://vendor.example/unsubscribe?token=opaque-value&email=team@example.com')
        result=classify(mail,lambda safe:calls.append(safe) or Decision('digest','产品推广','第三方服务营销',
                            evidence=offer,confidence=.99))
        self.assertEqual(result.category,'digest')
        self.assertEqual(len(calls),1)
        self.assertNotIn('opaque-value',calls[0].body)
        self.assertNotIn('token=',calls[0].body)
        self.assertIn(offer,calls[0].body)
        self.assertIn('opaque-value',mail.body)

    def test_html_and_encoded_unsubscribe_links_are_safe_to_analyze(self):
        calls=[]; offer='Product promotion'
        body=offer+'<a href="https://vendor.example/api/unsubscribe?email=team%40example.com&amp;token=private-value">Unsubscribe</a>'
        result=classify(self.mail(body),lambda safe:calls.append(safe) or Decision('digest','推广','产品广告',
                            evidence=offer,confidence=.99))
        self.assertEqual(result.category,'digest')
        self.assertNotIn('private-value',calls[0].body)

    def test_unsubscribe_token_in_headers_is_not_forwarded_or_stored_as_public(self):
        for field in ('subject','sender'):
            with self.subTest(field=field):
                calls=[]; mail=self.mail('Product promotion')
                setattr(mail,field,'https://vendor.example/unsubscribe?token=private-header-value')
                result=classify(mail,lambda safe:calls.append(safe) or Decision('digest','产品推广','广告',evidence='Product promotion',confidence=.99))
                self.assertEqual(result.category,'human')
                self.assertEqual(calls,[])

    def test_actual_credentials_and_account_links_still_never_reach_ai(self):
        calls=[]
        for text in (
            'Unsubscribe: https://vendor.example/unsubscribe?token=opaque\nAPI Key: company-value',
            'https://vendor.example/reset-password?token=actual-value',
            'https://vendor.example/login?token=actual-value',
            'https://vendor.example/preferences?token=actual-value',
            'https://vendor.example/unsubscribe/login?token=actual-value',
            'https://192.168.1.2/unsubscribe?token=actual-value',
            'https://user:actual-value@vendor.example/unsubscribe?token=opaque',
            'https://vendor.example/unsubscribe?token=opaque&api_key=actual-value',
            'https://vendor.example/unsubscribe?token=opaque\n网络配置 192.168.1.2',
            'https://vendor.example/unsubscribe?token=opaque\npassword=actual-value',
        ):
            with self.subTest(text=text):
                result=classify(self.mail(text),lambda safe:calls.append(safe))
                self.assertEqual(result.category,'human')
        self.assertEqual(calls,[])

    def test_customer_request_with_unsubscribe_footer_stays_business(self):
        calls=[]
        body='请协助开通试用账号。\nhttps://vendor.example/unsubscribe?token=opaque'
        result=classify(self.mail(body,'客户开通咨询'),lambda safe:calls.append(safe) or Decision('action','客户申请试用','业务请求'))
        self.assertEqual(result.category,'action')
        self.assertEqual(len(calls),1)
        self.assertNotIn('opaque',calls[0].body)

    def test_digest_uses_one_verifiable_paragraph_when_ai_quotes_several(self):
        first='We offer a paid AI visibility diagnosis for your product.'
        second='A complete diagnosis costs CNY 99.'
        mail=self.mail(first+'\nContact our sales team for details.\n'+second)
        result=classify(mail,lambda safe:Decision('digest','第三方产品推广','营销',evidence=first+'\n\n'+second,confidence=.98))
        self.assertEqual(result.category,'digest')
        self.assertIn(result.evidence,mail.body)
        fabricated=classify(mail,lambda safe:Decision('digest','推广','营销',evidence='这句中文不是邮件原文。',confidence=.99))
        self.assertEqual(fabricated.category,'human')

    def test_no_reply_sender_does_not_suppress_risk_or_customer_mail(self):
        mail=self.mail('异常登录，请检查账号安全。','安全告警')
        mail.sender='no-reply@mailsupport.aliyun.com'
        self.assertEqual(classify(mail,None).category,'action')
        mail.subject='客户申请'; mail.body='请协助开通试用。'
        self.assertEqual(classify(mail,lambda safe:Decision('action','客户申请','业务请求')).category,'action')

    def test_sensitive_attachment_is_not_bypassed_by_footer_cleanup(self):
        calls=[]
        mail=self.mail('https://vendor.example/unsubscribe?token=opaque',attachments=[('access.txt',b'token=company-value')])
        self.assertEqual(classify(mail,lambda safe:calls.append(safe)).category,'human')
        self.assertEqual(calls,[])

    def routine(self):
        body='定期修改密码提醒\n尊敬的用户:\n管理员已启用"定期修改密码提醒"功能。请及时登录网页版邮箱,进入设置-查看更多设置-账户与安全-账户安全修改账号support@oneasy.cc的密码。\n阿里邮箱团队'
        mail=self.mail(body,'【系统通知】定期修改密码提醒')
        mail.sender='no-reply@mailsupport.aliyun.com'
        return mail

    def test_known_periodic_password_notice_is_local_digest_with_clear_summary(self):
        calls=[]; mail=self.routine()
        result=classify(mail,lambda safe:calls.append(safe))
        self.assertEqual(result.category,'digest')
        self.assertFalse(result.sensitive)
        self.assertIn('定期修改密码',result.summary)
        self.assertEqual(calls,[])

    def test_periodic_template_does_not_hide_credentials_or_other_security_events(self):
        for change in ('credential','risk','html_risk','style_risk','attachment','sender','extra_request','different_account'):
            with self.subTest(change=change):
                mail=self.routine(); calls=[]
                if change=='credential': mail.body+='\n密码：actual-value'
                elif change=='risk': mail.body+='\n异常登录，请检查。'
                elif change=='html_risk': mail.body+='<img alt="异常登录，请检查。">'
                elif change=='style_risk': mail.body+='<style>异常登录，请检查。</style>'
                elif change=='attachment': mail.attachments=[('access.txt',b'token=actual-value')]
                elif change=='sender': mail.sender='customer@example.com'
                elif change=='extra_request': mail.body+='\n客户请求技术支持，请回复。'
                elif change=='different_account': mail.account='service@oneasy.cc'
                result=classify(mail,lambda safe:calls.append(safe))
                self.assertEqual(result.category,'human')
                self.assertEqual(calls,[])

    def test_periodic_plain_and_html_copies_with_typographic_quotes_match(self):
        mail=self.routine(); html=mail.body.replace('\n','<br>').replace('"','“')
        mail.body+='\n<html><head><title>阿里邮箱系统信模板</title><style>p{color:blue}</style></head><body>'+html+'</body></html>'
        self.assertEqual(classify(mail,None).category,'digest')

    def test_confirmed_marketing_is_archived_without_review_or_instant_notices(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory, patch('app.store.now',return_value=at('2026-10-28T12:00')):
            db=Store(Path(directory)/'test.db'); db.activate(at('2026-10-28T10:00'))
            mail=self.mail('Paid diagnosis offer.\nhttps://vendor.example/unsubscribe?token=opaque-value')
            decision=classify(mail,lambda safe:Decision('marketing','付费诊断推广','产品营销',evidence='Paid diagnosis offer.',confidence=.99))
            item=db.ingest(mail,decision)
            db.schedule(at('2026-10-28T18:00'))
            self.assertEqual(db.item(item)['category'],'marketing')
            self.assertEqual(db.rows("SELECT * FROM notices WHERE item_id=?",(item,)),[])
            self.assertEqual(db.report('lihan','2026-10-28')['review'],[])
            self.assertIn(item,[record['id'] for record in db.report('lihan','2026-10-28')['items']])
            self.assertNotIn('opaque-value',str(db.rows('SELECT * FROM items'))+str(db.rows('SELECT * FROM audit')))
