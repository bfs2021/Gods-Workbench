import {readFileSync} from 'node:fs';
import {createContext, runInContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../../web/pages/assets.html', import.meta.url), 'utf8');
const controller = readFileSync(new URL('../../web/js/controllers/assets-controller.js', import.meta.url), 'utf8');

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.title = '';
    this.ownText = '';
    this._className = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this._className.split(/\s+/).filter(Boolean));
        const enabled = force === undefined ? !names.has(name) : Boolean(force);
        if (enabled) names.add(name); else names.delete(name);
        this._className = [...names].join(' ');
        return enabled;
      },
      contains: name => this._className.split(/\s+/).includes(name),
    };
  }

  set className(value) { this._className = String(value || ''); }
  get className() { return this._className; }
  set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  append(...nodes) {
    for (const node of nodes) {
      if (node?.isFragment) this.children.push(...node.children);
      else this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  contains(target) { return Boolean(target) && (this === target || this.children.some(child => child.contains?.(target))); }
  querySelector() { return null; }
}

function walk(node) {
  return [node, ...node.children.flatMap(walk)];
}

function boardTab(id, name, active = false, icon = 'folder') {
  const tab = new FakeElement('button');
  tab.dataset.tab = id;
  tab.icon = icon;
  tab.setAttribute('aria-selected', String(active));
  tab.querySelector = selector => selector === 'span'
    ? {textContent: name}
    : selector === '[data-lucide]'
      ? {getAttribute: () => icon}
      : null;
  tab.clickCount = 0;
  tab.click = () => { tab.clickCount += 1; tab.setAttribute('aria-selected', 'true'); };
  return tab;
}

function loadSidebar(rows) {
  const frameTabs = rows.map(([id, name, active, icon]) => boardTab(id, name, active, icon));
  const categories = new FakeElement('nav');
  const overview = new FakeElement('button');
  const status = new FakeElement('div');
  const boardTabs = new FakeElement('div');
  const managerDocument = {
    URL: 'http://localhost/static/asset-manager.html?embedded=1&vault=1',
    readyState: 'complete',
    body: new FakeElement('body'),
    getElementById: id => id === 'assetBoardTabs' ? boardTabs : null,
    querySelectorAll: selector => selector === '#assetBoardTabs .asset-board-tab' ? frameTabs : [],
    querySelector: selector => {
      const tabMatch = selector.match(/data-tab="([^"]+)"/);
      if (tabMatch) return frameTabs.find(tab => tab.dataset.tab === tabMatch[1]) || null;
      return null;
    },
  };
  const frame = new FakeElement('iframe');
  frame.dataset.src = '/static/asset-manager.html?embedded=1&vault=1';
  frame.contentDocument = managerDocument;
  frame.contentWindow = {postMessage() {}};
  const elements = {assetVaultFrame: frame, assetVaultStatus: status, assetCategories: categories, assetSidebarOverview: overview};
  const document = {
    activeElement: null,
    body: new FakeElement('body'),
    documentElement: new FakeElement('html'),
    getElementById: id => elements[id] || null,
    createElement: tag => new FakeElement(tag),
    createDocumentFragment: () => Object.assign(new FakeElement('#fragment'), {isFragment: true}),
    addEventListener() {},
  };
  const location = {href: 'http://localhost/static/pages/assets.html', origin: 'http://localhost', search: '', hash: '', assign() {}};
  const window = {
    addEventListener() {},
    lucide: {createIcons() {}},
    location,
    parent: null,
  };
  window.parent = window;
  window.window = window;
  const context = createContext({
    window, document, location, history: {replaceState() {}},
    URL, URLSearchParams, CSS: {escape: value => String(value)},
    MutationObserver: class { observe() {} disconnect() {} },
    fetch: async () => ({ok: false, status: 401, json: async () => ({detail: {code: 'UNAUTHENTICATED', message: '未登录'}})}),
    setTimeout, clearTimeout, console,
  });
  runInContext(controller, context);
  return {categories, overview, frameTabs, window};
}

