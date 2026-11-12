import json
import threading
from datetime import timedelta
from pathlib import Path
import httpx

from .core import Mail, Decision, classify, now, stamp
from .alimail import AliMail
from .ai import CloudAI
from .notifications import item_text, report_text, message_parts, group_notice_text, group_system_text, group_report_parts, group_review_items, group_review_report_parts, system_notice_text
from .smtp import SMTPSender, error_text
from .wecom import WeComSender
from .recovery import diagnose


class Worker:
    def __init__(self,db,config,mail_api=None):
        self.db=db; self.config=config; self.stop=threading.Event(); self.lock=threading.RLock()
        self.mail_api=mail_api or (AliMail(config['alimail']) if config.get('alimail',{}).get('client_secret') else None)
        self.ai=CloudAI(config['ai']) if config.get('ai',{}).get('api_key') else None
        smtp=config.get('smtp',{})
        self.sender=SMTPSender(smtp) if all(smtp.get(k) for k in ('host','address','password')) else None
        wecom=config.get('wecom',{})
        self.wecom_sender=WeComSender(wecom) if wecom.get('enabled') else None
        self.retired_clients=[]
        self.paused=threading.Event(); self.wake=threading.Event()
        self.scanning=threading.Event()
        if db.get('paused',False): self.paused.set()

    def is_paused(self):
        return self.paused.is_set()

    def inactive(self):
        return self.stop.is_set() or self.is_paused()

    def set_paused(self,value):
        # Finish any in-flight send/move before acknowledging pause.
        with self.lock:
            if self.is_paused()==value: return
            with self.db.tx() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('paused',json.dumps(value)))
                self.db.log('monitoring_paused' if value else 'monitoring_resumed',actor='admin',db=db)
            if value: self.paused.set()
            else: self.paused.clear(); self.wake.set()

    def reload_connections(self,mail,ai):
        # Active mailbox scans retain their clients until the next polling round.
        for name,value,cls,key in [('mail_api',mail,AliMail,'client_secret'),('ai',ai,CloudAI,'api_key')]:
            old=getattr(self,name)
            if old and old.config==value: continue
            setattr(self,name,cls(value) if value.get(key) else None)
            if old: self.retired_clients.append(old.client)
            if name=='mail_api':
                for box in self.db.rows('SELECT account FROM mailboxes'):
                    state=self.db.get('scan_repair:'+box['account'])
                    if state: self.db.set('scan_repair:'+box['account'],{**state,'retry_at':now().isoformat()})

    def send_group(self,notice,item,text,identity,current,candidates=None):
        # One group message per reminder; durable SMTP routing survives a restart.
        if candidates is None:
            candidates=self.db.rows("SELECT n.*,p.userid FROM notices n JOIN people p ON p.id=n.owner WHERE n.state='pending' AND p.active=1 AND p.userid!='' AND n.channel='' AND n.part=0 AND n.item_id=? AND n.version=? AND n.kind=? AND n.payload=? AND n.due<=? AND n.retry_at<=? ORDER BY n.owner",
                (notice['item_id'],notice['version'],notice['kind'],notice['payload'],current.isoformat(),current.isoformat()))
            payload=json.loads(notice['payload'])
            account=item['account'] if item else payload.get('account')
            owners=self.db.owners(account) if account else None
            candidates=[n for n in candidates if owners is None or n['owner'] in owners]
        if not candidates: return False
        mention=notice['kind']!='system'
        userids=list(dict.fromkeys(n['userid'] for n in candidates)) if mention else []
        self.db.set('last_send',current.timestamp())
        for row in candidates:
            self.db.log('notification_attempt',notice['item_id'],{'notice':row['id'],'channel':'wecom','userid':row['userid'] if mention else ''})
        try:
            self.wecom_sender.send_text(text,userids,identity)
        except Exception:
            # Never persist transport exceptions, URLs, keys or platform response bodies.
            with self.db.tx() as db:
                for row in candidates:
                    db.execute("UPDATE notices SET channel='smtp' WHERE id=?",(row['id'],))
                    self.db.log('notification_fallback',notice['item_id'],{'notice':row['id'],'from':'wecom','to':'smtp','reason':'group_send_failed'},db=db)
            return False
        with self.db.tx() as db:
            for row in candidates:
                db.execute("UPDATE notices SET state='sent',channel='wecom',part=1,attempts=attempts+1,retry_at=?,last_error='' WHERE id=?",(current.isoformat(),row['id']))
                self.db.log('notification_sent',notice['item_id'],{'notice':row['id'],'channel':'wecom','userid':row['userid'] if mention else ''},db=db)
        return True

    @staticmethod
    def system_code(notice):
        code=json.loads(notice['payload']).get('code')
        if not code and notice['id'].startswith('alert:'): code=notice['id'].split(':',2)[1]
        return code if code in {'mailbox_scan_failed','move_or_restore_failed','missing_received_time',
                               'notification_loop_failed','worker_tick_failed'} else ''

    def send_system(self,notice,current):
        code=self.system_code(notice)
        pending=self.db.rows("SELECT n.*,p.active,p.email,p.userid FROM notices n LEFT JOIN people p ON p.id=n.owner WHERE n.kind='system' AND n.state='pending' AND n.part=0 AND n.due<=? AND n.retry_at<=? ORDER BY n.due,n.rowid",(current.isoformat(),current.isoformat()))
        rows=[]
        for row in pending:
            if self.system_code(row)!=code: continue
            account=json.loads(row['payload']).get('account')
            if row['active'] and row['email'] and (not account or row['owner'] in self.db.owners(account)):
                rows.append(row)
            else:
                with self.db.tx() as db:
                    db.execute("UPDATE notices SET state='cancelled' WHERE id=?",(row['id'],))
                    self.db.log('notification_cancelled',detail={'notice':row['id']},db=db)
        seed=next((row for row in rows if row['id']==notice['id']),None)
        if not seed: return
        def text_for(selected):
            accounts={json.loads(row['payload']).get('account') for row in selected}-{None,''}
            return system_notice_text(code,accounts,min(row['due'] for row in selected))
        group=[row for row in rows if row['channel']=='']
        if self.wecom_sender and seed['channel']=='' and group:
            if self.send_group(notice,None,text_for(group),notice['id'],current,candidates=group): return
        # One fallback email per current owner, covering all their affected mailboxes.
        selected=[row for row in rows if row['owner']==seed['owner']]
        attempts=max(row['attempts'] for row in selected)+1
        identity='|'.join(sorted(row['id'] for row in selected))
        message_id=self.sender.message_id(seed['email'],identity)
        with self.db.tx() as db:
            db.execute('INSERT OR IGNORE INTO sent_messages VALUES(?,?)',(message_id,current.isoformat()))
            for row in selected:
                db.execute("UPDATE notices SET channel='smtp' WHERE id=?",(row['id'],))
                self.db.log('notification_attempt',detail={'notice':row['id'],'channel':'smtp','recipient':seed['email'],'message_id':message_id},db=db)
        self.db.set('last_send',current.timestamp())
        error=''
        try: self.sender.send_text(seed['email'],text_for(selected),'【Oneasy·运行提醒】',identity)
        except Exception as failure: error=error_text(failure)
        retry=current+timedelta(seconds=min(3600,30*2**min(attempts,7))) if error else current
        with self.db.tx() as db:
            for row in selected:
                db.execute('UPDATE notices SET state=?,part=?,attempts=?,retry_at=?,last_error=? WHERE id=?',
                    ('pending' if error else 'sent',0 if error else 1,attempts,retry.isoformat(),error,row['id']))
                self.db.log('notification_failed' if error else 'notification_sent',detail={'notice':row['id'],'channel':'smtp','attempt':attempts},db=db)

    def sync(self):
        if self.config.get('demo'): return
        with self.lock:
            if self.inactive(): return
            current=now()
            if current.timestamp()-self.db.get('last_send',0)<4: return
            queue=self.db.rows("SELECT * FROM notices WHERE state='pending' AND due<=? AND retry_at<=? AND (kind='report_group' OR ?) AND (kind!='system' OR ?) ORDER BY CASE kind WHEN 'initial' THEN 0 WHEN 'system' THEN 0 WHEN 'deadline' THEN 1 WHEN 'review' THEN 2 ELSE 3 END,due,rowid LIMIT 1",(current.isoformat(),current.isoformat(),bool(self.sender),not self.scanning.is_set()))
            if not queue: return
            notice=queue[0]; payload=json.loads(notice['payload'])
            if notice['kind']=='system' and notice['part']==0 and self.system_code(notice):
                self.send_system(notice,current)
                return
            if notice['kind']=='report_group':
                self.send_group_report(notice,payload,current)
                return
            people=self.db.rows("SELECT * FROM people WHERE id=? AND active=1 AND email!=''",(notice['owner'],))
            item=self.db.item(notice['item_id']) if notice['item_id'] else None
            state='pending'; error=''; part=notice['part']; attempts=notice['attempts']
            invalid=notice['item_id'] and (not item or notice['owner'] not in self.db.owners(item['account']) or item['version']!=notice['version'])
            if item and item.get('is_test'): invalid=True
            if not people: invalid=True
            if notice['kind']=='system' and payload.get('account'):
                invalid=invalid or notice['owner'] not in self.db.owners(payload['account'])
            expired=notice['kind']=='deadline' and item and item['deadline'] and current>=stamp(item['deadline'])
            if invalid or expired:
                state='cancelled'
            else:
                if (notice['kind']=='review' and self.wecom_sender and people[0]['userid']
                        and notice['channel'] in {'','wecom'} and part==0
                        and self.review_in_daily_group(notice,current)):
                    return
                try:
                    if not people: raise ValueError('unverified recipient')
                    identity=notice['id']
                    if item:
                        title='收到业务来信' if notice['kind']=='initial' else '完成情况复核：请自行检查是否已处理，无需反馈。'
                        text=title+'\n'+item_text(item)
                        subject=('【Oneasy·业务来信】' if notice['kind']=='initial' else '【Oneasy·处理复核】')+item['subject']
                        if item.get('is_test'):
                            text='【测试邮件】仅供验证，无需实际开通或回复。\n'+item_text(item)
                            subject='【Oneasy·测试邮件】'+item['subject']
                    elif notice['kind']=='report':
                        report=json.loads(self.db.rows('SELECT payload FROM reports WHERE id=?',(payload['report_id'],))[0]['payload'])
                        if part==0 and payload.get('format')!='review-status':
                            payload={**payload,'format':'review-status'}
                            with self.db.tx() as db:
                                db.execute('UPDATE notices SET payload=? WHERE id=?',(json.dumps(payload),notice['id']))
                        text=report_text(report,legacy=payload.get('format') not in {'mail-only','review-status'},review_status=payload.get('format')=='review-status'); identity+=':'+str(report.get('revision',0))
                        subject='【Oneasy·每日汇总】'+report['day']
                    else:
                        text=payload['text']; subject='【Oneasy·运行提示】'
                    if self.wecom_sender and notice['kind']!='report' and notice['channel']=='' and part==0 and people[0]['userid']:
                        group_text=group_notice_text(item,notice['kind']) if item else group_system_text(text)
                        if self.send_group(notice,item,group_text,identity,current): return
                    with self.db.tx() as db:
                        db.execute("UPDATE notices SET channel='smtp' WHERE id=?",(notice['id'],))
                    if self.wecom_sender and notice['kind']!='report' and not people[0]['userid']:
                        self.db.log('notification_fallback',notice['item_id'],{'notice':notice['id'],'from':'wecom','to':'smtp','reason':'member_id_missing'})
                    parts=message_parts(text,identity)
                    delivery_id=identity+':'+str(part)
                    message_id=self.sender.message_id(people[0]['email'],delivery_id)
                    with self.db.tx() as db:
                        db.execute('INSERT OR IGNORE INTO sent_messages VALUES(?,?)',(message_id,current.isoformat()))
                    self.db.log('notification_attempt',notice['item_id'],{'notice':notice['id'],'channel':'smtp','part':part+1,'recipient':people[0]['email'],'message_id':message_id})
                    self.db.set('last_send',current.timestamp())
                    attempts+=1
                    suffix=f'（{part+1}/{len(parts)}）' if len(parts)>1 else ''
                    self.sender.send_text(people[0]['email'],parts[part],subject+suffix,delivery_id)
                    part+=1
                    state='sent' if part==len(parts) else 'pending'
                except Exception as failure:
                    error=error_text(failure)
            retry=current+timedelta(seconds=min(3600,30*2**min(attempts,7))) if error else current
            with self.db.tx() as db:
                db.execute('UPDATE notices SET state=?,part=?,attempts=?,retry_at=?,last_error=? WHERE id=?',
                           (state,part,attempts,retry.isoformat(),error,notice['id']))
                self.db.log('notification_failed' if error else 'notification_'+state,notice['item_id'],
                            {'notice':notice['id'],'channel':'smtp','part':part,'attempt':attempts},db=db)

    def daily_review_rows(self,owner,report,current):
        represented={item['id']:item.get('version',1) for item in group_review_items(report)
                     if item['category'] in {'action','human'}}
        rows=self.db.rows("SELECT n.* FROM notices n JOIN items i ON i.id=n.item_id WHERE n.kind='review' AND n.state='pending' AND n.owner=? AND n.part=0 AND n.channel IN ('','wecom') AND n.due<=? AND n.version=i.version AND i.is_test=0 AND EXISTS(SELECT 1 FROM mailbox_owners m WHERE m.account=i.account AND m.person_id=n.owner)",
                          (owner,current.isoformat()))
        return [row for row in rows if represented.get(row['item_id'])==row['version']]

    def finish_daily_reviews(self,connection,rows,report_notice):
        for row in rows:
            connection.execute("UPDATE notices SET state='sent',channel='wecom',part=1,retry_at='',last_error='' WHERE id=? AND state='pending'",(row['id'],))
            self.db.log('notification_merged',row['item_id'],{'notice':row['id'],'into':report_notice,'channel':'wecom'},db=connection)

    def review_in_daily_group(self,notice,current):
        reports=self.db.rows("SELECT n.*,r.payload AS report_payload FROM notices n JOIN reports r ON n.id='report_group:'||r.id WHERE n.owner=? AND n.state IN ('pending','sent') AND n.due<=? ORDER BY r.day DESC",(notice['owner'],current.isoformat()))
        for group in reports:
            report=json.loads(group['report_payload'])
            covered=self.daily_review_rows(notice['owner'],report,current)
            if not any(row['id']==notice['id'] for row in covered): continue
            if group['state']=='sent':
                with self.db.tx() as db: self.finish_daily_reviews(db,covered,group['id'])
            elif group['retry_at'] and stamp(group['retry_at'])>current:
                with self.db.tx() as db:
                    db.execute('UPDATE notices SET retry_at=? WHERE id=?',(group['retry_at'],notice['id']))
            else:
                self.send_group_report(group,json.loads(group['payload']),current)
            return True
        return False

    def send_group_report(self,notice,payload,current):
        # A separate durable task keeps group retries independent of the email window.
        people=self.db.rows("SELECT * FROM people WHERE id=? AND active=1 AND EXISTS(SELECT 1 FROM mailbox_owners WHERE person_id=people.id)",(notice['owner'],))
        reason='group_disabled' if not self.wecom_sender else 'recipient_removed' if not people else 'member_id_missing' if not people[0]['userid'] else ''
        part=notice['part']; attempts=notice['attempts']; error=''; state='pending'; covered=[]
        if reason:
            state='cancelled'
        else:
            try:
                report=json.loads(self.db.rows('SELECT payload FROM reports WHERE id=?',(payload['report_id'],))[0]['payload'])
                covered=self.daily_review_rows(notice['owner'],report,current)
                revision=report.get('revision',0)
                identity=notice['id']+':'+str(revision)
                owner_name=report.get('owner_name',people[0]['name'])
                if part==0 and (payload.get('format')!='brief-review' or payload.get('owner_name')!=people[0]['name']):
                    payload={**payload,'format':'brief-review','owner_name':people[0]['name']}
                    with self.db.tx() as db:
                        db.execute('UPDATE notices SET payload=? WHERE id=?',(json.dumps(payload),notice['id']))
                if payload.get('format')=='brief-review':
                    parts=group_review_report_parts(report,payload.get('owner_name',owner_name))
                elif payload.get('format') in {'review-status','mail-only','readable'}:
                    parts=group_report_parts(report,owner_name,legacy=payload['format']=='readable',review_status=payload['format']=='review-status')
                else:
                    # Finish pre-upgrade fragments using their original boundaries.
                    parts=message_parts('负责人：'+owner_name+'\n'+report_text(report,legacy=True),identity,limit=2048)
                self.db.set('last_send',current.timestamp())
                attempts+=1
                mention_items=group_review_items(report) if payload.get('format')=='brief-review' else report.get('notification_items',report['items'])
                userids=[people[0]['userid']] if any(i['category'] in {'action','info','human'} for i in mention_items) else []
                self.db.log('notification_attempt',detail={'notice':notice['id'],'channel':'wecom','part':part+1,'total':len(parts),'revision':revision,'userid':people[0]['userid'] if userids else ''})
                self.wecom_sender.send_text(parts[part],userids,identity+':'+str(part))
                part+=1
                state='sent' if part==len(parts) else 'pending'
            except Exception:
                # Transport exceptions can contain the secret webhook; keep them out of storage.
                error='企业微信群每日汇总发送失败，将重试'
        retry=current+timedelta(seconds=min(3600,30*2**min(attempts,7))) if error else current
        with self.db.tx() as db:
            db.execute("UPDATE notices SET state=?,channel='wecom',part=?,attempts=?,retry_at=?,last_error=? WHERE id=?",(state,part,attempts,retry.isoformat(),error,notice['id']))
            self.db.log('notification_failed' if error else 'notification_'+state,
                        detail={'notice':notice['id'],'channel':'wecom','part':part,'attempt':attempts,'reason':reason},db=db)
            if state=='sent': self.finish_daily_reviews(db,covered,notice['id'])
            elif state=='pending' and error:
                for row in covered:
                    db.execute("UPDATE notices SET channel='smtp',retry_at=?,last_error='' WHERE id=?",(current.isoformat(),row['id']))
                    self.db.log('notification_fallback',row['item_id'],{'notice':row['id'],'from':'wecom','to':'smtp','reason':'daily_group_send_failed'},db=db)

    def alert(self,code,account='',failure=None,record=True):
        detail={'account':account}
        if failure is not None:
            detail['failure_type']='timeout' if isinstance(failure,httpx.TimeoutException) else 'connection' if isinstance(failure,httpx.TransportError) else 'http_status' if isinstance(failure,httpx.HTTPStatusError) else 'unexpected'
            if isinstance(failure,httpx.HTTPStatusError): detail['http_status']=failure.response.status_code
        if record: self.db.log(code,detail=detail)
        day=now().date().isoformat()
        with self.db.tx() as db:
            owners=self.db.owners(account,db) if account else [r[0] for r in db.execute('SELECT DISTINCT person_id FROM mailbox_owners')]
            for owner in owners:
                identity=f'alert:{code}:{account}:{day}:{owner}'
                self.db.notice(db,identity,owner,'system',payload={'code':code,'account':account,'text':f'邮件助手需要检查：{code} {account}。请在管理页面查看运行状态。'})
                if code=='mailbox_scan_failed':
                    db.execute("UPDATE notices SET state='pending',due=?,retry_at='',channel='',attempts=0,last_error='' WHERE id=? AND state='cancelled' AND part=0 AND last_error='scan_recovered'",(now().isoformat(),identity))

    def scan_mailbox(self,box):
        account=box['account']; key='scan_repair:'+account
        with self.lock:
            if self.inactive() or not self.mail_api or not self.db.get('activated'): return
            state=self.db.get(key)
            if state and stamp(state['retry_at'])>now(): return
        try:
            with self.lock:
                if self.inactive(): return
                if state:
                    self.db.log('scan_repair_attempt',detail={'account':account,'action':state['action'],'reason':state['reason']})
                    if state['action'] in {'reconnect','refresh_token'}:
                        self.mail_api.recover(state['action'])
            complete=self.poll_mailbox(box)
        except Exception as failure:
            with self.lock:
                if self.inactive(): return
                current=now(); failures=(state or {}).get('failures',0)+1
                diagnosis=diagnose(failure,failures,current)
                retry=current+timedelta(seconds=diagnosis.pop('delay'))
                detail={'account':account,**diagnosis,'failures':failures,'retry_at':retry.isoformat()}
                self.db.set(key,detail)
                self.db.log('mailbox_scan_failed',detail=detail)
                self.db.log('scan_repair_scheduled',detail=detail)
                if failures>=3 or diagnosis['action']=='manual':
                    self.alert('mailbox_scan_failed',account,record=False)
            return
        with self.lock:
            if not complete or self.inactive(): return
            if state:
                self.db.set(key,None)
                self.db.log('scan_recovered',detail={'account':account,'result':'重新扫描成功，已恢复正常监控','failures':state['failures']})
            # A recovered mailbox must not send an old queued failure warning.
            with self.db.tx() as db:
                for row in db.execute("SELECT * FROM notices WHERE kind='system' AND state='pending'").fetchall():
                    if self.system_code(row)=='mailbox_scan_failed' and json.loads(row['payload']).get('account')==account:
                        db.execute("UPDATE notices SET state='cancelled',last_error='scan_recovered' WHERE id=?",(row['id'],))
                        self.db.log('notification_cancelled',detail={'notice':row['id'],'reason':'scan_recovered'},db=db)

    def poll_mailbox(self,box):
        with self.lock:
            if self.inactive(): return
            mail_api,ai=self.mail_api,self.ai
        if not mail_api: return
        activation=self.db.get('activated')
        if not activation: return
        with self.lock:
            if self.inactive(): return
            folders=mail_api.folders(box['account'])
        configured=json.loads(box['folders'])
        if configured: folders=[f for f in folders if f['id'] in configured]
        for folder in folders:
            with self.lock:
                if self.inactive(): return
                messages=iter(mail_api.messages(box['account'],folder['id']))
            while True:
                with self.lock:
                    if self.inactive(): return
                    try: metadata=next(messages)
                    except StopIteration: break
                mid=metadata['id']
                if self.db.rows('SELECT 1 FROM seen WHERE account=? AND source_id=?',(box['account'],mid)): continue
                if not metadata.get('receivedDateTime'):
                    self.alert('missing_received_time',box['account'])
                    continue
                received=stamp(metadata['receivedDateTime'])
                if self.db.rows('SELECT 1 FROM sent_messages WHERE message_id=?',(metadata.get('internetMessageId',''),)):
                    with self.db.tx() as db:
                        self.db.log('own_notification_skipped',detail={'account':box['account'],'source_id':mid},db=db)
                        db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(box['account'],mid,received.isoformat()))
                    continue
                if received>=stamp(activation):
                    if not self.db.rows('SELECT 1 FROM items WHERE account=? AND source_id=?',(box['account'],mid)):
                        self.db.log('read_started',detail={'account':box['account'],'source_id':mid})
                        try:
                            with self.lock:
                                if self.inactive(): return
                                mail=mail_api.read(box['account'],mid)
                            with self.lock:
                                if self.inactive(): return
                                decision=classify(mail,ai)
                        except Exception:
                            mail=Mail(box['account'],mid,received,'','','',folder=folder['id'])
                            decision=Decision('human','邮件读取未成功，请人工查看原邮箱','读取失败，保守即时提醒',sensitive=True)
                        with self.lock:
                            if self.inactive(): return
                            item_id=self.db.ingest(mail,decision)
                            self.db.log('read_finished',item_id or '',{'account':box['account']})
                with self.lock:
                    if self.inactive(): return
                    with self.db.tx() as db:
                        db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(box['account'],mid,received.isoformat()))
        with self.lock:
            if not self.inactive():
                self.db.set('last_scan:'+box['account'],now().isoformat())
                return True

    def moves(self):
        if not self.mail_api or self.inactive(): return
        for item in self.db.rows("SELECT i.*,m.move_enabled,m.review_folder FROM items i JOIN mailboxes m ON m.account=i.account WHERE i.move_state IN ('restore_pending','move_pending') OR (i.category='digest' AND i.reviewed=0 AND i.move_state='' AND m.move_enabled=1)"):
            with self.lock:
                if self.inactive(): return
                self.move_item(item)

    def move_item(self,item):
        try:
            restore=item['move_state']=='restore_pending'
            target=item['original_folder'] if restore else item['review_folder']
            if not target:
                target=self.mail_api.create_review_folder(item['account'])
                with self.db.tx() as db: db.execute('UPDATE mailboxes SET review_folder=? WHERE account=?',(target,item['account']))
            with self.db.tx() as db:
                db.execute('UPDATE items SET move_state=? WHERE id=?',('restore_pending' if restore else 'move_pending',item['id']))
                self.db.log('restore_started' if restore else 'move_started',item['id'],{'target':target},db=db)
            identity=self.db.get('move_identity:'+item['id'])
            try:
                current=self.mail_api.metadata(item['account'],item['source_id'])
                identity={'internetMessageId':current.get('internetMessageId'),'receivedDateTime':current.get('receivedDateTime')}
                self.db.set('move_identity:'+item['id'],identity)
            except Exception:
                current=self.locate_moved(item['account'],target,identity)
            if current.get('folderId')!=target: self.mail_api.move(item['account'],item['source_id'],target)
            try: confirmed=self.mail_api.metadata(item['account'],current['id'])
            except Exception: confirmed=self.locate_moved(item['account'],target,identity)
            if confirmed.get('folderId')!=target: raise ValueError('移动尚未确认')
            with self.db.tx() as db:
                db.execute('UPDATE items SET folder=?,source_id=?,move_state=? WHERE id=?',(target,confirmed['id'],'restored' if restore else 'moved',item['id']))
                db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(item['account'],confirmed['id'],item['received']))
                self.db.log('restored' if restore else 'moved',item['id'],{'from':item['folder'],'to':target},db=db)
        except Exception:
            self.alert('move_or_restore_failed',item['account'])

    def locate_moved(self,account,folder,identity):
        if not identity or not identity.get('internetMessageId') or not identity.get('receivedDateTime'):
            raise ValueError('移动结果需人工核对')
        matches=[m for m in self.mail_api.messages(account,folder) if m.get('internetMessageId')==identity['internetMessageId'] and m.get('receivedDateTime')==identity['receivedDateTime']]
        if len(matches)!=1: raise ValueError('移动结果不唯一，需人工核对')
        return {**matches[0],'folderId':folder}

    def tick(self):
        if self.config.get('demo') or self.inactive(): return
        with self.lock:
            retired,self.retired_clients=self.retired_clients,[]
        for client in retired: client.close()
        self.scanning.set()
        try:
            for box in self.db.rows('SELECT * FROM mailboxes'):
                if self.inactive(): return
                self.scan_mailbox(box)
            self.moves()
        finally:
            self.scanning.clear()
        with self.lock:
            if self.inactive(): return
            self.db.schedule(now(),group_reports=bool(self.wecom_sender))
        day=now().date().isoformat()
        if self.db.get('backup_day')!=day:
            backup_dir=Path(self.config.get('backup_dir',self.db.path.parent/'backups'))
            self.db.backup(backup_dir/f'{day}.db')
            for month in {now().strftime('%Y-%m'),(now().replace(day=1)-timedelta(days=1)).strftime('%Y-%m')}:
                self.db.archive(self.db.path.parent/'archive',month)
            self.db.set('backup_day',day)

    def run(self):
        def sender():
            while not self.stop.is_set():
                try: self.sync()
                except Exception: self.db.log('notification_loop_failed')
                self.stop.wait(4)
        sync_thread=threading.Thread(target=sender,daemon=True); sync_thread.start()
        while not self.stop.is_set():
            try: self.tick()
            except Exception: self.db.log('worker_tick_failed')
            self.wake.wait(60); self.wake.clear()
