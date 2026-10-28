const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.join(__dirname, '..', 'app', 'static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');

// A small DOM boundary keeps these tests offline while running the shipped scripts.
class Element {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.attributes = {};
        this.dataset = {};
        this.className = '';
        this.textContent = '';
        this.disabled = false;
        this.hidden = false;
        this.classList = {
            contains: name => this.className.split(/\s+/).includes(name),
            add: name => this.classList.toggle(name, true),
            remove: name => this.classList.toggle(name, false),
            toggle: (name, enabled) => {
                const names = new Set(this.className.split(/\s+/).filter(Boolean));
                const add = enabled === undefined ? !names.has(name) : enabled;
                if (add) names.add(name); else names.delete(name);
                this.className = [...names].join(' ');
                return add;
            }
        };
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    querySelectorAll() { return this.children.filter(child => child instanceof Element); }
    addEventListener() {}
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

async function dashboard(overrides = {}) {
    const elements = new Map();
    for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
        const element = new Element(match[1]);
        for (const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) element.setAttribute(attr[1], attr[2]);
        element.className = element.getAttribute('class') || '';
        element.disabled = /\bdisabled\b/.test(match[2]);
        element.hidden = /\bhidden\b/.test(match[2]);
        if (element.hidden) element.setAttribute('hidden', '');
        elements.set('#' + match[3], element);
    }
    const dot = new Element('span');
    dot.className = 'status-dot';
    elements.set('.status-dot', dot);
    for (const match of html.matchAll(/<button\b[^>]*data-tab="([^"]+)"/g)) {
        const button = new Element('button');
        button.dataset.tab = match[1];
        elements.get('#nav').append(button);
    }
    const model = {
        demo: false, activated: true, paused: false, desktop_available: true,
        counts: [], mailboxes: [], people: [], last_scans: {}, configured: {}, ...overrides
    };
    const requests = [];
    const confirms = [];
    const uri = 'oneasy-mail-assistant://control/8765/one-time-test-token';
    let closeCalls = 0;
    let confirmResult = true;
    let postError;
    let postWait;
    const location = { hash: '', href: 'http://127.0.0.1:8765/' };
    const context = vm.createContext({
        Node: Element,
        document: {
            querySelector: selector => elements.get(selector) || null,
            createElement: tag => new Element(tag)
        },
        window: { addEventListener() {}, close() { closeCalls++; } },
        location,
        history: { replaceState() {} },
        setTimeout() {}, clearTimeout() {},
        confirm(message) { confirms.push(message); return confirmResult; },
        async fetch(url, options) {
            if (url === '/api/state') return { ok: true, json: async () => ({ ...model }) };
            const data = JSON.parse(options.body);
            requests.push({ url, data });
            if (postWait) await postWait.promise;
            if (postError) return { ok: false, json: async () => ({ detail: postError }) };
            if (url === '/api/monitoring') {
                model.paused = data.paused;
                return { ok: true, json: async () => ({ paused: model.paused }) };
            }
            if (url === '/api/desktop/request') return { ok: true, json: async () => ({ url: uri }) };
            throw Error('Unexpected request: ' + url);
        }
    });
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'settings.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'admin.js'), 'utf8'), context);
    await new Promise(setImmediate);
    return {
        elements, model, requests, confirms, location, uri,
        get closeCalls() { return closeCalls; },
        get toast() { return elements.get('#toast').textContent; },
        get banner() { return elements.get('#banner').children.map(child => child.textContent).join(' '); },
        button(id) {
            const element = elements.get('#' + id);
            assert.ok(element, 'Runtime control must exist: ' + id);
            assert.equal(typeof element.onclick, 'function', 'Runtime control must be wired: ' + id);
            return element;
        },
        async reload() { await vm.runInContext('load()', context); },
        renderMail(item) { context.testMail=item; return vm.runInContext('card(testMail)', context); },
        setLeave(fn) { context.testLeave = fn; vm.runInContext('leaveSettings = testLeave', context); },
        cancelConfirm() { confirmResult = false; },
        failPost(message) { postError = message; },
        waitForPost(wait) { postWait = wait; }
    };
}

