import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import Mail,MAILBOXES,classify
from app.store import Store
from tests.test_core import at


def reminder(account='operation@oneasy.cc'):
    body=('Reminder to Change Password Regularly\nDear User:\n'
          'The postmaster of your domain has turned on the "Regular Password Change Reminder" function. '
          'Please sign in to your WebMail, go to Settings-Account and Security-Account Security '
          'and change your password of '+account+'.\nAlibaba Mail Team')
    return Mail(account,'english-reminder',at('2026-11-10T17:53'),
                '[System Notification] Reminder to Change Password Regularly',
                'no-reply@mailsupport.aliyun.com',body)


class EnglishPasswordNoticeTest(unittest.TestCase):
    def test_known_english_template_is_local_daily_review_in_all_six_mailboxes(self):
        for account in MAILBOXES:
            with self.subTest(account=account):
                calls=[];mail=reminder(account)
                result=classify(mail,lambda safe:calls.append(safe))
                self.assertEqual(result.category,'digest')
                self.assertFalse(result.sensitive)
                self.assertIn('定期修改密码',result.summary)
                self.assertIn(result.evidence,mail.subject+'\n'+mail.body)
                self.assertEqual(calls,[])

    def test_duplicate_plain_and_html_copies_and_template_title_match(self):
        mail=reminder();mail.body+='\n<html><head><title>阿里邮箱系统信模板</title><style>p {color:blue}</style></head><body>'+mail.body.replace('\n','<br>').replace('"','“')+'</body></html>'
        self.assertEqual(classify(mail,None).category,'digest')

    def test_complete_subject_in_plain_or_html_header_matches_both_languages(self):
        from tests.test_marketing_rules import MarketingRulesTest
        for mail in [reminder(),MarketingRulesTest().routine()]:
            with self.subTest(subject=mail.subject):
                mail.body='<title>'+mail.subject+'</title>\n'+mail.subject+'\n'+mail.body
                self.assertEqual(classify(mail,None).category,'digest')

    def test_english_case_and_typographic_whitespace_are_accepted(self):
        mail=reminder();mail.subject=mail.subject.lower();mail.body=mail.body.upper().replace(' ','\u00a0')
        self.assertEqual(classify(mail,None).category,'digest')

    def test_extra_content_or_unsafe_fields_never_use_routine_exception(self):
        for change in ['credentials','ip','risk','html_risk','style_risk','attachment','wrong_sender','wrong_account','business','different_subject']:
            with self.subTest(change=change):
                calls=[];mail=reminder()
                if change=='credentials':mail.body+='\nPassword: actual-value'
                elif change=='ip':mail.body+='\nLogin IP: 192.168.1.2'
                elif change=='risk':mail.body+='\nUnusual login detected.'
                elif change=='html_risk':mail.body+='<img alt="Suspicious login detected">'
                elif change=='style_risk':mail.body+='<style>Suspicious login detected</style>'
                elif change=='attachment':mail.attachments=[('access.txt',b'token=actual-value')]
                elif change=='wrong_sender':mail.sender='no-reply@other.example'
                elif change=='wrong_account':mail.account='support@oneasy.cc'
                elif change=='business':mail.body+='\nPlease open our customer trial account.'
                elif change=='different_subject':mail.subject='Password changed successfully'
                result=classify(mail,lambda safe:calls.append(safe))
                self.assertIn(result.category,{'action','human'})
                self.assertEqual(calls,[])

    def test_english_login_security_notifications_are_immediate_without_ai(self):
        for body in ['Unusual login detected.','Suspicious login detected.','Unrecognized sign-in detected.','Unauthorized login detected.','Security alert: unusual sign-in.']:
            with self.subTest(body=body):
                mail=reminder();mail.subject='Account security notice';mail.body=body
                self.assertEqual(classify(mail,None).category,'action')

    def test_english_reminder_has_no_item_reminders_and_is_in_daily_review(self):
        scratch=Path.cwd()/'.test-temp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder,patch('app.store.now',return_value=at('2026-11-10T17:54')):
            store=Store(Path(folder)/'test.db');store.activate(at('2026-11-10T10:00'))
            mail=reminder();key=store.ingest(mail,classify(mail,None));store.schedule(at('2026-11-10T18:00'))
            self.assertEqual(store.rows('SELECT * FROM notices WHERE item_id=?',(key,)),[])
            self.assertIn(key,[row['id'] for row in store.report('lihan','2026-11-10')['review']])


if __name__=='__main__':unittest.main()
