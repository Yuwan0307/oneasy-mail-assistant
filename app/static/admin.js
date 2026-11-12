const $ = s => document.querySelector(s);
const labels = { action: '业务请求', human: '转人工', info: '信息通知', digest: '每日复核', marketing: '营销推广·已忽略' };
const titles = { overview: '运行概览', business: '业务来信', review: '邮件人工复核', reports: '每日汇总', audit: '操作记录', settings: '邮箱与负责人', configuration: '设置', connections: '更改系统连接', smtp: '更改提醒发件箱', wecom: '企业微信群提醒', people: '负责人管理' };

let tab = 'overview', offset = 0, state;
let runtimeBusy = false;

function renderRuntimeControls() {
    const paused = !!state?.paused;
    const demo = !!state?.demo;
    const toggle = $('#toggle-monitoring');
    const label = paused ? '恢复监控' : '暂停监控';
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', demo ? label + '（演示模式不可用）' : !state?.activated ? '启用监控后可暂停或恢复' : label);
    toggle.disabled = runtimeBusy || !state || demo || !state.activated;
    for (const [id, hidden] of [['#monitoring-square', paused], ['#monitoring-circle', !paused]]) {
        if (hidden) $(id).setAttribute('hidden', '');
        else $(id).removeAttribute('hidden');
    }

    const hide = $('#hide-window');
    hide.setAttribute('title', demo ? '隐藏到后台（演示模式不可用）' : state?.desktop_available ? '隐藏到后台（保持当前监控状态）' : '关闭当前窗口（后台保持当前监控状态）');
    hide.disabled = runtimeBusy || !state || demo;
    const exit = $('#exit-app');
    exit.setAttribute('title', demo ? '完全退出（演示模式不可用）' : state?.desktop_available ? '完全退出（停止邮件检测和提醒）' : '完全退出（仅已安装的桌面客户端可用）');
    exit.disabled = runtimeBusy || !state || demo || !state.desktop_available;
}

async function runtimeControl(action) {
    if (runtimeBusy || !state || state.demo || (action === 'monitoring' && !state.activated)) return;
    if (action === 'exit' && !state.desktop_available) {
        toast('当前为浏览器页面，请通过已安装的桌面客户端完全退出。');
        return;
    }
    runtimeBusy = true;
    renderRuntimeControls();
    try {
        if (!await leaveSettings()) {
            toast('设置尚未保存，操作未执行。请先完成保存或放弃修改。');
            return;
        }
        if (action === 'monitoring') {
            const result = await api('/api/monitoring', { paused: !state.paused });
            await load();
            toast(result.paused ? '监控已暂停' : '监控已恢复');
            return;
        }
        if (action === 'exit' && !confirm('完全退出后将停止邮件检测和提醒，配置与记录保留。确定退出？')) return;
        if (!state.desktop_available) {
            toast('如窗口未关闭，请手动点击窗口 × 关闭；后台仍按当前监控状态运行。');
            window.close();
            return;
        }
        const result = await api('/api/desktop/request', { action });
        if (typeof result.url !== 'string' || !result.url.startsWith('oneasy-mail-assistant://control/')) {
            throw Error('桌面控制请求无效，请重新打开客户端后重试。');
        }
        location.href = result.url;
        toast(action === 'hide' ? '已请求隐藏到后台；如窗口未关闭，请按系统提示操作。' : '已请求完全退出；如窗口未关闭，请按系统提示操作。');
    } catch (error) {
        toast(error.message);
    } finally {
        runtimeBusy = false;
        renderRuntimeControls();
    }
}

function el(tag, text = '', cls = '') {
    const n = document.createElement(tag);
    n.textContent = text;
    if (cls) n.className = cls;
    return n;
}

function toast(text) {
    const t = $('#toast');
    t.textContent = text;
    t.classList.remove('hidden');
    setTimeout(() => t.classList.add('hidden'), 3000);
}

