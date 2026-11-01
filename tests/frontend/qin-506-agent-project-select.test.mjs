/**
 * QIN-506 回归：智能体项目下拉框必须只有一个加载与赋值负责人。
 *
 * 覆盖场景：
 * 1. 无 URL project_id 时，即使只有一个项目也保持未选中（AC1）；
 * 2. 用户显式选择项目后，迟到的项目列表响应不得清除或替换该选择（AC2）；
 * 3. URL 带有效 project_id 时预选并同步上下文，无效 ID 不形成上下文（AC3）；
 * 4. 两个控制器请求的两种完成顺序得到同一最终选择，回归“最后写入者胜出”的竞态（AC4）。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createContext, runInContext } from 'node:vm';

const TASKS_SOURCE = readFileSync(new URL('../../web/js/controllers/agent-tasks.js', import.meta.url), 'utf8');
const CONTROLLER_SOURCE = readFileSync(new URL('../../web/js/controllers/agents-controller.js', import.meta.url), 'utf8');
const PROJECTS_ROUTE = '/api/asset-registry/projects?archived=false&deleted=false';
const SOLO_PROJECT = [{ project_id: 'projSolo', name: '唯一工程' }];
const TWO_PROJECTS = [
  { project_id: 'projA', name: '工程 A' },
  { project_id: 'projB', name: '工程 B' }
];

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
    innerHTML: '',
    options: [],
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    emit(type, event = {}) {
      for (const handler of listeners.get(type) || []) {
        handler({ ...event, target: node, preventDefault: event.preventDefault || (() => {}) });
      }
    },
    appendChild(child) {
      child.parentElement = node;
      node.children.push(child);
      return child;
    },
    prepend(child) {
      child.parentElement = node;
      node.children.unshift(child);
      return child;
    },
    insertBefore(child, reference) {
      const index = reference ? node.children.indexOf(reference) : -1;
      if (index < 0) node.children.unshift(child);
      else node.children.splice(index, 0, child);
      child.parentElement = node;
      return child;
    },
    replaceChildren(...children) {
      node.children = [];
      for (const child of children) node.appendChild(child);
    },
    remove() {
      if (!node.parentElement) return;
      node.parentElement.children = node.parentElement.children.filter(child => child !== node);
      node.parentElement = null;
    },
    setAttribute(key, value) { node[key] = String(value); },
    removeAttribute(key) { delete node[key]; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    click() { node.clicked = true; },
    focus() {}
  };
  Object.defineProperty(node, 'firstChild', { get: () => node.children[0] || null });
  Object.defineProperty(node, 'firstElementChild', { get: () => node.children[0] || null });
  return node;
}

/**
 * 用真实 DOM 语义模拟 select：写入 innerHTML 重建 options 并把选中项复位到第一项，
 * 给 value 赋值时匹配不到选项就落回“未选中”。这样才能真实复现“后写入者清除选择”。
 */
