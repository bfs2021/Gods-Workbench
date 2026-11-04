import {readFileSync} from 'node:fs';
import {createContext, runInContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../../web/pages/assets.html', import.meta.url), 'utf8');
const controller = readFileSync(new URL('../../web/js/controllers/assets-controller.js', import.meta.url), 'utf8');
const manager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const managerCss = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.parentElement = null;
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
      const appended = node?.isFragment ? node.children : [node];
      for (const child of appended) {
        child.parentElement = this;
        this.children.push(child);
      }
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  contains(target) { return Boolean(target) && (this === target || this.children.some(child => child.contains?.(target))); }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (selector === '.asset-shell-overview-card' && node.classList.contains('asset-shell-overview-card')) return node;
    }
    return null;
  }
  querySelectorAll(selector) {
    return selector === '[data-asset-board]' ? walk(this).filter(node => node.dataset.assetBoard) : [];
  }
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
  for (const tab of frameTabs) {
    tab.click = () => {
      tab.clickCount += 1;
      for (const candidate of frameTabs) candidate.setAttribute('aria-selected', String(candidate === tab));
    };
  }
  const categories = new FakeElement('nav');
  const overview = new FakeElement('button');
  const overviewCard = new FakeElement('div');
  overviewCard.className = 'asset-shell-overview-card';
  overviewCard.append(overview);
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
  const elements = {assetVaultFrame: frame, assetVaultStatus: status, assetCategories: categories, assetSidebarOverview: overview, assetSidebarOverviewCard: overviewCard};
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
  return {categories, overview, overviewCard, frameTabs, window};
}

