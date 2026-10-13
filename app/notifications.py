import hashlib
import ast
import re
from collections import Counter

from .core import MAILBOXES, stamp


def message_parts(text, notice_id, limit=None):
    # Keep existing email boundaries; group limits include the numbered prefix.
    code=hashlib.sha256(notice_id.encode()).hexdigest()[:10]
    count=1
    while True:
        budget=128000 if limit is None else limit-len(f'【{code} · {count}/{count}】\n'.encode('utf-8'))
        if budget<4: raise ValueError('消息分片上限过小')
        pieces=[]; part=''; size=0
        for char in text:
            width=len(char.encode('utf-8'))
            if size+width>budget:
                pieces.append(part); part=''; size=0
            part+=char; size+=width
        if part: pieces.append(part)
        if limit is None or len(str(len(pieces)))<=len(str(count)): break
        count=len(pieces)
    return [f'【{code} · {i+1}/{len(pieces)}】\n{part}' for i,part in enumerate(pieces)]


def item_text(item):
    labels={'action':'需要处理','human':'请人工查看','info':'业务信息','digest':'未即时提醒，供人工复核'}
    lines=[f"邮箱：{item['account']}",f"分类：{labels[item['category']]}",f"主题：{item['subject']}",
           f"发件人：{item['sender']}",f"收到：{item['received']}",f"摘要：{item['summary']}",f"依据：{item['reason']}"]
    if item.get('deadline'): lines.append('客户截止：'+item['deadline'])
    if item.get('hints'): lines.append('回复参考要点：'+item['hints'])
    return '\n'.join(lines)


def report_text(report, legacy=False):
    lines=[f"邮件每日汇总 · {report['day']}",f"记录区间：{report['since']} 至 {report['cutoff']}",
           f"新增邮件 {len(report['items'])} 封；其中未即时提醒 {len(report['review'])} 封。"]
    if report['items'] or legacy:
        lines.append('请人工检查是否遗漏有价值的邮件；本系统不记录工作完成状态。')
    for account in sorted({i['account'] for i in report['items']}):
        lines.append('\n'+account)
        lines.extend(item_text(i)+'\n' for i in report['items'] if i['account']==account)
    if legacy:
        lines.append(f"\n操作记录（{len(report['operations'])} 条）")
        lines.extend(f"{a['time']} {a['actor']} {a['action']} {a['item_id']} {a['detail']}" for a in report['operations'])
    return ('【延迟补发】\n' if report.get('delayed') else '')+'\n'.join(lines)


def display_time(value):
    try: return stamp(value).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, AttributeError): return '时间待核对'


