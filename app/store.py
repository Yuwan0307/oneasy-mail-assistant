"""Small SQLite ledger and durable outbox; no raw email bodies are stored."""
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager, closing
from datetime import timedelta
from pathlib import Path

from .core import MAILBOXES, RULE_VERSION, first_review, now, sensitive, stamp
from .smtp import valid_email


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.tx() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY,name TEXT NOT NULL,userid TEXT NOT NULL DEFAULT '',verified INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS mailboxes(account TEXT PRIMARY KEY,label TEXT,owner TEXT NOT NULL,folders TEXT NOT NULL DEFAULT '[]',move_enabled INTEGER NOT NULL DEFAULT 0,review_folder TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,account TEXT,source_id TEXT,stable_id TEXT,received TEXT,created TEXT,subject TEXT,sender TEXT,summary TEXT,reason TEXT,category TEXT,owner TEXT,status TEXT,version INTEGER DEFAULT 1,deadline TEXT,evidence TEXT,hints TEXT,sensitive INTEGER,reviewed INTEGER DEFAULT 0,folder TEXT,original_folder TEXT,move_state TEXT DEFAULT '',model TEXT,rule_version TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS source_item ON items(account,stable_id);
            CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,item_id TEXT,version INTEGER,owner TEXT,kind TEXT,payload TEXT,state TEXT DEFAULT 'pending',due TEXT,attempts INTEGER DEFAULT 0,last_error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,owner TEXT,day TEXT,payload TEXT,UNIQUE(owner,day));
            CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT,actor TEXT,action TEXT,item_id TEXT,detail TEXT);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,time TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS seen(account TEXT,source_id TEXT,received TEXT,PRIMARY KEY(account,source_id));
            CREATE TABLE IF NOT EXISTS sent_messages(message_id TEXT PRIMARY KEY,time TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mailbox_owners(account TEXT,person_id TEXT,PRIMARY KEY(account,person_id));
            CREATE TABLE IF NOT EXISTS item_owners(item_id TEXT,person_id TEXT,report_at TEXT NOT NULL DEFAULT '',report_from TEXT NOT NULL DEFAULT '',PRIMARY KEY(item_id,person_id));
            ''')
            db.execute("INSERT OR IGNORE INTO people(id,name) VALUES('lihan','李涵')")
            db.executemany('INSERT OR IGNORE INTO mailboxes(account,label,owner) VALUES(?,?,?)', [(k,v,'lihan') for k,v in MAILBOXES.items()])
            columns={r['name'] for r in db.execute('PRAGMA table_info(notices)')}
            for name,definition in [('part','INTEGER NOT NULL DEFAULT 0'),('retry_at',"TEXT NOT NULL DEFAULT ''"),('channel',"TEXT NOT NULL DEFAULT ''")]:
                if name not in columns: db.execute(f'ALTER TABLE notices ADD COLUMN {name} {definition}')
            if 'report_at' not in {r['name'] for r in db.execute('PRAGMA table_info(items)')}:
                db.execute("ALTER TABLE items ADD COLUMN report_at TEXT NOT NULL DEFAULT ''")
            columns={r['name'] for r in db.execute('PRAGMA table_info(items)')}
            for name,definition in [('is_test','INTEGER NOT NULL DEFAULT 0'),('deadline_source',"TEXT NOT NULL DEFAULT ''"),('internal_followup',"TEXT NOT NULL DEFAULT ''")]:
                if name not in columns: db.execute(f'ALTER TABLE items ADD COLUMN {name} {definition}')
            if 'report_from' not in {r['name'] for r in db.execute('PRAGMA table_info(items)')}:
                db.execute("ALTER TABLE items ADD COLUMN report_from TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE items SET report_at=created WHERE report_at=''")
            if 'email' not in {r['name'] for r in db.execute('PRAGMA table_info(people)')}:
                db.execute("ALTER TABLE people ADD COLUMN email TEXT NOT NULL DEFAULT ''")
                db.execute('UPDATE people SET verified=0')
                db.execute("UPDATE notices SET part=0,retry_at='',attempts=0 WHERE state='pending'")
                self.log('notification_channel_changed',detail={'channel':'email'},db=db)
            if 'active' not in {r['name'] for r in db.execute('PRAGMA table_info(people)')}:
                db.execute('ALTER TABLE people ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
            if not db.execute("SELECT 1 FROM settings WHERE key='multiple_owners'").fetchone():
                db.execute('INSERT OR IGNORE INTO mailbox_owners SELECT account,owner FROM mailboxes')
                db.execute('INSERT OR IGNORE INTO item_owners SELECT id,owner,report_at,report_from FROM items')
                db.execute("INSERT INTO settings VALUES('multiple_owners','true')")

    @contextmanager
    def tx(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def rows(self, sql, args=()):
        with self.tx() as db:
            return [dict(row) for row in db.execute(sql,args)]

    def get(self, key, default=None):
        rows = self.rows('SELECT value FROM settings WHERE key=?',(key,))
        return json.loads(rows[0]['value']) if rows else default

    def set(self, key, value):
        with self.tx() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,encode(value)))

    def log(self, action, item_id='', detail=None, actor='system', db=None):
        # Call sites supply codes/IDs, never external exception text or raw mail.
        if db is None:
            with self.tx() as connection:
                self.log(action,item_id,detail,actor,connection)
            return
        db.execute('INSERT INTO audit(time,actor,action,item_id,detail) VALUES(?,?,?,?,?)',(now().isoformat(),actor,action,item_id,encode(detail or {})))

    def activate(self, current):
        with self.tx() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='activated'").fetchone():
                return
            db.execute('INSERT INTO settings VALUES(?,?)',('activated',encode(stamp(current).isoformat())))
            self.log('activated',detail={'at':stamp(current).isoformat()},db=db)

    def item(self, item_id):
        items = self.rows('SELECT * FROM items WHERE id=?',(item_id,))
        return items[0] if items else None

    def notice(self, db, key, owner, kind, item_id='', version=0, payload=None, due=None):
        db.execute('INSERT OR IGNORE INTO notices(id,item_id,version,owner,kind,payload,due) VALUES(?,?,?,?,?,?,?)',
                   (key,item_id,version,owner,kind,encode(payload or {}),(due or now()).isoformat()))

    def owners(self, account, connection=None):
        if connection is None:
            with self.tx() as db: return self.owners(account,db)
        return [r[0] for r in connection.execute('SELECT person_id FROM mailbox_owners WHERE account=? ORDER BY person_id',(account,))]

    def mailboxes(self):
        with self.tx() as db:
            return [{**dict(b),'owners':self.owners(b['account'],db)} for b in db.execute('SELECT * FROM mailboxes')]

    def item_notice(self, db, owner, kind, item_id, version, payload=None, due=None):
        # Recognize pre-upgrade IDs as well, so restarting cannot resend old reminders.
        old=db.execute('SELECT id FROM notices WHERE owner=? AND kind=? AND item_id=? AND version=?',
                       (owner,kind,item_id,version)).fetchone()
        if not old: self.notice(db,f'{kind}:{item_id}:{version}:{owner}',owner,kind,item_id,version,payload,due)

    def notify_item(self, db, kind, item_id, version, payload=None, due=None):
        item=db.execute('SELECT is_test FROM items WHERE id=?',(item_id,)).fetchone()
        if item and item['is_test']: return
        for row in db.execute('SELECT person_id FROM item_owners WHERE item_id=?',(item_id,)).fetchall():
            self.item_notice(db,row[0],kind,item_id,version,payload,due)

    def ingest(self, mail, decision):
        activation = self.get('activated')
        if not activation or stamp(mail.received) < stamp(activation):
            return None
        stable = mail.stable_id or mail.provider_id
        item_id = hashlib.sha256(f'{mail.account}\0{stable}'.encode()).hexdigest()[:24]
        with self.tx() as db:
            if db.execute('SELECT 1 FROM items WHERE id=?',(item_id,)).fetchone():
                return item_id
            owner = db.execute('SELECT owner FROM mailboxes WHERE account=?',(mail.account,)).fetchone()
            if owner is None:
                raise ValueError('邮箱不在许可范围')
            masked = decision.sensitive
            values = dict(id=item_id,account=mail.account,source_id=mail.provider_id,stable_id=stable,
                received=stamp(mail.received).isoformat(),created=now().isoformat(),report_at=now().isoformat(),
                subject='涉及敏感信息或未完整检查的邮件' if masked else mail.subject[:300],
                sender='请在原邮箱查看' if masked else mail.sender[:200],summary=decision.summary[:1200],reason=decision.reason[:600],
                category=decision.category,owner=owner[0],status='recorded',
                deadline=stamp(decision.deadline).isoformat() if decision.deadline else None,evidence='' if masked else decision.evidence[:300],
                hints='' if masked else decision.hints[:500],sensitive=int(masked),folder=mail.folder,original_folder=mail.folder,
                model=decision.model,rule_version=RULE_VERSION,is_test=int(decision.is_test),deadline_source=decision.deadline_source,
                internal_followup='' if masked else decision.internal_followup[:500])
            db.execute(f"INSERT INTO items({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
            db.executemany('INSERT INTO item_owners(item_id,person_id,report_at) VALUES(?,?,?)',
                           [(item_id,p,values['report_at']) for p in self.owners(mail.account,db)])
            self.log('classified',item_id,{'category':decision.category,'reason':values['reason'],'model':decision.model,'rule':RULE_VERSION},db=db)
            if decision.category in {'action','info','human'} and not decision.is_test:
                self.notify_item(db,'initial',item_id,1)
            else:
                self.log('instant_skipped',item_id,{'reason':'test_silent' if decision.is_test else 'marketing_ignored' if decision.category=='marketing' else 'daily_review'},db=db)
        return item_id

    def review(self, item_id, version, actor, action, connection=None):
        if action not in {'needs_action','restore'}:
            return False
        if connection is None:
            with self.tx() as db:
                return self.review(item_id,version,actor,action,db)
        else:
            db=connection
            item = db.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
            if not item or actor not in self.owners(item['account'],db) or item['version'] != version:
                return False
            db.execute('UPDATE items SET reviewed=1 WHERE id=?',(item_id,))
            if action == 'needs_action':
                db.execute("UPDATE items SET category='action',is_test=0,status='recorded',version=version+1 WHERE id=?",(item_id,))
                db.execute("UPDATE notices SET state='cancelled' WHERE item_id=? AND state='pending'",(item_id,))
                self.notify_item(db,'initial',item_id,version+1)
            if action in {'needs_action','restore'} and item['folder'] != item['original_folder']:
                db.execute("UPDATE items SET move_state='restore_pending' WHERE id=?",(item_id,))
            self.log('review_'+action,item_id,{'version':version},actor,db)
            return True

    def save_person(self, person_id, name, email, verified=False, userid=None):
        if not person_id or not name or sensitive(name) or len(name)>80 or not valid_email(email):
            raise ValueError('负责人信息无效')
        if userid is not None and (not isinstance(userid,str) or userid and
                (not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}',userid) or userid.lower() in {'all','@all'})):
            raise ValueError('企业微信成员账号无效，请填写成员账号，不要填写姓名或@所有人')
        with self.tx() as db:
            old = db.execute('SELECT * FROM people WHERE id=?',(person_id,)).fetchone()
            if old and not old['active']: raise ValueError('该负责人已移除，请使用新增负责人入口')
            if db.execute('SELECT 1 FROM people WHERE active=1 AND lower(email)=lower(?) AND id!=?',(email,person_id)).fetchone():
                raise ValueError('该提醒邮箱已属于另一位负责人，请直接选择已有负责人')
            revision = (old['revision']+1) if old else 1
            if old and old['email'] == email:
                verified = verified or bool(old['verified'])
            member=userid if userid is not None else old['userid'] if old else ''
            db.execute('INSERT INTO people(id,name,email,userid,verified,revision) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,email=excluded.email,userid=excluded.userid,verified=excluded.verified,revision=excluded.revision',
                       (person_id,name,email,member,int(verified),revision))
            if old and old['email'] != email:
                db.execute("UPDATE notices SET part=0,retry_at='',attempts=0 WHERE owner=? AND state='pending' AND kind!='report_group'",(person_id,))
            self.log('person_changed',detail={'id':person_id,'email':email,'userid':member,'verified':bool(verified),'revision':revision},actor='admin',db=db)

    def change_owner(self, account, person_id, actor):
        self.change_owners(account,[person_id],actor)

    def change_owners(self, account, people, actor, connection=None):
        if connection is None:
            with self.tx() as db: return self.change_owners(account,people,actor,db)
        db=connection
        if not isinstance(people,list) or not people or not all(isinstance(p,str) for p in people):
            raise ValueError('每个邮箱至少保留一位负责人')
        people=list(dict.fromkeys(people))
        if not db.execute('SELECT 1 FROM mailboxes WHERE account=?',(account,)).fetchone():
            raise ValueError('邮箱不存在')
        old=self.owners(account,db)
        if set(old)==set(people): return
        for person_id in people:
            person=db.execute('SELECT * FROM people WHERE id=? AND active=1',(person_id,)).fetchone()
            if not person or not valid_email(person['email']):
                raise ValueError('请先配置负责人的姓名和有效提醒邮箱')
        removed=set(old)-set(people)
        added=set(people)-set(old)
        activation=db.execute("SELECT value FROM settings WHERE key='activated'").fetchone()
        initial=json.loads(activation[0]) if activation else now().isoformat()
        windows={}
        for person_id in old:
            previous=db.execute("SELECT r.day FROM reports r JOIN notices n ON n.id='report:'||r.id WHERE r.owner=? AND n.state='sent' ORDER BY r.day DESC LIMIT 1",(person_id,)).fetchone()
            windows[person_id]=previous[0]+'T18:00:00+08:00' if previous else initial
        db.execute('DELETE FROM mailbox_owners WHERE account=?',(account,))
        db.executemany('INSERT INTO mailbox_owners VALUES(?,?)',[(account,p) for p in people])
        db.execute('UPDATE mailboxes SET owner=? WHERE account=?',(people[0],account))
        items=db.execute('SELECT * FROM items WHERE account=?',(account,)).fetchall()
        for item in items:
            memberships=db.execute('SELECT * FROM item_owners WHERE item_id=?',(item['id'],)).fetchall()
            carry=[]
            for member in memberships:
                since=windows.get(member['person_id'],initial)
                unseen=member['report_at'] and member['report_at']>since
                unseen=unseen or db.execute('SELECT 1 FROM audit WHERE item_id=? AND time>? LIMIT 1',(item['id'],since)).fetchone()
                if unseen: carry.append(min(since,member['report_from'] or since))
            notices=db.execute('SELECT * FROM notices WHERE item_id=? AND version=?',(item['id'],item['version'])).fetchall()
            for person_id in removed:
                db.execute('DELETE FROM item_owners WHERE item_id=? AND person_id=?',(item['id'],person_id))
                db.execute("UPDATE notices SET state='cancelled' WHERE item_id=? AND owner=? AND state='pending'",(item['id'],person_id))
            for person_id in added:
                db.execute('INSERT INTO item_owners VALUES(?,?,?,?)',
                           (item['id'],person_id,now().isoformat() if carry else '',min(carry) if carry else ''))
                # Transfer outstanding work, without replaying reminders already sent to all previous owners.
                for kind in {n['kind'] for n in notices}:
                    candidates=[n for n in notices if n['kind']==kind and n['owner'] in old]
                    pending=next((n for n in candidates if n['state']=='pending'),None)
                    existing=db.execute('SELECT id,state FROM notices WHERE item_id=? AND version=? AND kind=? AND owner=?',
                                        (item['id'],item['version'],kind,person_id)).fetchone()
                    if existing:
                        if pending and existing['state']=='cancelled':
                            db.execute("UPDATE notices SET state='pending',part=0,attempts=0,retry_at='',last_error='' WHERE id=?",(existing['id'],))
                        continue
                    source=(pending or candidates[0]) if candidates else None
                    if source:
                        self.item_notice(db,person_id,kind,item['id'],item['version'],json.loads(source['payload']),stamp(source['due']))
                        if not pending:
                            db.execute("UPDATE notices SET state='cancelled' WHERE item_id=? AND version=? AND kind=? AND owner=?",(item['id'],item['version'],kind,person_id))
            primary=db.execute('SELECT report_at,report_from FROM item_owners WHERE item_id=? AND person_id=?',(item['id'],people[0])).fetchone()
            db.execute('UPDATE items SET owner=?,report_at=?,report_from=? WHERE id=?',(people[0],primary[0],primary[1],item['id']))
        self.log('owners_changed',detail={'account':account,'from':old,'to':people,'transferred':len(items)},actor=actor,db=db)
        # Rebuild queued reports so removed recipients cannot receive this mailbox's data.
        affected=set(old)|set(people)
        for row in db.execute("SELECT r.* FROM reports r WHERE EXISTS(SELECT 1 FROM notices n WHERE n.id IN ('report:'||r.id,'report_group:'||r.id) AND n.state='pending')").fetchall():
            if row['owner'] not in affected: continue
            previous=json.loads(row['payload'])
            report=self.report_data(db,row['owner'],stamp(previous['cutoff']),since=previous['since'])
            report['revision']=previous.get('revision',0)+1
            db.execute('UPDATE reports SET payload=? WHERE id=?',(encode(report),row['id']))
            db.execute("UPDATE notices SET part=0,attempts=0,retry_at='',last_error='' WHERE id IN (?,?) AND state='pending'",('report:'+row['id'],'report_group:'+row['id']))
            self.log('report_rebuilt',detail={'report':row['id']},db=db)
        for person_id in removed:
            if not db.execute('SELECT 1 FROM mailbox_owners WHERE person_id=?',(person_id,)).fetchone():
                db.execute("UPDATE notices SET state='cancelled' WHERE owner=? AND state='pending'",(person_id,))

    def remove_person(self, person_id):
        with self.tx() as db:
            person=db.execute('SELECT * FROM people WHERE id=? AND active=1',(person_id,)).fetchone()
            if not person: raise ValueError('负责人不存在或已经移除')
            accounts=[r[0] for r in db.execute('SELECT account FROM mailbox_owners WHERE person_id=?',(person_id,))]
            for account in accounts:
                if len(self.owners(account,db))<2:
                    raise ValueError(f'{account} 只有这一位负责人，请先为该邮箱添加其他负责人')
            for account in accounts:
                self.change_owners(account,[p for p in self.owners(account,db) if p!=person_id],'admin',db)
            db.execute('UPDATE people SET active=0 WHERE id=?',(person_id,))
            db.execute("UPDATE notices SET state='cancelled' WHERE owner=? AND state='pending'",(person_id,))
            self.log('person_removed',detail={'id':person_id,'accounts':accounts},actor='admin',db=db)


    def report_data(self, db, owner, cutoff, since=None):
        previous=db.execute("SELECT r.day FROM reports r JOIN notices n ON n.id='report:'||r.id WHERE r.owner=? AND n.state='sent' ORDER BY r.day DESC LIMIT 1",(owner,)).fetchone()
        activation=db.execute("SELECT value FROM settings WHERE key='activated'").fetchone()
        activated=stamp(json.loads(activation[0])) if activation else cutoff
        inclusive=not previous if since is None else stamp(since)==activated
        since=stamp(since) if since is not None else stamp(previous[0]+'T18:00:00+08:00') if previous else activated
        items=[dict(i) for i in db.execute('SELECT o.report_at,o.report_from,i.* FROM items i JOIN item_owners o ON o.item_id=i.id WHERE o.person_id=? AND o.report_at>=? AND o.report_at<=? ORDER BY i.account,i.received',
               (owner,since.isoformat(),cutoff.isoformat())) if inclusive or i['report_at']>since.isoformat()]
        carried={i['id']:i['report_from'] for i in items if i['report_from']}
        earliest=min([since.isoformat(),*carried.values()])
        operations=[dict(a) for a in db.execute('SELECT * FROM audit WHERE time>? AND time<=? ORDER BY seq',(earliest,cutoff.isoformat()))]
        operations=[a for a in operations if (a['time']>since.isoformat() or a['item_id'] in carried and a['time']>carried[a['item_id']])
                    and (not a['item_id'] or db.execute('SELECT 1 FROM item_owners WHERE item_id=? AND person_id=?',(a['item_id'],owner)).fetchone())]
        person=db.execute('SELECT name FROM people WHERE id=?',(owner,)).fetchone()
        return dict(mode='notification',owner=owner,owner_name=person[0] if person else owner,day=cutoff.date().isoformat(),since=since.isoformat(),cutoff=cutoff.isoformat(),
                    items=items,notification_items=[i for i in items if not i['is_test']],review=[i for i in items if i['category']=='digest' and not i['reviewed'] and not i['is_test']],operations=operations)

    def schedule(self, current, group_reports=False):
        current = stamp(current)
        activation = self.get('activated')
        if not activation:
            return
        cutoff = current.replace(hour=18,minute=0,second=0,microsecond=0)
        if current < cutoff:
            cutoff -= timedelta(days=1)
        with self.tx() as db:
            # Sending a complete report advances its interval; no human confirmation exists.
            if cutoff >= stamp(activation):
                day = cutoff.date().isoformat()
                owners = {r[0] for r in db.execute('SELECT DISTINCT person_id FROM mailbox_owners')}
                for owner in owners:
                    if db.execute('SELECT 1 FROM reports WHERE owner=? AND day=?',(owner,day)).fetchone():
                        continue
                    report=self.report_data(db,owner,cutoff)
                    report['delayed']=current-cutoff>timedelta(minutes=3)
                    rid=f'{owner}:{day}'
                    db.execute('INSERT INTO reports VALUES(?,?,?,?)',(rid,owner,day,encode(report)))
                    db.execute("UPDATE notices SET state='cancelled' WHERE owner=? AND kind='report' AND state='pending'",(owner,))
                    self.notice(db,f'report:{rid}',owner,'report',payload={'report_id':rid},due=current)
                    if group_reports and db.execute("SELECT 1 FROM people WHERE id=? AND active=1 AND userid!=''",(owner,)).fetchone():
                        self.notice(db,f'report_group:{rid}',owner,'report_group',payload={'report_id':rid},due=current)
                        db.execute("UPDATE notices SET channel='wecom' WHERE id=?",('report_group:'+rid,))
                    self.log('report_created',detail={'report':rid,'review_count':len(report['review'])},db=db)
            for item in db.execute("SELECT * FROM items WHERE category IN ('action','human')").fetchall():
                review_due=first_review(item['received'])
                if review_due<=current:
                    self.notify_item(db,'review',item['id'],item['version'],due=review_due)
                if not item['deadline']: continue
                due = stamp(item['deadline'])-timedelta(hours=1)
                if not stamp(item['received']) < due <= current:
                    continue
                key=f'deadline:{item["id"]}:{item["version"]}'
                if current >= stamp(item['deadline']) or stamp(item['created']) >= due:
                    db.execute("UPDATE notices SET state='cancelled' WHERE item_id=? AND version=? AND kind='deadline' AND state='pending'",(item['id'],item['version']))
                    self.log_once(db,key,'expired_deadline_skipped',item['id'])
                    continue
                if due==review_due:
                    self.log_once(db,key,'reminder_merged',item['id'])
                else:
                    self.notify_item(db,'deadline',item['id'],item['version'],{'due':due.isoformat()},current)

    def log_once(self, db, key, action, item_id=''):
        if db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',(key,now().isoformat(),'ok')).rowcount:
            self.log(action,item_id,db=db)

    def report(self, owner, day):
        rows=self.rows('SELECT payload FROM reports WHERE owner=? AND day=?',(owner,day))
        return json.loads(rows[0]['payload']) if rows else None

    def backup(self, path):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        with self.lock, closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
        self.log('backup_completed')

    def archive(self, directory, month):
        directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
        payload={table:self.rows(f'SELECT * FROM {table} WHERE substr({field},1,7)=?',(month,))
                 for table,field in [('items','created'),('audit','time'),('reports','day')]}
        temporary=directory/(month+'.tmp')
        temporary.write_text(encode(payload),encoding='utf-8')
        temporary.replace(directory/(month+'.json'))
        self.log('monthly_archive_updated',detail={'month':month})
