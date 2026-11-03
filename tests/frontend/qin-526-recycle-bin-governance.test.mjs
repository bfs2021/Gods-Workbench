import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const shell = readFileSync(new URL('../../web/js/core/workbench-shell.js', import.meta.url), 'utf8');
const projects = readFileSync(new URL('../../web/pages/projects.html', import.meta.url), 'utf8');
const assetManager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const recycleRoute = readFileSync(new URL('../../gw/api/routes_asset_registry.py', import.meta.url), 'utf8');
const sharedShellPages = ['agents.html', 'assets.html', 'collab.html', 'index.html', 'production.html', 'projects.html', 'settings.html', 'storyboard.html', 'workflow.html', 'workshop.html'];

test('顶栏垃圾桶是当前页按钮，缺少抽屉时由共享壳层装载', () => {
  assert.match(shell, /<button type="button" id="topbarUnifiedTrashBtn"/);
  assert.doesNotMatch(shell, /href="projects\.html\?openTrash=1"/);
  assert.match(shell, /fetch\('\/static\/pages\/projects\.html'/);
  assert.match(shell, /document\.importNode\(node, true\)/);
  assert.match(shell, /document\.addEventListener\('click',[\s\S]*?#topbarUnifiedTrashBtn[\s\S]*?}, true\);/);
  assert.match(projects, /id="topbarUnifiedTrashBtn"/);
});

test('回收站顶栏同排保留刷新、改目录、清空占位三键', () => {
  const toolbar = projects.match(/<div class="gw-trash-toolbar"[\s\S]*?<\/div>/)?.[0] || '';
  assert.match(toolbar, /data-gw-recycler-refresh/);
  assert.match(toolbar, /data-gw-recycler-change/);
  assert.match(toolbar, /data-gw-recycler-clear/);
  assert.doesNotMatch(toolbar, /onclick=/);
  assert.match(shell, /refreshGlobalTrash/);
  assert.match(shell, /清空功能尚未开放/);
  assert.doesNotMatch(projects, /高级审计/);
  assert.doesNotMatch(projects, /刷新回收池/);
});

test('所有页面均引用当前共享壳层缓存版本', () => {
  for (const page of sharedShellPages) {
    const html = readFileSync(new URL(`../../web/pages/${page}`, import.meta.url), 'utf8');
    assert.match(html, /workbench-shell\.js\?v=20261103-qin526-recycler-qa/);
  }
});

test('目录设置页不再渲染回收站目录卡片', () => {
  const renderer = assetManager.match(/function renderGlobalDirectorySettings\(\)\{([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(renderer);
  assert.doesNotMatch(renderer, /回收站目录|storageDir_trash/);
});

test('目录提示回显当前配置，并在双击路径按钮时走 250ms 判定', () => {
  assert.match(shell, /当前回收站路径：\$\{effective\}（双击按钮可恢复默认）/);
  assert.match(shell, /settings\.dirs\?\.trash/);
  assert.match(shell, /event\.detail >= 2/);
  assert.match(shell, /setTimeout\([\s\S]*?\}, 250\)/);
  assert.match(shell, /确认恢复默认回收站路径/);
});

test('选择目录先登记允许根，再以 expected_version CAS 保存 dirs.trash', () => {
  const addRoot = shell.indexOf("storageJson('/api/storage-settings/roots', {");
  const patchSettings = shell.indexOf("storageJson('/api/storage-settings', {");
  assert.ok(addRoot >= 0 && patchSettings > addRoot);
  assert.match(shell, /expected_version: roots\.revision/);
  assert.match(shell, /trash: path/);
  assert.match(shell, /expected_version: current\.revision/);
  assert.match(shell, /ALLOWED_ROOT_ALREADY_REGISTERED/);
});

test('清空占位只展示说明，不调用物理清理接口', () => {
  const toolbar = projects.match(/<div class="gw-trash-toolbar"[\s\S]*?<\/div>/)?.[0] || '';
  assert.match(shell, /清空功能尚未开放/);
  assert.doesNotMatch(shell, /DELETE\s+\/api\/asset-registry\/recycle-bin/);
});

test('首次软删除先走默认或自定义设置引导，并用独立标记挡住旧免确认路径', () => {
  assert.match(assetManager, /ASSET_REGISTRY_RECYCLE_SETUP_KEY = 'gw_recycler_setup_done'/);
  const openRecycle = assetManager.indexOf('async function openRegistryRecycleDialog(');
  const setupGate = assetManager.indexOf('if(!readStoredBoolean(ASSET_REGISTRY_RECYCLE_SETUP_KEY, false))', openRecycle);
  const skipConfirm = assetManager.indexOf('if(readStoredBoolean(ASSET_REGISTRY_RECYCLE_CONFIRM_KEY, false))', openRecycle);
  assert.ok(openRecycle >= 0 && setupGate > openRecycle && skipConfirm > setupGate);
  assert.match(assetManager, /data-registry-recycler-setup-default/);
  assert.match(assetManager, /data-registry-recycler-setup-custom/);
  assert.match(assetManager, /changeRecyclerDirectory\?\.\(\{quiet:true\}\)/);
  assert.match(assetManager, /gw_recycler_mode/);
  assert.match(assetManager, /assetManagerApi\.deleteRegistryAsset\(\s*assetId,[\s\S]*?['"]X-GW-Recycle-Mode['"]:\s*recyclerMode/);
  assert.match(recycleRoute, /request_payload\.get\("recycler_mode"\) or x_gw_recycler_mode or "managed"/);
  assert.match(recycleRoute, /recycler_mode=recycler_mode/);
});
