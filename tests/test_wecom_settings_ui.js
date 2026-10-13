const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.join(__dirname, '..', 'app', 'static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');

// Run the shipped UI offline, keeping DOM events and form validation at the boundary.
class Element {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.attributes = {};
        this.dataset = {};
        this.listeners = new Map();
        this.className = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.required = false;
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
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent || '').join(' '); }
    append(...nodes) { nodes.forEach(node => { node.parentElement = this; this.children.push(node); }); }
    prepend(...nodes) { nodes.forEach(node => node.parentElement = this); this.children.unshift(...nodes); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    matches(selector) {
        if (selector.startsWith('#')) return this.id === selector.slice(1) || this.getAttribute('id') === selector.slice(1);
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        return this.tagName.toLowerCase() === selector;
    }
    querySelectorAll(selector) {
        const selectors = selector.split(',').map(value => value.trim());
        return this.children.flatMap(child => [
            ...(selectors.some(value => child.matches(value)) ? [child] : []), ...child.querySelectorAll(selector)
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(event, fn) { this.listeners.set(event, fn); }
    dispatch(event) { this.listeners.get(event)?.({ target: this }); }
    checkValidity() {
        return this.querySelectorAll('input,select').every(input => input.disabled ||
            ((!input.required || String(input.value).trim()) &&
             (input.type !== 'email' || !input.value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(input.value))));
    }
    reportValidity() { return this.checkValidity(); }
}

async function dashboard(route, overrides = {}) {
    const elements = new Map();
    for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
        const element = new Element(match[1]);
        for (const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) element.setAttribute(attr[1], attr[2]);
        element.className = element.getAttribute('class') || '';
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
        counts: [], mailboxes: [], people: [{ id: 'lihan', name: '李涵', email: 'li@example.com', userid: 'LiHan' }],
        last_scans: {}, configured: { mail: true, ai: true, smtp: true, wecom: true },
        wecom: { enabled: true, webhook_saved: true }, ...overrides
    };
    const requests = [], events = new Map(), timers = new Map();
    const location = new URL('http://127.0.0.1:8765/#' + encodeURIComponent(route));
    let nextTimer = 0;
    const context = vm.createContext({
        Node: Element, crypto: { randomUUID: () => 'new-owner' }, location,
        document: {
            querySelector: selector => elements.get(selector) || elements.get('#content').querySelector(selector),
            createElement: tag => new Element(tag)
        },
        window: { addEventListener(event, fn) { events.set(event, fn); } },
        history: { replaceState(_state, _title, hash) { location.hash = hash; } },
        setTimeout(fn, delay) { timers.set(++nextTimer, { fn, delay }); return nextTimer; },
        clearTimeout(id) { timers.delete(id); },
        confirm: () => true,
        async fetch(url, options) {
            if (url === '/api/state') return { ok: true, json: async () => ({ ...model }) };
            const data = JSON.parse(options.body);
            requests.push({ url, data });
            assert.ok(['/api/wecom', '/api/people', '/api/person'].includes(url), 'Unexpected request: ' + url);
            return { ok: true, json: async () => ({ ok: true }) };
        }
    });
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'settings.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(staticDir, 'admin.js'), 'utf8'), context);
    await new Promise(setImmediate);
    const content = elements.get('#content');
    return {
        elements, requests, location, content,
        form() { const form = content.querySelector('form'); assert.ok(form, 'Settings editor must render a form'); return form; },
        input(name) {
            const input = content.querySelector('#edit-' + name);
            assert.ok(input, 'Settings field must exist: ' + name);
            return input;
        },
        button(label) {
            const button = content.querySelectorAll('button').find(item => item.textContent === label);
            assert.ok(button, 'Settings button must exist: ' + label);
            return button;
        },
        change(name, value) {
            const input = this.input(name);
            if (input.type === 'checkbox') input.checked = value; else input.value = value;
            this.form().dispatch(input.type === 'checkbox' ? 'change' : 'input');
        },
        async autosave() {
            for (const [id, timer] of [...timers]) {
                if (timer.delay === 1000) { timers.delete(id); await timer.fn(); }
            }
            await new Promise(setImmediate);
        },
        async followRoute() { await events.get('hashchange')(); await new Promise(setImmediate); }
    };
}

test('WeCom configuration stays in system settings and opens its own route', async () => {
    const page = await dashboard('configuration');
    assert.match(page.content.textContent, /企业微信群提醒/);
    assert.match(page.content.textContent, /已启用/);
    await page.button('更改企业微信群提醒').onclick();
    assert.equal(decodeURIComponent(page.location.hash.replace(/^#/, '')), 'wecom');
    await page.followRoute();
    assert.equal(page.elements.get('#title').textContent, '企业微信群提醒');
    assert.equal(page.input('webhook').type, 'password');
    const overview = await dashboard('overview');
    assert.equal(overview.content.querySelectorAll('input').length, 0);
    assert.match(overview.elements.get('#reminder-channel').textContent, /企业微信群优先/);
    assert.match(overview.content.textContent, /每日完整汇总.*邮件/);
});

test('saved webhook stays blank and autosave preserves it without sending a reminder', async () => {
    const page = await dashboard('wecom', { wecom: { enabled: true, webhook_saved: true, webhook: 'existing-secret' } });
    const webhook = page.input('webhook');
    assert.equal(webhook.value, '');
    assert.match(webhook.placeholder, /已配置.*留空保留/);
    assert.equal(page.input('enabled').checked, true);
    assert.equal(page.requests.length, 0);
    assert.doesNotMatch(page.content.textContent, /existing-secret/);
    assert.match(page.content.textContent, /全体成员.*可见/);
    assert.match(page.content.textContent, /未填写.*成员账号.*邮件/);
    page.change('enabled', false);
    await page.autosave();
    assert.deepEqual(page.requests, [{ url: '/api/wecom', data: { enabled: false, webhook: '' } }]);
    assert.match(page.content.textContent, /已自动保存/);
});

test('new webhook is sent once and cleared before later automatic saves', async () => {
    const page = await dashboard('wecom', { configured: { wecom: false }, wecom: { enabled: false, webhook_saved: false } });
    page.change('webhook', '  https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new-secret  ');
    page.change('enabled', true);
    await page.autosave();
    assert.deepEqual(page.requests, [{ url: '/api/wecom', data: {
        enabled: true, webhook: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new-secret'
    } }]);
    assert.equal(page.input('webhook').value, '');
    assert.match(page.input('webhook').placeholder, /已配置.*留空保留/);
    page.change('enabled', false);
    await page.autosave();
    assert.deepEqual(page.requests[1], { url: '/api/wecom', data: { enabled: false, webhook: '' } });
});

test('demo WeCom settings disable all edit controls and cannot save', async () => {
    const page = await dashboard('wecom', { demo: true });
    assert.equal(page.input('enabled').disabled, true);
    assert.equal(page.input('webhook').disabled, true);
    page.change('enabled', false);
    await page.autosave();
    assert.equal(page.requests.length, 0);
});

test('person autosave accepts a blank optional WeCom account and keeps the email fallback', async () => {
    const page = await dashboard('person:lihan');
    assert.equal(page.input('userid').value, 'LiHan');
    assert.equal(page.input('userid').required, false);
    page.change('userid', '');
    await page.autosave();
    assert.deepEqual(page.requests, [{ url: '/api/people', data: {
        id: 'lihan', name: '李涵', email: 'li@example.com', userid: ''
    } }]);
    assert.match(page.content.textContent, /留空.*邮件/);
});

test('the existing email test preserves the edited WeCom account and remains explicit', async () => {
    const page = await dashboard('person:lihan');
    page.change('userid', '  LiHan.New  ');
    await page.autosave();
    assert.deepEqual(page.requests, [{ url: '/api/people', data: {
        id: 'lihan', name: '李涵', email: 'li@example.com', userid: 'LiHan.New'
    } }]);
    await page.button('发送测试邮件').onclick();
    assert.deepEqual(page.requests[1], { url: '/api/person', data: {
        id: 'lihan', name: '李涵', email: 'li@example.com', userid: 'LiHan.New'
    } });
});

test('overview reports the active channel without exposing a webhook', async () => {
    for (const wecom of [{ enabled: false, webhook_saved: true }, { enabled: true, webhook_saved: false }]) {
        const page = await dashboard('overview', { wecom, configured: { wecom: false } });
        const channel = page.elements.get('#reminder-channel');
        assert.ok(channel, 'Header must show the reminder channel');
        assert.match(channel.textContent, /邮件提醒/);
        assert.doesNotMatch(channel.textContent, /企业微信群优先/);
    }
});