function makeSelectNode() {
  const node = makeNode('select');
  let options = [{ value: '', text: '读取项目中…' }];
  let selectedIndex = 0;

  Object.defineProperty(node, 'options', { get: () => options });
  Object.defineProperty(node, 'selectedIndex', {
    get: () => selectedIndex,
    set: next => { selectedIndex = Number(next); }
  });
  Object.defineProperty(node, 'value', {
    get: () => (selectedIndex >= 0 && options[selectedIndex] ? options[selectedIndex].value : ''),
    set: next => {
      const target = String(next ?? '');
      selectedIndex = options.findIndex(option => option.value === target);
    }
  });
  Object.defineProperty(node, 'innerHTML', {
    get: () => node.serializedOptions || '',
    set: html => {
      node.serializedOptions = String(html);
      options = [...String(html).matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map(match => ({
        value: match[1],
        text: match[2]
      }));
      if (!options.length) options = [{ value: '', text: '' }];
      selectedIndex = 0;
    }
  });
  return node;
}

function jsonResponse(value) {
  return {
    ok: true,
    status: 200,
    headers: { get: name => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => value
  };
}

function deferred() {
  let settle;
  const promise = new Promise(resolve => { settle = resolve; });
  return { promise, resolve: settle };
}

const flush = async (rounds = 16) => {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
};

function createHarness({ search = '', projects = TWO_PROJECTS, agentConfigReady = false } = {}) {
  const nodes = new Map();
  const select = makeSelectNode();
  nodes.set('agentTaskProject', select);

  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id === 'agentTaskForm' ? 'form' : 'div'));
    return nodes.get(id);
  };

  const documentHandlers = [];
  const document = {
    readyState: 'loading',
    body: makeNode('body'),
    getElementById: getNode,
    createElement: tagName => makeNode(tagName),
    addEventListener(type, handler) { if (type === 'DOMContentLoaded') documentHandlers.push(handler); },
    querySelectorAll: () => [],
    querySelector: () => null,
    activeElement: null
  };

  const projectCalls = [];
  const calls = [];

  const fetchMock = async (path, options = {}) => {
    const route = String(path);
    calls.push({ path: route, options });
    if (route === PROJECTS_ROUTE) {
      const gate = deferred();
      projectCalls.push(gate);
      return gate.promise;
    }
    if (route === '/api/agent/config') {
      return jsonResponse(agentConfigReady
        ? {
          ready: true,
          profiles: [
            { config_snapshot_id: 'snap-1', provider_id: 'fake', model: 'fake-model', mode: 'approval' }
          ]
        }
        : { ready: false, profiles: [] });
    }
    if (route === '/api/chat/config') return jsonResponse({ configured: false, providers: [] });
    if (route === '/api/agent/runs' && (options.method || 'GET') === 'POST') {
      return jsonResponse({ job_id: 'job_agent_run-x', run_id: 'run-x', version: 1, state: 'accepted' });
    }
    if (route.startsWith('/api/jobs/')) {
      return jsonResponse({
        job_id: 'job_agent_run-x',
        state: 'accepted',
        poll_hint: route,
        agent_status: 'queued',
        run_id: 'run-x',
        version: 1,
        stage: null
      });
    }
    return {
      ok: false,
      status: 404,
      headers: { get: () => 'application/json' },
      json: async () => ({ detail: { code: 'NOT_FOUND', message: 'not found' } })
    };
  };

  const storage = new Map();
  const localStorage = {
    getItem: key => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: key => storage.delete(key)
  };

  const location = {
    search,
    origin: 'http://localhost',
    pathname: '/static/pages/agents.html',
    href: `http://localhost/static/pages/agents.html${search}`,
    hash: ''
  };
  const history = {
    replaceState(_state, _title, url) {
      location.href = String(url);
      location.search = new URL(String(url), location.origin).search;
    }
  };

  const windowObject = {
    location,
    history,
    localStorage,
    crypto: { randomUUID: () => 'qin-506-id' },
    setInterval: () => 1,
    clearInterval: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
    lucide: { createIcons: () => {} },
    confirm: () => true,
    AuraTraceBus: { history: () => [], subscribe: () => () => {}, publish: () => {} }
  };

  const sandbox = {
    window: windowObject,
    document,
    localStorage,
    location,
    history,
    fetch: fetchMock,
    URL: globalThis.URL,
    URLSearchParams: globalThis.URLSearchParams,
    AbortSignal: { timeout: () => undefined },
    CustomEvent: class { constructor(type, detail) { this.type = type; this.detail = detail; } },
    Blob: globalThis.Blob,
    alert: () => {},
    confirm: () => true,
    setTimeout: (callback, delay) => setTimeout(callback, Math.min(Number(delay) || 0, 1)),
    clearTimeout: handle => clearTimeout(handle)
  };

  const context = createContext(sandbox);
  // 与 web/pages/agents.html 的脚本顺序一致：先 agents-controller.js，再 agent-tasks.js。
  runInContext(CONTROLLER_SOURCE, context);
  runInContext(TASKS_SOURCE, context);

  return {
    context,
    select,
    projectCalls,
    calls,
    storage,
    window: windowObject,
    dispatchReady() { for (const handler of documentHandlers) handler(); },
    /** 按调用序号结算项目列表响应，用于构造不同完成顺序。 */
    resolveProjects(index) {
      assert.ok(projectCalls[index], `第 ${index} 个项目请求尚不存在`);
      projectCalls[index].resolve(jsonResponse({ projects }));
    },
    projectPostBody() {
      const call = calls.find(item => item.path === '/api/agent/runs' && item.options?.method === 'POST');
      return call ? JSON.parse(call.options.body) : null;
    },
    selectProject(projectId) {
      select.value = projectId;
      select.emit('change');
    }
  };
}

