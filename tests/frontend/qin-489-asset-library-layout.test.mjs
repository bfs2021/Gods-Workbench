import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const html = readFileSync(new URL('../../web/pages/assets.html', import.meta.url), 'utf8');
const shell = readFileSync(new URL('../../web/js/controllers/assets-controller.js', import.meta.url), 'utf8');
const manager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const vaultCss = readFileSync(new URL('../../web/css/pages/asset-vault.css', import.meta.url), 'utf8');
const managerCss = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');

test('QIN-540: 三种模式的首次高度估算遵循方形与原图比例', () => {
    const source = manager.slice(manager.indexOf('function registryVirtualCardHeight('), manager.indexOf('function registryVirtualHeightCacheKey('));
    const build = new Function('registryVisualType', 'assetSupportsImagePreview', 'registryAspect', 'registrySimple', `${source}\nreturn registryVirtualCardHeight;`);
    const item = {kind:'image',width:400,height:200};
    const square = build(x => x.kind, () => false, 'square', false)(item, 202);
    const original = build(x => x.kind, () => false, 'original', false)(item, 202);
    const simple = build(x => x.kind, () => false, 'original', true)(item, 202);
    // QIN-541 仅标准卡正文重排到 44；AC7 要求方形模式保留原 92px 正文估算。
    // square = 200 + 92 + 14；original = 100 + 44 + 14；简洁模式正文隐藏，估高 114。
    assert.equal(square, 306);
    assert.equal(original, 158);
    assert.equal(simple, 114);
});

test('QIN-540: 缺宽高的图片加载后写入比例，已有元数据和方形模式保持不变', () => {
    const build = new Function('registryAspect', `${functionSource('applyRegistryVirtualPreviewAspect')}\nreturn applyRegistryVirtualPreviewAspect;`);
    const values = new Map();
    const thumb = {style:{getPropertyValue:key => values.get(key) || '',setProperty:(key,value) => values.set(key,value)}};
    const image = {naturalWidth:300,naturalHeight:600,closest:() => thumb};
    build('original')(image);
    assert.equal(values.get('--asset-media-aspect'), '0.5');
    values.set('--asset-media-aspect', '2');
    build('original')(image);
    assert.equal(values.get('--asset-media-aspect'), '2');
    values.clear();
    build('square')(image);
    assert.equal(values.size, 0);
});

test('QIN-540: 高度变化后按可见卡片恢复滚动锚点', () => {
    const scrollRoot = {scrollTop:150,clientWidth:500,clientHeight:300};
    const grid = {clientWidth:500,closest:() => scrollRoot,style:{setProperty(){}},querySelectorAll:() => [],innerHTML:''};
    const root = {querySelector:() => grid};
    const oldLayout = {key:'key',width:500,cols:1,itemLayouts:[{top:0,height:100},{top:110,height:100}],start:0,end:2,totalHeight:210};
    const newLayout = {width:500,cols:1,itemLayouts:[{top:0,height:200},{top:210,height:100}],totalHeight:310};
    const build = new Function('root','registryVirtualState','registryVirtualItemsForActiveBoard','registryVirtualizationEnabled','registryVirtualStateKey','registryVirtualLayout','registryVirtualRange','captureRegistryPreviewImages','registryVirtualGridMarkup','restoreRegistryPreviewImages','applyRegistryVirtualPreviewAspect','refreshIcons','observeRegistryVirtualCards','scheduleRegistryPreviewImages', `${functionSource('updateRegistryVirtualWindow')}\nreturn updateRegistryVirtualWindow;`);
    const update = build(root,oldLayout,() => ({items:[{id:'a'},{id:'b'}]}),() => true,() => 'key',() => newLayout,() => ({start:0,end:2}),() => new Map(),() => '',() => {},() => {},() => {},() => {},() => {});
    assert.equal(update(true), true);
    assert.equal(scrollRoot.scrollTop, 250);
});

function functionSource(name){
    const start = manager.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const open = manager.indexOf('{', start);
    let depth = 0;
    for(let index = open; index < manager.length; index += 1){
        if(manager[index] === '{') depth += 1;
        else if(manager[index] === '}'){
            depth -= 1;
            if(depth === 0) return manager.slice(start, index + 1);
        }
    }
    assert.fail(`函数未闭合：${name}`);
}

