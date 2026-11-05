/**
 * QIN-533（GitHub #581）：统一资产库中栏移除智能筛选，仅保留目录层级并默认收起。
 *
 * 断言面覆盖票面 AC1～AC5：
 * - AC1 智能筛选页与双段拨片下线，机架外壳只渲染目录层级，模式状态与记忆键删除；
 * - AC2 目录层级工具条保留展开 / 目录层级 / 清空筛选；旧机架折叠态由 QIN-544 单栏树取代；
 * - AC3 自定义看板的分组目录与参考画布入口迁到目录层级页底部，能力不丢；
 * - AC4 筛选数据通道与清空筛选行为不变；
 * - AC5 测试登记到 node-files.txt 并完成卫生准入分片。
 */
import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);
const CSS = readFileSync(
    new URL('../../web/css/pages/asset-manager.css', import.meta.url),
    'utf8'
);
const CONTROLLER = readFileSync(
    new URL('../../web/js/controllers/assets-controller.js', import.meta.url),
    'utf8'
);
const NODE_FILES = readFileSync(
    new URL('./node-files.txt', import.meta.url),
    'utf8'
);
const SHARD_URL = new URL('../hygiene/fixtures/admissions/issue-581.json', import.meta.url);

function functionSource(name){
    let start = MANAGER.indexOf(`function ${name}(`);
    if(start < 0) start = MANAGER.indexOf(`async function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const parenOpen = MANAGER.indexOf('(', start);
    let parenDepth = 0;
    let cursor = parenOpen;
    for(; cursor < MANAGER.length; cursor += 1){
        if(MANAGER[cursor] === '(') parenDepth += 1;
        else if(MANAGER[cursor] === ')'){
            parenDepth -= 1;
            if(parenDepth === 0) break;
        }
    }
    const open = MANAGER.indexOf('{', cursor);
    let depth = 0;
    for(let index = open; index < MANAGER.length; index += 1){
        if(MANAGER[index] === '{') depth += 1;
        else if(MANAGER[index] === '}'){
            depth -= 1;
            if(depth === 0) return MANAGER.slice(start, index + 1);
        }
    }
    assert.fail(`函数未闭合：${name}`);
}

test('AC1: 中栏智能筛选页与双段拨片下线，机架只渲染目录层级', () => {
    for(const removed of [
        'renderAssetRackFilters',
        'renderAssetRackSegmented',
        'renderAssetRackRecentCapsules',
        'renderAssetRackKindBadges',
        'renderAssetRackTagCloud',
        'renderAssetRackCollections',
        'renderAssetRackPresets',
        'assetRackKindLabel',
        'ASSET_RACK_KIND_LABELS',
    ]){
        assert.ok(!MANAGER.includes(removed), `智能筛选专属渲染必须整体删除：${removed}`);
    }
    for(const removed of ['assetRackMode', 'ASSET_RACK_MODE_KEY', 'setAssetRackMode', 'data-asset-rack-mode']){
        assert.ok(!MANAGER.includes(removed), `机架模式状态与点击委托必须删除：${removed}`);
    }
    assert.ok(!MANAGER.includes('asset_registry_rack_mode_v1'), '模式记忆键必须删除');
    assert.ok(!functionSource('renderAssetRackFolders').includes('清空智能筛选'), '机架目录页不得再出现「清空智能筛选」');
    assert.ok(!functionSource('renderAssetRackRail').includes('智能筛选'), '图标条不得再出现智能筛选');
    assert.ok(!MANAGER.includes('标签与 AI 分类'), '机架内不得再出现标签与 AI 分类分组');
    assert.ok(!CONTROLLER.includes('智能筛选尚未就绪'), '控制器提示语不得再指向已下线入口');

    // 头部只剩标题 + 收起按钮；总览与系统看板共用同一套机架
    const title = functionSource('renderAssetRackTitle');
    assert.match(title, /目录层级/, '中栏标题必须是「目录层级」');
    assert.ok(!title.includes('>智能筛选'), '标题不得再出现智能筛选');
    for(const render of ['renderAssetRack', 'renderSystemBoardRack']){
        const source = functionSource(render);
        assert.match(source, /renderAssetRackTitle\(\)/, `${render} 必须渲染目录层级标题`);
        assert.ok(!source.includes('renderAssetRackFilters'), `${render} 不得再渲染智能筛选页`);
        assert.ok(!source.includes('data-asset-rack-mode'), `${render} 不得再渲染模式切换按钮`);
    }
    assert.match(
        functionSource('renderAssetRack'),
        /collapsed \? '' : renderAssetRackFolders\(\)/,
        '展开态只能渲染目录层级页'
    );
});

test('AC2: 目录层级工具条保留展开 / 目录层级 / 清空筛选', () => {
    const read = functionSource('readAssetRackCollapsed');
    assert.match(
        MANAGER,
        /const ASSET_RACK_COLLAPSED_KEY = 'asset_registry_rack_collapsed_v2';/,
        '折叠记忆键必须升到 v2，避免旧版「已展开」记录延续'
    );
    assert.match(read, /stored === null \? true/, '缺少记录时必须默认收起');
    assert.match(read, /catch\(_\) \{ return true; \}/, 'localStorage 不可用时同样默认收起');

    const rail = functionSource('renderAssetRackRail');
    assert.match(rail, /data-asset-rack-expand/, '图标条必须能展开中栏');
    assert.match(rail, /'folder-tree', '目录层级'/, '图标条必须保留目录层级入口');
    assert.match(rail, /data-registry-clear="all"/, '图标条必须保留清空筛选');
    assert.ok(!rail.includes('智能筛选'), '图标条不得再有智能筛选入口');

    // 「目录层级」图标等价展开：同一属性走既有展开分支
    assert.match(
        MANAGER,
        /if\(target\.closest\?\.\('\[data-asset-rack-expand\]'\)\)\{ setAssetRackCollapsed\(false\); return; \}/,
        '点击目录层级/展开必须落到展开分支'
    );
    assert.match(functionSource('setAssetRackCollapsed'), /saveAssetRackCollapsed\(assetRackCollapsed\)/);
    assert.doesNotMatch(functionSource('render'), /asset-rack-collapsed/, '新树布局不再写入旧中栏折叠态');
});

test('AC3: 看板分组目录与参考画布入口迁到目录层级页底部', () => {
    const folders = functionSource('renderAssetRackFolders');
    assert.match(folders, /renderAssetRackBoardExtras\(board\)/, '目录层级页必须承载看板附加入口');
    assert.match(folders, /renderAssetRackFolderToolbar\(\)/, '目录操作工具坞必须保留');
    assert.match(folders, /renderAssetRackFolderRoots\(\)/, '目录树必须保留');

    const extras = functionSource('renderAssetRackBoardExtras');
    assert.match(extras, /renderCustomBoardGroups\(board, items\)/, '分组目录入口必须保留');
    assert.match(extras, /renderCustomBoardReferenceCanvases\(board, items\)/, '参考画布入口必须保留');

    // 迁移后仍只在看板态出现，总览不额外增加分组/参考画布卡片
    assert.match(folders, /const board = assetRackBoard\(\);/, '看板态判定必须保留');
    const toolbar = functionSource('renderAssetRackFolderToolbar');
    assert.match(toolbar, /data-board-folder-create/, '看板态必须保留新建文件夹');
    assert.match(toolbar, /data-board-source-settings/, '看板态必须保留素材来源设置');
});

test('AC4: 筛选数据通道与清空筛选行为不变', () => {
    // 通道仍在：各类 data-registry-* 过滤与「清空筛选」统一委托未被本票改动
    for(const channel of ['data-registry-kind', 'data-registry-tag', 'data-registry-recent', 'data-registry-collection']){
        assert.ok(MANAGER.includes(channel), `筛选通道必须保留：${channel}`);
    }
    assert.match(
        MANAGER,
        /if\(key==='all'\)\{[\s\S]{0,240}?boardSourceSelection\[board\.id\]=''; boardFolderSelection\[board\.id\]=''/,
        '清空筛选必须仍同步复位看板来源与目录选择'
    );
    assert.match(functionSource('renderRegistryFilterChips'), /registry-filter-chips/, '工具栏筛选胶囊必须保留');

    // 纯前端结构与样式：不得引入外部依赖或新网络请求
    assert.doesNotMatch(MANAGER, /<script[^>]+src="https?:/, '禁止外部脚本');
    const rackCss = CSS.slice(CSS.indexOf('QIN-524 · 统一资产库中栏'));
    assert.ok(rackCss.includes('.asset-rack-title'), '目录层级标题必须有自己的样式');
    assert.doesNotMatch(rackCss, /asset-rack-seg|asset-rack-capsule|asset-rack-kind|asset-rack-pill|asset-rack-preset|asset-rack-cloud/, '智能筛选专属样式必须清理');
});

test('AC5: 测试登记到 node-files.txt 并完成卫生准入分片', () => {
    assert.match(
        NODE_FILES,
        /^tests\/frontend\/qin-533-asset-rack-folders-only\.test\.mjs$/m,
        '新增测试必须登记到 node-files.txt'
    );
    assert.ok(existsSync(SHARD_URL), '缺少卫生准入分片 issue-581.json');
    const shard = JSON.parse(readFileSync(SHARD_URL, 'utf8'));
    assert.equal(shard.schema, 'gw.cleanroom-admission-shard/v1');
    assert.equal(shard.issue, 581);
    const paths = (shard.files || []).map(entry => String(entry));
    for(const required of [
        'web/js/modules/asset-manager.js',
        'web/css/pages/asset-manager.css',
        'web/js/controllers/assets-controller.js',
        'tests/frontend/qin-524-asset-nav-redesign.test.mjs',
        'tests/frontend/qin-527-custom-library-rack.test.mjs',
        'tests/frontend/qin-533-asset-rack-folders-only.test.mjs',
        'tests/frontend/node-files.txt',
        'tests/hygiene/fixtures/admissions/issue-581.json',
    ]){
        assert.ok(paths.includes(required), `准入分片必须声明可改文件：${required}`);
    }
});
