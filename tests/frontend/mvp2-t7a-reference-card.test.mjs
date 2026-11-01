// MVP2-T7a（issue #415）参考卡 UI node 测试：
// AC1 选中/拒绝/备注（≤500）/顺序 的渲染与提交全部走 mock fetch；
// AC2 无框架、无 CDN，样式复用既有黑金 class/token；
// AC3 用户输入与错误信息纯文本渲染（防 XSS）；
// AC4 mock API 失败时展示明确错误态，不白屏。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../../web/js/controllers/agent-tasks.js', import.meta.url), 'utf8');
const PROJECT_ID = 'project-mvp2';
const SESSION_ID = 'session-mvp2';

function makeNode(tagName = 'div') {
    const listeners = new Map();
    const node = {
        tagName: tagName.toUpperCase(),
        children: [],
        dataset: {},
        disabled: false,
        hidden: false,
        value: '',
        textContent: '',
        className: '',
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        emit(type, event = {}) {
            const handler = listeners.get(type)?.at(-1);
            return handler?.({ ...event, target: this, preventDefault: event.preventDefault || (() => {}) });
        },
        appendChild(child) {
            child.parentElement = this;
            this.children.push(child);
            return child;
        },
        replaceChildren(...children) {
            this.children = [];
            for (const child of children) this.appendChild(child);
        },
        remove() {
            if (!this.parentElement) return;
            this.parentElement.children = this.parentElement.children.filter(child => child !== this);
            this.parentElement = null;
        },
        click() { this.clicked = true; },
        focus() {}
    };
    Object.defineProperty(node, 'firstElementChild', { get: () => node.children[0] || null });
    return node;
}

function jsonResponse(value) {
    return {
        ok: true,
        status: 200,
        headers: { get: name => (String(name).toLowerCase() === 'content-type' ? 'application/json' : null) },
        json: async () => value
    };
}

function errorResponse(status, message) {
    return {
        ok: false,
        status,
        headers: { get: name => (String(name).toLowerCase() === 'content-type' ? 'application/json' : null) },
        json: async () => ({ detail: { message } })
    };
}

function referenceSet(items, version = 3) {
    return {
        session_id: SESSION_ID,
        project_id: PROJECT_ID,
        version,
        items: items.map((item, index) => ({
            asset_id: item.asset_id,
            decision: item.decision || 'pending',
            note: item.note ?? null,
            order: item.order ?? index,
            updated_at: '2026-10-28T00:00:00+00:00',
            ...(Object.hasOwn(item, 'score') ? { score: item.score } : {}),
            ...(item.tags ? { tags: item.tags } : {})
        }))
    };
}

const DEFAULT_ITEMS = [
    { asset_id: 'asset-alpha', score: 8.5, order: 0 },
    { asset_id: 'asset-beta', decision: 'rejected', note: '光线偏冷', order: 1 },
    { asset_id: 'asset-gamma', tags: ['近景', '暖色'], order: 2 }
];

async function waitFor(predicate, label = '本地模拟请求') {
    for (let index = 0; index < 50; index += 1) {
        if (predicate()) return;
        await new Promise(resolve => setImmediate(resolve));
    }
    assert.fail(`等待${label}超时`);
}