test('AC1 总览与新建资源库共处一张卡片，两个按钮动作互不穿透', () => {
  const card = html.match(/<div class="asset-shell-overview-card"[\s\S]*?<\/div>\s*<\/div>/)?.[0] || '';
  assert.ok(card, '缺少总览与新建按钮合一卡片');
  assert.match(card, /id="assetSidebarOverview"[^>]*WorkbenchAssets\.activateAssetBoard\('registry'\)/);
  assert.match(card, /id="assetSidebarCreate" class="hw-btn hw-btn-icon" onclick="WorkbenchAssets\.openCreateAssetLibrary\(\)"/);
  assert.match(html, /#assetSidebarCreate\s*\{[^}]*width:\s*32px;[^}]*flex:\s*0 0 32px/);
  assert.match(controller, /createButtonClickHandler = createButton \? event => event\.stopPropagation\(\) : null/);
  assert.match(card, /data-lucide="plus"/);
});

test('AC2/AC3 导航对齐机架字号与行距，并以无编号树状导轨分组', async () => {
  const ui = loadSidebar([
    ['registry', '资产总览', false, 'library-big'],
    ['scripts', '剧本资产', false, 'file-text'],
    ['projects', '项目制片', true, 'clapperboard'],
    ['assets', '托管资产', false, 'image'],
    ['workflows', '工作流', false, 'workflow'],
    ['canvas-assets', '画布资产', false, 'layout-dashboard'],
    ['prompts', '提示词', false, 'text-cursor-input'],
    ['local', '上传资源', false, 'cloud-upload'],
    ['custom-brand', '品牌素材', false, 'folder'],
    ['source_library_root', '已登记库', false, 'database'],
  ]);
  const groups = ui.categories.children;
  assert.deepEqual(groups.map(group => group.getAttribute('aria-label')), ['系统预设', '用户资源']);
  const systemButtons = walk(groups[0]).filter(node => node.dataset.assetBoardLevel === 'primary');
  const nestedButtons = walk(groups[0]).filter(node => node.dataset.assetBoardLevel === 'nested');
  const userButtons = walk(groups[1]).filter(node => node.dataset.assetBoardLevel === 'primary');
  assert.deepEqual(systemButtons.map(button => button.dataset.assetBoard), ['projects', 'assets', 'canvas-assets', 'prompts', 'local']);
  assert.deepEqual(systemButtons.map(button => button.textContent), ['项目制片', '托管资产', '画布资产', '提示词', '上传资源']);
  assert.deepEqual(nestedButtons.map(button => button.dataset.assetBoard), ['scripts', 'workflows']);
  assert.deepEqual(userButtons.map(button => button.dataset.assetBoard), ['custom-brand', 'source_library_root']);
  assert.ok(walk(groups[0]).filter(node => node.dataset.assetBoard).every(button => !/^\s*\d+\s*$/.test(button.textContent)));
  assert.ok(walk(groups[0]).filter(node => node.dataset.assetBoard).every(button => button.classList.contains('tree-node-card')));
  assert.match(html, /\.asset-shell-nav-button \.asset-shell-nav-label\s*\{[^}]*font-size:\s*var\(--gw-type-body\) !important;[^}]*font-weight:\s*400;[^}]*transform:\s*none !important;/);
  assert.match(html, /\.asset-shell-nav-group\s*\{[^}]*margin:\s*0 0 12px;[^}]*padding:\s*0 0 0 20px/);
  assert.match(html, /#assetCategories\s*\{[^}]*position:\s*relative/);
  assert.match(html, /#assetCategories::before\s*\{[^}]*top:\s*8px; bottom:\s*16px; left:\s*14px; width:\s*1\.5px/);
  assert.match(html, /\.asset-shell-nav-group::before\s*\{\s*content:\s*none;/);
  assert.match(html, /\.asset-shell-nav-sublist\[hidden\]\s*\{\s*display:\s*none !important;/);
  assert.match(html, /\.asset-shell-nav-collapse\s*\{[^}]*width:\s*22px;[^}]*height:\s*22px/);
  assert.match(html, /\.asset-shell-nav-collapse\[aria-expanded="false"\]\s+svg\s*\{[^}]*rotate\(-90deg\)/);
  assert.match(html, /\.asset-shell-nav-group-heading\s*\{[^}]*font-size:\s*var\(--gw-type-body-sm\);[^}]*font-weight:\s*var\(--gw-weight-medium\);[^}]*line-height:\s*var\(--gw-leading-body-sm\)/);
  assert.match(html, /\.asset-shell-nav-button\s*\{[^}]*min-height:\s*42px;[^}]*font-weight:\s*400/);
  assert.match(html, /\.asset-shell-nav-group-items\s*\{[^}]*gap:\s*16px/);
  assert.match(html, /\.asset-shell-nav-sublist\s*\{[^}]*gap:\s*12px; margin:\s*12px[^;]*; padding:\s*2px 0 0 12px/);
  assert.match(html, /\.asset-shell-overview-card\s*\{[^}]*min-height:\s*48px;[^}]*border-radius:\s*12px/);
  assert.match(html, /\.asset-shell-overview-card\.is-active\s*\{[^}]*border-color:/);
  assert.match(html, /#assetVaultSidebar \.asset-shell-nav-button::before\s*\{[^}]*content:\s*"" !important;[^}]*left:\s*-14px;[^}]*width:\s*6px/);
  assert.match(html, /#assetVaultSidebar \.asset-shell-nav-button-sub::before\s*\{[^}]*left:\s*-21px;[^}]*width:\s*6px/);
  assert.match(html, /#assetVaultSidebar \.asset-shell-nav-button \.card-connector-pin\s*\{[^}]*background:\s*#403a2f;[^}]*box-shadow:\s*none;[^}]*opacity:\s*\.55/);
  assert.match(html, /#assetVaultSidebar \.asset-shell-nav-button\.is-active \.card-connector-pin\s*\{[^}]*background:\s*#dfc384;[^}]*box-shadow:\s*0 0 4px 1px[^}]*opacity:\s*1/);
  assert.match(html, /#assetVaultSidebar \.asset-shell-nav-button\s*\{[^}]*overflow:\s*visible !important/);
  assert.match(managerCss, /\.registry-toolbar-shell-actions\s*\{[^}]*display:\s*flex/);
  assert.match(html, /\.asset-shell-nav-empty\s*\{[^}]*font-size:\s*var\(--gw-type-body-sm\)/);
  assert.equal(walk(groups[0]).find(node => node.dataset.assetBoard === 'projects').classList.contains('is-active'), true);
  assert.ok(walk(groups[0]).filter(node => node.dataset.assetBoard).every(button => button.children.some(child => child.className === 'card-connector-pin')));

  const projects = walk(groups[0]).find(node => node.dataset.assetBoard === 'projects');
  const projectChildren = walk(groups[0]).find(node => node.className === 'asset-shell-nav-sublist');
  assert.equal(projects.getAttribute('aria-expanded'), 'true');
  assert.equal(projectChildren.hidden, false);
  projects.listeners.dblclick();
  assert.equal(projectChildren.hidden, true);
  assert.equal(projects.getAttribute('aria-expanded'), 'false');
  projects.listeners.dblclick();
  assert.equal(projectChildren.hidden, false);
  assert.equal(projects.getAttribute('aria-expanded'), 'true');

  const scripts = walk(groups[0]).find(node => node.dataset.assetBoard === 'scripts');
  const scriptsClick = scripts.listeners.click();
  assert.equal(scripts.classList.contains('is-active'), true, '首击立即切换按钮高亮，不等待 iframe 状态回读');
  assert.equal(scripts.getAttribute('aria-current'), 'page');
  assert.equal(projects.classList.contains('is-active'), false);
  await scriptsClick;
  assert.equal(ui.frameTabs.find(tab => tab.dataset.tab === 'scripts').clickCount, 1, '嵌套剧本入口须继续切换原看板');
  assert.equal(ui.frameTabs.find(tab => tab.dataset.tab === 'scripts').getAttribute('aria-selected'), 'true');
});

test('AC1/AC14 总览状态可见，侧栏移除导入入口并把两个操作放在移出分组行末端', () => {
  const ui = loadSidebar([
    ['registry', '资产总览', true, 'library-big'],
    ['projects', '项目制片', false, 'clapperboard'],
    ['assets', '托管资产', false, 'image'],
  ]);
  assert.equal(ui.overview.classList.contains('is-active'), true);
  assert.equal(ui.overview.getAttribute('aria-pressed'), 'true');
  assert.equal(ui.overviewCard.classList.contains('is-active'), true, '总览激活时整张外卡片高亮');
  assert.match(html, /<div id="assetSidebarActionState" hidden aria-hidden="true">[\s\S]*id="btnUnassignedInbox"/);
  assert.doesNotMatch(html, /asset-shell-sidebar-actions/);
  assert.doesNotMatch(html, /onclick="WorkbenchAssets\.uploadAsset\(\)"[^>]*title="导入外部资产文件"/);
  const toolbarStart = manager.indexOf('function renderRegistryToolbar(');
  const toolbarEnd = manager.indexOf('function renderRegistryEditableTags(', toolbarStart);
  const toolbar = manager.slice(toolbarStart, toolbarEnd);
  const groupActions = toolbar.match(/<div class="registry-toolbar-group-actions">([\s\S]*?)<\/div>\s*<\/div>/)?.[1] || '';
  assert.match(groupActions, /data-registry-group-remove[\s\S]*data-vault-shell-unassigned[\s\S]*data-vault-shell-settings/);
  assert.match(manager, /onclick="parent\.GWUnassignedInbox\.open\(\)"/);
  assert.match(manager, /onclick="parent\.WorkbenchAssets\.openAssetSettings\(\)"/);
  assert.match(managerCss, /\.registry-toolbar-group-actions\s*\{[^}]*margin-left:\s*auto/);
  assert.match(controller, /function syncToolbarEntry\(button = entry\(\)\)/);
  assert.match(html, /#assetCategories::before\s*\{[^}]*linear-gradient\(to bottom,#dfc384/);
  assert.match(controller, /className = 'card-connector-pin'/);
  assert.match(html, /#dfc384/);
});

test('AC6/AC7 总览和分类按钮点击时即时同步整卡及按钮选中态', async () => {
  const ui = loadSidebar([
    ['registry', '资产总览', false, 'library-big'],
    ['projects', '项目制片', true, 'clapperboard'],
    ['scripts', '剧本资产', false, 'file-text'],
  ]);
  const activation = ui.window.WorkbenchAssets.activateAssetBoard('registry');
  assert.equal(ui.overviewCard.classList.contains('is-active'), true);
  assert.equal(ui.overview.getAttribute('aria-pressed'), 'true');
  await activation;
  assert.equal(ui.overviewCard.classList.contains('is-active'), true);

  const projects = walk(ui.categories).find(node => node.dataset.assetBoard === 'projects');
  const change = projects.listeners.click();
  assert.equal(projects.classList.contains('is-active'), true, '分类首击时同步选中样式');
  assert.equal(ui.overviewCard.classList.contains('is-active'), false, '离开总览后整卡取消选中态');
  await change;
});

test('AC12/AC13 折叠状态跨分类重渲染保持，右侧小按钮独立切换子项', async () => {
  const ui = loadSidebar([
    ['registry', '资产总览', false, 'library-big'],
    ['projects', '项目制片', true, 'clapperboard'],
    ['scripts', '剧本资产', false, 'file-text'],
    ['assets', '托管资产', false, 'image'],
    ['workflows', '工作流', false, 'workflow'],
  ]);
  const lookup = () => ({
    parent: walk(ui.categories).find(node => node.dataset.assetBoard === 'projects'),
    children: walk(ui.categories).find(node => node.className === 'asset-shell-nav-sublist' && node.getAttribute('aria-label') === '项目制片子项'),
    toggle: walk(ui.categories).find(node => node.dataset.navigationCollapse === 'projects'),
  });
  let {parent, children, toggle} = lookup();
  assert.equal(toggle.getAttribute('aria-controls'), children.id);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  let stoppedPropagation = 0;
  toggle.listeners.click({stopPropagation() { stoppedPropagation += 1; }});
  assert.equal(stoppedPropagation, 1, '右侧控制按钮不能触发父项点击');
  assert.equal(children.hidden, true);
  assert.equal(parent.getAttribute('aria-expanded'), 'false');
  assert.equal(ui.frameTabs.find(tab => tab.dataset.tab === 'projects').clickCount, 0);

  await lookup().parent.listeners.click();
  ({parent, children, toggle} = lookup());
  assert.equal(children.hidden, true, '切换其他看板并重渲染后仍保持收起');
  assert.equal(parent.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-label'), '展开项目制片子项');

  toggle.listeners.click({stopPropagation() { stoppedPropagation += 1; }});
  assert.equal(children.hidden, false);
  assert.equal(parent.getAttribute('aria-expanded'), 'true');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(toggle.getAttribute('aria-label'), '折叠项目制片子项');
  assert.equal(ui.frameTabs.find(tab => tab.dataset.tab === 'projects').clickCount, 0);
});
