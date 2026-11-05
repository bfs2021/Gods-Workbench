// QIN-546: 我的资源整理交互——右键菜单、拖拽移动、重名处理与撤销
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');
const manifest = readFileSync(new URL('./node-files.txt', import.meta.url), 'utf8');

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

function functionSlice(name){
    // 参数含解构（{a=1}）时花括号计数会提前归零，这里按顶层 `\n}` 截取
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `函数应存在：${name}`);
    const end = source.indexOf(String.fromCharCode(10) + '}', start);
    assert.ok(end > start, `函数未闭合：${name}`);
    return source.slice(start, end + 2);
}

function loadOrganizeHelpers({mine = true, roots = ['root_a'], diskPath = id => `/mnt/${id}`} = {}){
    const names = [        'assetOrganizeIsMineTree',
        'assetOrganizeWritableRoot',
        'assetOrganizeTargetFromKey',
        'assetOrganizeParentPath',
        'assetOrganizeLeafName',
        'assetOrganizeAbsPath',
        'assetOrganizeErrorText',
    ];
    const body = names.map(functionSource).join('\n');
    const factory = new Function(
        'assetTreeTab',
        'assetRackRootDiskPath',
        `${body}; return {assetOrganizeTargetFromKey, assetOrganizeParentPath, assetOrganizeLeafName, assetOrganizeAbsPath, assetOrganizeErrorText};`
    );
    return factory(mine ? 'mine' : 'system', diskPath);
}

