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
const ASSETS_CONTROLLER = readFileSync(
    new URL('../../web/js/controllers/assets-controller.js', import.meta.url),
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
    // 【QIN-544】中栏由左栏单栏树替代：自定义看板与总览共用同一棵树面板
    assert.match(
        library,
        /renderAssetTreePanel\(\)/,
        '自定义看板左栏必须复用同一棵单栏树面板'
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
        /\(activeTab === 'registry' \|\| customLibraryActive \|\| systemRackActive\) && assetRackCollapsed \? ' asset-rack-collapsed' : ''/,
        '看板页（含系统看板）折叠态必须写回根容器类名'
    );
    assert.match(
        MANAGER,
        /const SYSTEM_BOARD_RACK_TABS = \['scripts','projects','assets','workflows','prompts','canvas-assets','local'\];/,
        '系统看板必须登记进机架外壳覆盖清单'
    );
    // 【QIN-544】中栏机架由左栏单栏树替代：系统看板视图统一接入树面板
    for(const render of ['renderScriptAssetsManager','renderProjectManager','renderAssetManager','renderWorkflowManager','renderPromptManager','renderCanvasAssetsManager','renderLocalManager']){
        const source = functionSource(render);
        assert.match(source, /renderAssetTreePanel\(\)/, `${render} 左栏必须接入单栏树面板`);
        assert.ok(!source.includes('asset-smart-filter-scroll'), `${render} 不得再输出旧版智能筛选滚动容器`);
    }
    assert.match(
        functionSource('refreshRegistryAssetsFromRack'),
        /activeTab = 'registry'/,
        '系统看板态的机架筛选必须跳转资产总览应用，不得做无落点的假按钮'
    );
});