async function api(path, data) {
    const r = await fetch(path, {
        method: data ? 'POST' : 'GET',
        headers: { ...(data ? { 'Content-Type': 'application/json' } : {}) },
        body: data ? JSON.stringify(data) : undefined
    });
    if (!r.ok) {
        let e = await r.json();
        throw Error(e.detail || '请求失败');
    }
    return r.json();
}

function button(text, fn, secondary = false) {
    const b = el('button', text, secondary ? 'secondary' : '');
    b.type = 'button';
    b.onclick = async () => {
        b.disabled = true;
        try { await fn(); }
        catch (e) { toast(e.message); }
        finally { b.disabled = false; }
    };
    return b;
}

function panel(title) {
    const p = el('section', '', 'panel');
    if (title) p.append(el('h3', title));
    return p;
}

function pager(total, size, callback) {
    const p = el('div', '', 'pagination');
    p.append(el('span', `共 ${total} 条记录 · 当前第 ${Math.floor(offset / size) + 1} 页`));
    
    const actions = el('div', '', 'row');
    const prev = button('上一页', () => { offset = Math.max(0, offset - size); return callback(); }, true);
    const next = button('下一页', () => { offset += size; return callback(); }, true);
    
    prev.disabled = offset === 0;
    next.disabled = offset + size >= total;
    actions.append(prev, next);
    p.append(actions);
    return p;
}

function time(v) {
    return v ? v.replace('T', ' ').replace('+08:00', '').slice(0, 19) : '—';
}

function table(headers, rows) {
    const wrap = el('div', '', 'table-wrap');
    const t = el('table');
    const head = el('tr');
    headers.forEach(h => head.append(el('th', h)));
    t.append(head);
    rows.forEach(row => {
        const tr = el('tr');
        row.forEach(v => {
            const td = el('td');
            if (v instanceof Node) td.append(v);
            else td.textContent = v;
            tr.append(td);
        });
        t.append(tr);
    });
    wrap.append(t);
    return wrap;
}

function card(item) {
    const c = el('article', '', 'card');
    const head = el('div', '', 'row between');
    head.append(
        el('span', item.account, 'muted'),
        el('span', item.is_test ? '测试邮件' : item.category==='digest' && item.reviewed ? '已复核' : labels[item.category], 'badge' + (item.category === 'human' ? ' warn' : ''))
    );
    
    c.append(
        head,
        el('h3', item.subject || '无主题'),
        el('p', item.summary, 'detail'),
        el('p', `发件人：${item.sender} · 接收时间：${time(item.received)}`, 'muted'),
        el('p', `判断依据：${item.reason}`, 'detail')
    );

    if (item.deadline && !item.is_test) c.append(el('p', `客户截止时间：${time(item.deadline)}（证据：${item.evidence}）`));
    if (item.internal_followup) c.append(el('p', `内部跟进建议（${item.is_test ? '测试模板，无需执行' : '非客户截止时间'}）：${item.internal_followup}`, 'detail'));
    if (item.hints && !item.is_test) c.append(el('p', '回复参考要点：' + item.hints, 'detail'));

    const actions = el('div', '', 'row');
    const act = action => async () => {
        await api('/api/action', { id: item.id, version: item.version, action });
        toast('操作已记录');
        await load();
    };

    if (['digest','marketing'].includes(item.category)) actions.append(button('更正为业务邮件', act('needs_action'), true));
    if (item.folder !== item.original_folder) actions.append(button('恢复原位置', act('restore'), true));
    
    const a = el('a', '打开原邮箱处理');
    a.href = 'https://qiye.aliyun.com';
    a.target = '_blank';
    a.rel = 'noopener';
    a.className = 'button secondary';
    actions.append(a);
    
    c.append(actions);
    return c;
}

