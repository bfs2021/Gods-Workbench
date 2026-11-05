// QIN-544: 资产库左侧单栏树重构——系统/我的切换、去中间栏、项目层级、管线样式
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

function renderAssetTreeNodeForTest(options){
    const start = source.indexOf('function renderAssetTreeNode(');
    const end = source.indexOf(String.fromCharCode(10) + '}', start) + 2;
    assert.ok(start >= 0 && end > 1, 'renderer 函数应存在且闭合');
    const renderer = new Function(
        'assetTreeSelectedPathKeys',
        'assetTreeSelectedKey',
        'escapeAttr',
        'escapeHtml',
        `${source.slice(start, end)}; return renderAssetTreeNode;`
    )(
        () => new Set(),
        '',
        value => String(value ?? ''),
        value => String(value ?? '')
    );
    return renderer(options);
}

test('AC1: 左栏顶部系统资源/我的资源两个标签页，各一棵树', () => {
    const panel = functionSource('renderAssetTreePanel');
    assert.match(panel, /data-asset-tree-tab="system"[^>]*>系统资源</, '系统资源标签页应存在');
    assert.match(panel, /data-asset-tree-tab="mine"[^>]*>我的资源</, '我的资源标签页应存在');
    assert.match(panel, /mine \? renderAssetTreeMineTree\(\) : renderAssetTreeSystemTree\(\)/, '两个标签页各渲染一棵树');
    assert.match(functionSource('activateAssetTreeTab'), /assetTreeTab = next/, '标签页切换应更新状态');
    assert.doesNotMatch(functionSource('render'), /asset-rack-collapsed/, '新树布局不得沿用旧机架折叠态');
    assert.doesNotMatch(styles, /\.asset-rack-collapsed\b/, '旧机架折叠 CSS 应已清理');
    assert.match(styles, /\.asset-manager-root\.asset-manager-root-library\.registry-detail-collapsed \{ grid-template-columns:240px minmax\(0,1fr\) 44px; \}/, '详情面板 44px 折叠样式必须保留');
    for(const banned of ['全部', '未分类', '回收站']){
        assert.ok(!panel.includes(`>${banned}<`), `树面板不得设固定入口：${banned}`);
    }
});

test('AC2: 系统资源树只读：项目层级完整、剧本/画布/提示词平级、无工作流入口', () => {
    const systemTree = functionSource('renderAssetTreeSystemTree');
    assert.match(systemTree, /renderAssetTreeProjectsBranch\(\)/, '项目制片分支应接入');
    assert.match(systemTree, /assetTreeSystemBoards\(\)/, '系统看板节点应接入');
    assert.match(systemTree, /board\.id === 'prompts' \? '提示词库' : board\.name/, '提示词节点标签应与票面一致');
    const boards = functionSource('assetTreeSystemBoards');
    assert.match(boards, /\['scripts', 'canvas-assets', 'prompts'\]/, '剧本/画布/提示词平级');
    assert.ok(!boards.includes('workflows'), '系统资源树不得有工作流入口');
    const branch = functionSource('renderAssetTreeProjectsBranch');
    assert.match(branch, /system:projects/, '项目分支节点 key 应使用 system:projects 前缀');
    assert.match(functionSource('assetTreeProjectGroupNode'), /ASSET_TREE_GROUP_LABELS/, '条目分组用中文标签（剧集/场景/镜头/角色/任务）');
    const labels = source.match(/const ASSET_TREE_GROUP_LABELS = \{[\s\S]*?\};/)?.[0] || '';
    for(const label of ['剧集', '场景', '镜头', '角色', '任务']){
        assert.ok(labels.includes(label), `分组标签应含：${label}`);
    }
    assert.match(functionSource('ensureAssetTreeProjectData'), /getRegistryProject/, '项目详情走 QIN-543 接口');
});

