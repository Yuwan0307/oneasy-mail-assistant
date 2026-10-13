"""Alibaba Mail standard edition API. Deliberately no send/delete/draft API."""
import time
from urllib.parse import quote, urlsplit
import httpx

from .core import MAILBOXES, Mail, plain, stamp


class AliMail:
    def __init__(self, config, client=None):
        self.config=config
        self.client=client or httpx.Client(timeout=30,follow_redirects=False,trust_env=False)
        self.base='https://alimail-cn.aliyuncs.com'
        self.token=''
        self.expires=0

    def recover(self, action):
        if action=='reconnect':
            previous=self.client
            self.client=httpx.Client(timeout=30,follow_redirects=False,trust_env=False)
            previous.close()
        elif action=='refresh_token':
            self.token=''; self.expires=0

    def request(self, method, path, **kwargs):
        for attempt in range(2):
            if time.time() >= self.expires:
                response=self.client.post(self.base+'/oauth2/v2.0/token',data={
                    'grant_type':'client_credentials','client_id':self.config['client_id'],'client_secret':self.config['client_secret']})
                response.raise_for_status()
                data=response.json()
                self.token=data['access_token']
                self.expires=time.time()+int(data['expires_in'])-60
            response=self.client.request(method,self.base+path,headers={'Authorization':'bearer '+self.token},**kwargs)
            if response.status_code==401 and attempt==0:
                self.expires=0
                continue
            if not response.is_redirect:
                response.raise_for_status()
            return response

    def user(self, account):
        if account not in MAILBOXES:
            raise ValueError('邮箱不在许可范围')
        return '/v2/users/'+quote(account,safe='')

    def folders(self, account):
        data=self.request('GET',self.user(account)+'/mailFolders',params={'recursive':'true'}).json()
        return [f for f in data['folders'] if f.get('folderType') not in {'ROOT','SENT','DRAFTS','TRASH'} and f['id'] not in {'1','5','6'}]

    def messages(self, account, folder):
        cursor=''
        visited=set()
        while True:
            result=self.request('GET',self.user(account)+f'/mailFolders/{quote(folder,safe="")}/messages',params={
                'cursor':cursor,'size':100,'$select':'receivedDateTime,internetMessageId','isAscending':'false'}).json()
            yield from result['messages']
            if not result.get('hasMore'):
                break
            cursor=result.get('nextCursor','')
            if not cursor or cursor in visited:
                raise ValueError('分页游标异常')
            visited.add(cursor)

    def metadata(self, account, message_id, body=False):
        fields='receivedDateTime,internetMessageId'+(',id,subject,from,folderId,body' if body else '')
        return self.request('GET',self.user(account)+f'/messages/{quote(message_id,safe="")}',params={'$select':fields}).json()['message']

    def read(self, account, message_id):
        data=self.metadata(account,message_id,body=True)
        body=data.get('body')
        if not isinstance(body,dict) or 'receivedDateTime' not in data:
            raise ValueError('邮件正文或接收时间缺失')
        text=(body.get('bodyText') or '')+'\n'+(body.get('bodyHtml') or '')
        # Retain HTML attribute values for local sensitivity checks; never render it.
        attachments=[]
        try:
            # Always query attachments: hasAttachments excludes inline images.
            parts=self.request('GET',self.user(account)+f'/messages/{quote(message_id,safe="")}/attachments').json()['attachments']
            if len(parts)>20 or sum(int(p.get('size',0)) for p in parts)>10*1024*1024:
                raise ValueError('附件超过检查范围')
            for part in parts:
                name=part.get('name') or 'unknown.bin'
                if int(part.get('size',0))>5*1024*1024:
                    raise ValueError('附件过大')
                path=self.user(account)+f'/messages/{quote(message_id,safe="")}/attachments/{quote(part["id"],safe="")}/$value'
                response=self.request('GET',path)
                location=response.headers.get('location')
                if not location and 'application/json' in response.headers.get('content-type',''):
                    location=response.json().get('location')
                if location:
                    host=urlsplit(location)
                    if host.scheme!='https' or not host.hostname or not any(host.hostname.endswith('.'+domain) or host.hostname==domain for domain in ['aliyuncs.com','aliyun.com','mxhichina.com']):
                        raise ValueError('附件下载地址未被许可')
                    # No bearer token is forwarded to the download host.
                    with self.client.stream('GET',location) as download:
                        download.raise_for_status()
                        content=bytearray()
                        for chunk in download.iter_bytes():
                            content.extend(chunk)
                            if len(content)>5*1024*1024:
                                raise ValueError('附件过大')
                        attachments.append((name,bytes(content)))
                else:
                    if len(response.content)>5*1024*1024:
                        raise ValueError('附件过大')
                    attachments.append((name,response.content))
        except Exception:
            attachments=[('unverified.bin',b'')]
        return Mail(account,data['id'],stamp(data['receivedDateTime']),data.get('subject',''),
                    data.get('from',{}).get('email',''),text,attachments,data.get('folderId','2'),data['id'])

    def move(self, account, message_id, folder):
        if folder in {'1','5','6'}:
            raise ValueError('不允许移入发件、草稿或删除文件夹')
        self.request('POST',self.user(account)+'/messages/move',json={'ids':[message_id],'destinationFolderId':folder})

    def create_review_folder(self, account):
        existing=next((f for f in self.folders(account) if f['displayName']=='助手待复核'),None)
        if existing:
            return existing['id']
        return self.request('POST',self.user(account)+'/mailFolders',json={'displayName':'助手待复核'}).json()['folder']['id']