test('AC1: 右键菜单提供新建/重命名/删除/在系统中打开，双击改名与分组「+」入口', () => {
    const menu = functionSource('renderAssetOrganizeMenu');
    for(const label of ['新建子文件夹', '重命名', '在系统中打开', '删除（进回收站）']){
        assert.ok(menu.includes(label), `右键菜单应含：${label}`);
    }
    assert.match(menu, /data-asset-organize-new/);
    assert.match(menu, /data-asset-organize-rename/);
    assert.match(menu, /data-asset-organize-open/);
    assert.match(menu, /data-asset-organize-delete/);
    // 挂载根不允许改名与删除，只做新建/打开
    assert.match(menu, /asset-organize-rename \$\{target\.isRoot \? 'disabled' : ''\}/, '挂载根禁止重命名');
    assert.match(menu, /asset-organize-delete \$\{target\.isRoot \? 'disabled' : ''\}/, '挂载根禁止删除');
    // 双击名称内联改名
    assert.match(source, /document\.addEventListener\('dblclick', event => \{[\s\S]{0,400}asset-tree-name/, '双击名称应触发内联改名');
    assert.match(functionSource('beginAssetOrganizeRename'), /data-asset-organize-rename-input/, '内联输入框应带稳定 data 属性');
    assert.match(functionSource('commitAssetOrganizeRename'), /'PATCH'/, '改名走 PATCH 整理接口');
    // 分组标题悬停「+」
    const mineTree = functionSource('renderAssetTreeMineTree');
    assert.match(mineTree, /data-asset-organize-group-new/, '分组标题应提供新建入口');
    assert.match(styles, /\.asset-tree-group-label:hover \.asset-tree-group-add/, '「+」应悬停显示');
    // 删除先确认、进回收站
    const del = functionSource('assetOrganizeDeleteFolder');
    assert.match(del, /kind:'delete'/, '删除必须先弹确认');
    assert.match(del, /folders\/delete/, '删除走回收站接口');
});

test('AC2: 拖拽移动——卡片多选、文件夹整树移动、落点区分放进去与插到中间', () => {
    // 素材卡片带整理拖拽属性；非「我的资源」视图在 dragstart 里显式取消
    assert.match(source, /data-registry-card="\$\{escapeAttr\(item\.id\)\}" draggable="true" data-registry-drag="\$\{escapeAttr\(item\.id\)\}"/, '素材卡片应带整理拖拽属性');
    const dragStart = functionSource('beginAssetOrganizeDrag');
    assert.match(dragStart, /assetOrganizeIsMineTree\(\)\)\{ event\.preventDefault\(\); return; \}/, '系统资源视图取消拖拽');
    assert.match(dragStart, /registrySelectedIds/, '多选时整组一起拖');
    const fab = functionSource('assetTreeFolderNodeFor');
    assert.match(fab, /draggable:true/, '子目录节点可拖动');
    const nodeStart = source.indexOf('function renderAssetTreeNode(');
    const nodeSource = source.slice(nodeStart, source.indexOf(String.fromCharCode(10) + '}', nodeStart) + 2);
    assert.match(nodeSource, /data-asset-tree-drag/, '节点渲染输出拖拽 key');
    assert.match(nodeSource, /draggable="true"/, '仅可整理节点输出 draggable');
    // 落点区分
    const zone = functionSource('assetOrganizeDropZoneFor');
    assert.match(zone, /'insert'/, '应存在「插到中间」落点');
    assert.match(zone, /'into'/, '应存在「放进去」落点');
    assert.match(styles, /is-organize-drop-into/, '放进去需高亮');
    assert.match(styles, /is-organize-drop-before/, '插到中间需显示插入线');
    // 真实磁盘移动
    assert.match(functionSource('assetOrganizeMoveOne'), /\/api\/local-assets\/move/, '文件移动走磁盘移动接口');
    assert.match(functionSource('assetOrganizeMoveFolder'), /folders\/move/, '文件夹移动走子树移动接口');
    assert.match(functionSource('assetOrganizeDropTargetFor'), /startsWith\(`\$\{assetOrganizeDrag\.path\}\/`\)/, '整树移动禁止落入自身子目录');
    assert.match(functionSource('assetOrganizeErrorText'), /INVALID_MOVE_DESTINATION/, '非法落点有明确文案');
});

test('AC3: 重名弹窗提供跳过/改名/覆盖，文件占用有明确提示', () => {
    const dialog = functionSource('renderAssetOrganizeDialog');
    for(const label of ['跳过', '改名', '覆盖']){
        assert.ok(dialog.includes(label), `重名弹窗应提供：${label}`);
    }
    assert.match(dialog, /data-asset-organize-conflict="skip"/);
    assert.match(dialog, /data-asset-organize-conflict="rename"/);
    assert.match(dialog, /data-asset-organize-conflict="overwrite"/);
    const move = functionSource('assetOrganizeMoveOne');
    assert.match(move, /assetOrganizeAskConflict/, '遇到 TARGET_EXISTS 应弹重名处理');
    assert.match(move, /conflict_resolution:choice/, '选择应回传冲突策略');
    assert.match(functionSource('assetOrganizeErrorText'), /FILE_LOCKED/, '文件占用需独立错误文案');
});

test('AC4: Ctrl+Z 撤销最近操作并给出 toast 提示', () => {
    const keydown = functionSource('handleAssetOrganizeKeydown');
    assert.match(keydown, /ctrlKey \|\| event\.metaKey/, '支持 Ctrl/Cmd');
    assert.match(keydown, /!== 'z'/, '快捷键为 Z');
    assert.match(keydown, /assetOrganizeUndoStack\.length/, '无历史时不拦截');
    assert.match(keydown, /detailViewerState\?\.open/, '详情查看态让位给既有撤销');
    assert.match(functionSource('assetOrganizeUndoLast'), /\/api\/local-assets\/undo/, '撤销走整理撤销接口');
    assert.match(functionSlice('showAssetOrganizeToast'), /asset-organize-toast/, '需要 toast 提示');
    assert.match(functionSlice('showAssetOrganizeToast'), /data-asset-organize-undo-now/, 'toast 内提供撤销入口');
    assert.match(styles, /\.asset-organize-toast \{/, 'toast 样式应存在');
    assert.match(source, /ASSET_ORGANIZE_UNDO_LIMIT/, '撤销栈需有上限');
});

test('AC5: 系统资源与聚合根不响应任何整理操作', () => {
    const helpers = loadOrganizeHelpers();
    assert.equal(helpers.assetOrganizeTargetFromKey('system:projects'), null, '系统资源节点不可整理');
    assert.equal(helpers.assetOrganizeTargetFromKey('mine:inbox'), null, '收集箱不可整理');
    assert.equal(helpers.assetOrganizeTargetFromKey('mine:root:__automatic__'), null, '聚合根不可整理');
    const noPath = loadOrganizeHelpers({diskPath: () => ''});
    assert.equal(noPath.assetOrganizeTargetFromKey('mine:root:root_a'), null, '无物理路径的根不可整理');
    const systemTab = loadOrganizeHelpers({mine: false});
    assert.equal(systemTab.assetOrganizeTargetFromKey('mine:root:root_a'), null, '系统资源标签页内一律不可整理');
    const root = helpers.assetOrganizeTargetFromKey('mine:root:root_a');
    assert.deepEqual(root, {key:'mine:root:root_a', rootId:'root_a', path:'', isRoot:true}, '挂载根作为容器可接收拖放');
    const deep = helpers.assetOrganizeTargetFromKey('mine:root:root_a::a/b');
    assert.equal(deep.path, 'a/b');
    assert.equal(deep.isRoot, false);
    // 右键菜单对不可整理节点不拦原生菜单
    assert.match(functionSource('openAssetOrganizeMenu'), /if\(!target\) return;/, '不可整理节点直接放行');
});

test('AC6: 路径拼接与落点语义自洽', () => {
    const helpers = loadOrganizeHelpers();
    assert.equal(helpers.assetOrganizeAbsPath('root_a', 'a/b'), '/mnt/root_a/a/b', '相对路径拼到挂载根');
    assert.equal(helpers.assetOrganizeAbsPath('root_a', ''), '/mnt/root_a', '根目录即挂载路径');
    assert.equal(helpers.assetOrganizeParentPath('a/b/c'), 'a/b', '父级路径');
    assert.equal(helpers.assetOrganizeParentPath('c'), '', '顶层父级为空');
    assert.equal(helpers.assetOrganizeLeafName('a/b/c'), 'c');
    assert.equal(helpers.assetOrganizeErrorText({code:'FILE_LOCKED'}).includes('占用'), true, '占用错误可读');
    assert.equal(helpers.assetOrganizeErrorText({code:'CROSS_ROOT_FORBIDDEN'}).includes('跨'), true, '跨根错误可读');
    // 本测试自身必须登记到前端清单
    assert.ok(manifest.includes('tests/frontend/qin-546-asset-organize.test.mjs'), '新增测试需登记到 node-files.txt');
});

test('AC1/AC2 回归：浏览器验收暴露的三处实现缺陷已修复', async () => {
    // 1) 行按钮同时带 data-asset-tree-node 与 .asset-tree-row，改名时不能再按后代去找行，
    //    否则选择器恒为空、内联输入框永远不出现（真机验收首轮就是这里挂的）。
    const rename = functionSource('beginAssetOrganizeRename');
    assert.doesNotMatch(rename, /\]\s+\.asset-tree-row`/, '改名不得把行按钮当容器查后代');
    assert.match(rename, /\[data-asset-tree-node="\$\{CSS\.escape\(target\.key\)\}"\]`/, '改名应直接定位行按钮本身');
    assert.match(rename, /row\?\.querySelector\('\.asset-tree-name'\)/, '名称节点仍从行内查');
    // 右键菜单的焦点返还目标同理，必须是行按钮本身
    assert.match(functionSource('openAssetOrganizeMenu'), /const trigger = row;/, '焦点返还目标应为行按钮');

    // 2) 单击选中会异步重绘整棵树，原生 dblclick 的第二击可能落在新节点上；
    //    点击委托用 detail 计数兜底，保证双击改名稳定生效。
    const clickDetail = /Number\(event\.detail\) >= 2[\s\S]{0,200}asset-tree-name[\s\S]{0,400}beginAssetOrganizeRename\(/;
    assert.match(source, clickDetail, '点击委托应兜底识别双击改名');

    // 3) /api/local-assets/folders/move 的 target 是「最终路径」，与素材移动语义不同：
    //    文件夹拖拽必须在落点目录后补上本层名字，否则拖进任何已存在目录都会 TARGET_EXISTS。
    const moveFolder = functionSource('assetOrganizeMoveFolder');
    assert.match(moveFolder, /destPath === drag\.path/, '拖回原位置应判定为无变化');
    assert.doesNotMatch(moveFolder, /assetOrganizeParentPath\(drag\.path\) === destPath/, '不应再用父级比较');
    assert.ok(moveFolder.includes('assetTreeSelectedKey = `mine:root:${destRootId}::${destPath}`'), '选中键直接用最终路径');

    const drop = functionSource('dropAssetOrganize');
    assert.match(drop, /const leaf = assetOrganizeLeafName\(drag\.path\);/, '落点计算需取本层名字');
    assert.match(drop, /const folderDestPath = destPath \? `\$\{destPath\}\/\$\{leaf\}` : leaf;/, '最终路径要拼上本层名字');
    assert.match(drop, /assetOrganizeMoveFolder\(drag, hit\.target\.rootId, folderDestPath\)/, '传给移动接口的必须是最终路径');

    // 行为验证：用注入假件跑真实的 dropAssetOrganize 分支
    const factory = new Function(
        'assetOrganizeDrag','assetOrganizeDropTargetFor','assetOrganizeDropZoneFor','clearAssetOrganizeDropHint',
        'assetOrganizeParentPath','assetOrganizeLeafName','assetOrganizeMoveFolder','assetOrganizeMoveAssets','document',
        `${'async ' + drop}; return {dropAssetOrganize, setDrag(value){ assetOrganizeDrag = value; }};`
    );
    function runDrop({zone, drag, target}){
        const calls = {folders: [], assets: []};
        const helpers = {
            assetOrganizeParentPath: path => String(path || '').split('/').slice(0, -1).join('/'),
            assetOrganizeLeafName: path => String(path || '').split('/').pop(),
            clearAssetOrganizeDropHint: () => {},
            assetOrganizeDropZoneFor: () => zone,
            assetOrganizeDropTargetFor: () => ({row: {classList: {remove(){}}}, target}),
            assetOrganizeMoveFolder: (dragValue, rootId, destPath) => { calls.folders.push({rootId, destPath}); },
            assetOrganizeMoveAssets: (ids, rootId, destPath) => { calls.assets.push({ids, rootId, destPath}); },
            document: {querySelectorAll: () => []},
        };
        const api = factory(
            drag, helpers.assetOrganizeDropTargetFor, helpers.assetOrganizeDropZoneFor, helpers.clearAssetOrganizeDropHint,
            helpers.assetOrganizeParentPath, helpers.assetOrganizeLeafName,
            helpers.assetOrganizeMoveFolder, helpers.assetOrganizeMoveAssets, helpers.document
        );
        return api.dropAssetOrganize({preventDefault(){}}).then(() => calls);
    }

    const into = await runDrop({
        zone: 'into',
        drag: {kind: 'folder', key: 'k', rootId: 'root_a', path: '甲部', assetIds: []},
        target: {key: 'k2', rootId: 'root_a', path: '乙部', isRoot: false},
    });
    assert.deepEqual(into.folders, [{rootId: 'root_a', destPath: '乙部/甲部'}], '「放进去」应拼成 目标目录/本层名字');

    const sibling = await runDrop({
        zone: 'insert',
        drag: {kind: 'folder', key: 'k', rootId: 'root_a', path: '乙部/甲部', assetIds: []},
        target: {key: 'k2', rootId: 'root_a', path: '乙部', isRoot: false},
    });
    assert.deepEqual(sibling.folders, [{rootId: 'root_a', destPath: '甲部'}], '「插到中间」应落到同级并保留名字');

    const assets = await runDrop({
        zone: 'into',
        drag: {kind: 'assets', key: '', rootId: '', path: '', assetIds: ['ast_0001']},
        target: {key: 'k2', rootId: 'root_a', path: '甲部', isRoot: false},
    });
    assert.deepEqual(assets.assets, [{ids: ['ast_0001'], rootId: 'root_a', destPath: '甲部'}], '素材移动仍传目标目录本身');
});