test('默认不预选：无 URL project_id 时即使只有一个项目下拉框仍保持空白', async () => {
  const harness = createHarness({ projects: SOLO_PROJECT });
  harness.dispatchReady();
  await flush();

  assert.equal(harness.projectCalls.length, 2, '两个控制器都会请求项目列表');
  harness.resolveProjects(0);
  harness.resolveProjects(1);
  await flush();

  assert.equal(harness.select.options.length, 2, '占位项加唯一项目应可选');
  assert.ok(harness.select.innerHTML.includes('projSolo'), '唯一项目仍必须出现在选项里');
  assert.equal(harness.select.value, '', '单项目不得自动选中');
  assert.equal(harness.context.window.WorkbenchAgents.getProjectId(), null, '不得形成项目上下文');
});

test('两种响应完成顺序得到同一结论：默认保持未选中', async () => {
  for (const order of [[0, 1], [1, 0]]) {
    const harness = createHarness({ projects: SOLO_PROJECT });
    harness.dispatchReady();
    await flush();
    harness.resolveProjects(order[0]);
    await flush();
    harness.resolveProjects(order[1]);
    await flush();
    assert.equal(harness.select.value, '', `响应顺序 ${order.join('→')} 时仍应保持未选中`);
  }
});

test('用户显式选择后，迟到的项目列表响应不得清除或替换选择', async () => {
  const harness = createHarness({ projects: TWO_PROJECTS, agentConfigReady: true });
  harness.dispatchReady();
  await flush();

  // 先结算加载器自己的请求，让选项真正可用（第二项请求仍挂起，模拟晚到响应）。
  harness.resolveProjects(1);
  await flush();
  assert.ok(harness.select.options.length >= 3, '项目选项应已就绪');

  harness.selectProject('projB');
  assert.equal(harness.context.window.WorkbenchAgents.getProjectId(), 'projB');

  harness.resolveProjects(0);
  await flush();
  assert.equal(harness.select.value, 'projB', '晚到响应不得清除用户选择');
  assert.equal(harness.context.window.WorkbenchAgents.getProjectId(), 'projB', '项目上下文应保持用户选择');

  // 创建任务必须携带所选 project_id。
  harness.select.parentElement = null;
  const form = harness.context.document.getElementById('agentTaskForm');
  harness.context.document.getElementById('agentTaskGoal').value = 'QIN-506 上下文校验';
  form.emit('submit');
  await flush();
  const body = harness.projectPostBody();
  assert.ok(body, '应发出创建任务请求');
  assert.equal(body.project_id, 'projB', '创建任务请求必须携带用户所选 project_id');
});

test('URL 带有效 project_id 时预选并同步上下文（两种响应顺序一致）', async () => {
  for (const order of [[0, 1], [1, 0]]) {
    const harness = createHarness({ search: '?project_id=projA', projects: TWO_PROJECTS });
    harness.dispatchReady();
    await flush();
    harness.resolveProjects(order[0]);
    await flush();
    harness.resolveProjects(order[1]);
    await flush();
    assert.equal(harness.select.value, 'projA', `响应顺序 ${order.join('→')} 时应预选 URL 项目`);
    assert.equal(harness.context.window.WorkbenchAgents.getProjectId(), 'projA', 'URL 有效项目应形成上下文');
  }
});

test('URL 带无效 project_id 时不预选且不形成项目上下文', async () => {
  const harness = createHarness({ search: '?project_id=projMissing', projects: TWO_PROJECTS });
  harness.dispatchReady();
  await flush();
  harness.resolveProjects(0);
  harness.resolveProjects(1);
  await flush();

  assert.equal(harness.select.value, '', '无效 ID 不得被绑定');
  assert.ok(harness.select.innerHTML.includes('projA'), '有效项目仍应可选');
  assert.equal(harness.context.window.WorkbenchAgents.getProjectId(), null, '无效 ID 不得形成项目上下文');
});

test('控制器不再重绘或写入项目下拉框（单负责人结构守卫）', () => {
  assert.doesNotMatch(CONTROLLER_SOURCE, /renderProjectOptions/, '控制器不得保留重绘下拉框的实现');
  assert.doesNotMatch(
    CONTROLLER_SOURCE,
    /agentTaskProject[\s\S]{0,200}?\.value\s*=/,
    '控制器不得写入项目下拉框的值'
  );
  assert.match(
    TASKS_SOURCE,
    /const matched = projects\.find\(item => String\(item\.project_id\) === \(currentProjectId \|\| urlProjectId\)\)/,
    '项目下拉框的校验与赋值应集中在 agent-tasks.js 的 loadProjects()'
  );
});
