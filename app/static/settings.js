// 设置页单独维护，日常页面不展开凭据和配置表单。
let leaveSettings = async () => true;
let settingsDirty = () => false;

function ownerNames(box) {
    return (box.owners || [box.owner]).map(id => state.people.find(p => p.id === id)?.name || id).join('、');
}

function field(form, key, label, value = '', type = 'text') {
    const input = el('input');
    input.name = key;
    input.id = 'edit-' + key;
    input.type = type;
    input.value = value;
    input.required = type !== 'password';
    if (type === 'password') input.autocomplete = 'new-password';
    const caption = el('label', label);
    caption.htmlFor = input.id;
    form.append(caption, input);
    return input;
}

function editor(target, title, back) {
    target.append(button('← 返回', () => navigate(back), true));
    const section = panel(title);
    const form = el('form', '', 'smtp-form');
    section.append(el('p', '修改后自动保存；空白密钥保留原配置。', 'muted'), form);
    target.append(section);
    return form;
}

function autoSave(form, path, read, afterSave = () => {}) {
    const status = el('p', state.demo ? '演示模式：不可修改配置' : '修改后自动保存', 'save-status muted');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    let baseline = JSON.stringify(read());
    let dirty = false, timer, saving = null, failed = false;
    const discard = button('放弃未保存修改', async () => {
        clearTimeout(timer);
        if (saving) await saving;
        dirty = false;
        await load();
    }, true);
    discard.hidden = true;
    form.append(status, discard);
    const mark = (text, error = false) => {
        status.textContent = text;
        status.className = 'save-status ' + (error ? 'error' : 'muted');
        discard.hidden = !error;
    };
    async function flush() {
        clearTimeout(timer);
        if (saving) {
            await saving;
            return dirty && !failed ? flush() : !dirty;
        }
        if (!dirty || state.demo) return true;
        if (!form.checkValidity()) {
            mark('尚未保存：请补全有效信息。', true);
            return false;
        }
        const data = read();
        const snapshot = JSON.stringify(data);
        if (snapshot === baseline) {
            dirty = false;
            mark('配置未变化');
            return true;
        }
        failed = false;
        mark('正在自动保存…');
        saving = (async () => {
            try {
                await api(path, data);
                baseline = snapshot;
                if (JSON.stringify(read()) === snapshot) {
                    afterSave(data);
                    baseline = JSON.stringify(read());
                    dirty = false;
                }
                mark('已自动保存');
            } catch (error) {
                failed = true;
                mark('未保存：' + error.message, true);
            }
        })();
        await saving;
        saving = null;
        return dirty && !failed ? flush() : !dirty;
    }
    const changed = () => {
        dirty = true;
        failed = false;
        clearTimeout(timer);
        mark('等待自动保存…');
        timer = setTimeout(flush, 1000);
    };
    form.addEventListener('input', changed);
    form.addEventListener('change', changed);
    form.onsubmit = event => { event.preventDefault(); flush(); };
    settingsDirty = () => dirty || !!saving;
    leaveSettings = flush;
    if (state.demo) form.querySelectorAll('input,select,button').forEach(input => input.disabled = true);
    return flush;
}

function configuration(target) {
    const entries = [
        ['系统连接', `阿里邮箱：${state.configured.mail ? '已配置' : '待配置'} · AI：${state.configured.ai ? '已配置' : '待配置'}`, '更改系统连接', 'connections'],
        ['提醒发件箱', state.configured.smtp ? '已配置' : '待配置', '更改提醒发件箱', 'smtp'],
        ['企业微信群提醒', state.configured.wecom ? '已启用 · 群提醒优先，邮件兜底' : state.wecom?.webhook_saved ? '已配置 · 未启用' : '待配置 · 当前使用邮件提醒', '更改企业微信群提醒', 'wecom'],
        ['负责人管理', `共 ${state.people.length} 位负责人，可负责一个或多个邮箱`, '管理负责人', 'people']
    ];
    for (const [name, description, label, route] of entries) {
        const section = panel(name);
        section.append(el('p', description, 'muted'), button(label, () => navigate(route), true));
        target.append(section);
    }
}

function connectionEditor(target) {
    const form = editor(target, '更改系统连接', 'configuration');
    const saved = state.connections || {};
    const inputs = {};
    for (const [key, label, secret] of [
        ['client_id', '阿里邮箱 Client ID', false],
        ['client_secret', '阿里邮箱 Client Secret', true],
        ['base_url', 'AI API 地址', false],
        ['model', 'AI 模型', false],
        ['api_key', 'AI API Key', true]
    ]) {
        inputs[key] = field(form, key, label, secret ? '' : saved[key] || '', secret ? 'password' : 'text');
        if (secret) inputs[key].placeholder = saved[key + '_saved'] ? '已配置，留空保留' : '尚未配置';
        if (key === 'client_id') inputs[key].required = false;
    }
    form.append(el('p', '更换应用 ID 或 AI 服务地址时，请同时填写对应的新密钥。保存失败时继续使用原配置。', 'muted'));
    autoSave(form, '/api/connections', () => Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()])), data => {
        for (const key of ['client_secret', 'api_key']) {
            if (data[key]) {
                inputs[key].value = '';
                inputs[key].placeholder = '已配置，留空保留';
            }
        }
    });
}

