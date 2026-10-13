import json
import unittest
from unittest.mock import patch
import httpx

from app.alimail import AliMail
from app.ai import CloudAI, deadline_from_evidence
from app.core import Mail
from tests.test_core import at


class AdapterTest(unittest.TestCase):
    def test_recovery_rebuilds_secure_client_or_invalidates_expired_token(self):
        old=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200)))
        new=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200)))
        self.addCleanup(new.close)
        api=AliMail({'client_id':'id','client_secret':'secret'},old)
        api.token='test'; api.expires=99999
        with patch('app.alimail.httpx.Client',return_value=new) as factory:
            api.recover('reconnect')
        self.assertTrue(old.is_closed); self.assertIs(api.client,new)
        self.assertFalse(factory.call_args.kwargs['follow_redirects'])
        self.assertFalse(factory.call_args.kwargs['trust_env'])
        self.assertEqual(api.token,'test')
        api.recover('refresh_token')
        self.assertEqual(api.token,''); self.assertEqual(api.expires,0)

    def test_ali_read_preserves_selected_message_details(self):
        message={
            'id':'m1','mailId':'m1','subject':'客户开通咨询',
            'from':{'email':'client@example.cc'},'folderId':'9',
            'receivedDateTime':'2026-10-09T04:00:00Z',
            'internetMessageId':'<customer-1@example.cc>',
            'body':{'bodyText':'请开通20个账号','bodyHtml':''},'isRead':False,
        }
        def reply(request):
            if request.url.path.endswith('/token'):
                return httpx.Response(200,json={'access_token':'test','expires_in':3600})
            if request.url.path.endswith('/messages/m1'):
                selected=set(request.url.params['$select'].split(','))|{'id','mailId'}
                return httpx.Response(200,json={'message':{k:v for k,v in message.items() if k in selected}})
            if request.url.path.endswith('/attachments'):
                return httpx.Response(200,json={'attachments':[]})
            return httpx.Response(404)
        client=httpx.Client(transport=httpx.MockTransport(reply))
        api=AliMail({'client_id':'id','client_secret':'secret'},client)

        mail=api.read('service@oneasy.cc','m1')

        self.assertEqual((mail.subject,mail.sender,mail.folder),
                         ('客户开通咨询','client@example.cc','9'))

    def test_explicit_utc_deadline_is_converted_to_beijing(self):
        result=deadline_from_evidence('请于2026-10-10 12:00 UTC回复',at('2026-10-09T12:00'))
        self.assertEqual(result.isoformat(),'2026-10-10T20:00:00+08:00')

    def test_ali_pages_body_attachments_and_move_contract(self):
        requests=[]
        def reply(request):
            requests.append(request)
            path=request.url.path
            if path.endswith('/token'):
                return httpx.Response(200,json={'access_token':'test','expires_in':3600})
            if path.endswith('/mailFolders'):
                return httpx.Response(200,json={'folders':[{'id':'2','folderType':'INBOX'},{'id':'1','folderType':'SENT'},{'id':'9','folderType':'CUSTOM'}]})
            if path.endswith('/mailFolders/2/messages'):
                page=request.url.params.get('cursor')
                return httpx.Response(200,json={'messages':[{'id':'m2' if page else 'm1','receivedDateTime':'2026-10-09T04:00:00Z'}], 'hasMore':not bool(page),'nextCursor':'page2' if not page else ''})
            if path.endswith('/attachments'):
                return httpx.Response(200,json={'attachments':[]})
            if path.endswith('/messages/m1'):
                self.assertIn('body',request.url.params['$select'])
                return httpx.Response(200,json={'message':{'id':'m1','subject':'开通','receivedDateTime':'2026-10-09T04:00:00Z','from':{'email':'client@example.cc'},'folderId':'2','body':{'bodyText':'请开通','bodyHtml':'<p>请开通</p>'},'isRead':False}})
            if path.endswith('/messages/move'):
                self.assertEqual(json.loads(request.content),{'ids':['m1'],'destinationFolderId':'9'})
                return httpx.Response(200,json={})
            return httpx.Response(404)
        client=httpx.Client(transport=httpx.MockTransport(reply))
        api=AliMail({'client_id':'id','client_secret':'secret'},client)
        self.assertEqual([f['id'] for f in api.folders('service@oneasy.cc')],['2','9'])
        self.assertEqual([m['id'] for m in api.messages('service@oneasy.cc','2')],['m1','m2'])
        mail=api.read('service@oneasy.cc','m1')
        self.assertIn('请开通',mail.body)
        self.assertEqual(mail.received.hour,12)
        api.move('service@oneasy.cc','m1','9')
        with self.assertRaises(ValueError):
            api.folders('unauthorized@example.cc')
        self.assertFalse(any(r.method in {'PATCH','DELETE'} for r in requests))

    def test_ai_rejects_fabricated_deadline_and_low_confidence_digest(self):
        payload={'category':'digest','summary':'广告','reason':'营销','confidence':.6,'evidence':'打折','deadline':'2026-10-10T18:00:00+08:00','deadline_evidence':'尽快','hints':''}
        client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload)}}]})))
        model=CloudAI({'api_key':'key','model':'qwen-plus'},client)
        result=model(Mail('service@oneasy.cc','1',at('2026-10-09T12:00'),'广告','a@b.cc','打折，尽快'))
        self.assertIsNone(result.deadline)
        self.assertEqual(result.category,'human')

if __name__=='__main__': unittest.main()