async function load() {
    try {
        state = await api('/api/state');
    } catch (error) {
        state = undefined;
        renderRuntimeControls();
        $('#service-state').textContent = '后台连接中断';
        document.querySelector('.status-dot').classList.remove('on');
        document.querySelector('.status-dot').classList.remove('paused');
        throw error;
    }
    renderRuntimeControls();
    const groupReminders = !!state.wecom?.enabled && !!state.wecom?.webhook_saved;
    $('#reminder-channel').textContent = '六个邮箱统一监管 · ' + (groupReminders ? '企业微信群优先 · 邮件兜底' : '邮件提醒到负责人');
    $('#service-state').textContent = state.demo ? '演示工作空间' : !state.activated ? '等待启用监控' : state.paused ? '监控已暂停' : '监控已启用';
    document.querySelector('.status-dot').classList.toggle('on', !!state.activated && !state.demo && !state.paused);
    document.querySelector('.status-dot').classList.toggle('paused', !!state.activated && !state.demo && !!state.paused);
    $('#title').textContent = titles[tab] || (tab.startsWith('person:') ? '负责人设置' : '邮箱设置');
    leaveSettings = async () => true;
    settingsDirty = () => false;
    $('#nav').querySelectorAll('button').forEach(b => {
        const active = b.dataset.tab === tab || (b.dataset.tab === 'settings' && tab.startsWith('mailbox:'));
        b.classList.toggle('active', active);
        if (active) b.setAttribute('aria-current', 'page');
        else b.removeAttribute('aria-current');
    });

    const banner = $('#banner');
    banner.replaceChildren();
    if (state.demo) {
        banner.append(el('div', '演示模式：以下为构造样例，尚未连接真实邮箱、AI 或提醒发件箱。', 'banner'));
    } else if (!state.activated) {
        banner.append(el('div', '尚未启用监控。完成管理员配置和账号验证后，在“邮箱与负责人”中启用。', 'banner'));
    } else if (state.paused) {
        banner.append(el('div', '监控已暂停：邮件检测和提醒暂停。点击右上角圆形按钮恢复监控。', 'banner paused-banner'));
    }

    const target = $('#content');
    target.replaceChildren();

    if (tab === 'overview') {
        const cards = el('div', '', 'cards');
        const overviewData = [
            ['监管邮箱', 6, '客户来信统一记录'],
            ['业务来信', state.counts.filter(x => ['action','info','human'].includes(x.category)).reduce((n, x) => n + x.n, 0), '单向提醒，不跟踪人工完成状态'],
            ['待人工复核', state.pending_review ?? state.counts.filter(x => x.category === 'digest').reduce((n, x) => n + x.n, 0), '已复核记录保留在每日汇总与历史记录中']
        ];
        for (const [name, num, desc] of overviewData) {
            const c = el('div', '', 'card');
            c.append(el('span', name, 'muted'), el('div', String(num), 'metric'), el('div', desc, 'muted'));
            cards.append(c);
        }
        target.append(cards);

        const p = panel('邮箱监控状态');
        p.append(table(['邮箱', '业务用途', '负责人', '最近扫描'], state.mailboxes.map(b => [
            b.account,
            b.label,
            ownerNames(b),
            time(state.last_scans[b.account])
        ])));
        target.append(p);

        const reminders = panel('提醒安排');
        reminders.append(
            el('p', '业务相关或不确定来信即时提醒；需要处理的来信在首次 18:00 复核一次；明确截止时间前一小时额外复核。'),
            el('p', groupReminders ? '即时及截止前提醒优先发企业微信群并 @ 相关负责人，未配置成员账号或群发送失败时改发邮件。每天18:00群内按负责人合并简短复核清单；每日完整汇总仍通过邮件发送。未填成员账号只发邮件。' : '即时及复核提醒、每日完整汇总（北京时间18:00）发送邮件到负责人。', 'muted'),
            el('p', '18:00 及以后收到的邮件，首次日常复核为次日 18:00。涉及 IP、网络配置、凭证或未完整检查的附件，直接转人工。', 'muted')
        );
        target.append(reminders);
    } else if (tab === 'business' || tab === 'review') {
        const data = await api('/api/items?view=' + tab + '&offset=' + offset);
        if (!data.items.length) {
            target.append(el('div', tab === 'business' ? '暂无业务来信' : '暂无非即时提醒邮件', 'panel empty'));
        }
        data.items.forEach(item => target.append(card(item)));
        target.append(pager(data.total, 30, load));
    } else if (tab === 'reports') {
        const data = await api('/api/reports');
        const p = panel('每天 18:00 汇总 · 按负责人归集');
        if (!data.length) p.append(el('p', '日报生成后将在这里显示。', 'muted'));
        data.forEach(report => p.append(
            button(`${report.day} · ${state.people.find(person => person.id === report.owner)?.name || report.owner}`, () => showReport(report.id), true),
            el('span', ' ')
        ));
        target.append(p);
    } else if (tab === 'audit') {
        const data = await api('/api/audit?offset=' + offset);
        const p = panel('追加式操作记录');
        p.append(button('导出完整台账', async () => {
            const response = await fetch('/api/export');
            if (!response.ok) throw Error('导出失败');
            const url = URL.createObjectURL(await response.blob());
            const link = el('a');
            link.href = url;
            link.download = 'oneasy-ledger.json';
            link.click();
            URL.revokeObjectURL(url);
        }, true));
        p.append(table(['时间', '操作者', '操作', '对象 / 详情'], data.items.map(item => [
            time(item.time), item.actor, item.action, `${item.item_id || '系统'} ${item.detail}`
        ])));
        target.append(p, pager(data.total, 50, load));
    } else {
        await settings(target);
    }
}