test('AC3: 我的资源树：收集箱第一位（占位）、挂载文件夹完整子目录树、底部挂载入口', () => {
    const mineTree = functionSource('renderAssetTreeMineTree');
    assert.match(mineTree, /mine:inbox/, '收集箱节点应存在');
    assert.match(mineTree, /收集箱/, '收集箱文案应存在');
    const inboxPos = mineTree.indexOf('mine:inbox');
    const rootsPos = mineTree.indexOf('assetTreeRootsList()');
    assert.ok(inboxPos >= 0 && rootsPos >= 0 && inboxPos < rootsPos, '收集箱应排在挂载文件夹之前');
    assert.match(mineTree, /assetTreeRootsList\(\)/, '挂载文件夹树应接入根目录清单');
    const panel = functionSource('renderAssetTreePanel');
    assert.match(panel, /data-asset-tree-mount/, '面板底部挂载按钮应存在');
    assert.match(functionSource('renderAssetTreeMineTree'), /assetRackFolderTree/, '目录树复用 QIN-542 完整磁盘子目录树');
    assert.match(functionSource('assetTreeFolderNodeFor'), /assetTreeFolderNodeFor/, '目录节点递归渲染子目录');
    assert.match(functionSource('toggleAssetTreeNode'), /loadAssetRackDiskFolders/, '展开目录节点应懒加载磁盘子目录');
    assert.match(functionSource('toggleAssetTreeNode'), /ensureAssetTreeProjectData/, '展开项目节点应懒加载 QIN-543 详情');
});