def hint_lines(value):
    value=str(value or '').strip()
    if value.startswith(('[','{','(')):
        try: parsed=ast.literal_eval(value)
        except (ValueError,SyntaxError,TypeError,RecursionError):
            # Older AI results may contain a list cut off at the storage limit.
            recovered=[]
            if value.startswith(('[','(')):
                for token in re.findall(r"'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"",value):
                    try: line=ast.literal_eval(token)
                    except (ValueError,SyntaxError): continue
                    if isinstance(line,str) and line.strip(): recovered.append(line.strip())
            return recovered+['部分建议内容不完整，请结合原邮件核对。']
        return [line.strip() for line in parsed if isinstance(line,str) and line.strip()] if isinstance(parsed,(list,tuple)) else []
    return [re.sub(r'^\s*(?:[-•*]|\d+[.、])\s*','',line).strip()
            for line in re.split(r'[\n；;]+',value) if line.strip()]


def group_item_text(item, compact=False):
    def value(key,limit):
        text=str(item.get(key) or '')
        return text if not compact or len(text)<=limit else text[:limit]+'…'
    lines=[f"主题：{value('subject',120)}",f"邮箱：{item['account']}",
           f"发件人：{value('sender',100)}",f"收信时间：{display_time(item['received'])}（北京时间）",
           f"事项：{value('summary',240)}"]
    if item.get('deadline'): lines.append('客户要求的截止时间：'+display_time(item['deadline']))
    elif '截止要求需人工确认' in str(item.get('reason') or ''):
        lines.append('时间要求：客户提出了时间要求，具体截止时间请查看原邮件并人工确认。')
    hints=hint_lines(item.get('hints'))
    if hints:
        if compact:
            incomplete=hints[-1]=='部分建议内容不完整，请结合原邮件核对。'
            hints=[h[:80]+('…' if len(h)>80 else '') for h in (hints[:-1] if incomplete else hints)[:3]]
            if incomplete: hints.append('部分建议内容不完整，请结合原邮件核对。')
        lines.extend(['建议处理（供参考）：',*['• '+h for h in hints]])
    return '\n'.join(lines)


def group_notice_text(item,kind):
    if kind=='deadline':
        title='【截止前复核】'
        prompt='客户要求的回复时间即将到来，请核对是否已处理；已处理可忽略。'
    elif kind=='review':
        title='【处理复核】'
        prompt='请核对这封邮件是否已处理；已处理可忽略此提醒。'
    else:
        title={'human':'【新邮件 · 请人工查看】','info':'【新邮件 · 业务信息】'}.get(item['category'],'【新邮件 · 需处理】')
        prompt='请查看以下来信，并按需处理。'
    return title+'\n'+prompt+'\n\n'+group_item_text(item,compact=True)+'\n\n请登录原邮箱查看和处理。'


def group_system_text(text):
    labels={'missing_received_time':'邮件收信时间异常','move_or_restore_failed':'邮件移动或恢复未成功',
            'mailbox_scan_failed':'邮箱扫描未成功','notification_loop_failed':'提醒发送流程异常',
            'worker_tick_failed':'邮件检测流程异常'}
    for code,label in labels.items(): text=text.replace(code,label)
    return '【运行提醒】\n'+text


def system_notice_text(code,accounts,detected):
    labels={'mailbox_scan_failed':'邮箱扫描未成功','move_or_restore_failed':'邮件移动或恢复未成功',
            'missing_received_time':'邮件收信时间异常','notification_loop_failed':'提醒发送流程异常',
            'worker_tick_failed':'邮件检测流程异常'}
    lines=['【运行提醒 · '+labels[code]+'】','发现时间：'+display_time(detected)+'（北京时间）']
    if accounts: lines.extend([f'受影响邮箱（{len(accounts)}个）：',*['• '+account for account in sorted(accounts)]])
    if code=='mailbox_scan_failed':
        lines.extend(['情况：上述邮箱扫描仍未成功，相关新邮件提醒可能延迟。',
                      '助手已记录诊断结果，将继续尝试恢复并检查扫描结果。',
                      '如涉及权限或配置问题，需管理员处理；您仍可直接登录原邮箱处理来信。'])
    else:
        lines.append('请登录原邮箱核对相关邮件，并在助手中查看运行状态及操作记录。')
    lines.append('同类异常已合并提醒；若持续异常，请联系管理员检查。')
    return '\n'.join(lines)


def group_report_text(report,owner_name,legacy=False):
    items=report['items']; review=[i for i in items if i['category']=='digest']
    business=[i for i in items if i['category']!='digest']
    blocks=[f"负责人：{owner_name}\n统计时段：{display_time(report['since'])} 至 {display_time(report['cutoff'])}（北京时间）\n"
            f"本期新增 {len(items)} 封：业务来信 {len(business)} 封，待人工复核 {len(review)} 封。"]
    if report.get('delayed'): blocks.append('本次为延迟补发，以下内容对应上述统计时段。')
    labels={'action':'需关注','human':'请人工查看','info':'业务信息','digest':'待人工复核'}
    for heading,group in [('业务来信',business),('待人工复核',review)]:
        if not group: continue
        blocks.append(heading+f'（{len(group)}封）'+('\n以下邮件未即时提醒，请核对是否遗漏有价值的来信。' if heading=='待人工复核' else ''))
        for index,item in enumerate(group,1):
            blocks.append(f"{index}. {MAILBOXES.get(item['account'],'监管邮箱')} · {labels[item['category']]}\n"+group_item_text(item))
    if not legacy:
        if items: blocks.append('请在原邮箱处理业务；未即时提醒的邮件请人工复核。完整操作记录和分类依据可在邮件助手中查看。')
        return '\n\n'.join(blocks)
    # Only already-started pre-upgrade reports keep their original fragment boundaries.
    counts=Counter(a['action'] for a in report['operations'])
    groups={
        '完成邮件读取':('read_finished',),'完成邮件分类':('classified',),
        '发送提醒成功':('notification_sent',),'提醒改用邮件发送':('notification_fallback',),
        '邮件移动／恢复':('moved','restored'),'人工复核／更正':('review_needs_action','review_keep','review_restore','admin_action'),
        '生成／更新汇总':('report_created','report_rebuilt'),
        '设置或负责人调整':('wecom_changed','smtp_changed','connections_changed','person_changed','person_removed','owners_changed','mailbox_changed'),
        '数据备份／归档':('backup_completed','monthly_archive_updated'),
        '扫描自动诊断／恢复尝试':('scan_repair_scheduled','scan_repair_attempt'),
        '扫描恢复成功':('scan_recovered',),
        '监控或窗口操作':('activated','monitoring_paused','monitoring_resumed','desktop_hide_requested','desktop_hide_accepted','desktop_exit_requested','desktop_exit_accepted'),
    }
    rows=[]
    for label,actions in groups.items():
        total=sum(counts.pop(action,0) for action in actions)
        if total: rows.append(f'• {label}：{total}次')
    errors=sum(counts.pop(action) for action in list(counts) if action.endswith('_failed') or action=='missing_received_time')
    if errors: rows.append(f'• 需检查的异常记录：{errors}条，请到助手的“操作记录”查看。')
    # Intermediate technical events stay in the ledger, not in the group message.
    for action in ('read_started','notification_attempt','notification_pending','instant_skipped','move_started','restore_started'):
        counts.pop(action,None)
    if sum(counts.values()): rows.append(f'• 其他操作记录：{sum(counts.values())}条')
    blocks.append('系统操作摘要\n'+('\n'.join(rows) if rows else '本期无须关注的系统操作。'))
    blocks.append('请在原邮箱处理业务；未即时提醒的邮件请人工复核。完整操作记录和分类依据可在邮件助手中查看。')
    return '\n\n'.join(blocks)


def group_report_parts(report,owner_name,legacy=False):
    text=group_report_text(report,owner_name,legacy=legacy)
    title='每日邮件汇总 · '+report['day']+(' · 更新版' if report.get('revision') else '')
    count=1
    while True:
        budget=2048-len(f'【{title} · 第{count}/{count}条】\n'.encode('utf-8'))
        pieces=[]; current=''
        paragraphs=text.split('\n\n')
        for index,paragraph in enumerate(paragraphs):
            block=paragraph+('\n\n' if index<len(paragraphs)-1 else '')
            if len(block.encode('utf-8'))<=budget:
                units=[block]
            else:
                units=block.splitlines(keepends=True)
            for unit in units:
                if current and len((current+unit).encode('utf-8'))>budget:
                    pieces.append(current); current=''
                while len(unit.encode('utf-8'))>budget:
                    head=unit.encode('utf-8')[:budget].decode('utf-8',errors='ignore')
                    end=max(head.rfind(mark) for mark in '。！？；')+1
                    if end>len(head)//2: head=head[:end]
                    pieces.append(head); unit=unit[len(head):]
                current+=unit
        if current: pieces.append(current)
        if len(str(len(pieces)))<=len(str(count)): break
        count=len(pieces)
    return [f'【{title} · 第{i+1}/{len(pieces)}条】\n{piece}' for i,piece in enumerate(pieces)]
