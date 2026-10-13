import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from app.core import Decision, Mail
from app.web import create_app
from app.worker import Worker
from app.store import Store
from tests.test_core import at


class WebTest(unittest.TestCase):
    def setUp(self):
        scratch=Path.cwd()/'.test-temp'; scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch); self.addCleanup(self.tmp.cleanup)
        self.db=Store(Path(self.tmp.name)/'app.db')
        self.db.activate(at('2026-10-09T10:00'))
        self.config={'admin_token':'a'*32,'wecom':{},'demo':True}

    def test_local_dashboard_has_no_body_or_secrets(self):
        client=TestClient(create_app(self.db,self.config),base_url='http://127.0.0.1',client=('127.0.0.1',1))
        self.assertEqual(client.get('/api/state').status_code,200)
        result=client.get('/api/state',headers={'Authorization':'Bearer '+'a'*32})
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(result.json()['mailboxes']),6)
        self.assertNotIn('admin_token',result.text)
        self.assertEqual(client.post('/api/activate',headers={'Authorization':'Bearer '+'a'*32}).status_code,409)


    def test_worker_skips_history_and_fails_closed_when_read_fails(self):
        class MailAPI:
            def folders(self, account): return [{'id':'2'}]
            def messages(self, account, folder):
                return [{'id':'old','receivedDateTime':'2026-10-09T01:00:00Z'},
                        {'id':'new','receivedDateTime':'2026-10-09T05:00:00Z'}]
            def read(self, account, mid):
                if mid=='old': raise AssertionError('历史邮件不允许读取')
                raise ConnectionError('secret must not leak')
        worker=Worker(self.db,self.config,mail_api=MailAPI())
        worker.poll_mailbox(self.db.rows('select * from mailboxes')[0])
        items=self.db.rows('select * from items')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['category'],'human')
        self.assertNotIn('secret',str(self.db.rows('select * from audit')))




    def test_move_tracks_changed_provider_id_and_restores(self):
        mail=Mail('service@oneasy.cc','old-id',at('2026-10-09T12:00'),'广告','ad@example.cc','广告')
        item=self.db.ingest(mail,Decision('digest','广告','无业务关联'))
        with self.db.tx() as db: db.execute("UPDATE mailboxes SET move_enabled=1,review_folder='9' WHERE account='service@oneasy.cc'")
        class Moves:
            def __init__(self): self.mid='old-id'; self.folder='2'
            def metadata(self,account,mid):
                if mid!=self.mid: raise ValueError('provider ID changed')
                return {'id':self.mid,'folderId':self.folder,'internetMessageId':'message-identity','receivedDateTime':'2026-10-09T04:00:00Z'}
            def messages(self,account,folder): return [self.metadata(account,self.mid)] if folder==self.folder else []
            def move(self,account,mid,folder): self.mid='new-id' if folder=='9' else 'restored-id'; self.folder=folder
        worker=Worker(self.db,self.config,mail_api=Moves()); worker.moves()
        self.assertEqual(self.db.item(item)['source_id'],'new-id')
        self.assertEqual(self.db.item(item)['move_state'],'moved')
        self.db.review(item,1,'lihan','restore'); worker.moves()
        self.assertEqual(self.db.item(item)['folder'],'2')
        self.assertEqual(self.db.item(item)['source_id'],'restored-id')


if __name__=='__main__': unittest.main()