function smtpEditor(target) {
    const form = editor(target, '更改提醒发件箱', 'configuration');
    const saved = state.smtp || {};
    const address = field(form, 'address', '发件邮箱', saved.address, 'email');
    const host = field(form, 'host', 'SMTP 服务器', saved.host);
    const port = field(form, 'port', '端口', saved.port || 465, 'number');
    port.min = '1'; port.max = '65535';
    const password = field(form, 'password', '客户端授权码', '', 'password');
    password.required = !state.smtp_password_saved;
    password.placeholder = state.smtp_password_saved ? '已配置，留空保留' : '填写客户端授权码';
    const security = el('select');
    security.id = 'edit-security';
    for (const [value, label] of [['ssl', 'SSL/TLS（通常使用465端口）'], ['starttls', 'STARTTLS（通常使用587端口）']]) {
        const option = el('option', label);
        option.value = value;
        option.selected = value === (saved.security || 'ssl');
        security.append(option);
    }
    const caption = el('label', '加密方式');
    caption.htmlFor = security.id;
    form.append(caption, security, el('p', '更换发件账号或服务器时，请同时填写对应授权码。这里只发送内部提醒，客户邮件仍由人工回复。', 'muted'));
    autoSave(form, '/api/smtp', () => ({address: address.value.trim(), host: host.value.trim(), port: Number(port.value), security: security.value, password: password.value}), data => {
        if (data.password) {
            password.value = '';
            password.required = false;
            password.placeholder = '已配置，留空保留';
        }
    });
}

function wecomEditor(target) {
    const form = editor(target, '企业微信群提醒', 'configuration');
    const saved = state.wecom || {};
    const enabled = el('input');
    enabled.id = 'edit-enabled';
    enabled.name = 'enabled';
    enabled.type = 'checkbox';
    enabled.checked = !!saved.enabled;
    const label = el('label', '启用企业微信群提醒');
    label.prepend(enabled);
    form.append(label);
    const webhook = field(form, 'webhook', '群机器人 Webhook', '', 'password');
    webhook.placeholder = saved.webhook_saved ? '已配置，留空保留' : '填写群机器人 Webhook';
    form.append(
        el('p', '启用后，即时及复核提醒优先发到同一个企业微信群，并 @ 相关负责人；未填写企业微信成员账号或群发送失败时改发邮件。', 'muted'),
        el('p', '每日完整汇总（北京时间18:00）同步发群和邮件，群内 @ 报告负责人；未填成员账号只发邮件。长内容编号分条，不截断。', 'muted'),
        el('p', '群内全体成员均可见提醒和汇总内容，敏感信息仍按原规则遮蔽。自动保存不会发送测试提醒。', 'muted')
    );
    autoSave(form, '/api/wecom', () => ({ enabled: enabled.checked, webhook: webhook.value.trim() }), data => {
        if (data.webhook) {
            webhook.value = '';
            webhook.placeholder = '已配置，留空保留';
        }
    });
}

function peoplePage(target) {
    const actions = el('div', '', 'row page-actions');
    actions.append(button('← 返回设置', () => navigate('configuration'), true), button('添加负责人', () => navigate('person:new')));
    target.append(actions);
    for (const person of state.people) {
        const section = panel(person.name);
        const boxes = state.mailboxes.filter(b => b.owners.includes(person.id));
        section.append(el('p', person.email || '尚未填写提醒邮箱', 'muted'));
        if (person.userid) section.append(el('p', '企业微信成员账号：' + person.userid, 'muted'));
        section.append(el('p', boxes.length ? '负责：' + boxes.map(b => b.account).join('、') : '尚未分配邮箱', 'muted'));
        const remove = button('移除负责人', async () => {
            await api('/api/people/remove', {id: person.id});
            toast('负责人已移除');
            await load();
        }, true);
        remove.disabled = state.demo;
        const row = el('div', '', 'row');
        row.append(button('更改', () => navigate('person:' + person.id), true), remove);
        section.append(row);
        target.append(section);
    }
}

