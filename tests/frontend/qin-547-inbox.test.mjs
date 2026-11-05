// QIN-547: 收集箱——合并托管资产与上传资源为默认收集文件夹
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');

function functionSource(name){
    const marker = `function ${name}(`;
    const start = source.indexOf(marker);
    assert.ok(start >= 0, `函数应存在：${name}`);
    let depth = 0;
    for(let index = source.indexOf('{', start); index < source.length; index += 1){
        if(source[index] === '{') depth += 1;
        else if(source[index] === '}'){
            depth -= 1;
            if(depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`函数未闭合：${name}`);
}

function boardSource(){
    const start = source.indexOf('const SYSTEM_ASSET_BOARDS = [');
    const end = source.indexOf('];', start);
    assert.ok(start >= 0 && end > start, '系统看板清单应存在');
    return source.slice(start, end);
}

test('AC4: 左栏不再有「托管资产」「上传资源」入口', () => {
    const boards = boardSource();
    assert.ok(!boards.includes("id:'assets'"), '托管资产看板入口应移除');
    assert.ok(!boards.includes("id:'local'"), '上传资源看板入口应移除');
    assert.ok(!boards.includes('托管资产'), '托管资产文案不得留在左栏看板');
    assert.ok(!boards.includes('上传资源'), '上传资源文案不得留在左栏看板');
    const rackTabs = source.match(/const SYSTEM_BOARD_RACK_TABS = \[[^\]]*\];/)?.[0] || '';
    assert.ok(!rackTabs.includes("'assets'") && !rackTabs.includes("'local'"), '机架标签同步移除两个入口');
});

test('AC1/AC5: 收集箱在「我的资源」第一位，带未读角标', () => {
    const mineTree = functionSource('renderAssetTreeMineTree');
    assert.match(mineTree, /mine:inbox/, '收集箱节点 key 应存在');
    assert.match(mineTree, /name:'收集箱'/, '收集箱文案应存在');
    assert.ok(mineTree.indexOf('mine:inbox') < mineTree.indexOf('assetTreeRootsList()'), '收集箱排在挂载文件夹之前');
    assert.match(mineTree, /badge:assetInboxUnreadCount\(\)/, '未读数量应作为角标传入');
    assert.match(functionSource('assetInboxUnreadCount'), /unread_count/, '角标读取后端未读数');
    const nodeStart = source.indexOf('function renderAssetTreeNode(');
    const nodeBody = source.slice(nodeStart, source.indexOf('\nfunction ', nodeStart + 10));
    assert.match(nodeBody, /asset-tree-badge/, '节点渲染应输出角标元素');
    assert.match(styles, /\.asset-tree-badge\s*\{/, '角标样式应存在');
});

test('AC1/AC2: 收集箱视图读取真实接口并区分三类来源', () => {
    assert.match(functionSource('loadAssetInbox'), /'\/api\/asset-inbox\?limit=500'/, '列表走收集箱接口');
    assert.match(functionSource('loadAssetInbox'), /inboxSettings = data\.settings/, '设置随列表一起读取');
    assert.match(functionSource('renderInboxSourceChips'), /collect_inbox/, '浏览器收集来源应可筛选');
    assert.match(functionSource('renderInboxSourceChips'), /asset_library/, '托管资产来源应可筛选');
    assert.match(functionSource('renderInboxManager'), /renderInboxCard/, '收集箱素材应渲染卡片');
    assert.match(functionSource('openAssetInbox'), /activeTab = 'inbox'/, '进入收集箱切换视图');
    assert.match(functionSource('selectAssetTreeNode'), /openAssetInbox\(\)/, '树节点点击应打开收集箱');
    assert.match(source, /else if\(activeTab === 'inbox'\) renderInboxManager\(\);/, '主渲染分发应接入收集箱');
});

test('AC1: 设置里可改位置并提示是否迁移已有文件', () => {
    const panel = functionSource('renderInboxSettingsPanel');
    assert.match(panel, /data-inbox-path/, '位置输入框应存在');
    assert.match(panel, /data-inbox-migrate/, '迁移勾选项应存在');
    assert.match(panel, /迁移到新位置/, '迁移提示文案应存在');
    const save = functionSource('saveAssetInboxSettings');
    assert.match(save, /'\/api\/asset-inbox\/settings'/, '保存位置走收集箱设置接口');
    assert.match(save, /method:'PUT'/, '保存位置使用 PUT');
    assert.match(save, /migrate/, '请求体携带迁移开关');
});

test('AC2/AC3: 上传落收集箱、迁移托管资产有独立入口', () => {
    const upload = functionSource('uploadAssetInboxFiles');
    assert.match(upload, /FormData/, '上传使用 multipart');
    assert.match(upload, /'\/api\/asset-inbox\/upload'/, '上传走收集箱接口');
    const migrate = functionSource('migrateAssetLibraryToInbox');
    assert.match(migrate, /'\/api\/asset-inbox\/migrate'/, '迁移走收集箱迁移接口');
    assert.match(migrate, /moved_files/, '迁移结果回显迁入文件数');
    assert.match(functionSource('renderInboxManager'), /data-inbox-migrate-run/, '视图应有迁移入口');
    assert.match(functionSource('renderInboxManager'), /data-inbox-upload/, '视图应有上传入口');
});