test('AC2: 看板目录以素材来源与真实物理子目录为准，点击后按目录分页读取右网格', () => {
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
    assert.match(tree, /customBoardFolderTree\(items, directories, summary/, '子目录层级必须复用既有物理路径聚类');
    assert.match(functionSource('assetRackRootOnlyCount'), /registryAssetFolderPath\(item\)/, '根目录节点必须只统计无子目录素材');

    const select = functionSource('selectAssetRackFolder');
    assert.match(select, /boardSourceSelection\[board\.id\]/, '点击来源必须写回看板既有来源选择');
    assert.match(select, /boardFolderSelection\[board\.id\]/, '点击目录必须写回看板既有目录选择');
    assert.match(select, /refreshRegistryAssets\(\)/, '点击后按所选目录重新读取素材');
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

    // 智能筛选页已由 QIN-533 下线：看板附加入口改由目录层级页承载
    assert.match(functionSource('renderAssetRackFolders'), /renderAssetRackBoardExtras\(board\)/, '分组目录与参考画布入口必须迁到目录层级页');
    assert.ok(!MANAGER.includes('renderAssetRackFilters'), '智能筛选页必须整体下线');

    // 清理看板筛选时同时复位看板来源/目录选择
    assert.match(
        MANAGER,
        /if\(key==='all'\)\{[\s\S]{0,240}?boardSourceSelection\[board\.id\]=''; boardFolderSelection\[board\.id\]=''/,
        '清空智能筛选必须同步复位看板来源与目录选择'
    );
});

test('AC3/AC14: 左栏不再渲染底部操作组，保留未归属状态代理', () => {
    assert.doesNotMatch(SHELL_HTML, /id="assetSidebarSmartFilter"/, '左栏底部不得再有重复的智能筛选按钮');
    assert.doesNotMatch(SHELL_HTML, /WorkbenchAssets\.toggleSmartFilter\(\)/, '左栏不得再转发智能筛选动作');
    assert.doesNotMatch(
        SHELL_HTML,
        /asset-shell-sidebar-actions|onclick="WorkbenchAssets\.uploadAsset\(\)"[^>]*title="导入外部资产文件"/,
        '左栏不再渲染底部操作组或导入外部资产文件按钮'
    );
    assert.match(
        SHELL_HTML,
        /<div id="assetSidebarActionState" hidden aria-hidden="true">[\s\S]*id="btnUnassignedInbox"/,
        '未归属状态代理继续留给看板工具栏同步'
    );
    // 中栏只剩目录层级：「目录层级」标题承担唯一入口，不得回退成按钮
    assert.match(functionSource('renderAssetRackTitle'), /目录层级/, '中栏标题必须是目录层级');
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

test('AC2 补充: 自动归档同样下钻真实子目录，目录索引补齐但不得递归重绘', () => {
    const tree = functionSource('assetRackFolderTree');
    assert.doesNotMatch(
        tree,
        /root === '__automatic__'\)\s*return \[\]/,
        '自动归档不得再返回空树：生产库绝大多数素材都落在未显式登记的根上'
    );
    assert.match(tree, /assetRackAutomaticItems\(board\)/, '自动归档必须按未登记来源的素材建树');
    assert.match(tree, /assetRackAutomaticDirectories\(items\)/, '自动归档必须合并各根目录的已登记目录');

    const autoItems = functionSource('assetRackAutomaticItems');
    assert.match(autoItems, /excludedSourceIds/, '自动归档口径必须与来源计数一致（含排除来源）');
    assert.match(autoItems, /storage_root_id/, '自动归档必须按素材真实根归属过滤');
    assert.match(
        functionSource('assetRackRootOnlyCount'),
        /assetRackAutomaticItems\(board\)/,
        '自动归档下的根目录素材也必须能单独点开'
    );

    // 目录摘要由后端聚合，不再为建树自动拉取素材分页。
    const index = functionSource('ensureAssetRackDirectoryIndex');
    assert.match(index, /loadAssetRackRegisteredFolders\(\)/);
    assert.doesNotMatch(index, /loadMoreRegistryAssets\(/);
    assert.match(tree, /registryIndexedFolders/);

    // 死循环回归：看板渲染入口不得调用收尾会无条件 render() 的目录加载
    const library = functionSource('renderCustomAssetLibrary');
    assert.doesNotMatch(
        library,
        /loadCustomBoardFolderDirectories\(/,
        '看板渲染入口不得调用 loadCustomBoardFolderDirectories：它收尾会 render()，互相触发即死循环'
    );
    assert.match(library, /ensureAssetRackDirectoryIndex\(\)/, '看板渲染入口必须触发目录索引补齐');
});

test('AC2 补充: 目录树必须合并磁盘真实子目录，新根目录默认展开', () => {
    // 素材路径只能推出「有素材的目录」，磁盘上的空目录（如 3d/、111111111/）必须走磁盘浏览
    const disk = functionSource('loadAssetRackDiskFolders');
    assert.match(disk, /\/api\/storage-settings\/browse\?path=/, '磁盘子目录必须走只读目录浏览接口');
    assert.match(disk, /credentials:'same-origin'/, '目录浏览必须携带会话凭证');
    assert.match(disk, /__automatic__/, '自动归档必须聚合池内各素材根的磁盘目录');
    assert.match(disk, /registryDiskFolderCache\.set\(key, \[\]\)/, '浏览失败（404/无权）必须静默降级为空，不得报错');
    assert.match(disk, /await loadAssetAllowedRootPaths\(\)/, '浏览前必须先解析允许根路径映射');

    // 历史索引根（root_xxx）不在本机库清单里，路径只能从允许根快照取
    const allowed = functionSource('loadAssetAllowedRootPaths');
    assert.match(allowed, /\/api\/storage-settings\/roots/, '必须读取允许根快照解析根目录磁盘路径');
    assert.match(allowed, /catch\(_\) \{\}/, '快照不可读（editor 403）必须静默降级');

    const rootPath = functionSource('assetRackRootDiskPath');
    assert.match(rootPath, /source\?\.path/, '看板来源优先用看板绑定的路径');
    assert.match(rootPath, /assetAllowedRootPaths\.get\(id\)/, '历史索引根必须回退到允许根快照');

    const tree = functionSource('assetRackFolderTree');
    assert.match(tree, /assetRackDiskDirectories\(root\)/, '普通来源建树必须合并磁盘目录');
    assert.match(tree, /assetRackDiskDirectoriesForItems\(items\)/, '自动归档建树必须合并各素材根的磁盘目录');

    // 新登记的根目录必须默认展开：旧逻辑只在 localStorage 为空时 seed 一次，此后新根永远折叠
    const expand = functionSource('ensureAssetRackRootsExpanded');
    assert.match(expand, /ASSET_RACK_KNOWN_ROOTS_KEY/, '必须持久化已知根目录集合以区分新根');
    assert.match(expand, /assetRackExpandedFolders\.add/, '新出现的根目录必须写入展开集合');
    assert.match(
        functionSource('renderAssetRackFolders'),
        /ensureAssetRackRootsExpanded\(\)/,
        '目录层级渲染必须先确保根目录展开'
    );

    // 子目录展开时懒加载磁盘深层
    const toggle = functionSource('toggleAssetRackFolder');
    assert.match(toggle, /loadAssetRackDiskFolders\(rootId, path\)/, '展开目录节点必须懒加载磁盘子目录');
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

test('QIN-532: 看板来源和自动归档范围随分页查询发送', () => {
    const build = new Function('URLSearchParams', `
        const REGISTRY_PAGE_SIZE=1500, registrySort='newest', registryProjectFilterId='', registryQuery='', registryKind='', registryStatCategory='', registryCollectionId='', registryRootId='', registryRecentDays=0;
        const registryIncludeTags=new Set(), registryExcludeTags=new Set(), activeTab='board', boardFolderSelection={}, registryNavFolder={};
        const boardSourceSelection={board:'__automatic__'};
        const board={id:'board',system:false,automaticHidden:false,sources:[{id:'a',hidden:false},{id:'b',hidden:true}],excludedSourceIds:['c']};
        const assetBoardById=()=>board, registryDynamicPageSize=()=>120;
        ${functionSource('registryQueryString')}
        return registryQueryString;
    `)(URLSearchParams);
    const params = new URLSearchParams(build());
    assert.equal(params.get('dynamic_limit'), '120');
    assert.deepEqual(JSON.parse(params.get('board_sources')), {
        visible:['a'], known:['a','b','c'], automatic:true, automatic_only:true,
    });
});

test('QIN-532 AC3: 路由离开前同步保存，并恢复任一资产看板', () => {
    const save = functionSource('saveRegistryBrowseSession');
    assert.match(save, /if\(!registrySessionReady \|\| !assetBoardById\(activeTab\)\) return false;/, '所有有效系统/自定义看板都必须保存会话');
    assert.doesNotMatch(save, /system !== false/, '系统看板不得被会话保存过滤掉');
    assert.match(MANAGER, /window\.GWAssetBrowseSession = Object\.freeze\(\{save:saveRegistryBrowseSession, saveForRoute:saveRegistryBrowseSessionForRoute\}\)/, '宿主必须能同步触发素材页保存并标记工作台路由');
    assert.match(MANAGER, /REGISTRY_ROUTE_PENDING_KEY = 'gw\.registry\.browse\.route\.pending'/, '工作台离页必须使用独立的一次性恢复标记');

    const restore = functionSource('restoreRegistryBrowseSession');
    assert.match(restore, /saved\.tab && saved\.tab !== 'registry' && assetBoardById\(saved\.tab\)/, '恢复时必须重新激活任何仍有效的看板');
    assert.doesNotMatch(restore, /assetBoardById\(saved\.tab\)\?\.system === false/, '系统看板不得被恢复过滤掉');
    assert.match(restore, /parentNavigation\?\.type === 'reload' && !restoreForShellRoute/, '父页面曾刷新时仍允许明确发生过的工作台路由往返');
    assert.match(restore, /sessionStorage\.removeItem\(REGISTRY_ROUTE_PENDING_KEY\)/, '一次性路由标记必须在恢复尝试后消费');
    assert.match(restore, /return desired/, '恢复流程必须把滚动值交给初始化收尾阶段');
    assert.match(functionSource('applyRegistryBrowseSessionScroll'), /desired <= area\.scrollHeight - area\.clientHeight/, '必须等待滚动容器完成布局后再应用原位置');
    assert.match(functionSource('applyRegistryBrowseSessionScroll'), /area\.scrollTop = desired/, '布局完成后必须写回原滚动位置');
    assert.match(MANAGER, /registrySessionReady = true;\s*if\(Number\.isFinite\(restoredScroll\)\) await applyRegistryBrowseSessionScroll\(restoredScroll\);\s*saveRegistryBrowseSession\(\)/, '最终 session 写入必须发生在还原滚动之后');

    assert.match(ASSETS_CONTROLLER, /document\.addEventListener\('click', saveBrowseStateBeforeRoute, true\)/, '宿主导航点击捕获阶段必须先保存 iframe 状态');
    assert.match(ASSETS_CONTROLLER, /window\.addEventListener\('popstate', saveBrowseStateBeforeRoute, true\)/, '浏览器前进/后退必须先保存 iframe 状态');
    assert.match(ASSETS_CONTROLLER, /typeof document\.addEventListener === 'function'[\s\S]*?typeof window\.addEventListener === 'function'/, '非浏览器轻量宿主中不可假定 DOM 事件 API 存在');
    assert.match(ASSETS_CONTROLLER, /browseSession\?\.saveForRoute\?\.\(\)/, '工作台路由离开必须同步保存并标记恢复');
    assert.match(ASSETS_CONTROLLER, /window\.addEventListener\('pagehide', saveBrowseStateBeforeRoute\)/, '整页离开时必须保存 iframe 状态');
    assert.match(ASSETS_CONTROLLER, /browseSession\?\.save\?\.\(\)/, '整页刷新只保存状态，不标记为工作台路由');
});