test('QIN-560 AC1/AC2: 资产页只保留 QIN-544 内嵌树，创建入口留在顶栏', () => {
    assert.doesNotMatch(html, /id="assetVaultSidebar"|id="assetCategories"/);
    assert.match(html, /id="assetSidebarCreate" class="hw-btn hw-btn-icon shrink-0" onclick="WorkbenchAssets\.openCreateAssetLibrary\(\)"/);
    assert.match(html, /<main id="assetVaultStage">[\s\S]*?<iframe id="assetVaultFrame"/);
    assert.match(shell, /async function openCreateAssetLibrary\(\)/);
    assert.match(shell, /waitForControl\('#assetBoardTabs \[data-board-create\]'\)/);
    assert.match(manager, /data-asset-tree-tab="system"/);
    assert.match(manager, /data-asset-tree-tab="mine"/);
    assert.doesNotMatch(manager, /\{id:'governance', name:'治理建议'/);
    assert.match(manager, /data-governance-open/);
    assert.match(vaultCss, /html\.vault-shell-embedded[^\n]*#assetBoardTabs \{ display: none !important; \}/);
});

test('AC2: 状态显示在固定顶栏并从嵌入资产管理器回写', () => {
    const statusIndex = html.indexOf('ASSET VAULT');
    const statusNodeIndex = html.indexOf('id="assetVaultStatus"');
    const workspaceIndex = html.indexOf('class="asset-shell-workspace');
    assert.ok(statusIndex >= 0 && statusNodeIndex > statusIndex && statusNodeIndex < workspaceIndex);
    assert.doesNotMatch(html.slice(html.indexOf('id="navPillsGroup"'), workspaceIndex), /assetVaultStatus/);
    assert.match(manager, /window\.parent\.document\.getElementById\('assetVaultStatus'\)/);
    assert.match(manager, /embeddedVault \? '' : `<div id="assetStatus"/);
});

test('AC3/AC14: 左栏移除导入按钮，资产操作放在移出分组行末端', () => {
    // 【QIN-527】左栏底部重复的「智能筛选」按钮已移除，入口收敛到中栏双模机架拨片
    assert.doesNotMatch(html, /id="assetSidebarSmartFilter"/);
    assert.doesNotMatch(html, /WorkbenchAssets\.toggleSmartFilter\(\)/);
    assert.match(html, /<div id="assetSidebarActionState" hidden aria-hidden="true">[\s\S]*id="btnUnassignedInbox"/);
    assert.doesNotMatch(html, /asset-shell-sidebar-actions|assetSidebarSettings|WorkbenchAssets\.uploadAsset\(\)/);
    const toolbarStart = manager.indexOf('function renderRegistryToolbar(');
    const toolbarEnd = manager.indexOf('function renderRegistryEditableTags(', toolbarStart);
    const toolbar = manager.slice(toolbarStart, toolbarEnd);
    const groupActions = toolbar.match(/<div class="registry-toolbar-group-actions">([\s\S]*?)<\/div>\s*<\/div>/)?.[1] || '';
    assert.match(groupActions, /data-registry-group-remove[\s\S]*data-vault-shell-unassigned[\s\S]*data-vault-shell-settings/);
    assert.match(toolbar, /onclick="parent\.GWUnassignedInbox\.open\(\)"/);
    assert.match(toolbar, /onclick="parent\.WorkbenchAssets\.openAssetSettings\(\)"/);
    assert.match(managerCss, /\.registry-toolbar-group-actions\s*\{[^}]*margin-left:\s*auto/);
    assert.match(shell, /function syncToolbarEntry\(button = entry\(\)\)/);
    assert.match(shell, /async function openAssetSettings\(\)/);
    assert.match(shell, /#assetBoardTabs \[data-board-settings-open\]/);
    assert.match(vaultCss, /registry-toolbar \.vault-filters-toggle \{ display: none !important; }/);
    assert.match(vaultCss, /asset-manager-root-registry\.asset-registry-filters-collapsed/);
});

test('AC4: 刷新按钮位于搜索前，搜索框宽度为原来的约一半', () => {
    const toolbarStart = manager.indexOf('function renderRegistryToolbar(');
    const refreshIndex = manager.indexOf('data-refresh-assets', toolbarStart);
    const searchIndex = manager.indexOf('id="registrySearch"', toolbarStart);
    assert.ok(toolbarStart >= 0 && refreshIndex > toolbarStart && searchIndex > refreshIndex);
    assert.match(vaultCss, /registry-refresh \{ order: -2/);
    assert.match(vaultCss, /asset-search-wrap \{ order: -1; flex: 0 1 calc\(25% - 44px\); max-width: calc\(25% - 44px\)/);
    assert.match(vaultCss, /flex-basis: calc\(25% - 52px\)/);
});

test('QIN-505 AC1: 扩展名与导入元数据统一映射到素材分类', () => {
    const extensionStart = manager.indexOf('const REGISTRY_ASSET_CATEGORY_EXTENSIONS =');
    const categoryStart = manager.indexOf('\nfunction registryAssetCategory(', extensionStart);
    const categoryEnd = manager.indexOf('\nfunction customAssetLibraryIncludesAsset(', categoryStart);
    const visualStart = manager.indexOf('\nfunction registryVisualType(', categoryStart);
    const visualEnd = manager.indexOf('\nfunction registryMediaAspectStyle(', visualStart);
    const extensionFunctionStart = manager.indexOf('function registryAssetExtension(');
    assert.ok(extensionStart >= 0 && extensionFunctionStart >= 0 && categoryStart > extensionStart && categoryEnd > categoryStart && visualStart > categoryEnd && visualEnd > visualStart);
    const categorySources = [
        manager.slice(extensionFunctionStart, extensionStart),
        manager.slice(extensionStart, categoryStart),
        manager.slice(categoryStart, categoryEnd),
        manager.slice(visualStart, visualEnd),
    ].join('\n');
    const buildCategory = new Function('assetFileExtension', 'detailViewerType', `${categorySources}\nreturn {category:registryAssetCategory,visual:registryVisualType};`);
    const classify = buildCategory(
        item => String(item.name || '').toLowerCase().match(/\.([a-z0-9]+)$/)?.[1] || '',
        (_source, item) => item.kind === 'text' ? 'text' : ['image','video','audio','document','model','font'].includes(item.kind) ? item.kind : 'other',
    );
    for (const [name, item, expected] of [
        ['frame.png', {kind:'indexed',name:'frame.png'}, 'image'],
        ['clip.mov', {kind:'indexed',name:'clip.mov'}, 'video'],
        ['sound.aiff', {kind:'indexed',name:'sound.aiff'}, 'audio'],
        ['plan.xlsx', {kind:'indexed',name:'plan.xlsx'}, 'document'],
        ['model.glb', {kind:'indexed',name:'model.glb'}, 'model'],
        ['family.woff2', {kind:'indexed',name:'family.woff2'}, 'font'],
        ['path metadata', {kind:'indexed',display_path:'D:\\library\\frame.exr'}, 'image'],
        ['extension metadata', {kind:'indexed',metadata:{extension:'.otf'}}, 'font'],
        ['explicit text kind', {kind:'text'}, 'document'],
        ['unknown.bin', {kind:'indexed',name:'unknown.bin'}, 'other'],
    ]) {
        assert.equal(classify.category(item), expected, name);
        assert.equal(classify.visual(item), expected === 'document' ? 'text' : expected, name);
    }
    assert.match(manager, /\{id:'font', name:'字体'/);
});

test('QIN-505 AC2-AC5: 类型徽标承载分类色，简洁模式保留瀑布流及素材比例', () => {
    assert.doesNotMatch(managerCss, /\.registry-card::before \{[^}]*height:5px/);
    assert.match(vaultCss, /registry-card::before \{ display: none !important; \}/);
    assert.match(managerCss, /\.registry-card-format \{[^}]*background:color-mix\(in hsl,var\(--asset-type-color\) 50%,#000 50%\)/);
    assert.match(vaultCss, /registry-card \.registry-card-format \{[^}]*background: color-mix\(in hsl,var\(--asset-type-color\) 50%,#000 50%\) !important; color: var\(--vault-accent\) !important/);
    assert.doesNotMatch(vaultCss, /registry-card\.asset-type-video \.registry-card-format \{[^}]*color:/);
    // 【QIN-534 更新】金库壳选中态由「深底 + 1px 线」升级为黑金微光渐变 + 暗金描边 + 呼吸光晕（AC1）。
    // 【QIN-534 复验】金色背景亮度提升 50%：两段金色叠层不透明度 .16/.05 → .24/.075。
    assert.match(vaultCss, /registry-card:is\(\.active,\.selected\) \{ background: linear-gradient\(165deg,rgba\(223,195,132,\.24\),rgba\(223,195,132,\.075\)\),#0f1218 !important; border: 1px solid #dfc384 !important; box-shadow: 0 0 10px rgba\(223,195,132,\.22\),inset 0 1px rgba\(255,255,255,\.1\) !important; \}/);
    assert.match(managerCss, /\.registry-card\.active \.registry-card-selection-overlay,\.registry-card\.selected \.registry-card-selection-overlay \{ display:none; \}/);
    assert.match(vaultCss, /grid-template-columns: repeat\(auto-fill,minmax\(min\(100%,var\(--registry-card-size,176px\)\),min\(100%,var\(--registry-card-size,176px\)\)\)\)/);
    assert.match(managerCss, /grid-template-columns:repeat\(auto-fill,minmax\(min\(100%,var\(--registry-card-size,176px\)\),min\(100%,var\(--registry-card-size,176px\)\)\)\)/);
    assert.doesNotMatch(vaultCss, /registry-simple[^}]*aspect-ratio:\s*(?:1|16\/9)/);
    assert.doesNotMatch(vaultCss, /registry-simple \.registry-card \.asset-thumb :is\(img, video\) \{ position: absolute/);
    assert.match(managerCss, /\.registry-simple \.registry-card \.asset-card-body \{ display:none !important; \}/);
    assert.match(vaultCss, /registry-simple \.registry-card \.asset-card-body \{ display: none !important; \}/);
    const heightStart = manager.indexOf('function registryVirtualCardHeight(');
    const heightOpen = manager.indexOf('){', heightStart) + 1;
    let heightDepth = 0;
    let heightEnd = heightOpen;
    for(; heightEnd < manager.length; heightEnd += 1){
        if(manager[heightEnd] === '{') heightDepth += 1;
        else if(manager[heightEnd] === '}' && --heightDepth === 0){ heightEnd += 1; break; }
    }
    assert.ok(heightStart >= 0 && heightOpen > heightStart && heightEnd > heightOpen);
    const estimateHeight = new Function(
        'registryVisualType','assetSupportsImagePreview','registryAspect','registrySimple',
        `${manager.slice(heightStart,heightEnd)}\nreturn registryVirtualCardHeight;`,
    )(
        item => item.kind,
        () => false,
        'original',
        true,
    );
    assert.ok(estimateHeight({kind:'image',width:600,height:1200},176) > estimateHeight({kind:'image',width:1200,height:600},176), '简洁模式虚拟瀑布流按素材原始比例估高');
    const toolbarStart = manager.indexOf('function renderRegistryToolbar(');
    const toolbarEnd = manager.indexOf('\nfunction renderRegistryEditableTags(', toolbarStart);
    const toolbar = manager.slice(toolbarStart, toolbarEnd);
    // QIN-531 修复：胶囊改由 renderRegistrySelectCapsule 产出（自绘 listbox）。
    assert.match(toolbar, /renderRegistrySelectCapsule\(\{kind:'sort', icon:'arrow-down-wide-narrow', label:'排序'\}\)/);
    assert.match(toolbar, /renderRegistrySelectCapsule\(\{kind:'group', icon:'layers', label:'分组'\}\)/);
    assert.match(toolbar, /\$\{coverflowControl\}\s*<button class="hw-btn registry-refresh"/);
    assert.match(toolbar, /options\.hideHeading \? '' :/);
    assert.match(manager, /renderRegistryToolbar\(null, registryCategoryLabel[\s\S]*?\{allowUrl:true,hideHeading:true\}/);
    // QIN-531 修复：原生 select 换成自绘 listbox，改为断言触发器与圆角菜单。
    assert.match(managerCss, /\.registry-toolbar-select \.registry-select-menu \{[^}]*border-radius: 10px/);
    assert.match(managerCss, /registry-toolbar-select > span \{[\s\S]*?display: flex !important;[\s\S]*?align-items: center !important;[\s\S]*?height: 32px !important/);
    assert.match(managerCss, /\.registry-toolbar-select \{[\s\S]*?border: 0 !important;[\s\S]*?background: transparent !important;[\s\S]*?box-shadow: none !important;[\s\S]*?transform: none !important;/);
    assert.match(vaultCss, /registry-toolbar-select \{ height:32px; min-height:32px; align-items:center; \}/);
    assert.match(vaultCss, /registry-coverflow-open \{ order: -3;[^}]*\}/);
    assert.match(managerCss, /\.registry-select-option:is\(:hover,:focus-visible\) \{[\s\S]*?background: rgb\(223 195 132 \/ \.14\) !important/);
    assert.match(managerCss, /\.asset-search-wrap \{[^}]*border: 0 !important/);
    assert.match(managerCss, /\.asset-search \{[\s\S]*?border: 1px solid rgb\(223 195 132 \/ \.2\) !important/);
});

test('QIN-505 AC6: Shift+滚轮即时缩放卡片并保存设置', () => {
    const writes = [];
    let pendingSave;
    let saveCount = 0;
    let status = '';
    let pendingLayout;
    let masonryCalls = 0;
    let masonryRemeasureCalls = 0;
    let virtualCalls = 0;
    const masonryGrid = {};
    const content = {style:{setProperty:(key,value) => writes.push([key,value])}};
    const root = {
        querySelectorAll:selector => selector === '.registry-content' ? [content] : selector.includes('.registry-grid:not') ? [masonryGrid] : [],
        querySelector:() => null,
    };
    const handle = new Function(
        'root','REGISTRY_CARD_SIZE','clearTimeout','setTimeout','saveRegistryViewSettings','setStatus','requestAnimationFrame','scheduleRegistryMasonryLayout','updateRegistryVirtualWindow','remeasureRegistryMasonryGrid',
        `let registryCardSize=176; let registryCardSizeSaveTimer=0; let registryCardSizeLayoutFrame=0; let registryCardSizeVirtualMeasureRevision=0; ${functionSource('handleRegistryCardResizeWheel')}\nreturn {handle:handleRegistryCardResizeWheel,size:()=>registryCardSize};`,
    )(
        root,
        {min:120,max:360},
        () => {},
        callback => { pendingSave = callback; return 1; },
        () => { saveCount += 1; },
        value => { status = value; },
        callback => { pendingLayout = callback; return 1; },
        () => { masonryCalls += 1; },
        () => { virtualCalls += 1; },
        grid => { assert.equal(grid, masonryGrid); masonryRemeasureCalls += 1; },
    );
    let prevented = false;
    handle.handle({
        shiftKey:true,ctrlKey:false,metaKey:false,altKey:false,deltaY:1,
        target:{closest:selector => selector === '[data-registry-zoom-area]' || selector === '.registry-content.registry-layout-grid'},
        preventDefault:() => { prevented = true; },
    });
    assert.equal(prevented, true);
    assert.equal(handle.size(), 164);
    assert.deepEqual(writes, [['--registry-card-size','164px']]);
    pendingLayout();
    assert.equal(masonryRemeasureCalls, 1, '普通瀑布流在下一帧强制重测所有卡片的新高度');
    assert.equal(masonryCalls, 1, '非虚拟瀑布流在下一帧立即重测卡片高度');
    assert.equal(virtualCalls, 0);
    pendingSave();
    assert.equal(saveCount, 1);
    assert.equal(status, '预览大小 164px');
});

test('QIN-505 AC6: 普通瀑布流缩放会使旧高度跨度失效并重测', () => {
    const cards = [
        {dataset:{},classList:{contains:() => false}},
        {dataset:{},classList:{contains:() => false}},
    ];
    const grid = {
        classList:{contains:() => false},
        querySelectorAll:selector => selector === ':scope > .registry-card' ? cards : [],
    };
    let measured;
    const remeasure = new Function(
        'updateRegistryMasonryCards',
        `${functionSource('remeasureRegistryMasonryGrid')}\nreturn remeasureRegistryMasonryGrid;`,
    )((actualCards, actualGrid) => { measured = {cards:actualCards,grid:actualGrid}; });
    remeasure(grid);
    assert.ok(cards.every(card => card.dataset.registryMasonryDirty === 'true'));
    assert.deepEqual(measured, {cards,grid});
});

test('QIN-505 AC6: 虚拟瀑布流缩放后快速重算卡片列与位置', () => {
    const pendingLayouts = [];
    let forceCalls = 0;
    const lastArguments = [];
    const content = {style:{setProperty:() => {}}};
    const root = {
        querySelectorAll:selector => selector === '.registry-content' ? [content] : [],
        querySelector:selector => selector === '.registry-grid-virtualized' ? {} : null,
    };
    const handle = new Function(
        'root','REGISTRY_CARD_SIZE','clearTimeout','setTimeout','saveRegistryViewSettings','setStatus','requestAnimationFrame','scheduleRegistryMasonryLayout','updateRegistryVirtualWindow','remeasureRegistryMasonryGrid',
        `let registryCardSize=176; let registryCardSizeSaveTimer=0; let registryCardSizeLayoutFrame=0; let registryCardSizeVirtualMeasureRevision=0; ${functionSource('handleRegistryCardResizeWheel')}\nreturn handleRegistryCardResizeWheel;`,
    )(
        root,
        {min:120,max:360},
        () => {},
        () => 1,
        () => {},
        () => {},
        callback => { pendingLayouts.push(callback); return pendingLayouts.length; },
        () => assert.fail('虚拟瀑布流不得调用常规高度观察器'),
        (...args) => { forceCalls += 1; lastArguments.push(args); },
        () => assert.fail('虚拟瀑布流不得调用普通网格高度重测'),
    );
    handle({
        shiftKey:true,ctrlKey:false,metaKey:false,altKey:false,deltaY:-1,
        target:{closest:selector => selector === '[data-registry-zoom-area]' || selector === '.registry-content.registry-layout-grid'},
        preventDefault:() => {},
    });
    pendingLayouts.shift()();
    assert.deepEqual(lastArguments[0], [true,true], '第一帧先用最新宽度估算重排，避免整库离屏测量卡住预览');
    assert.equal(pendingLayouts.length, 1, '先布局后安排可见卡片精确回测');
    pendingLayouts.shift()();
    assert.deepEqual(lastArguments[1], [true,true], '第二帧从当前尺寸的可见卡片回填精确高度并再次排布');
    assert.equal(forceCalls, 2);
});

test('QIN-560 AC4: 窄屏内容区仍由内嵌单栏树和主区域承载', () => {
    assert.doesNotMatch(html, /#assetCategories|#assetVaultSidebar/);
    assert.match(html, /#assetVaultStage \{ flex: 1; min-width: 0; min-height: 0; display: flex/);
    assert.match(html, /#assetVaultFrame \{ flex: 1; width: 100%;/);
    assert.match(manager, /renderAssetTreePanel\(\)/);
});

test('AC7: 左栏全部入口共用固定统计行和工具栏行，视图留在资源库页内切换', () => {
    assert.match(manager, /if\(document\.documentElement\.classList\.contains\('vault-shell-embedded'\)\)[\s\S]*root\.insertAdjacentHTML\('afterbegin', `\$\{renderRegistryOverview\(\)\}\$\{renderRegistryToolbar\(null,/);
    assert.ok(manager.includes("${embeddedVault ? '' : renderRegistryOverview()}"), '独立资产管理页仍显示统计区');
    assert.ok(manager.includes("${embeddedVault ? '' : renderRegistryToolbar(null, registryCategoryLabel"), '独立资产总览仍显示工具栏');
    assert.ok(manager.includes("${embeddedVault ? '' : renderRegistryToolbar(board, board.name"), '独立自建资源库仍显示工具栏');
    assert.match(vaultCss, /#assetManagerRoot > \.registry-overview-shell \{[\s\S]*grid-row: 1;/);
    assert.match(vaultCss, /#assetManagerRoot > \.registry-toolbar \{[\s\S]*grid-row: 2;/);
    assert.match(vaultCss, /#assetManagerRoot > \.asset-panel\.asset-content \{[\s\S]*grid-row: 3;/);
    for (const id of ['registry', 'scripts', 'projects', 'assets', 'workflows', 'prompts', 'canvas-assets', 'local']) {
        assert.ok(manager.includes(`id:'${id}'`), `资源库缺少入口：${id}`);
    }
    assert.doesNotMatch(manager, /activeTab === 'governance'/);
});

test('资产列表将本地 indexed 素材映射为媒体 URL 并兼容 size_bytes', () => {
    const start = manager.indexOf('function normalizeRegistryAsset(item={}){');
    const end = manager.indexOf('\nfunction registryStructureId', start);
    assert.ok(start >= 0 && end > start, '资产归一化函数应可定位');
    const normalizeRegistryAsset = runInNewContext(`${manager.slice(start, end)}\nnormalizeRegistryAsset`);

    const local = normalizeRegistryAsset({
        id: 'ast_local',
        kind: 'indexed',
        storage_root_id: 'root_local',
        display_path: 'images/frame.png',
        size_bytes: 2745794,
    });
    assert.equal(local.url, '/api/asset-registry/assets/ast_local/media');
    assert.equal(local.size, 2745794);

    const explicit = normalizeRegistryAsset({
        id: 'ast_explicit',
        storage_root_id: 'root_local',
        latest_image_version_url: '/versions/original/media',
        public_url: '/public.png',
        url: '/asset.png',
        size_bytes: '42',
    });
    assert.equal(explicit.url, '/versions/original/media');
    assert.equal(explicit.size, 42);

    const remote = normalizeRegistryAsset({
        id: 'ast_remote',
        is_remote: true,
        storage_root_id: 'root_local',
        size_bytes: 7,
    });
    assert.equal(remote.url, '');
});

test('QIN-504 AC1: 虚拟网格渲染后重新绑定滚动窗口', () => {
    const renderStart = manager.indexOf('function render(){');
    const renderEnd = manager.indexOf('\nfunction updateSearchQueryFromInput', renderStart);
    assert.ok(renderStart >= 0 && renderEnd > renderStart);
    const render = manager.slice(renderStart, renderEnd);
    assert.ok(render.indexOf('scheduleRegistryVirtualWindow();') > render.indexOf('scheduleRegistryMasonryLayout();'));
});

test('QIN-504 AC2: 虚拟卡片按预览尺寸占用固定宽度列', () => {
    const columnSource = functionSource('registryGridColumnCount');
    const heightKeySource = functionSource('registryVirtualHeightCacheKey');
    const measureSource = functionSource('registryVirtualMeasureCardHeights');
    const layoutSource = functionSource('registryVirtualLayout');
    const grid = {clientWidth:1750,querySelectorAll:() => []};
    let template = '176px 176px 176px 176px 176px 176px 176px 176px 176px';
    const buildLayout = new Function(
        'root','getComputedStyle','REGISTRY_VIRTUAL_GAP','REGISTRY_CARD_SIZE','registryCardSize','registrySimple','registryAspect','registryVirtualCardHeight','registryVirtualHeightCache','registryVirtualHeightCacheMode',
        `${columnSource}\n${heightKeySource}\n${measureSource}\n${layoutSource}\nreturn registryVirtualLayout;`,
    );
    const layout = buildLayout(
        {querySelector:selector => selector === '.registry-grid' ? grid : null},
        () => ({gridTemplateColumns:template,columnGap:'10px',rowGap:'10px',gridAutoRows:'8px'}),
        10,
        {min:96,max:480,fallback:176},
        176,
        false,
        'square',
        () => 300,
        new Map(),
        '',
    );
    const regular = layout([{id:'a'},{id:'b'}]);
    assert.equal(regular.cols, 9);
    assert.equal(regular.cardWidth, 176);
    template = 'repeat(9, minmax(0px, 1fr))';
    assert.equal(layout([{id:'a'},{id:'b'}]).cols, 9);
});

test('QIN-504 AC6: 瀑布流进入虚拟窗口后保留逐列高度差并完整覆盖可视区', () => {
    const columnSource = functionSource('registryGridColumnCount');
    const heightKeySource = functionSource('registryVirtualHeightCacheKey');
    const measureSource = functionSource('registryVirtualMeasureCardHeights');
    const layoutSource = functionSource('registryVirtualLayout');
    const rowSource = functionSource('registryVirtualRowAt');
    const rangeSource = functionSource('registryVirtualRange');
    const grid = {clientWidth:1750,querySelectorAll:() => []};
    const buildVirtualLayout = new Function(
        'root','getComputedStyle','REGISTRY_VIRTUAL_GAP','REGISTRY_CARD_SIZE','registryCardSize','registrySimple','registryAspect','registryVirtualCardHeight','registryVirtualHeightCache','registryVirtualHeightCacheMode','REGISTRY_VIRTUAL_OVERSCAN_ROWS','registryFocusRestoreId',
        `${columnSource}\n${heightKeySource}\n${measureSource}\n${layoutSource}\n${rowSource}\n${rangeSource}\nreturn {registryVirtualLayout, registryVirtualRange};`,
    );
    const {registryVirtualLayout, registryVirtualRange} = buildVirtualLayout(
        {querySelector:selector => selector === '.registry-grid' ? grid : null},
        () => ({gridTemplateColumns:'repeat(9, minmax(0px, 1fr))',columnGap:'10px',rowGap:'10px',gridAutoRows:'8px'}),
        10,
        {min:96,max:480,fallback:176},
        176,
        false,
        'original',
        item => item.virtualHeight,
        new Map(),
        '',
        3,
        '',
    );
    const heights = [500,100,200,300,400,150,250];
    const items = Array.from({length:301}, (_,index) => ({
        id:`asset-${index}`,
        virtualHeight:heights[index % heights.length],
    }));
    const beforeAppend = registryVirtualLayout(items.slice(0,100));
    const layout = registryVirtualLayout(items);

    assert.equal(layout.cols, 9);
    assert.deepEqual(layout.itemLayouts.slice(0, 9).map(position => position.top), Array(9).fill(0));
    assert.deepEqual(layout.itemLayouts.slice(0, 9).map(position => position.column), [0,1,2,3,4,5,6,7,8]);
    for(let column=0; column<layout.cols; column++){
        const positions = layout.itemLayouts.filter(position => position.column === column);
        for(let i=1; i<positions.length; i++){
            assert.equal(positions[i].top - (positions[i-1].top + positions[i-1].height), 10, '同列卡片间距固定且不重叠');
        }
    }
    assert.ok(layout.rowTops.every((top,index,tops) => index === 0 || top >= tops[index - 1]), '按素材顺序的 top 位置保持单调，供二分窗口定位');
    assert.deepEqual(
        layout.itemLayouts.slice(0,100).map(({left,top,span}) => [left,top,span]),
        beforeAppend.itemLayouts.map(({left,top,span}) => [left,top,span]),
        '加载第二页时，已有卡片位置不应被重新打乱',
    );

    const scrollTop = 3000;
    const viewportHeight = 500;
    const range = registryVirtualRange(items, layout, scrollTop, viewportHeight);
    const visibleStart = scrollTop - 3 * 260;
    const visibleEnd = scrollTop + viewportHeight + 3 * 260;
    assert.ok(range.start > 0);
    assert.ok(range.end < items.length);
    layout.itemLayouts.forEach((position,index) => {
        if(position.top + position.height >= visibleStart && position.top <= visibleEnd){
            assert.ok(index >= range.start && index < range.end, `可视区中的 ${items[index].id} 应在虚拟窗口中`);
        }
    });
});

test('QIN-504 AC6: 现有卡片按 DOM 实测高度续接 CSS 瀑布行', () => {
    const columnSource = functionSource('registryGridColumnCount');
    const heightKeySource = functionSource('registryVirtualHeightCacheKey');
    const measureSource = functionSource('registryVirtualMeasureCardHeights');
    const layoutSource = functionSource('registryVirtualLayout');
    const measuredCards = [
        {dataset:{registryCard:'asset-a'},getBoundingClientRect:() => ({height:501})},
        {dataset:{registryCard:'asset-b'},getBoundingClientRect:() => ({height:101})},
    ];
    const grid = {clientWidth:1750,querySelectorAll:() => measuredCards};
    const buildLayout = new Function(
        'root','getComputedStyle','REGISTRY_VIRTUAL_GAP','REGISTRY_CARD_SIZE','registryCardSize','registrySimple','registryAspect','registryVirtualCardHeight','registryVirtualHeightCache','registryVirtualHeightCacheMode',
        `${columnSource}\n${heightKeySource}\n${measureSource}\n${layoutSource}\nreturn registryVirtualLayout;`,
    );
    const layout = buildLayout(
        {querySelector:selector => selector === '.registry-grid' ? grid : null},
        () => ({gridTemplateColumns:'repeat(7, minmax(0px, 1fr))',columnGap:'10px',rowGap:'10px',gridAutoRows:'8px'}),
        10,
        {min:96,max:480,fallback:176},
        176,
        false,
        'original',
        item => item.virtualHeight,
        new Map(),
        '',
    )([
        {id:'asset-a',virtualHeight:150},
        {id:'asset-b',virtualHeight:500},
        {id:'asset-c',virtualHeight:220},
    ]);

    assert.equal(layout.itemLayouts[0].height, 501);
    assert.equal(layout.itemLayouts[1].height, 101);
    const firstSpan = layout.itemLayouts[0].span;
    const previousSpanHeight = Math.max(0, firstSpan - 1) * layout.rowHeight + Math.max(0, firstSpan - 2) * layout.rowGap;
    const assignedSpanHeight = firstSpan * layout.rowHeight + (firstSpan - 1) * layout.rowGap;
    assert.ok(previousSpanHeight < layout.itemLayouts[0].height && assignedSpanHeight >= layout.itemLayouts[0].height);
});

test('QIN-504 AC6: 首次虚拟渲染沿用已有滚动窗口，避免回顶后重复绘制', () => {
    const renderSource = functionSource('renderRegistryVirtualGrid');
    let measuredWidth = 0;
    const grid = {clientWidth:1720};
    const scrollRoot = {clientWidth:1750,clientHeight:700,scrollTop:1234,querySelector:selector => selector === '.registry-grid' ? grid : null};
    const root = {querySelector:selector => selector === '.registry-assets-scroll' ? scrollRoot : null};
    const layout = {cols:7,width:1720,totalHeight:9000,itemLayouts:Array.from({length:301},(_,index)=>({top:index*18,left:0,width:241,height:200}))};
    let rangeArgs = null;
    const buildRenderer = new Function(
        'root','registryVirtualLayout','registryVirtualRange','registryVirtualStateKey','registryVirtualGridMarkup','savedScrollPositions',
        `let registryVirtualState={};\n${renderSource}\nreturn {renderRegistryVirtualGrid, state:()=>registryVirtualState};`,
    );
    const renderer = buildRenderer(
        root,
        (_items,width) => { measuredWidth = width; return layout; },
        (...args) => { rangeArgs = args; return {start:30,end:90}; },
        () => 'asset-key',
        (_items,_layout,range) => `window:${range.start}-${range.end}`,
        {registry:0},
    );

    assert.equal(renderer.renderRegistryVirtualGrid(Array(301).fill({id:'asset'})), 'window:30-90');
    assert.equal(rangeArgs[2], 1234, '首次窗口应直接匹配已有滚动位置');
    assert.equal(rangeArgs[3], 700, '首次窗口应使用真实可视区高度');
    assert.equal(measuredWidth, 1720, '首次布局使用现有网格的实际宽度');
    assert.equal(renderer.state().width, 1720, '首次绘制即记录实际宽度，避免挂载后无意义整窗重绘');
});

test('QIN-504 AC6: 新网格首次绘制前同步恢复滚动锚点', () => {
    const restoreSource = functionSource('restoreRegistryVirtualScroll');
    let scrollTop = 0;
    const scrollRoot = {
        get scrollTop(){ return scrollTop; },
        set scrollTop(value){ scrollTop = value; },
        querySelectorAll:() => [{
            dataset:{registryCard:'asset-a'},
            getBoundingClientRect:() => ({top:1300 - scrollTop}),
        }],
        getBoundingClientRect:() => ({top:0}),
    };
    const restore = new Function(`${restoreSource}\nreturn restoreRegistryVirtualScroll;`)();

    restore(scrollRoot, 1200, 'asset-a', 80);

    assert.equal(scrollRoot.scrollTop, 1220, '按旧卡片视口偏移修正新容器位置，避免首帧从顶部重绘');
});

test('QIN-504 AC3: 快速分页后同步资产库计数与续载状态', () => {
    const footer = functionSource('syncRegistryVaultFooter');
    const loadStart = manager.indexOf('async function loadMoreRegistryAssets(){');
    const loadEnd = manager.indexOf('// 【浏览预加载】契约', loadStart);
    assert.ok(loadStart >= 0 && loadEnd > loadStart);
    assert.match(footer, /已加载\$\{registryPageHasMore/);
    assert.match(footer, /registryPageHasMore \? '下滑加载更多'/);
    assert.match(manager.slice(loadStart, loadEnd), /syncRegistryVaultFooter\(\);/);
});
test('QIN-504 AC5: 图片延迟加载使用透明占位，透出素材卡片深色底色', () => {
    const deferred = functionSource('deferRegistryPreviewThumb');
    const placeholder = manager.match(/const REGISTRY_PREVIEW_PLACEHOLDER = '([^']+)';/);
    assert.ok(placeholder, '应定义缩略图占位资源');
    assert.match(placeholder[1], /^data:image\/svg\+xml,/);
    assert.doesNotMatch(placeholder[1], /(?:fill=|#(?:fff|ffffff))/i);
    assert.match(deferred, /setAttribute\('src', REGISTRY_PREVIEW_PLACEHOLDER\)/);
});

test('QIN-504 AC6: 已加载预览离开虚拟窗口后上下返回仍复用原图片节点', () => {
    const loadedImage = {
        dataset:{registryPreviewLoaded:'true'},
        currentSrc:'https://example.test/thumb/asset-a.png',
        isConnected:true,
    };
    const card = image => ({
        dataset:{registryCard:'asset-a'},
        querySelectorAll:() => [image],
    });
    loadedImage.closest = () => card(loadedImage);
    const placeholder = {
        dataset:{registryPreviewSrc:'/thumb/asset-a.png'},
        closest:null,
        replaceWith(image){
            this.replacedWith = image;
            image.isConnected = true;
            this.isConnected = false;
        },
    };
    placeholder.closest = () => card(placeholder);
    let visibleImages = [loadedImage];
    const root = {
        querySelectorAll(selector){
            return selector.includes('registry-preview-loaded')
                ? visibleImages.filter(image => image.dataset.registryPreviewLoaded === 'true')
                : visibleImages.filter(image => image.dataset.registryPreviewSrc);
        },
    };
    const buildCache = new Function(
        'root','document',
        `const registryPreviewImageCache = new Map();\n${functionSource('registryPreviewCacheKey')}\n${functionSource('captureRegistryPreviewImages')}\n${functionSource('restoreRegistryPreviewImages')}\nreturn {captureRegistryPreviewImages, restoreRegistryPreviewImages};`,
    );
    const {captureRegistryPreviewImages, restoreRegistryPreviewImages} = buildCache(root, {baseURI:'https://example.test/assets.html'});
    const cache = captureRegistryPreviewImages();

    loadedImage.isConnected = false;
    visibleImages = [placeholder];
    restoreRegistryPreviewImages(cache);
    assert.equal(placeholder.replacedWith, loadedImage);

    loadedImage.isConnected = false;
    const returnedPlaceholder = {...placeholder, replacedWith:null};
    returnedPlaceholder.closest = () => card(returnedPlaceholder);
    visibleImages = [returnedPlaceholder];
    restoreRegistryPreviewImages(cache);
    assert.equal(returnedPlaceholder.replacedWith, loadedImage);

    const update = functionSource('updateRegistryVirtualWindow');
    const captureIndex = update.indexOf('captureRegistryPreviewImages()');
    const rebuildIndex = update.indexOf('grid.innerHTML =');
    const restoreIndex = update.indexOf('restoreRegistryPreviewImages(registryPreviewCache)');
    const scheduleIndex = update.indexOf('scheduleRegistryPreviewImages()');
    assert.ok(captureIndex >= 0 && rebuildIndex > captureIndex && restoreIndex > rebuildIndex && scheduleIndex > restoreIndex);
});

test('QIN-504 AC7: 只在向下滚动时预加载视口外下一批20张预览', () => {
    const visibleImages = [100, 200, 300].map((top,index) => ({id:`visible-${index}`, baseTop:top}));
    const aheadImages = Array.from({length:40}, (_,index) => ({id:`ahead-${index}`, baseTop:1000 + index * 100}));
    const images = [...visibleImages, ...aheadImages].map(image => ({
        id:image.id,
        dataset:{registryPreviewSrc:`/thumb/${image.id}.png`},
        getBoundingClientRect(){
            const top = image.baseTop - scrollRoot.scrollTop;
            return {top,bottom:top + 80,left:10,right:90};
        },
    }));
    const callbacks = [];
    const listeners = {};
    let currentTime = 500;
    const scrollRoot = {
        scrollTop:0,
        addEventListener(name,listener){listeners[name] = listener;},
        removeEventListener(name){delete listeners[name];},
        getBoundingClientRect(){return {top:0,bottom:600,left:0,right:100};},
    };
    const activated = [];
    const activate = image => {
        if(!image.dataset.registryPreviewSrc || image.dataset.registryPreviewQueued === 'true') return false;
        activated.push(image.id || image.dataset.registryPreviewSrc);
        image.dataset.registryPreviewQueued = 'true';
        return true;
    };
    const buildScheduler = new Function(
        'root','REGISTRY_PREVIEW_SCROLL_AHEAD_COUNT','requestAnimationFrame','activateRegistryPreviewImage','performance','REGISTRY_PREVIEW_SCROLL_PRELOAD_INTERVAL_MS',
        `let registryPreviewFallbackCleanup = null;\n${functionSource('scheduleRegistryPreviewFallback')}\nreturn scheduleRegistryPreviewFallback;`,
    );
    const schedule = buildScheduler(
        {querySelectorAll:() => images.filter(image => image.dataset.registryPreviewSrc)},
        20,
        callback => {callbacks.push(callback); return callbacks.length;},
        activate,
        {now:() => currentTime},
        120,
    );
    schedule(scrollRoot);
    assert.deepEqual(activated, visibleImages.map(image => image.id));

    scrollRoot.scrollTop = 50;
    listeners.scroll();
    callbacks.shift()();
    assert.equal(activated.length, 3, '普通滚动只补充当前视口素材');

    listeners.wheel({deltaY:50});
    callbacks.shift()();
    assert.equal(activated.length, 23, '下滑后只在当前可见项之外再加载20张');
    assert.equal(activated.filter(id => id.includes('ahead-')).length, 20);

    currentTime = 550;
    listeners.wheel({deltaY:50});
    assert.equal(callbacks.length, 0, '同一连续滚轮动作的重复事件不重复排一批');

    scrollRoot.scrollTop = 0;
    currentTime = 560;
    listeners.wheel({deltaY:-50});
    callbacks.shift()();
    assert.equal(activated.length, 23, '上滑不继续加载新的视口外批次');

    scrollRoot.scrollTop = 50;
    currentTime = 570;
    listeners.wheel({deltaY:50});
    callbacks.shift()();
    assert.equal(activated.length, 43, '继续下滑时才加载下一批20张');
    // QIN-518 AC2：预览预取改为带 200px 视口提前量，消除快速滚动白块。
    assert.equal(manager.match(/const REGISTRY_PREVIEW_ROOT_MARGIN = '([^']+)'/)?.[1], '200px 0px');

    assert.match(manager, /const REGISTRY_PAGE_SIZE = 1500/);
    assert.match(functionSource('loadMoreRegistryAssets'), /registryQueryString\(\{offset,cursor\}\)/);
    assert.match(functionSource('prefetchRegistryNextPage'), /registryQueryString\(\{offset,cursor\}\)/);
});

test('QIN-532: Shift+滚轮可缩至 96px 并提示边界', () => {
    let save;
    let status = '';
    const content = {style:{setProperty(){}}};
    const root = {querySelectorAll:() => [content], querySelector:() => null};
    const handle = new Function(
        'root','REGISTRY_CARD_SIZE','clearTimeout','setTimeout','saveRegistryViewSettings','setStatus','requestAnimationFrame','scheduleRegistryMasonryLayout','updateRegistryVirtualWindow','remeasureRegistryMasonryGrid',
        `let registryCardSize=108; let registryCardSizeSaveTimer=0; let registryCardSizeLayoutFrame=0; let registryCardSizeVirtualMeasureRevision=0; ${functionSource('handleRegistryCardResizeWheel')}\nreturn {handle:handleRegistryCardResizeWheel,size:()=>registryCardSize};`,
    )(root, {min:96,max:360}, () => {}, callback => {save = callback;}, () => {}, value => {status = value;}, () => 1, () => {}, () => {}, () => {});
    handle.handle({shiftKey:true, deltaY:1, target:{closest:() => true}, preventDefault(){}});
    save();
    assert.equal(handle.size(), 96);
    assert.equal(status, '预览大小 96px（已到最小）');
});

test('QIN-532: 图片异步改变高度后合并重测并修正虚拟布局', () => {
    let frame;
    let updates = 0;
    const cards = [{dataset:{registryCard:'a'}, getBoundingClientRect:() => ({height:300})}];
    const grid = {querySelectorAll:() => cards};
    const schedule = new Function('root','requestAnimationFrame','registryVirtualItemsForActiveBoard','registryVirtualState','updateRegistryVirtualWindow',
        `let registryVirtualRemeasureFrame=0; ${functionSource('scheduleRegistryVirtualRemeasure')}\nreturn scheduleRegistryVirtualRemeasure;`)(
        {querySelector:() => grid}, callback => {frame=callback; return 1;},
        () => ({items:[{id:'a'}]}), {itemLayouts:[{height:120}]},
        force => {assert.equal(force,true); updates++;},
    );
    schedule();
    schedule();
    frame();
    assert.equal(updates,1);
});
