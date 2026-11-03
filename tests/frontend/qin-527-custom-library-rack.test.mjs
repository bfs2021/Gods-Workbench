/**
 * QIN-527（GitHub #569）：统一资产库中栏重构补全 —— 自定义看板接入双模机架。
 *
 * 断言面覆盖票面 AC1～AC5：
 * - AC1 自定义看板中栏改用同一套 asset-rack 双模机架，旧堆叠导航与冗长灰字彻底下线；
 * - AC2 看板目录层级以看板素材来源 + 真实物理子目录为准，点击纯前端收敛，工具坞保留；
 * - AC3 左栏底部重复的「智能筛选」按钮移除，未归属/导入/资产设置顺序不变；
 * - AC4 无根目录时给出可点的「登记素材根目录」引导，直接唤起既有登记弹窗；
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
const SHELL_HTML = readFileSync(
    new URL('../../web/pages/assets.html', import.meta.url),
    'utf8'
);
const NODE_FILES = readFileSync(
    new URL('./node-files.txt', import.meta.url),
    'utf8'
);
const SHARD_URL = new URL('../hygiene/fixtures/admissions/issue-569.json', import.meta.url);

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

test('AC1: 自定义看板中栏改用同一套双模机架，旧堆叠导航与冗长灰字下线', () => {
    const library = functionSource('renderCustomAssetLibrary');
    assert.match(
        library,
        /renderAssetRack\('', 'library-board-nav'\)/,
        '自定义看板中栏必须复用总览同一套机架，并保留看板中栏定位类'
    );
    for(const legacy of ['asset-smart-filter-scroll', 'library-board-summary', 'library-rule-row', 'nav-hint']){
        assert.ok(!library.includes(legacy), `看板中栏不得再输出旧版堆叠导航节点：${legacy}`);
    }
    assert.ok(!library.includes('renderCustomBoardFolders'), '旧文件夹堆叠面板必须下线');
    assert.ok(!library.includes('renderCustomBoardSources'), '旧来源堆叠面板必须下线');

    // 冗长灰字提示整体清除（不残留任何占位长句）
    for(const noisy of [
        '暂无文件夹，可点击右上角新建',
        '选择素材后使用“加入分组”创建目录',
        '保存参考画布后会显示在这里',
        '此看板与已登记素材来源同步，新增文件会在索引后显示。',
        '自动归档与用户路径相互独立，点击上方来源即可切换。',
    ]){
        assert.ok(!MANAGER.includes(noisy), `必须彻底清除冗长灰字提示：${noisy}`);
    }

    // 看板中栏同样 240px，且可折叠为 44px 图标条
    assert.match(
        CSS,
        /\.asset-manager-root\.asset-manager-root-library \{ grid-template-columns:240px minmax\(0,1fr\) var\(--registry-detail-width,360px\); \}/,
        '看板中栏必须与总览一致收窄为 240px'
    );
    assert.match(
        CSS,
        /\.asset-manager-root\.asset-manager-root-library\.asset-rack-collapsed \{ grid-template-columns:44px minmax\(0,1fr\)/,
        '看板中栏折叠态必须收为 44px'
    );
    assert.match(
        MANAGER,
        /\(activeTab === 'registry' \|\| customLibraryActive\) && assetRackCollapsed \? ' asset-rack-collapsed' : ''/,
        '看板页折叠态必须写回根容器类名'
    );
});

test('AC2: 看板目录以素材来源与真实物理子目录为准，点击纯前端收敛右网格', () => {
    const roots = functionSource('assetRackBoardRoots');
    assert.match(roots, /board\.sources/, '看板来源必须取自看板自身绑定来源');
    assert.match(roots, /item\.storage_root_id/, '来源计数必须来自看板素材池');
    assert.match(roots, /__automatic__/, '必须保留「自动归档」来源');
    assert.match(roots, /counts\.get/, '每个来源必须带真实素材计数');
    assert.match(
        functionSource('assetRackRoots'),
        /const board = assetRackBoard\(\);\s*\n\s*if\(board\) return assetRackBoardRoots\(board\);/,
        '机架根节点必须在看板态切换为看板来源'
    );

    const tree = functionSource('assetRackFolderTree');
    assert.match(tree, /registryRootFolders\.get\(root\)/, '看板子目录必须读该来源已加载的真实目录');
    assert.match(tree, /customBoardFolderTree\(items, directories\)/, '子目录层级必须复用既有物理路径聚类');
    assert.match(functionSource('assetRackRootOnlyCount'), /registryAssetFolderPath\(item\)/, '根目录节点必须只统计无子目录素材');

    const select = functionSource('selectAssetRackFolder');
    assert.match(select, /boardSourceSelection\[board\.id\]/, '点击来源必须写回看板既有来源选择');
    assert.match(select, /boardFolderSelection\[board\.id\]/, '点击目录必须写回看板既有目录选择');
    assert.match(select, /render\(\);/, '点击后必须即时重渲染网格');
    assert.doesNotMatch(select, /refreshRegistryAssets|apiJsonResponse/, '目录过滤不得触发网络重载');
    assert.match(
        functionSource('registryNavFolderMatches'),
        /if\(assetRackBoard\(\)\) return true;/,
        '看板态不得二次叠加目录过滤，避免与看板自身过滤冲突'
    );

    // 微型工具坞在看板态可用
    const toolbar = functionSource('renderAssetRackFolderToolbar');
    assert.match(toolbar, /data-board-folder-create="\$\{escapeAttr\(board\.id\)\}"/, '看板态必须复用既有新建文件夹弹窗');
    assert.match(toolbar, /data-board-source-settings/, '看板态必须保留素材来源设置入口');
    assert.match(toolbar, /data-asset-rack-expand-all/, '必须保留展开/折叠全部');
    assert.match(toolbar, /data-asset-rack-clear-folder/, '必须保留清除目录过滤');
    assert.match(MANAGER, /assetRackFolderTree|assetRackRootOnlyCount/, '目录树必须在看板态生效');

    // 隐藏文件夹能力在看板态保留（行内单图标）
    assert.match(functionSource('renderAssetRackFolderNode'), /data-board-folder-visibility/, '看板态必须保留隐藏文件夹开关');

    // 智能筛选按看板范围取数
    assert.match(functionSource('renderAssetRackKindBadges'), /assetRackBoardPool\(board\)/, '类型徽标计数必须按看板素材池统计');
    assert.match(functionSource('renderAssetRackTagCloud'), /customBoardTagEntries\(assetRackBoardPool\(board\)\)/, '标签云必须只看板内标签');
    assert.match(functionSource('renderAssetRackFilters'), /renderAssetRackBoardExtras\(board\)/, '看板分组与参考画布入口必须保留');

    // 清理看板筛选时同时复位看板来源/目录选择
    assert.match(
        MANAGER,
        /if\(key==='all'\)\{[\s\S]{0,240}?boardSourceSelection\[board\.id\]=''; boardFolderSelection\[board\.id\]=''/,
        '清空智能筛选必须同步复位看板来源与目录选择'
    );
});

test('AC3: 左栏底部重复的智能筛选入口已移除，其余动作顺序不变', () => {
    assert.doesNotMatch(SHELL_HTML, /id="assetSidebarSmartFilter"/, '左栏底部不得再有重复的智能筛选按钮');
    assert.doesNotMatch(SHELL_HTML, /WorkbenchAssets\.toggleSmartFilter\(\)/, '左栏不得再转发智能筛选动作');
    assert.match(
        SHELL_HTML,
        /class="asset-shell-sidebar-actions[^]*?id="btnUnassignedInbox"[^]*?WorkbenchAssets\.uploadAsset\(\)[^]*?id="assetSidebarSettings"/,
        '底部必须保持「未归属 → 导入外部资产文件 → 资产设置」的整洁顺序'
    );
    // 中栏拨片承担唯一筛选入口，不得回退成按钮
    assert.match(functionSource('renderAssetRackSegmented'), /智能筛选/);
});

test('AC4: 无根目录时给出可点的登记引导，直接唤起既有登记弹窗', () => {
    const roots = functionSource('renderAssetRackFolderRoots');
    assert.match(roots, /asset-rack-empty-guide/, '空状态必须渲染引导卡片');
    assert.match(roots, /data-asset-rack-register-root/, '空状态必须提供可点的登记按钮');
    assert.match(roots, /登记素材根目录/, '引导按钮文案必须自解释');
    assert.match(
        MANAGER,
        /data-asset-rack-register-root\]'\)\)\{[\s\S]{0,220}?openBoardSourceModal\(board \? board\.id : '', 'new'\);/,
        '引导按钮必须直接唤起既有素材来源登记弹窗'
    );
    assert.match(CSS, /\.asset-rack-empty-guide \{ display:flex; flex-direction:column;/, '引导卡片必须有独立布局样式');
});

test('AC5: 测试登记到 node-files.txt 并完成卫生准入分片', () => {
    assert.match(
        NODE_FILES,
        /^tests\/frontend\/qin-527-custom-library-rack\.test\.mjs$/m,
        '新增测试必须登记到 node-files.txt'
    );
    assert.ok(existsSync(SHARD_URL), '缺少卫生准入分片 issue-527.json');
    const shard = JSON.parse(readFileSync(SHARD_URL, 'utf8'));
    assert.equal(shard.schema, 'gw.cleanroom-admission-shard/v1');
    assert.equal(shard.issue, 569);
    const paths = (shard.files || []).map(entry => String(entry));
    for(const required of [
        'web/js/modules/asset-manager.js',
        'web/css/pages/asset-manager.css',
        'web/pages/assets.html',
        'tests/frontend/qin-527-custom-library-rack.test.mjs',
        'tests/frontend/node-files.txt',
        'tests/hygiene/fixtures/admissions/issue-569.json',
    ]){
        assert.ok(paths.includes(required), `准入分片必须声明可改文件：${required}`);
    }
    const accepted = (shard.accepted_test_files || []).map(entry => entry.path);
    assert.ok(
        accepted.includes('tests/frontend/qin-527-custom-library-rack.test.mjs'),
        '新增前端测试必须在准入分片登记'
    );
});