function createHarness(referencesHandler, { search = '' } = {}) {
    const ids = [
        'agentTaskForm', 'agentTaskCreate', 'agentTaskLoad', 'agentTaskRunId', 'agentTaskStatus',
        'agentTaskProject', 'agentTaskGoal', 'agentTaskConfigProfile', 'agentTaskApplyConfiguration',
        'agentTaskReview', 'agentTaskReviewScore', 'agentTaskCandidate',
        'agentTaskClarification', 'agentTaskQuestion', 'agentTaskExports', 'agentTraceStream'
    ];
    const nodes = new Map(ids.map(id => [id, makeNode(id === 'agentTaskForm' ? 'form' : 'div')]));
    const documentListeners = new Map();
    const calls = [];
    let randomIdCalls = 0;

    const document = {
        readyState: 'loading',
        body: makeNode('body'),
        getElementById: id => nodes.get(id) || null,
        createElement: tagName => makeNode(tagName),
        addEventListener(type, handler) { documentListeners.set(type, handler); }
    };
    const window = {
        location: { search },
        crypto: { randomUUID: () => `test-id-${++randomIdCalls}` },
        setInterval: () => 0,
        clearInterval: () => {},
        confirm: () => true,
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} }
    };
    const fetch = async (path, options = {}) => {
        calls.push({ path, options });
        if (path === '/api/asset-registry/projects?archived=false&deleted=false') {
            return jsonResponse({ projects: [{ project_id: PROJECT_ID, name: 'MVP2 项目' }] });
        }
        if (path === '/api/agent/config') return jsonResponse({ ready: false });
        return referencesHandler(path, options);
    };

    runInNewContext(source, { document, window, fetch, URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }, URLSearchParams, Blob });

    const harness = {
        nodes,
        calls,
        async init() { await documentListeners.get('DOMContentLoaded')(); },
        panel() { return nodes.get('agentTaskForm').children.at(-1); },
        sessionInput() { return harness.panel().children.find(child => child.tagName === 'INPUT'); },
        cards() { return harness.panel().children.at(-1).children.slice(); },
        button(card, label) { return flatten(card).find(node => node.textContent === label); },
        textarea(card) { return flatten(card).find(node => node.tagName === 'TEXTAREA'); },
        async read(sessionId = SESSION_ID) {
            // QIN-506：项目下拉框不再自动预选（即使只有一个项目），参考读取需要显式项目上下文，
            // 因此这里模拟真实用户先在下拉框选中项目，再读取参考集。
            harness.nodes.get('agentTaskProject').value = PROJECT_ID;
            harness.sessionInput().value = sessionId;
            await harness.panel().children.find(child => child.textContent === '读取参考').emit('click');
            await waitFor(() => /已读取|暂无参考条目|失败/.test(harness.statusNode().textContent));
        },
        // 面板结构固定：标题 / 会话输入 / 读取按钮 / 状态文本 / 卡片列表
        statusNode() { return harness.panel().children[3]; },
        puts() { return harness.calls.filter(call => (call.options.method || 'GET') === 'PUT'); },
        putBodies() { return harness.puts().map(call => JSON.parse(call.options.body)); }
    };
    return harness;
}

function flatten(node) {
    return [node, ...(node.children || []).flatMap(flatten)];
}

function collectText(node) {
    return flatten(node).map(item => item.textContent).join('\n');
}

test('AC1 读取后按 order 渲染参考卡：asset_id、摘要、当前结论与顺序', async () => {
    const harness = createHarness(async path => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) return jsonResponse(referenceSet(DEFAULT_ITEMS));
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();

    const cards = harness.cards();
    assert.equal(cards.length, 3);
    assert.equal(cards[0].dataset.referenceAssetId, 'asset-alpha');
    assert.equal(cards[1].dataset.referenceAssetId, 'asset-beta');
    assert.equal(cards[2].dataset.referenceAssetId, 'asset-gamma');
    assert.match(collectText(cards[0]), /参考素材 asset-alpha/);
    assert.match(collectText(cards[0]), /分值 8\.5/);
    assert.match(collectText(cards[0]), /当前：待定 · 顺序 0/);
    assert.match(collectText(cards[1]), /当前：已拒 · 顺序 1/);
    assert.match(collectText(cards[2]), /近景 · 暖色/, '标签摘要应按顺序拼接');
    assert.match(harness.statusNode().textContent, /已读取 3 条参考 · 版本 3/);
});