test('AC4: 中栏机架下线：全部视图接入树面板，智能筛选调用点清零', () => {
    for(const render of ['renderRegistryManager','renderCustomAssetLibrary','renderScriptAssetsManager','renderProjectManager','renderAssetManager','renderWorkflowManager','renderPromptManager','renderCanvasAssetsManager','renderLocalManager']){
        assert.match(functionSource(render), /renderAssetTreePanel\(\)/, `${render} 左栏应接入树面板`);
    }
    // 智能筛选残留（最近 7 天、AI 分类等）不再被任何视图渲染入口调用（仅剩定义与包装互调）
    for(const render of ['renderRegistryManager','renderCustomAssetLibrary','renderScriptAssetsManager','renderProjectManager','renderAssetManager','renderWorkflowManager','renderPromptManager','renderCanvasAssetsManager','renderLocalManager']){
        assert.ok(!functionSource(render).includes('renderAssetSmartFilterContents'), `${render} 不得再调用智能筛选残留`);
    }
    const callers = (source.match(/renderAssetSmartFilterContents\(/g) || []).length;
    assert.ok(callers <= 2, '智能筛选仅剩定义与 panel 包装互调，无视图调用点');
});
test('AC5: 项目制片分类看板不再显示，改由树+素材网格承载', () => {
    const manager = functionSource('renderProjectManager');
    assert.ok(!manager.includes('project-entity-groups'), '分类看板容器应移除');
    assert.ok(!manager.includes('data-project-entity'), '条目卡片看板应移除');
    assert.ok(!manager.includes('renderSystemBoardRack'), '旧中栏应移除');
    assert.match(manager, /renderAssetTreePanel\(\)/, '左栏树应接入');
    assert.match(manager, /renderRegistryAssetSections/, '右侧为素材网格');
});

test('AC6: 管线样式：竖线+圆点、金色支路、选中淡金底、空目录变暗、分组小灰字', () => {
    assert.match(styles, /\.asset-tree-node::before/, '竖线伪元素应存在');
    assert.match(styles, /\.asset-tree-node\.is-path::before/, '根到选中支路竖线点亮金色');
    assert.match(styles, /\.asset-tree-node\.is-path \.asset-tree-dot/, '支路节点圆点点亮');
    assert.match(styles, /\.asset-tree-node\.is-selected > \.asset-tree-row/, '选中淡金底');
    assert.match(styles, /\.asset-tree-node\.is-empty/, '数量为 0 的目录变暗');
    assert.match(styles, /\.asset-tree-group-label/, '分组标题小号灰字');
    assert.match(styles, /\.asset-tree-row\s*\{[^}]*height:34px/, '行高 32-36px 区间');
    const nodeStart = source.indexOf('function renderAssetTreeNode(');
    const nodeRow = source.slice(nodeStart, source.indexOf(String.fromCharCode(10) + '}', nodeStart) + 2);
    assert.match(nodeRow, /is-path/, '节点渲染应输出支路状态类');
    assert.match(nodeRow, /is-selected/, '节点渲染应输出选中态类');
    assert.match(nodeRow, /is-group-label/, '节点渲染应输出分组标题类');
    const emptyNodeHtml = renderAssetTreeNodeForTest({
        key:'system:projects:project:group:scene', name:'场景', count:0, hasChildren:true, emptyAble:false,
    });
    const emptyNodeClasses = emptyNodeHtml.match(/<div class="([^"]+)"/)?.[1].split(/\s+/) || [];
    assert.ok(emptyNodeClasses.includes('is-empty'), '可展开的零数量分组渲染后应含 is-empty 类');
    const populatedNodeHtml = renderAssetTreeNodeForTest({
        key:'system:projects:project:group:character', name:'角色', count:2, hasChildren:true,
    });
    assert.doesNotMatch(populatedNodeHtml, /class="[^"]*\bis-empty\b/, '有数量的可展开分组不应变暗');
    const uncountedNodeHtml = renderAssetTreeNodeForTest({
        key:'system:scripts', name:'剧本资产', count:'', hasChildren:false,
    });
    assert.doesNotMatch(uncountedNodeHtml, /class="[^"]*\bis-empty\b/, 'count 为空字符串的节点数量未知，不应变暗');
});
test('AC7: 面包屑逐级可点击 + 键盘上下切换与回车进入', () => {
    assert.match(functionSource('renderAssetTreeBreadcrumb'), /data-asset-tree-crumb/, '面包屑应输出可点击节点');
    const crumbClick = functionSource('handleAssetTreeCrumbClick');
    assert.match(crumbClick, /registryProjectFilterId = ''/, '根级面包屑应清空过滤');
    assert.match(crumbClick, /assetTreeSelectedKey = ''/, '根级面包屑应清空树选中态');
    assert.match(crumbClick, /activeTab = 'registry'/, '根级面包屑应返回资产总览');
    assert.match(crumbClick, /render\(\)/, '根级面包屑应立即重绘页面');
    assert.match(crumbClick, /refreshRegistryAssets\(false\)/, '根级面包屑应刷新未过滤素材');
    assert.match(source, /document\.querySelector\('\.asset-breadcrumbs \.asset-heading-row'\)\?\.addEventListener\('click'/, '面包屑容器应自行接收点击委托');
    const keydown = functionSource('handleAssetTreeKeydown');
    assert.match(keydown, /ArrowDown/, '下键切换');
    assert.match(keydown, /ArrowUp/, '上键切换');
    assert.match(keydown, /Enter/, '回车展开或进入');
    assert.match(keydown, /stopPropagation/, '树内按键不向下穿透');
    assert.match(source, /document\.addEventListener\('keydown', handleAssetTreeKeydown, \{capture:true\}\)/, '键盘捕获委托应绑定');
});
test('AC8: 选中态分发：项目/分组/条目/目录各自收敛素材', () => {
    const select = functionSource('selectAssetTreeNode');
    assert.match(select, /registryProjectFilterId = pid/, '点项目应设置项目过滤');
    assert.match(select, /entityTypes:new Set\(\[typeId\]\)/, '点分组应设置类型过滤');
    assert.match(select, /entityId/, '点条目应设置条目过滤');
    assert.match(select, /selectAssetRackFolder\(rootId/, '点目录应走目录过滤');
    assert.match(source, /filter\(registryProjectEntityMatches\)/, '素材可见链路接入条目过滤');
    assert.match(functionSource('renderProjectManager'), /registryVisibleAssets\(\)/, '项目制片视图素材来自统一可见链路');
});
