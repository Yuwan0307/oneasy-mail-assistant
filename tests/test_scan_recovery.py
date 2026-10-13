import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core import Mail, Decision
from app.store import Store
from app.worker import Worker
from tests.smtp_support import SMTP_CONFIG
from tests.test_core import at
from tests.test_wecom_delivery import GroupSender


def http_failure(status,headers=None):
    request=httpx.Request('GET','https://example.invalid/private')
    response=httpx.Response(status,request=request,headers=headers)
    return httpx.HTTPStatusError('private secret',request=request,response=response)


class Inbox:
    def __init__(self,failures):
        self.failures=list(failures); self.calls=[]; self.repairs=[]
    def folders(self,account):
        self.calls.append(account)
        if account=='support@oneasy.cc' and self.failures: raise self.failures.pop(0)
        return []
    def recover(self,action): self.repairs.append(action)


class ScanRecoveryTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'test.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.db.save_person('lihan','李涵','li@example.com',userid='LiHan')
        self.config={'smtp':dict(SMTP_CONFIG)}
        self.api=Inbox([httpx.ConnectError('private secret')])
        self.worker=Worker(self.db,self.config,mail_api=self.api)
        self.group=GroupSender(); self.worker.wecom_sender=self.group
        self.key='scan_repair:support@oneasy.cc'

    def tick(self,when):
        with patch('app.worker.now',return_value=at(when)),patch('app.store.now',return_value=at(when)), \
             patch.object(self.db,'schedule'),patch.object(self.db,'backup'),patch.object(self.db,'archive'):
            self.worker.tick(); self.worker.sync()

    def test_connection_recovers_after_backoff_without_a_group_alert(self):
        self.tick('2026-10-09T12:00')
        state=self.db.get(self.key)
        self.assertEqual(state['action'],'reconnect')
        self.assertEqual(self.group.messages,[])
        self.tick('2026-10-09T12:00:30')
        self.assertEqual(self.api.calls.count('support@oneasy.cc'),1)
        self.assertGreater(self.api.calls.count('service@oneasy.cc'),1)
        self.tick('2026-10-09T12:01')
        self.assertEqual(self.api.repairs,['reconnect'])
        self.assertIsNone(self.db.get(self.key))
        self.assertEqual(self.db.get('last_scan:support@oneasy.cc'),'2026-10-09T12:01:00+08:00')
        self.assertTrue(self.db.rows("SELECT 1 FROM audit WHERE action='scan_recovered'"))
        self.assertNotIn('private secret',json.dumps(self.db.rows('SELECT * FROM audit')))
        self.assertEqual(self.group.messages,[])

    def test_persistent_failure_notifies_once_without_mentions_after_three_failures(self):
        self.api.failures=[httpx.ReadTimeout('private') for _ in range(5)]
        for when in ('2026-10-09T12:00','2026-10-09T12:01'):
            self.tick(when); self.assertEqual(self.group.messages,[])
        self.tick('2026-10-09T12:03')
        self.assertEqual(len(self.group.messages),1); self.assertEqual(self.group.messages[0][1],[])
        self.tick('2026-10-09T12:08')
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(self.db.get(self.key)['failures'],4)

    def test_rate_limit_waits_for_retry_after_without_rebuilding_connection(self):
        self.api.failures=[http_failure(429,{'Retry-After':'600'})]
        self.tick('2026-10-09T12:00'); self.tick('2026-10-09T12:05')
        self.assertEqual(self.api.calls.count('support@oneasy.cc'),1)
        self.tick('2026-10-09T12:10')
        self.assertIsNone(self.db.get(self.key)); self.assertEqual(self.api.repairs,[])

    def test_expired_auth_refreshes_but_permission_error_does_not_change_credentials(self):
        self.api.failures=[http_failure(401)]
        self.tick('2026-10-09T12:00'); self.tick('2026-10-09T12:01')
        self.assertEqual(self.api.repairs,['refresh_token'])
        self.api.failures=[http_failure(403)]
        self.tick('2026-10-09T12:02')
        self.assertEqual(self.db.get(self.key)['action'],'manual')
        self.assertEqual(len(self.group.messages),1); self.assertEqual(self.group.messages[0][1],[])
        self.tick('2026-10-09T12:03')
        self.assertEqual(self.api.repairs,['refresh_token'])

    def test_restart_and_pause_preserve_repair_window(self):
        self.tick('2026-10-09T12:00')
        self.worker=Worker(Store(self.db.path),self.config,mail_api=self.api)
        self.worker.wecom_sender=self.group
        self.worker.set_paused(True); self.tick('2026-10-09T12:01')
        self.assertEqual(self.api.repairs,[])
        self.worker.set_paused(False); self.tick('2026-10-09T12:02')
        self.assertEqual(self.api.repairs,['reconnect']); self.assertIsNone(self.db.get(self.key))

    def test_success_cancels_an_old_unsent_scan_alert(self):
        with patch('app.worker.now',return_value=at('2026-10-09T12:00')),patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.worker.alert('mailbox_scan_failed','support@oneasy.cc')
        self.api.failures=[]
        self.tick('2026-10-09T12:01')
        self.assertEqual(self.group.messages,[])
        self.assertEqual(self.db.rows("SELECT state FROM notices WHERE kind='system'")[0]['state'],'cancelled')

    def test_invalid_client_and_certificate_require_manual_check(self):
        failure=http_failure(401)
        failure.request=httpx.Request('POST','https://example.invalid/oauth2/v2.0/token')
        certificate=httpx.ConnectError('private')
        certificate.__cause__=ssl.SSLCertVerificationError('private certificate')
        for error in (failure,certificate):
            with self.subTest(error=type(error).__name__):
                self.db.set(self.key,None); self.api.failures=[error]
                self.tick('2026-10-09T12:00')
                self.assertEqual(self.db.get(self.key)['action'],'manual')
                self.assertEqual(self.api.repairs,[])

    def test_new_outage_can_notify_when_prior_unsent_warning_was_cancelled(self):
        with patch('app.worker.now',return_value=at('2026-10-09T12:00')),patch('app.store.now',return_value=at('2026-10-09T12:00')):
            self.worker.alert('mailbox_scan_failed','support@oneasy.cc')
        self.api.failures=[]; self.tick('2026-10-09T12:01')
        self.assertEqual(self.group.messages,[])
        self.api.failures=[httpx.ConnectError('private') for _ in range(3)]
        for when in ('2026-10-09T12:02','2026-10-09T12:03','2026-10-09T12:05'): self.tick(when)
        self.assertEqual(len(self.group.messages),1)
        self.assertEqual(self.group.messages[0][1],[])

    def test_retry_after_http_date_is_respected_for_server_unavailability(self):
        self.api.failures=[http_failure(503,{'Retry-After':'Fri, 09 Oct 2026 04:10:00 GMT'})]
        self.tick('2026-10-09T12:00'); self.tick('2026-10-09T12:05')
        self.assertEqual(self.api.calls.count('support@oneasy.cc'),1)
        self.tick('2026-10-09T12:10')
        self.assertIsNone(self.db.get(self.key))

    def test_recovered_scan_does_not_duplicate_already_detected_business_mail(self):
        class PartialInbox(Inbox):
            def folders(self,account): return [{'id':'2'}] if account=='support@oneasy.cc' else []
            def messages(self,account,folder):
                yield {'id':'m1','receivedDateTime':'2026-10-09T11:00:00+08:00'}
                if self.failures: raise self.failures.pop(0)
                yield {'id':'m2','receivedDateTime':'2026-10-09T11:01:00+08:00'}
            def read(self,account,mid): return Mail(account,mid,at('2026-10-09T11:00'),'业务请求','client@example.com','请开通')
        self.api=PartialInbox([httpx.ConnectError('private')]); self.worker.mail_api=self.api
        self.worker.ai=lambda mail:Decision('action','客户开通','客户请求')
        self.tick('2026-10-09T12:00'); self.tick('2026-10-09T12:01'); self.tick('2026-10-09T12:02')
        self.assertEqual(len(self.db.rows('SELECT * FROM items')),2)
        self.assertEqual(len(self.db.rows("SELECT * FROM notices WHERE kind='initial'")),2)
        self.assertEqual(len(self.group.messages),2)
        self.assertTrue(all(message[1]==['LiHan'] for message in self.group.messages))