test('AC1 选中与拒绝提交准确的 decision、order 与 expected_version', async () => {
    let payload = null;
    const harness = createHarness(async (path, options) => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) return jsonResponse(referenceSet(DEFAULT_ITEMS));
        if (path === `/api/agent/sessions/${SESSION_ID}/references/asset-alpha` && options.method === 'PUT') {
            payload = JSON.parse(options.body);
            return jsonResponse(referenceSet([{ asset_id: 'asset-alpha', decision: payload.decision, order: 0 }], 4));
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();
    await harness.button(harness.cards()[0], '选中').emit('click');
    await waitFor(() => harness.statusNode().textContent.includes('已保存'));
    assert.ok(payload, '选中应提交一次 PUT');

    assert.equal(payload.project_id, PROJECT_ID);
    assert.equal(payload.decision, 'selected');
    assert.equal(payload.note, null);
    assert.equal(payload.order, 0);
    assert.equal(payload.expected_version, 3);
    assert.equal(harness.puts()[0].options.credentials, 'same-origin');
    assert.match(harness.statusNode().textContent, /已保存 asset-alpha：已选 · 顺序 0 · 版本 4/);

    payload = null;
    const cards = harness.cards();
    await harness.button(cards[0], '拒绝').emit('click');
    await waitFor(() => payload !== null && harness.statusNode().textContent.includes('已拒'));
    assert.equal(payload.decision, 'rejected');
    assert.equal(payload.expected_version, 4, '提交应带上一次写入后的最新版本号');
});

test('AC1 备注随保存提交，现有 decision 与顺序保持不变', async () => {
    let payload = null;
    const harness = createHarness(async (path, options) => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) {
            return jsonResponse(referenceSet([{ asset_id: 'asset-beta', decision: 'selected', note: '光线偏冷', order: 1 }]));
        }
        if (path === `/api/agent/sessions/${SESSION_ID}/references/asset-beta` && options.method === 'PUT') {
            payload = JSON.parse(options.body);
            return jsonResponse(referenceSet([{ asset_id: 'asset-beta', decision: payload.decision, note: payload.note, order: payload.order }], 5));
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();
    assert.equal(harness.textarea(harness.cards()[0]).value, '光线偏冷');
    harness.textarea(harness.cards()[0]).value = '改为暖色侧光';
    await harness.button(harness.cards()[0], '保存备注').emit('click');
    await waitFor(() => payload !== null);

    assert.equal(payload.note, '改为暖色侧光');
    assert.equal(payload.decision, 'selected');
    assert.equal(payload.order, 1);
    assert.equal(payload.project_id, PROJECT_ID);
});

test('AC1 备注超过 500 字拒绝提交且不发请求', async () => {
    const harness = createHarness(async path => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) {
            return jsonResponse(referenceSet([{ asset_id: 'asset-note' }]));
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();
    harness.textarea(harness.cards()[0]).value = '备注'.repeat(251); // 502 字
    await harness.button(harness.cards()[0], '保存备注').emit('click');
    await waitFor(() => harness.statusNode().textContent.includes('超过'));

    assert.equal(harness.puts().length, 0, '超长备注不得提交');
    assert.match(harness.statusNode().textContent, /备注超过 500 字，已拒绝提交（当前 502 字）/);
    assert.equal(harness.cards().length, 1, '拒绝后卡片仍在，不白屏');
});

test('AC1 下移只重排受影响条目并逐条提交新 order', async () => {
    const orders = { 'asset-alpha': 0, 'asset-beta': 1, 'asset-gamma': 2 };
    let version = 3;
    const snapshot = () => referenceSet([
        { asset_id: 'asset-alpha', order: orders['asset-alpha'] },
        { asset_id: 'asset-beta', order: orders['asset-beta'] },
        { asset_id: 'asset-gamma', order: orders['asset-gamma'] }
    ], version);
    const harness = createHarness(async (path, options) => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references` && (options.method || 'GET') === 'GET') return jsonResponse(snapshot());
        if ((options.method || 'GET') === 'PUT') {
            const assetId = decodeURIComponent(path.split('/').pop());
            const body = JSON.parse(options.body);
            orders[assetId] = body.order;
            version += 1;
            return jsonResponse(snapshot());
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();
    await harness.button(harness.cards()[0], '下移').emit('click');
    await waitFor(() => harness.puts().length === 2);

    assert.deepEqual(harness.puts().map(call => call.path), [
        `/api/agent/sessions/${SESSION_ID}/references/asset-beta`,
        `/api/agent/sessions/${SESSION_ID}/references/asset-alpha`
    ]);
    assert.deepEqual(harness.putBodies().map(body => [body.order, body.expected_version]), [[0, 3], [1, 4]]);
    assert.deepEqual(harness.cards().map(card => card.dataset.referenceAssetId), ['asset-beta', 'asset-alpha', 'asset-gamma']);
    assert.deepEqual(harness.putBodies().map(body => body.decision), ['pending', 'pending'], '移动不得改写策展结论');
});

test('AC1 首末卡片的移动按钮禁用，缺失会话与项目时不发请求', async () => {
    const harness = createHarness(async path => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) {
            return jsonResponse(referenceSet([{ asset_id: 'asset-alpha', order: 0 }, { asset_id: 'asset-beta', order: 1 }]));
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();
    assert.equal(harness.button(harness.cards()[0], '上移').disabled, true);
    assert.equal(harness.button(harness.cards()[0], '下移').disabled, false);
    assert.equal(harness.button(harness.cards()[1], '下移').disabled, true);

    const before = harness.calls.length;
    harness.sessionInput().value = '';
    await harness.panel().children.find(child => child.textContent === '读取参考').emit('click');
    await waitFor(() => harness.statusNode().textContent.includes('请先填写'));
    assert.equal(harness.calls.length, before, '缺会话编号不得发请求');
    assert.match(harness.statusNode().textContent, /请先填写参考会话编号；不会隐式创建会话/);

    harness.nodes.get('agentTaskProject').value = '';
    harness.sessionInput().value = SESSION_ID;
    await harness.panel().children.find(child => child.textContent === '读取参考').emit('click');
    await waitFor(() => harness.statusNode().textContent.includes('请先选择所属项目'));
    assert.equal(harness.calls.length, before, '缺项目上下文时应 fail closed');
    assert.match(harness.statusNode().textContent, /请先选择所属项目；缺少项目上下文不会读取参考集/);
});

test('AC1 URL 查询参数 session_id 预填会话编号', async () => {
    const harness = createHarness(async path => {
        if (path === '/api/agent/sessions/session-from-url/references') return jsonResponse(referenceSet([]));
        throw new Error(`未预期的本地请求：${path}`);
    }, { search: `?project_id=${PROJECT_ID}&session_id=session-from-url` });

    await harness.init();
    assert.equal(harness.sessionInput().value, 'session-from-url');
    await harness.panel().children.find(child => child.textContent === '读取参考').emit('click');
    await waitFor(() => harness.statusNode().textContent.includes('暂无参考条目'));
    assert.ok(harness.calls.some(call => call.path === '/api/agent/sessions/session-from-url/references'));
    assert.match(harness.statusNode().textContent, /暂无参考条目 · 版本 3/);
});

test('AC3 asset_id、标签、备注与分值一律纯文本渲染，不产生标签节点', async () => {
    const injection = '<script>alert(1)</script>';
    const harness = createHarness(async path => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) {
            return jsonResponse(referenceSet([
                { asset_id: injection, tags: ['<img src=x onerror=alert(2)>'], note: '"><svg onload=alert(3)>', order: 0 }
            ]));
        }
        throw new Error(`未预期的本地请求：${path}`);
    });

    await harness.init();
    await harness.read();

    const text = collectText(harness.panel());
    assert.ok(text.includes(injection), '注入串应作为纯文本原样呈现');
    assert.ok(text.includes('<img src=x onerror=alert(2)>'));
    assert.ok(text.includes('"><svg onload=alert(3)>'));
    const htmlCarriers = flatten(harness.panel()).filter(node => typeof node.innerHTML === 'string' && node.innerHTML.includes('alert('));
    assert.deepEqual(htmlCarriers, [], '任何节点都不得通过 innerHTML 承载用户输入');
    assert.equal(harness.textarea(harness.cards()[0]).value, '"><svg onload=alert(3)>');
});

test('AC3 服务端错误信息以纯文本展示，不做 HTML 拼接', async () => {
    const harness = createHarness(async () => errorResponse(500, '<img src=x onerror=alert(9)>服务端异常'));

    await harness.init();
    await harness.read();

    assert.equal(harness.statusNode().textContent, '读取失败：<img src=x onerror=alert(9)>服务端异常；请稍后重试。');
    assert.equal(flatten(harness.panel()).some(node => typeof node.innerHTML === 'string' && node.innerHTML.includes('onerror')), false);
});

test('AC4 GET 404/500、PUT 401/409 均有明确错误态且不白屏', async () => {
    const notFound = createHarness(async () => errorResponse(404, '参考策展会话不存在'));
    await notFound.init();
    await notFound.read();
    assert.match(notFound.statusNode().textContent, /读取失败：参考策展会话不存在（HTTP 404）；不会隐式创建会话/);
    assert.equal(notFound.panel().children.length > 0, true, '面板结构保留，不白屏');

    const serverError = createHarness(async () => errorResponse(500, '服务暂时不可用'));
    await serverError.init();
    await serverError.read();
    assert.match(serverError.statusNode().textContent, /读取失败：服务暂时不可用；请稍后重试/);

    const forbidden = createHarness(async (path, options) => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) return jsonResponse(referenceSet(DEFAULT_ITEMS));
        if (options.method === 'PUT') return errorResponse(401, '未认证');
        throw new Error(`未预期的本地请求：${path}`);
    });
    await forbidden.init();
    await forbidden.read();
    await forbidden.button(forbidden.cards()[0], '选中').emit('click');
    await waitFor(() => forbidden.statusNode().textContent.includes('没有访问权限'));
    assert.match(forbidden.statusNode().textContent, /提交失败：没有访问权限（HTTP 401）；请确认登录后重试/);
    assert.equal(forbidden.cards().length, 3, '失败后仍保留已渲染卡片');

    let reads = 0;
    const conflict = createHarness(async (path, options) => {
        if (path === `/api/agent/sessions/${SESSION_ID}/references`) {
            reads += 1;
            return jsonResponse(referenceSet(DEFAULT_ITEMS, reads === 1 ? 3 : 9));
        }
        if (options.method === 'PUT') return errorResponse(409, '参考集版本已改变，请重新读取后重试');
        throw new Error(`未预期的本地请求：${path}`);
    });
    await conflict.init();
    await conflict.read();
    await conflict.button(conflict.cards()[0], '选中').emit('click');
    await waitFor(() => reads === 2);
    await waitFor(() => conflict.statusNode().textContent.includes('已重新读取'));
    assert.equal(reads, 2, '冲突后应自动重读一次');
    assert.match(conflict.statusNode().textContent, /已重新读取最新参考集/);
    assert.match(conflict.cards()[0] && collectText(conflict.cards()[0]), /顺序 0/);
});

test('AC2 纯原生实现：无框架/CDN/外部 URL，样式沿用既有 token', () => {
    const stripLicenseHeader = value => value.replace(/^\/\*[\s\S]*?\*\//, '');
    const body = stripLicenseHeader(source);
    assert.ok(!/https?:\/\//.test(body), '控制器不得引入外部 URL/CDN');
    assert.ok(!/\brequire\s*\(/.test(body), '不得使用 CommonJS');
    assert.ok(!/^\s*import\s/m.test(body), '不得引入 ES 模块依赖');

    const start = body.indexOf('// ---- MVP2-T7a 参考策展卡');
    const end = body.indexOf('function bind()', start);
    assert.ok(start > 0 && end > start, '应存在参考卡实现区间');
    const section = body.slice(start, end);
    for (const forbidden of ['innerHTML', 'insertAdjacentHTML', 'outerHTML', 'document.write', 'eval(']) {
        assert.equal(section.includes(forbidden), false, `参考卡不得使用 ${forbidden}`);
    }
    for (const token of ['hw-btn', 'gw-type-body-sm', 'border-white/10', 'bg-black/50']) {
        assert.ok(section.includes(token), `应沿用既有样式 token：${token}`);
    }
    assert.ok(section.includes('textContent'), '动态文本必须走 textContent');
});

test('测试文件已登记 tests/frontend/node-files.txt（缺失则 CI 红）', () => {
    const listed = readFileSync(new URL('./node-files.txt', import.meta.url), 'utf8');
    assert.ok(listed.includes('tests/frontend/mvp2-t7a-reference-card.test.mjs'), 'node-files.txt 应登记本测试');
});