function personEditor(target, id) {
    const creating = id === 'new';
    const person = creating ? {} : state.people.find(p => p.id === id);
    if (!person) { target.append(el('p', '负责人不存在或已移除。', 'panel')); return; }
    const personId = creating ? 'p-' + crypto.randomUUID() : person.id;
    const form = editor(target, creating ? '添加负责人' : '更改负责人', 'people');
    form.parentElement.querySelector('p').textContent = '填写完整姓名和提醒邮箱后自动保存，随后可将负责人分配给一个或多个邮箱。';
    const name = field(form, 'name', '姓名', person.name || '');
    name.maxLength = 80;
    const email = field(form, 'email', '接收提醒的邮箱', person.email || '', 'email');
    const userid = field(form, 'userid', '企业微信成员账号（用于群内@）', person.userid || '');
    userid.required = false;
    form.append(el('p', '成员账号可留空，留空时只发邮件。启用群提醒后，每日完整汇总同步发群和提醒邮箱，并在群内 @ 报告负责人。', 'muted'));
    const read = () => ({id: personId, name: name.value.trim(), email: email.value.trim(), userid: userid.value.trim()});
    const flush = autoSave(form, '/api/people', read, () => {
        if (tab === 'person:new' && currentRoute() === tab) {
            tab = 'person:' + personId;
            history.replaceState(null, '', '#' + encodeURIComponent(tab));
        }
    });
    const test = button('发送测试邮件', async () => {
        if (!form.reportValidity() || !await flush()) return;
        await api('/api/person', read());
        toast('测试邮件已提交，请检查收件箱');
    }, true);
    test.disabled = state.demo;
    form.append(test, el('p', '测试为可选操作；自动保存不会发送测试邮件。', 'muted'));
}

function mailboxEditor(target, account) {
    const box = state.mailboxes.find(b => b.account === account);
    if (!box) { target.append(el('p', '邮箱不存在。', 'panel')); return; }
    const form = editor(target, box.account + ' · ' + box.label, 'settings');
    form.parentElement.querySelector('p').textContent = '可选择多位负责人；启用群提醒后，即时及复核提醒合并 @ 已配置成员账号的人，每日完整汇总按负责人分别发群和邮件；未填成员账号只发邮件。修改后自动保存。';
    const owners = el('fieldset', '', 'owner-options');
    owners.append(el('legend', '负责人（至少选择一位）'));
    const choices = state.people.map(person => {
        const input = el('input');
        input.type = 'checkbox';
        input.value = person.id;
        input.checked = box.owners.includes(person.id);
        const label = el('label', person.name + ' · ' + (person.email || '请先完善提醒邮箱'));
        label.prepend(input);
        owners.append(label);
        return input;
    });
    form.append(owners, button('添加或管理负责人', () => navigate('people'), true));
    const folders = field(form, 'folders', '监控文件夹 ID，以逗号分隔', JSON.parse(box.folders).join(','));
    folders.required = false;
    folders.placeholder = '留空监控收件箱、垃圾箱及全部自定义文件夹';
    const move = el('input');
    move.type = 'checkbox'; move.checked = !!box.move_enabled;
    const label = el('label', '开启待复核邮件自动移入“助手待复核”（已忽略推广除外）');
    label.prepend(move);
    form.append(label);
    autoSave(form, '/api/mailbox', () => ({
        account: box.account,
        owners: choices.filter(input => input.checked).map(input => input.value),
        folders: folders.value.split(',').map(value => value.trim()).filter(Boolean),
        move_enabled: move.checked
    }));
}

function settings(target) {
    const routes = {configuration, connections: connectionEditor, smtp: smtpEditor, wecom: wecomEditor, people: peoplePage};
    if (routes[tab]) return routes[tab](target);
    if (tab.startsWith('person:')) return personEditor(target, tab.slice(7));
    if (tab.startsWith('mailbox:')) return mailboxEditor(target, tab.slice(8));
    const section = panel('监管邮箱与负责人');
    section.append(el('p', '每个邮箱可同时配置多位负责人，同一位负责人也可负责多个邮箱。', 'muted'));
    section.append(table(['邮箱', '业务用途', '负责人', '操作'], state.mailboxes.map(box => [
        box.account, box.label, ownerNames(box), button('更改', () => navigate('mailbox:' + box.account), true)
    ])));
    target.append(section);
    const manage = panel('负责人及提醒方式');
    manage.append(el('p', `已有 ${state.people.length} 位负责人。具体信息在管理页维护。`, 'muted'), button('管理负责人', () => navigate('people'), true));
    target.append(manage);
    const enable = panel('监控状态');
    enable.append(el('p', state.activated ? '已启用 · ' + time(state.activated) : '尚未启用：仅处理启用后收到的新邮件。', 'muted'));
    if (!state.activated) {
        const activate = button('完成接入检查并启用', async () => {
            await api('/api/activate', {});
            toast('已启用新邮件监控');
            await load();
        });
        activate.disabled = state.demo;
        enable.append(activate);
    }
    target.append(enable);
}

window.addEventListener('beforeunload', event => {
    if (settingsDirty()) { event.preventDefault(); event.returnValue = ''; }
});
