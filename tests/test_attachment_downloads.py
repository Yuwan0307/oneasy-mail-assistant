import unittest
import httpx
from app.alimail import AliMail


class AttachmentDownloadsTest(unittest.TestCase):
    def read(self,location):
        downloads=[]
        def reply(request):
            path=request.url.path
            if path.endswith('/token'): return httpx.Response(200,json={'access_token':'unit-test-only','expires_in':3600})
            if path.endswith('/messages/mail'):
                return httpx.Response(200,json={'message':{'id':'mail','receivedDateTime':'2026-11-03T09:38:00+08:00',
                    'subject':'广告','from':{'email':'sales@example.com'},'body':{'bodyText':'产品推广'}}})
            if path.endswith('/attachments'): return httpx.Response(200,json={'attachments':[{'id':'image','name':'名片.png','size':'13'}]})
            if path.endswith('/$value'): return httpx.Response(302,headers={'location':location})
            downloads.append(request)
            return httpx.Response(200,content=b'\x89PNG\r\n\x1a\nimage')
        with httpx.Client(transport=httpx.MockTransport(reply),follow_redirects=False) as client:
            result=AliMail({'client_id':'test','client_secret':'test'},client).read('service@oneasy.cc','mail')
        return result,downloads

    def test_relative_download_redirect_resolves_without_forwarding_bearer(self):
        result,requests=self.read('/download/image?ticket=unit-test-only')
        self.assertEqual(result.attachments,[('名片.png',b'\x89PNG\r\n\x1a\nimage')])
        self.assertEqual(len(requests),1)
        self.assertEqual(str(requests[0].url),'https://alimail-cn.aliyuncs.com/download/image?ticket=unit-test-only')
        self.assertNotIn('authorization',requests[0].headers)

    def test_untrusted_download_hosts_remain_blocked_after_resolution(self):
        for location in ('https://evil.example/download','//evil.example/download','http://alimail-cn.aliyuncs.com/download'):
            with self.subTest(location=location):
                result,requests=self.read(location)
                self.assertEqual(result.attachments,[('unverified.bin',b'')])
                self.assertEqual(requests,[])
