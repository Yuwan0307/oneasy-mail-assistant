import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import Mail,Decision,classify
from app.store import Store
from tests.test_core import at


class WebAdFilterTest(unittest.TestCase):
    def mail(self,body=None,attachments=None):
        return Mail('service@oneasy.cc','ad',at('2026-11-03T09:38'),
                    '官网推广与AI收录服务咨询','sales@example.com',
                    body or '我是百度推广顾问，看您公司备案了网站，有计划通过百度推广您们公司的官网吗？我们现在也做豆包、DeepSeek、千问等各种AI收录跟推荐，请问贵公司有需求吗？',
                    attachments or [])

    def test_explicit_ad_with_business_card_and_ad_example_images_is_filtered_locally(self):
        mail=self.mail(attachments=[('宣传名片.png',b'\x89PNG\r\n\x1a\nimage'),('广告位展示示例.jpg',b'\xff\xd8\xffimage')])
        result=classify(mail,lambda mail:self.fail('Explicit ad must be filtered locally'))
        self.assertEqual(result.category,'marketing')
        self.assertFalse(result.sensitive)
        self.assertIn('无效广告',result.reason)

    def test_website_ads_and_ai_listing_sales_are_filtered_without_a_model(self):
        for body in ('我们提供官网广告投放服务，欢迎联系咨询。',
                     '我们专注网站推广服务，请问您有需求吗？',
                     '我们提供AI收录和推荐服务，欢迎联系。',
                     '我们提供GEO优化服务，可提高大模型推荐曝光，有需求欢迎咨询。'):
            with self.subTest(body=body):
                self.assertEqual(classify(self.mail(body),None).category,'marketing')

    def test_customer_inquiry_about_advertising_is_not_treated_as_vendor_sales(self):
        for body in ('我们希望推广公司官网，请贵公司提供报价。',
                     '请问你们能否提供AI收录服务？我们需要了解详情。',
                     '我们主营网站广告推广业务，计划采购Oneasy的云服务器，能否提供报价及试用？',
                     '我们提供网站广告投放服务。希望了解贵司的机房托管套餐和价格。',
                     '我们主营网站广告推广业务，需要一台Oneasy云服务器，请问价格多少？',
                     '我们提供网站广告投放服务。贵司的机房托管套餐怎么收费？',
                     '我们提供网站广告投放服务，欢迎联系。另想问贵司Oneasy系统多少钱？'):
            with self.subTest(body=body):
                result=classify(self.mail(body),lambda mail:Decision('action','客户咨询','需要响应'))
                self.assertEqual(result.category,'action')

    def test_existing_business_and_customer_requests_override_ad_copy(self):
        for extra in ('另请协助开通Oneasy账号。','我们已有合同，需要开具发票。','已购买的服务有故障，请处理。','这是法律投诉，请回复。'):
            with self.subTest(extra=extra):
                mail=self.mail(); mail.body+='\n'+extra
                self.assertEqual(classify(mail,lambda mail:Decision('action','业务事项','客户请求')).category,'action')

    def test_risk_credentials_and_sensitive_attachment_names_are_not_hidden(self):
        for extra,name,want in (('异常登录，请核对。','名片.png','action'),
                                ('API Key: company-value','名片.png','human'),
                                ('','内网配置.png','human'),('','服务合同.png','human')):
            with self.subTest(extra=extra,name=name):
                mail=self.mail(); mail.body+='\n'+extra
                if extra: mail.attachments=[]
                else: mail.attachments=[(name,b'\x89PNG\r\n\x1a\nimage')]
                result=classify(mail,lambda mail:self.fail('Sensitive content must not reach AI'))
                self.assertEqual(result.category,want)

    def test_unknown_oversized_or_credential_attachments_keep_manual_protection(self):
        for attachment in (('unverified.bin',b''),('方案.txt',b'token=company-value'),
                           ('名片.png',b'not-an-image'),('名片.png',b'\x89PNG\r\n\x1a\n'+b'a'*(5*1024*1024))):
            with self.subTest(name=attachment[0],size=len(attachment[1])):
                result=classify(self.mail(attachments=[attachment]),lambda mail:self.fail('Unchecked attachment must not reach AI'))
                self.assertEqual(result.category,'human')

    def test_filtered_ad_is_retained_without_instant_or_review_tasks(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory, patch('app.store.now',return_value=at('2026-11-03T10:00')):
            db=Store(Path(directory)/'mail.db'); db.activate(at('2026-11-03T08:00'))
            mail=self.mail(); result=classify(mail,None); key=db.ingest(mail,result)
            db.schedule(at('2026-11-03T18:00'))
            self.assertEqual(db.item(key)['category'],'marketing')
            self.assertEqual(db.rows('SELECT * FROM notices WHERE item_id=?',(key,)),[])
            self.assertEqual(db.report('lihan','2026-11-03')['review'],[])
            self.assertEqual(len(db.report('lihan','2026-11-03')['items']),1)
