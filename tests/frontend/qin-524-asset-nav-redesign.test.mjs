/**
 * QIN-524（GitHub #563）：统一资产库中栏重构 —— 双模紧凑机架。
 *
 * 断言面覆盖票面 AC1～AC5：
 * - AC1 双段模式拨片（目录层级 / 智能筛选）+ localStorage 记忆；
 * - AC2 纯物理目录树（登记根目录 + relative_path 真实子目录）、展开折叠、计数徽标、点击即时过滤；
 * - AC3 紧凑硬件胶囊筛选（时间跨度单行、类型双列、标签云包含/排除）；
 * - AC4 240px 收窄 + 44px 折叠图标条 + 黑金令牌、零外部依赖；
 * - AC5 测试自身登记到 node-files.txt 并完成卫生准入分片。
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
const NODE_FILES = readFileSync(
    new URL('./node-files.txt', import.meta.url),
    'utf8'
);
const SHARD_URL = new URL('../hygiene/fixtures/admissions/issue-524.json', import.meta.url);

function functionSource(name){
    let start = MANAGER.indexOf(`function ${name}(`);
    if(start < 0) start = MANAGER.indexOf(`async function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    // 先跨过参数表（可能含 `item={}` 这类默认值），再定位函数体首个 `{`
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

test('AC1: 中栏只剩目录层级入口，双段拨片已由 QIN-533 下线', () => {
    // QIN-533 更新：双段拨片与智能筛选页整体删除，本用例改为守住「只剩目录层级」的现状
    assert.ok(!MANAGER.includes('renderAssetRackSegmented'), '双段拨片渲染必须下线');
    assert.ok(!MANAGER.includes('data-asset-rack-mode'), '模式切换按钮与点击委托必须下线');
    assert.ok(!MANAGER.includes('asset_registry_rack_mode_v1'), '模式记忆键必须下线');
    const title = functionSource('renderAssetRackTitle');
    assert.match(title, /目录层级/, '中栏头部必须保留目录层级标题');

    // 记忆：缺省收起，用户显式展开后才保持展开
    const readCollapsed = functionSource('readAssetRackCollapsed');
    assert.match(readCollapsed, /localStorage\.getItem\(ASSET_RACK_COLLAPSED_KEY\)/);
    assert.match(readCollapsed, /stored === null \? true/, '缺少记录时必须默认收起');
    const setCollapsed = functionSource('setAssetRackCollapsed');
    assert.match(setCollapsed, /saveAssetRackCollapsed\(assetRackCollapsed\)/, '收起/展开后必须持久化');
    assert.match(setCollapsed, /render\(\);/, '收起/展开只重渲染中栏所在视图，不做整页跳转');
    assert.doesNotMatch(setCollapsed, /location\.(reload|href)/, '禁止整页刷新');

    // 点击派发登记
    assert.match(
        MANAGER,
        /if\(target\.closest\?\.\('\[data-asset-rack-collapse\]'\)\)\{ setAssetRackCollapsed\(true\); return; \}/,
        '收起按钮必须走统一委托'
    );
    assert.match(
        MANAGER,
        /if\(target\.closest\?\.\('\[data-asset-rack-expand\]'\)\)\{ setAssetRackCollapsed\(false\); return; \}/,
        '展开按钮必须走统一委托'
    );
});

test('AC2: 目录层级只呈现登记根目录与真实物理子目录，点击即时过滤右网格', () => {
    const roots = functionSource('assetRackRoots');
    assert.match(roots, /registryFacets\.roots/, '根目录必须来自已登记素材来源');
    assert.match(roots, /availability \|\| ''\) !== 'removed'/, '已移除来源不得出现在目录树');

    // 物理子目录层级来自素材 relative_path（registryAssetFolderPath 派生）
    const tree = functionSource('assetRackFolderTree');
    assert.match(tree, /item\.storage_root_id/, '子目录树必须按存储来源归属');
    assert.match(tree, /customBoardFolderTree\(items, directories, summary/, '目录树必须复用既有物理路径聚类');
    assert.match(functionSource('registryAssetFolderPath'), /relative_path/, '目录路径必须取自素材真实相对路径');

    // 节点：三角展开、文件夹图标、等宽计数徽标
    const node = functionSource('renderAssetRackFolderNode');
    assert.match(node, /data-asset-rack-toggle=/, '必须提供展开/折叠三角');
    assert.match(node, /'folder-open' : 'folder'/, '必须按选中态切换文件夹图标');
    assert.match(node, /<em>\$\{Number\(node\.count \|\| 0\)\}<\/em>/, '必须带素材计数徽标');
    assert.match(CSS, /\.asset-rack-root-btn em[\s\S]{0,400}?font-family:var\(--gw-font-mono\)/, '计数徽标必须使用等宽字体');

    // 点击节点：目录状态更新后按当前目录重新读取分页
    const select = functionSource('selectAssetRackFolder');
    assert.match(select, /registryNavFolder = /, '点击必须写入目录过滤态');
    assert.match(select, /refreshRegistryAssets\(\)/, '点击后必须按所选目录重新加载');
    assert.match(
        functionSource('registryVisibleAssets'),
        /\.filter\(registryNavFolderMatches\)/,
        '目录过滤必须收敛到统一可见资产管线'
    );
    assert.match(functionSource('registryAssetMatchesFolder'), /startsWith\(`\$\{selected\}\/`\)/, '必须包含子目录素材');

    // 顶部微型操作按钮，且清理所有冗长灰字提示
    const toolbar = functionSource('renderAssetRackFolderToolbar');
    assert.match(toolbar, /data-asset-rack-folder-create/, '必须有 [+] 新建文件夹');
    assert.match(toolbar, /data-asset-rack-expand-all/, '必须有展开/折叠全部');
    const folders = functionSource('renderAssetRackFolders');
    for(const noisy of ['暂无文件夹，可点击右上角添加', '选择素材后使用加入分镜创建目录', '以脚本更新全部可变录音']){
        assert.ok(!folders.includes(noisy), `目录层级不得再出现冗长提示：${noisy}`);
    }
    assert.doesNotMatch(folders, /点击包含，减号排除/, '目录层级必须去掉筛选说明长文');

    // 清除目录过滤与筛选胶囊
    assert.match(functionSource('clearAssetRackFolder'), /registryNavFolder = \{rootId:'', path:''\}/);
    assert.match(
        functionSource('renderRegistryFilterChips'),
        /data-asset-rack-clear-folder/,
        '目录过滤必须在筛选胶囊上可见可清除'
    );
});

test('AC3: 智能筛选页已由 QIN-533 下线，机架不再渲染任何筛选分组', () => {
    // QIN-533 更新：中栏只保留目录层级，智能筛选专属渲染与样式必须整体清理
    for(const removed of [
        'renderAssetRackFilters',
        'renderAssetRackRecentCapsules',
        'renderAssetRackKindBadges',
        'renderAssetRackTagCloud',
        'renderAssetRackCollections',
        'renderAssetRackPresets',
    ]){
        assert.ok(!MANAGER.includes(removed), `智能筛选专属渲染必须删除：${removed}`);
    }
    for(const label of ['时间跨度', '标签与 AI 分类']){
        assert.ok(!MANAGER.includes(label), `机架不得再输出筛选分组：${label}`);
    }
    for(const render of ['renderAssetRack', 'renderSystemBoardRack']){
        assert.ok(!functionSource(render).includes('role="tablist"'), `${render} 不得再有模式拨片 tablist`);
    }
    assert.ok(!MANAGER.includes('data-asset-rack-mode'), '中栏不得再有模式切换按钮');
    for(const cssRule of ['.asset-rack-capsules', '.asset-rack-kind-grid', '.asset-rack-pill', '.asset-rack-presets', '.asset-rack-seg']){
        assert.ok(!CSS.includes(cssRule), `智能筛选专属样式必须清理：${cssRule}`);
    }

    // 清空筛选能力保留，入口收敛到折叠图标条
    assert.match(functionSource('renderAssetRackRail'), /data-registry-clear="all"/, '图标条必须保留清空筛选');
});

test('AC4: 中栏 240px、只用黑金令牌', () => {
    assert.match(
        CSS,
        /\.asset-manager-root\.asset-manager-root-registry \{ grid-template-columns:240px minmax\(0,1fr\);/,
        '统一资产库中栏必须收窄为 240px'
    );

    const rack = functionSource('renderAssetRack');
    assert.match(rack, /data-asset-rack-collapse/, '必须有收起按钮');
    assert.match(rack, /renderAssetRackRail\(\)/, '折叠态必须渲染图标条');
    const rail = functionSource('renderAssetRackRail');
    assert.match(rail, /data-asset-rack-expand/, '图标条必须能展开中栏');
    assert.match(rail, /'folder-tree', '目录层级'/, '图标条必须保留目录层级入口');
    assert.match(rail, /data-registry-clear="all"/, '图标条必须保留清空筛选');

    assert.match(functionSource('setAssetRackCollapsed'), /saveAssetRackCollapsed\(assetRackCollapsed\)/);
    assert.doesNotMatch(functionSource('render'), /asset-rack-collapsed/, '新树布局不再写入旧中栏折叠态');

    // 黑金令牌 + 零外部依赖
    const rackCssStart = CSS.indexOf('QIN-524 · 统一资产库中栏');
    assert.ok(rackCssStart > 0, '缺少机架样式段');
    const rackCss = CSS.slice(rackCssStart);
    assert.ok(rackCss.includes('#dfc384'), '选中态必须使用香槟钛金');
    assert.doesNotMatch(rackCss, /https?:\/\//, '机架样式不得引入外部 CDN');
    assert.doesNotMatch(rackCss, /@import/, '机架样式不得新增 @import');
    assert.doesNotMatch(MANAGER, /<script[^>]+src="https?:/, '禁止外部脚本');
});

test('AC5: 测试登记到 node-files.txt 并完成卫生准入分片', () => {
    assert.match(
        NODE_FILES,
        /^tests\/frontend\/qin-524-asset-nav-redesign\.test\.mjs$/m,
        '新增测试必须登记到 node-files.txt'
    );
    assert.ok(existsSync(SHARD_URL), '缺少卫生准入分片 issue-524.json');
    const shard = JSON.parse(readFileSync(SHARD_URL, 'utf8'));
    assert.equal(shard.schema, 'gw.cleanroom-admission-shard/v1');
    assert.equal(shard.issue, 524);
    const paths = (shard.files || []).map(entry => String(entry));
    for(const required of [
        'web/js/modules/asset-manager.js',
        'web/css/pages/asset-manager.css',
        'tests/frontend/qin-524-asset-nav-redesign.test.mjs',
        'tests/frontend/node-files.txt',
        'tests/hygiene/fixtures/admissions/issue-524.json',
    ]){
        assert.ok(paths.includes(required), `准入分片必须声明可改文件：${required}`);
    }
    const accepted = (shard.accepted_test_files || []).map(entry => entry.path);
    assert.ok(
        accepted.includes('tests/frontend/qin-524-asset-nav-redesign.test.mjs'),
        '新增前端测试必须在准入分片登记'
    );
});
