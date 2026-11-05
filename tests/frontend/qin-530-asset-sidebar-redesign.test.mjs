import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import {createContext, runInContext} from 'node:vm';

const html = readFileSync(new URL('../../web/pages/assets.html', import.meta.url), 'utf8');
const controller = readFileSync(new URL('../../web/js/controllers/assets-controller.js', import.meta.url), 'utf8');
const manager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const managerCss = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');

function functionSource(name, source = manager) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0, '函数应存在：' + name);
  let depth = 0;
  for (let index = source.indexOf('{', start); index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail('函数未闭合：' + name);
}

test('QIN-560 AC1/AC2: 外壳不再渲染第二套资产树导航', () => {
  assert.doesNotMatch(html, /id="assetVaultSidebar"|id="assetCategories"|asset-shell-nav-(?:button|group)/);
  assert.doesNotMatch(controller, /syncCategories|assetCategories|asset-shell-nav-/);
  assert.match(html, /<main id="assetVaultStage">[\s\S]*?<iframe id="assetVaultFrame"/);
  assert.match(manager, /renderAssetTreePanel/);
});

test('QIN-560 AC3: QIN-544 内嵌树保留双标签、树选择与面包屑', () => {
  const panel = functionSource('renderAssetTreePanel');
  assert.match(panel, /data-asset-tree-tab="system"[^>]*>系统资源/);
  assert.match(panel, /data-asset-tree-tab="mine"[^>]*>我的资源/);
  assert.match(functionSource('activateAssetTreeTab'), /assetTreeTab = next/);
  assert.match(manager, /function selectAssetTreeNode\(/);
  assert.match(manager, /function handleAssetTreeCrumbClick\(/);
  assert.match(manager, /data-asset-tree-crumb/);
});

test('QIN-560 AC3: 点击单栏树入口切换托管、上传与用户资源库看板', () => {
  const activatedBoardIds = [];
  const context = createContext({
    assetTreeSelectedKey: '',
    activatedBoardIds,
    assetBoardById: id => ({id}),
    activateAssetBoard: id => activatedBoardIds.push(id),
  });
  runInContext([
    functionSource('handleAssetTreeNodeClick'),
    functionSource('selectAssetTreeNode'),
  ].join('\n'), context);

  for (const boardId of ['assets', 'local', 'library_example', 'source_library_example']) {
    const key = `mine:board:${boardId}`;
    const row = {dataset: {assetTreeNode: key}};
    context.event = {
      target: {
        closest: selector => selector === '[data-asset-tree-node]' ? row : null,
      },
    };
    assert.equal(runInContext('handleAssetTreeNodeClick(event)', context), true);
    assert.equal(runInContext('assetTreeSelectedKey', context), key);
    assert.equal(activatedBoardIds.at(-1), boardId);
  }
  assert.deepEqual(activatedBoardIds, ['assets', 'local', 'library_example', 'source_library_example']);
});

test('QIN-560 AC3: 上传资源页返回根面包屑并清除 registry 筛选和旧选中态', async () => {
  const activatedBoardIds = [];
  const refreshCalls = [];
  const renderCalls = [];
  const context = createContext({
    assetTreeSelectedKey: 'mine:board:local',
    registryProjectFilterId: 'project-r3',
    registryProjectEntityFilter: { projectId: 'project-r3', entityTypes: new Set(['scene']), entityId: 'scene-r3' },
    registryNavFolder: { rootId: 'root-r3', path: 'characters/lead' },
    registryRootId: 'root-r3',
    activeTab: 'local',
    registryGroupSimilar: false,
    activateAssetBoard: id => activatedBoardIds.push(id),
    refreshRegistryAssets: force => { refreshCalls.push(force); return Promise.resolve(); },
    render: () => renderCalls.push('render'),
  });
  runInContext([
    functionSource('handleAssetTreeCrumbClick'),
    functionSource('handleLocalUploadBackClick'),
  ].join('\n'), context);
  const button = {};
  context.event = {
    target: {
      closest: selector => selector === '[data-localup-back-to-tree]' ? button : null,
    },
  };

  assert.equal(runInContext('handleLocalUploadBackClick(event)', context), true);
  assert.equal(runInContext('assetTreeSelectedKey', context), '');
  assert.equal(runInContext('registryProjectFilterId', context), '');
  assert.equal(runInContext('registryProjectEntityFilter', context), null);
  assert.equal(runInContext('registryNavFolder.rootId', context), '');
  assert.equal(runInContext('registryNavFolder.path', context), '');
  assert.equal(runInContext('registryRootId', context), '');
  assert.equal(runInContext('activeTab', context), 'registry');
  assert.deepEqual(refreshCalls, [false]);
  assert.deepEqual(activatedBoardIds, []);
  await Promise.resolve();
  assert.equal(renderCalls.length, 2, '根面包屑立即渲染，并在 registry 请求完成后再次渲染');
  context.event.target.closest = () => null;
  assert.equal(runInContext('handleLocalUploadBackClick(event)', context), false);
});

test('QIN-560 AC3: 未归属状态桥与 iframe 状态回写仍保留', () => {
  assert.match(html, /<div id="assetSidebarActionState" hidden aria-hidden="true">[\s\S]*?id="btnUnassignedInbox"/);
  assert.match(controller, /function syncToolbarEntry\(button = entry\(\)\)/);
  assert.match(controller, /function mirrorFrameDegradation\(\)/);
  assert.match(controller, /function openRequestedAsset\(\)/);
  assert.match(controller, /waitForControl\('#assetBoardTabs \[data-tab="registry"\]'\)/);
});

test('QIN-560 AC4: 主内容区占满工作区且没有外壳栏布局残留', () => {
  assert.match(html, /#assetVaultStage \{ flex: 1; min-width: 0; min-height: 0; display: flex/);
  assert.match(html, /#assetVaultFrame \{ flex: 1; width: 100%;/);
  assert.match(html, /class="asset-shell-workspace flex-1 flex overflow-hidden min-h-0"/);
  assert.doesNotMatch(html, /#assetVaultSidebar|#assetCategories/);
});

test('QIN-560 AC7: 创建与挂载操作使用共享硬件按钮类', () => {
  const createButton = html.match(/<button type="button" id="assetSidebarCreate"[\s\S]*?<\/button>/)?.[0] || '';
  assert.match(createButton, /class="hw-btn hw-btn-icon shrink-0"/);
  assert.match(createButton, /WorkbenchAssets\.openCreateAssetLibrary\(\)/);
  assert.match(controller, /async function openCreateAssetLibrary\(\)/);
  assert.match(manager, /parent\?\.document\?\.getElementById\('assetSidebarCreate'\)/);
  assert.match(functionSource('renderAssetTreePanel'), /class="hw-btn hw-btn-gold" type="button" data-asset-tree-mount aria-label="挂载文件夹"/);
  assert.match(managerCss, /\.asset-tree-footer \.hw-btn \{ width:100%; \}/);
});