test('AC1 总览与新建资源库共处一张卡片，两个按钮动作互不穿透', () => {
  const card = html.match(/<div class="asset-shell-overview-card"[\s\S]*?<\/div>\s*<\/div>/)?.[0] || '';
  assert.ok(card, '缺少总览与新建按钮合一卡片');
  assert.match(card, /id="assetSidebarOverview"[^>]*WorkbenchAssets\.activateAssetBoard\('registry'\)/);
  assert.match(card, /id="assetSidebarCreate"[^>]*event\.stopPropagation\(\);WorkbenchAssets\.openCreateAssetLibrary\(\)/);
  assert.match(card, /data-lucide="plus"/);
});

test('AC2/AC3 动态导航以无编号树状导轨分成系统预设与用户资源', async () => {
  const ui = loadSidebar([
    ['registry', '资产总览', false, 'library-big'],
    ['scripts', '剧本资产', false, 'file-text'],
    ['projects', '项目制片', true, 'clapperboard'],
    ['assets', '托管资产', false, 'image'],
    ['workflows', '工作流', false, 'workflow'],
    ['canvas-assets', '画布资产', false, 'layout-dashboard'],
    ['prompts', '提示词', false, 'text-cursor-input'],
    ['local', '上传资源', false, 'cloud-upload'],
    ['governance', '治理建议', false, 'list-checks'],
    ['custom-brand', '品牌素材', false, 'folder'],
    ['source_library_root', '已登记库', false, 'database'],
  ]);
  const groups = ui.categories.children;
  assert.deepEqual(groups.map(group => group.getAttribute('aria-label')), ['系统预设', '用户资源']);
  const systemButtons = walk(groups[0]).filter(node => node.dataset.assetBoardLevel === 'primary');
  const nestedButtons = walk(groups[0]).filter(node => node.dataset.assetBoardLevel === 'nested');
  const userButtons = walk(groups[1]).filter(node => node.dataset.assetBoardLevel === 'primary');
  assert.deepEqual(systemButtons.map(button => button.dataset.assetBoard), ['projects', 'assets', 'canvas-assets', 'prompts', 'local', 'governance']);
  assert.deepEqual(systemButtons.map(button => button.textContent), ['项目制片', '托管资产', '画布资产', '提示词', '上传资源', '治理建议']);
  assert.deepEqual(nestedButtons.map(button => button.dataset.assetBoard), ['scripts', 'workflows']);
  assert.deepEqual(userButtons.map(button => button.dataset.assetBoard), ['custom-brand', 'source_library_root']);
  assert.ok(walk(groups[0]).filter(node => node.dataset.assetBoard).every(button => !/^\s*\d+\s*$/.test(button.textContent)));
  assert.ok(walk(groups[0]).filter(node => node.dataset.assetBoard).every(button => button.classList.contains('tree-node-card')));
  assert.equal(walk(groups[0]).find(node => node.dataset.assetBoard === 'projects').classList.contains('is-active'), true);
  await walk(groups[0]).find(node => node.dataset.assetBoard === 'scripts').listeners.click();
  assert.equal(ui.frameTabs.find(tab => tab.dataset.tab === 'scripts').clickCount, 1, '嵌套剧本入口须继续切换原看板');
});

test('AC1/AC4 总览状态可见，底栏保留三键横排且高度约 40px', () => {
  const ui = loadSidebar([
    ['registry', '资产总览', true, 'library-big'],
    ['projects', '项目制片', false, 'clapperboard'],
    ['assets', '托管资产', false, 'image'],
  ]);
  assert.equal(ui.overview.classList.contains('is-active'), true);
  assert.equal(ui.overview.getAttribute('aria-pressed'), 'true');
  const footer = html.match(/<div class="asset-shell-sidebar-actions[\s\S]*?<\/div>\s*<\/aside>/)?.[0] || '';
  assert.match(footer, /id="btnUnassignedInbox"/);
  assert.match(footer, /onclick="WorkbenchAssets\.uploadAsset\(\)"[^>]*>[\s\S]*?<span>导入<\/span>/);
  assert.match(footer, /id="assetSidebarSettings"/);
  assert.equal((footer.match(/<button\b/g) || []).length, 3);
  assert.match(html, /\.asset-shell-sidebar-actions\s*\{[^}]*height:\s*40px/);
  assert.match(html, /\.asset-shell-nav-group::before[^}]*#dfc384/);
  assert.match(controller, /className = 'card-connector-pin'/);
  assert.match(html, /#dfc384/);
});
