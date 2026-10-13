import argparse
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from .config import initialize_config
from .core import Mail,Decision,MAILBOXES,now
from .store import Store

def seed_demo(db):
    day=now().replace(hour=0,minute=0,second=0,microsecond=0)
    db.activate(day)
    examples=[('客户申请开通服务','请协助开通客户服务','action'),('渠道合作确认','合作伙伴确认收到合作资料','info'),
              ('设备信息待人工查看','涉及公司设备信息，请人工处理','human'),('投递失败通知','自动邮件投递失败，需要人工检查','action'),
              ('推广活动介绍','与公司业务无关的广告推广','digest'),('客户法律咨询','客户咨询合同条款，需要人工回复','action')]
    for index,(account,(subject,summary,category)) in enumerate(zip(MAILBOXES,examples)):
        mail=Mail(account,f'demo-{index}',day+timedelta(hours=10,minutes=index),subject,'sample@example.invalid','构造示例')
        decision=Decision(category,summary,'演示数据，不是实际分类',deadline=day+timedelta(days=1,hours=17) if index==0 else None,sensitive=category=='human')
        db.ingest(mail,decision)
    db.schedule(day+timedelta(hours=18))


def restore_backup(source, destination):
    source=Path(source); destination=Path(destination)
    if not source.is_file() or destination.exists():
        raise ValueError('备份不存在或目标已存在；恢复必须使用新的文件路径')
    destination.parent.mkdir(parents=True,exist_ok=True)
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True); dst=sqlite3.connect(destination)
    try:
        if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('备份校验失败')
        src.backup(dst)
    finally: src.close(); dst.close()


def configure(path):
    initialize_config(path)
    print('本机配置已准备，已有设置保留。启动后直接打开管理网页，在“邮箱与负责人”中维护系统配置。')


def main():
    parser=argparse.ArgumentParser(description='Oneasy 邮件助手')
    parser.add_argument('command',choices=['setup','run','demo','backup','restore','export'])
    parser.add_argument('--data',type=Path,default=Path('data'))
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--file',type=Path)
    args=parser.parse_args(); args.data=args.data.resolve(); args.data.mkdir(parents=True,exist_ok=True)
    config_path=args.data/'config.json'; db_path=args.data/'main.db'
    if args.command=='setup': configure(config_path); return
    if args.command=='restore':
        if not args.file: parser.error('restore 需要 --file 备份路径')
        restore_backup(args.file.resolve(),db_path); print('已恢复至新目录。请在启动前重新配置并核对启用时间。'); return
    if args.command in {'backup','export'}:
        if not db_path.exists() or not args.file: parser.error('需要现有数据库和 --file 输出路径')
        db=Store(db_path)
        if args.command=='backup': db.backup(args.file)
        else:
            args.file.write_text(json.dumps({table:db.rows('SELECT * FROM '+table) for table in ['items','audit','reports','notices','people','mailboxes','mailbox_owners','item_owners']},ensure_ascii=False,indent=2),encoding='utf-8')
            db.log('ledger_exported',actor='admin')
        print('已保存：'+str(args.file)); return
    if args.command=='demo':
        if config_path.exists(): parser.error('演示请使用独立 --data 目录，避免混入正式数据')
        db=Store(args.data/'demo.db'); seed_demo(db)
        config={'demo':True}
        from .web import create_app
        app=create_app(db,config)
        print('本地演示：http://127.0.0.1:'+str(args.port))
    else:
        config=initialize_config(config_path)
        from .web import create_app
        app=create_app(Store(db_path),config,run_worker=True)
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False,log_level='warning')


if __name__=='__main__': main()