async function showReport(id) {
    const data = await api('/api/reports?report_id=' + encodeURIComponent(id));
    const target = $('#content');
    target.replaceChildren();
    const items = data.items || [...(data.open || []), ...(data.review || [])];
    const p = panel(data.day + ' 每日汇总');
    p.append(
        el('p', `本期邮件 ${items.length} 封；待复核 ${(data.review || []).length} 封；已复核 ${items.filter(item => item.category==='digest' && item.reviewed).length} 封；已忽略推广 ${items.filter(item => item.category==='marketing').length} 封；操作 ${data.operations.length} 条。`),
        el('p', '供人工查阅，不需要确认或标记完成。', 'muted')
    );
    target.append(p);
    for (const box of state.mailboxes) {
        const subset = items.filter(item => item.account === box.account);
        if (subset.length) {
            target.append(el('h3', box.account + ' · ' + box.label));
            subset.forEach(item => target.append(card(item)));
        }
    }
    const operations = panel('本期操作汇总');
    operations.append(table(['时间', '操作', '详情'], data.operations.map(item => [
        time(item.time), item.action, item.detail
    ])));
    target.append(operations);
}

function currentRoute() {
    const route = decodeURIComponent(location.hash.slice(1)) || 'overview';
    return titles[route] || route.startsWith('person:') || route.startsWith('mailbox:') ? route : 'overview';
}

async function navigate(next) {
    if (next === tab) return;
    location.hash = encodeURIComponent(next);
}

window.addEventListener('hashchange', async () => {
    const next = currentRoute();
    if (!await leaveSettings()) {
        history.replaceState(null, '', '#' + encodeURIComponent(tab));
        return;
    }
    tab = next;
    offset = 0;
    load().catch(error => toast(error.message));
});
$('#nav').onclick = event => {
    const item = event.target.closest('button[data-tab]');
    if (item) navigate(item.dataset.tab);
};
$('#open-settings').onclick = () => navigate('configuration');
$('#refresh').onclick = async () => {
    if (await leaveSettings()) load().catch(error => toast(error.message));
};
$('#toggle-monitoring').onclick = () => runtimeControl('monitoring');
$('#hide-window').onclick = () => runtimeControl('hide');
$('#exit-app').onclick = () => runtimeControl('exit');

tab = currentRoute();
load().catch(error => toast(error.message));