test('pause changes the service status and icon, and resume needs no confirmation', async () => {
    const page = await dashboard();
    const toggle = page.button('toggle-monitoring');
    assert.equal(toggle.getAttribute('aria-label'), '暂停监控');
    assert.equal(page.elements.get('#monitoring-square').getAttribute('hidden'), null);
    assert.notEqual(page.elements.get('#monitoring-circle').getAttribute('hidden'), null);
    await toggle.onclick();
    assert.deepEqual(page.requests, [{ url: '/api/monitoring', data: { paused: true } }]);
    assert.equal(page.elements.get('#service-state').textContent, '监控已暂停');
    assert.equal(page.elements.get('.status-dot').classList.contains('on'), false);
    assert.match(page.banner, /监控已暂停/);
    assert.equal(toggle.getAttribute('aria-label'), '恢复监控');
    assert.notEqual(page.elements.get('#monitoring-square').getAttribute('hidden'), null);
    assert.equal(page.elements.get('#monitoring-circle').getAttribute('hidden'), null);
    await toggle.onclick();
    assert.deepEqual(page.requests[1], { url: '/api/monitoring', data: { paused: false } });
    assert.equal(page.elements.get('#service-state').textContent, '监控已启用');
    assert.equal(page.elements.get('.status-dot').classList.contains('on'), true);
    assert.equal(page.confirms.length, 0);
});

test('demo controls cannot act and inactive monitoring keeps the original status', async () => {
    for (const overrides of [{ demo: true, paused: true }, { activated: false, paused: true }]) {
        const page = await dashboard(overrides);
        assert.equal(page.button('toggle-monitoring').disabled, true);
        await page.button('toggle-monitoring').onclick();
        assert.equal(page.requests.length, 0);
        assert.equal(page.elements.get('#service-state').textContent, overrides.demo ? '演示工作空间' : '等待启用监控');
        assert.doesNotMatch(page.banner, /监控已暂停/);
        if (overrides.demo) {
            for (const id of ['hide-window', 'exit-app']) {
                assert.equal(page.button(id).disabled, true);
                await page.button(id).onclick();
            }
            assert.equal(page.requests.length, 0);
            assert.equal(page.closeCalls, 0);
            assert.equal(page.confirms.length, 0);
        }
    }
});

test('failed settings save blocks pause, hide, and exit before confirmation or requests', async () => {
    for (const id of ['toggle-monitoring', 'hide-window', 'exit-app']) {
        const page = await dashboard();
        let saveCalls = 0;
        page.setLeave(async () => { saveCalls++; return false; });
        await page.button(id).onclick();
        assert.equal(saveCalls, 1);
        assert.equal(page.requests.length, 0);
        assert.equal(page.confirms.length, 0);
        assert.equal(page.closeCalls, 0);
        assert.equal(page.location.href, 'http://127.0.0.1:8765/');
        assert.equal(page.button(id).disabled, false);
    }
});

test('all controls stay disabled during save and concurrent clicks cannot duplicate an action', async () => {
    const page = await dashboard();
    const save = deferred();
    let saveCalls = 0;
    page.setLeave(() => { saveCalls++; return save.promise; });
    const pending = page.button('toggle-monitoring').onclick();
    assert.equal(page.requests.length, 0);
    for (const id of ['toggle-monitoring', 'hide-window', 'exit-app']) assert.equal(page.button(id).disabled, true);
    await page.button('toggle-monitoring').onclick();
    await page.button('exit-app').onclick();
    assert.equal(saveCalls, 1);
    save.resolve(true);
    await pending;
    assert.equal(page.requests.length, 1);
    assert.equal(page.button('toggle-monitoring').disabled, false);
});

test('hide asks the native helper to close the window without stopping monitoring', async () => {
    const page = await dashboard();
    await page.button('hide-window').onclick();
    assert.deepEqual(page.requests, [{ url: '/api/desktop/request', data: { action: 'hide' } }]);
    assert.equal(page.location.href, page.uri);
    assert.equal(page.closeCalls, 0);
    assert.equal(page.model.paused, false);
    assert.equal(page.confirms.length, 0);
    assert.match(page.toast, /已请求/);
    assert.match(page.toast, /系统提示/);
});

