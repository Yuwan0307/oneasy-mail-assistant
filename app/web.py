import json
import re
from pathlib import Path
from contextlib import asynccontextmanager
import threading
import secrets
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from .core import now
from .store import encode
from .worker import Worker
from .config import save_config, connection_config, AI_DEFAULTS
from .smtp import SMTPSender, valid_email, validate_config, error_text
from .core import sensitive
from .wecom import WeComSender, validate_config as wecom_config
from .deployment import VERSION

STATIC=Path(__file__).parent/'static'


def check_local(request):
    if not request.client or request.client.host not in {'127.0.0.1','::1'} or request.url.hostname not in {'127.0.0.1','localhost','::1'}:
        raise HTTPException(403,'管理页面仅允许在运行电脑本机访问')
    if request.url.path.startswith('/api/'):
        origin=request.headers.get('origin')
        if origin is not None and origin!=str(request.url).split('/api/',1)[0]:
            raise HTTPException(403,'不允许其他网页操作本机助手')
        if request.headers.get('sec-fetch-site','none') not in {'same-origin','none'}:
            raise HTTPException(403,'不允许其他网页操作本机助手')


def create_app(db,config,run_worker=False,desktop_control=False,desktop_port=8765):
    worker=Worker(db,config)
    desktop_tokens={}
    @asynccontextmanager
    async def lifespan(app):
        thread=None
        if run_worker:
            thread=threading.Thread(target=worker.run,daemon=True); thread.start()
        yield
        worker.stop.set(); worker.wake.set()
        if thread: thread.join(timeout=3)
    app=FastAPI(title='Oneasy 邮件助手',docs_url=None,redoc_url=None,lifespan=lifespan)
    app.state.worker=worker
    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    def auth(request): check_local(request)

    @app.middleware('http')
    async def local_only(request,call_next):
        try: check_local(request)
        except HTTPException as error:
            return JSONResponse({'detail':error.detail},status_code=error.status_code)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Frame-Options']='DENY'
        return response

    @app.get('/')
    def index(): return FileResponse(STATIC/'index.html')

    @app.get('/api/health')
    def health(): return {'app':'oneasy-mail-assistant','version':VERSION}

    @app.get('/api/state')
    def state(request:Request):
        auth(request)
        return {'demo':config.get('demo',False),'activated':db.get('activated'),'people':db.rows('SELECT * FROM people WHERE active=1'),
            'paused':worker.is_paused(),'desktop_available':desktop_control and not config.get('demo',False),
            'mailboxes':db.mailboxes(),
            'counts':db.rows('SELECT category,status,count(*) n FROM items GROUP BY category,status'),
            'pending_review':db.rows("SELECT count(*) n FROM items WHERE category='digest' AND reviewed=0")[0]['n'],
            'configured':{'mail':bool(worker.mail_api),'ai':bool(worker.ai),'smtp':bool(worker.sender),'wecom':bool(worker.wecom_sender)},
            'wecom':{'enabled':bool(config.get('wecom',{}).get('enabled')),'webhook_saved':bool(config.get('wecom',{}).get('webhook'))},
            'smtp':{key:config.get('smtp',{}).get(key,default) for key,default in [('address',''),('host',''),('port',465),('security','ssl')]},
            'smtp_password_saved':bool(config.get('smtp',{}).get('password')),
            'connections':{'client_id':config.get('alimail',{}).get('client_id',''),
                'client_secret_saved':bool(config.get('alimail',{}).get('client_secret')),
                'base_url':config.get('ai',{}).get('base_url',AI_DEFAULTS['base_url']),
                'model':config.get('ai',{}).get('model',AI_DEFAULTS['model']),
                'api_key_saved':bool(config.get('ai',{}).get('api_key'))},
            'last_scans':{b['account']:db.get('last_scan:'+b['account']) for b in db.rows('SELECT account FROM mailboxes')}}

    @app.post('/api/monitoring')
    def monitoring(request:Request,data:dict):
        auth(request)
        if config.get('demo'): raise HTTPException(409,'演示模式不可暂停或恢复监控')
        if not db.get('activated'): raise HTTPException(409,'请先启用新邮件监控')
        if set(data)!={'paused'} or type(data.get('paused')) is not bool:
            raise HTTPException(400,'监控状态无效')
        worker.set_paused(data['paused'])
        return {'paused':worker.is_paused()}

    @app.post('/api/desktop/request')
    def desktop_request(request:Request,data:dict):
        auth(request)
        if not desktop_control or config.get('demo'):
            raise HTTPException(409,'请通过已部署的桌面客户端操作')
        if set(data)!={'action'} or not isinstance(data.get('action'),str) or data['action'] not in {'hide','exit'}:
            raise HTTPException(400,'桌面操作无效')
        with worker.lock:
            current=time.monotonic()
            for token in list(desktop_tokens):
                if desktop_tokens[token][1]<=current: del desktop_tokens[token]
            if len(desktop_tokens)>=32: raise HTTPException(429,'请稍后再试')
            token=secrets.token_urlsafe(32)
            desktop_tokens[token]=(data['action'],current+120)
            db.log('desktop_'+data['action']+'_requested',actor='admin')
        return {'url':f'oneasy-mail-assistant://control/{desktop_port}/{token}'}

    @app.post('/api/desktop/command')
    def desktop_command(request:Request,data:dict):
        auth(request)
        if not desktop_control or config.get('demo'):
            raise HTTPException(409,'请通过已部署的桌面客户端操作')
        token=data.get('token')
        if set(data)!={'token'} or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',token):
            raise HTTPException(400,'桌面操作凭证无效')
        with worker.lock:
            command=desktop_tokens.pop(token,None)
            if not command or command[1]<=time.monotonic():
                raise HTTPException(409,'桌面操作已失效，请重新点击按钮')
            db.log('desktop_'+command[0]+'_accepted',actor='admin')
        return {'action':command[0]}

    @app.get('/api/items')
    def items(request:Request,view:str='all',offset:int=0,account:str=''):
        auth(request)
        where={'business':"category!='digest'",'review':"category='digest' AND reviewed=0",'all':'1'}.get(view,'1')
        args=[]
        if account: where+=' AND account=?'; args.append(account)
        return {'total':db.rows('SELECT count(*) n FROM items WHERE '+where,args)[0]['n'],
            'items':db.rows('SELECT * FROM items WHERE '+where+' ORDER BY received DESC LIMIT 30 OFFSET ?',args+[max(0,offset)])}

    @app.get('/api/reports')
    def reports(request:Request,report_id:str=''):
        auth(request)
        if report_id:
            record=db.rows('SELECT payload FROM reports WHERE id=?',(report_id,))
            if not record: raise HTTPException(404,'日报不存在')
            return json.loads(record[0]['payload'])
        return db.rows('SELECT id,owner,day FROM reports ORDER BY day DESC')

    @app.get('/api/audit')
    def audit(request:Request,offset:int=0):
        auth(request)
        return {'total':db.rows('SELECT count(*) n FROM audit')[0]['n'],'items':db.rows('SELECT * FROM audit ORDER BY seq DESC LIMIT 50 OFFSET ?',(max(0,offset),))}

    @app.get('/api/export')
    def export(request:Request):
        auth(request); db.log('ledger_exported',actor='admin')
        data={table:db.rows('SELECT * FROM '+table) for table in ['items','audit','reports','notices','mailboxes','people','mailbox_owners','item_owners']}
        return Response(encode(data),media_type='application/json',headers={'Content-Disposition':'attachment; filename="oneasy-ledger.json"'})

    @app.post('/api/activate')
    def activate(request:Request):
        auth(request)
        if config.get('demo') or db.get('activated'): raise HTTPException(409,'演示模式或已经启用')
        recipients=db.rows('SELECT p.* FROM mailbox_owners m LEFT JOIN people p ON m.person_id=p.id')
        missing=db.rows('SELECT 1 FROM mailboxes m WHERE NOT EXISTS (SELECT 1 FROM mailbox_owners o WHERE o.account=m.account)')
        if not worker.mail_api or not worker.sender or missing or any(not p['active'] or not valid_email(p['email']) for p in recipients):
            raise HTTPException(400,'请先配置阿里邮箱、提醒发件箱，并为每个邮箱设置有效的负责人提醒地址')
        try:
            for box in db.rows('SELECT * FROM mailboxes'): worker.mail_api.folders(box['account'])
        except Exception: raise HTTPException(400,'接入检查失败，请按管理员清单核对权限') from None
        db.activate(now()); return {'ok':True}

    @app.post('/api/action')
    async def action(request:Request):
        auth(request); data=await request.json(); item=db.item(data.get('id'))
        if not item: raise HTTPException(404,'记录不存在')
        if data.get('action') not in {'needs_action','restore'}: raise HTTPException(400,'单向提醒不接受完成或确认操作')
        with worker.lock:
            ok=db.review(item['id'],int(data['version']),item['owner'],data['action'])
        if not ok: raise HTTPException(409,'记录已更新，请刷新')
        db.log('admin_action',item['id'],{'action':data['action']},'admin')
        return {'ok':True}

    @app.post('/api/person')
    async def person(request:Request):
        auth(request); data=await request.json()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',data.get('id','')) or not valid_email(data.get('email')):
            raise HTTPException(400,'内部标识或负责人邮箱地址无效')
        name=data.get('name','')
        if not isinstance(name,str) or not name.strip() or len(name)>80 or sensitive(name):
            raise HTTPException(400,'负责人姓名无效')
        if config.get('demo'): raise HTTPException(409,'演示模式不发送连接测试，请在正式模式配置')
        try:
            with worker.lock:
                if worker.is_paused(): raise HTTPException(409,'监控已暂停，请恢复监控后发送测试邮件')
                if not worker.sender: raise HTTPException(400,'请先保存提醒发件箱配置')
                db.log('binding_test_started',detail={'id':data['id'],'recipient':data['email']},actor='admin')
                identity='binding:'+secrets.token_hex(16)
                with db.tx() as connection:
                    connection.execute('INSERT INTO sent_messages VALUES(?,?)',(worker.sender.message_id(data['email'],identity),now().isoformat()))
                worker.sender.send_text(data['email'],'Oneasy 邮件助手：邮件提醒连接测试。请查看邮箱及企业微信新邮件提醒，无需回复。',
                                        '【Oneasy】邮件提醒连接测试',identity)
                db.save_person(data['id'],name.strip(),data['email'],verified=True,userid=data.get('userid'))
        except HTTPException: raise
        except Exception as failure:
            db.log('binding_test_failed',detail={'id':data['id']},actor='admin')
            raise HTTPException(400,error_text(failure)) from None
        return {'ok':True}

    @app.post('/api/people')
    async def save_person(request:Request):
        auth(request); data=await request.json()
        if config.get('demo'): raise HTTPException(409,'演示模式不保存负责人')
        if not isinstance(data,dict) or not isinstance(data.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',data['id']):
            raise HTTPException(400,'负责人标识无效')
        name=data.get('name'); email=data.get('email')
        if not isinstance(name,str) or not isinstance(email,str): raise HTTPException(400,'请填写姓名和提醒邮箱')
        try:
            with worker.lock: db.save_person(data['id'],name.strip(),email.strip(),userid=data.get('userid'))
        except ValueError as error: raise HTTPException(400,str(error)) from None
        return {'ok':True}

    @app.post('/api/people/remove')
    async def remove_person(request:Request):
        auth(request); data=await request.json()
        if config.get('demo'): raise HTTPException(409,'演示模式不移除负责人')
        if not isinstance(data,dict) or not isinstance(data.get('id'),str): raise HTTPException(400,'负责人标识无效')
        try:
            with worker.lock: db.remove_person(data['id'])
        except ValueError as error: raise HTTPException(400,str(error)) from None
        return {'ok':True}

    @app.post('/api/connections')
    async def connections(request:Request):
        auth(request)
        if config.get('demo'): raise HTTPException(409,'演示模式不保存系统凭据')
        data=await request.json()
        try:
            with worker.lock:
                mail,ai=connection_config(data,config)
                updated={**config,'alimail':mail,'ai':ai}
                save_config(db.path.parent/'config.json',updated)
                config.update(updated)
                worker.reload_connections(mail,ai)
                db.log('connections_changed',detail={'mail_configured':bool(mail['client_secret']),'ai_configured':bool(ai['api_key'])},actor='admin')
        except ValueError as error: raise HTTPException(400,str(error)) from None
        except Exception: raise HTTPException(503,'系统配置保存失败，请检查本机数据目录权限') from None
        return {'ok':True}

    @app.post('/api/wecom')
    async def wecom_settings(request:Request):
        auth(request)
        if config.get('demo'): raise HTTPException(409,'演示模式不保存群提醒配置')
        data=await request.json()
        try:
            with worker.lock:
                value=wecom_config(data,config.get('wecom',{}))
                updated={**config,'wecom':value}
                save_config(db.path.parent/'config.json',updated)
                config.update(updated)
                worker.wecom_sender=WeComSender(value) if value['enabled'] else None
                db.log('wecom_changed',detail={'enabled':value['enabled'],'webhook_saved':bool(value['webhook'])},actor='admin')
        except ValueError as error: raise HTTPException(400,str(error)) from None
        except Exception: raise HTTPException(503,'群提醒配置保存失败，请检查本机数据目录权限') from None
        return {'ok':True}

    @app.post('/api/smtp')
    async def smtp_settings(request:Request):
        auth(request)
        if config.get('demo'): raise HTTPException(409,'演示模式不保存发件凭据')
        data=await request.json()
        try:
            with worker.lock:
                old=config.get('smtp',{})
                if not data.get('password'):
                    if (data.get('host'),data.get('address'))!=(old.get('host'),old.get('address')):
                        raise ValueError('更换服务器或发件账号后，请重新填写授权码')
                    data['password']=old.get('password','')
                smtp=validate_config(data)
                updated={**config,'smtp':smtp}
                save_config(db.path.parent/'config.json',updated)
                with db.tx() as connection:
                    if smtp!=old: connection.execute('UPDATE people SET verified=0')
                    db.log('smtp_changed',detail={'address':smtp['address']},actor='admin',db=connection)
                config.update(updated); worker.sender=SMTPSender(smtp)
        except ValueError as failure: raise HTTPException(400,str(failure)) from None
        except Exception: raise HTTPException(503,'发件配置保存失败，请检查本机数据目录权限') from None
        return {'ok':True}

    @app.post('/api/mailbox')
    async def mailbox(request:Request):
        auth(request); data=await request.json()
        box=next((b for b in db.rows('SELECT * FROM mailboxes') if b['account']==data.get('account')),None)
        if not box: raise HTTPException(404,'邮箱不存在')
        try:
            with worker.lock:
                folders=data.get('folders',json.loads(box['folders']))
                if not isinstance(folders,list) or not all(isinstance(f,str) and re.fullmatch(r'[\w-]{1,100}',f) for f in folders):
                    raise ValueError('文件夹 ID 无效')
                with db.tx() as connection:
                    owners=data.get('owners',[data['owner']] if data.get('owner') else db.owners(box['account'],connection))
                    db.change_owners(box['account'],owners,'admin',connection)
                    connection.execute('UPDATE mailboxes SET folders=?,move_enabled=? WHERE account=?',(encode(folders),int(bool(data.get('move_enabled',box['move_enabled']))),box['account']))
                    db.log('mailbox_changed',detail={'account':box['account'],'folders':folders,'move_enabled':bool(data.get('move_enabled',box['move_enabled']))},actor='admin',db=connection)
        except ValueError as error: raise HTTPException(400,str(error)) from None
        except Exception: raise HTTPException(503,'设置保存失败，请检查本地服务状态') from None
        return {'ok':True}
    return app