test('controls stay disabled while the desktop request is in flight', async () => {
    const page = await dashboard();
    const post = deferred();
    page.waitForPost(post);
    const pending = page.button('hide-window').onclick();
    await new Promise(setImmediate);
    for (const id of ['toggle-monitoring', 'hide-window', 'exit-app']) assert.equal(page.button(id).disabled, true);
    await page.button('hide-window').onclick();
    assert.equal(page.requests.length, 1);
    assert.equal(page.location.href, 'http://127.0.0.1:8765/');
    post.resolve();
    await pending;
    assert.equal(page.location.href, page.uri);
    assert.equal(page.button('hide-window').disabled, false);
});

test('exit saves first, confirms once, and dispatches only when accepted', async () => {
    const page = await dashboard();
    const order = [];
    page.setLeave(async () => {
        assert.equal(page.confirms.length, 0, 'Save must happen before exit confirmation');
        order.push('saved');
        return true;
    });
    page.cancelConfirm();
    await page.button('exit-app').onclick();
    assert.deepEqual(order, ['saved']);
    assert.equal(page.confirms.length, 1);
    assert.match(page.confirms[0], /停止邮件检测和提醒/);
    assert.equal(page.requests.length, 0);
    const accepted = await dashboard();
    await accepted.button('exit-app').onclick();
    assert.deepEqual(accepted.requests, [{ url: '/api/desktop/request', data: { action: 'exit' } }]);
    assert.equal(accepted.confirms.length, 1);
    assert.equal(accepted.location.href, accepted.uri);
    assert.equal(accepted.closeCalls, 0);
    assert.match(accepted.toast, /已请求/);
    assert.doesNotMatch(accepted.toast, /已完全退出|已停止/);
});

test('browser mode offers manual window close and cannot promise a full exit', async () => {
    const page = await dashboard({ desktop_available: false });
    assert.equal(page.button('exit-app').disabled, true);
    await page.button('exit-app').onclick();
    assert.equal(page.requests.length, 0);
    assert.equal(page.confirms.length, 0);
    await page.button('hide-window').onclick();
    assert.equal(page.closeCalls, 1);
    assert.equal(page.requests.length, 0);
    assert.match(page.toast, /手动/);
    assert.match(page.toast, /后台/);
});

test('a rejected control request displays the server error and re-enables controls', async () => {
    const page = await dashboard();
    page.failPost('暂时无法暂停监控');
    await page.button('toggle-monitoring').onclick();
    assert.equal(page.toast, '暂时无法暂停监控');
    assert.equal(page.button('toggle-monitoring').disabled, false);
    assert.equal(page.button('hide-window').disabled, false);
    assert.equal(page.button('exit-app').disabled, false);
    assert.equal(page.model.paused, false);
});

test('pending review count and historical reviewed badge reflect different states', async () => {
    const page = await dashboard({ pending_review: 1, counts: [{ category: 'digest', n: 3 }] });
    const cards = page.elements.get('#content').children[0];
    assert.equal(cards.children[2].children[0].textContent, '待人工复核');
    assert.equal(cards.children[2].children[1].textContent, '1');
    const mail = page.renderMail({ id: 'checked', version: 1, account: 'support@oneasy.cc',
        category: 'digest', reviewed: 1, subject: '已复核的通知', summary: '日常通知',
        sender: 'vendor@example.com', received: '2026-10-28T13:00:00+08:00', reason: '已人工核对' });
    assert.equal(mail.children[0].children[1].textContent, '已复核');
});

test('ignored marketing is not counted as business and remains correctable in reports', async () => {
    const page = await dashboard({ pending_review: 0, counts: [{ category: 'action', n: 1 }, { category: 'marketing', n: 4 }] });
    const cards = page.elements.get('#content').children[0];
    assert.equal(cards.children[1].children[1].textContent, '1');
    const mail = page.renderMail({ id: 'offer', version: 1, account: 'support@oneasy.cc', category: 'marketing',
        subject: '产品推广', summary: '已忽略', sender: 'sales@example.com', received: '2026-10-28T12:00:00+08:00', reason: '无客户事项' });
    assert.equal(mail.children[0].children[1].textContent, '营销推广·已忽略');
    assert.ok(mail.children.flatMap(child => child.children).some(child => child.textContent === '更正为业务邮件'));
});
