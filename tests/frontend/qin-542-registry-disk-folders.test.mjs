/**
 * QIN-542（GitHub #598）：output 等用户资源子目录未完整带入目录树。
 * AC1 目录树消费全部已加载层级缓存、逐层懒加载、任意深度可展开；
 * AC2 空目录展示并可展开或标明「空」；AC3 计数双口径（总数/本层）UI 区分；
 * AC4 非 admin 403 显式提示；AC5 测试登记 node-files.txt 与卫生准入分片。
 */
import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);
const NODE_FILES = readFileSync(
    new URL('./node-files.txt', import.meta.url),
    'utf8'
);
const SHARD_URL = new URL('../hygiene/fixtures/admissions/issue-598.json', import.meta.url);

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

test('AC1: 拼树消费全部已加载层级缓存，深层目录进树', () => {
    const disk = functionSource('assetRackDiskDirectories');
    assert.match(disk, /registryDiskFolderCache\.get\(`\$\{id\}::\$\{prefix\}`\)/,
        '必须按「根 ID + 路径」键递归读取磁盘目录缓存');
    assert.match(disk, /walk\(relPath\)/, '必须从指定层级起递归合并全部已加载层级');
    assert.match(disk, /merged\.includes\(rel\)/, '跨层级合并必须去重');

    const tree = functionSource('assetRackFolderTree');
    assert.match(tree, /assetRackDiskDirectories\(root\)/,
        '普通来源建树必须合并全部已加载层级的磁盘目录');
    assert.match(tree, /assetRackDiskDirectoriesForItems\(items\)/,
        '自动归档建树必须合并各素材根的磁盘目录');

    // 叶子目录的懒加载入口：展开触发逐层加载，加载完成（无论有无子目录）即重绘
    const toggle = functionSource('toggleAssetRackFolder');
    assert.match(toggle, /loadAssetRackDiskFolders\(rootId, path\)/,
        '展开目录节点必须触发逐层懒加载');
    assert.match(toggle, /\.then\(\(\) => \{\s*if\(assetRackExpandedFolders\.has\(value\)\) render\(\);\s*\}\)/,
        '加载完成（无论有无子目录）必须重绘以呈现深层目录或空标记');
});

test('AC1/AC2: 全部节点保留展开入口，叶子不再是无操作占位', () => {
    const node = functionSource('renderAssetRackFolderNode');
    assert.match(node, /data-asset-rack-toggle=/, '必须提供展开/折叠三角');
    assert.doesNotMatch(node, /twisty-spacer/,
        '叶子节点不得再渲染不可操作的占位符（懒加载入口被切断的根因）');
    assert.match(node, /const expandable = true;/, '全部节点保留展开入口');
});

test('AC2: 已确认的空目录标明「空」徽标，有已索引素材的目录不得误标', () => {
    const node = functionSource('renderAssetRackFolderNode');
    assert.match(node, /registryDiskFolderCache\.has\(`\$\{root\}::\$\{node\.path\}`\)/,
        '空标记必须以磁盘缓存已加载为前提');
    assert.match(node, /!node\.hasIndexedAssets/,
        'QA 复验缺陷修复：有已索引素材（无子目录）的目录不得标「空」，判定必须包含素材计数');
    assert.match(node, /registryDiskFolderPermissionDenied/,
        '权限被拒（403）时不得把空缓存误标为空目录');
    assert.match(node, /空目录：无已索引素材与子目录/, '空徽标必须有自解释提示');
    assert.match(node, />空<\/em>/, '空目录必须以「空」徽标呈现');

    // 动态行为验证：QA 场景回归 —— 有文件无子目录的目录（c 2 本层2 / files_only 5 本层5）不得再标「空」
    const renderNode = new Function(
        'assetRackBoard',
        'registryDiskFolderCache',
        'registryDiskFolderPermissionDenied',
        'assetRackExpandedFolders',
        'escapeAttr',
        'escapeHtml',
        'customBoardFolderIsHidden',
        'boardHiddenFolders',
        'boardSourceSelection',
        'boardFolderSelection',
        'registryNavFolder',
        `
            ${functionSource('renderAssetRackFolderNode')}
            return renderAssetRackFolderNode;
        `
    )(
        () => null,
        new Map([['rootA::docs', []], ['rootA::vacant', []]]),
        false,
        new Set(),
        value => String(value),
        value => String(value),
        () => false,
        {},
        {},
        {},
        {rootId:'', path:''},
    );
    const withFiles = {path:'docs', name:'docs', count:2, selfCount:2, children:[], hasIndexedAssets:true};
    assert.ok(!renderNode('rootA', withFiles).includes('>空</em>'),
        '有 2 个已索引文件、无子目录的目录不得标「空」（QA：c 2 本层2 空）');
    const withMoreFiles = {path:'files_only', name:'files_only', count:5, selfCount:5, children:[], hasIndexedAssets:true};
    assert.ok(!renderNode('rootA', withMoreFiles).includes('>空</em>'),
        '有 5 个已索引文件、无子目录的目录不得标「空」（QA：files_only 5 本层5 空）');
    const vacant = {path:'vacant', name:'vacant', count:0, selfCount:0, children:[], hasIndexedAssets:false};
    assert.ok(renderNode('rootA', vacant).includes('>空</em>'),
        '磁盘浏览确认无子目录且无已索引素材的空目录必须标「空」');
});

test('AC3: 计数双口径（含子目录总数 + 本层文件数）在节点徽标区分', () => {
    const node = functionSource('renderAssetRackFolderNode');
    assert.match(node, /<em>\$\{Number\(node\.count \|\| 0\)\}<\/em>/, '主徽标保持含子目录总数');
    assert.match(node, /本层\$\{Number\(node\.selfCount\)\}/, '本层徽标必须明确标注「本层」');

    // 动态行为验证：跨层级累加 + 后端 total_count 覆盖口径 + 分类看板累加口径
    const buildTree = new Function('registryAssetFolderPath', `
        ${functionSource('customBoardFolderTree')}
        return customBoardFolderTree;
    `)(item => String(item.folder || ''));
    const summary = [
        {path:'output', count:2, total_count:4, categories:{image:2}},
        {path:'output/sub', count:1, total_count:2, categories:{image:1}},
        {path:'output/sub/deeper', count:1, total_count:1, categories:{image:1}},
    ];
    const tree = buildTree([], ['output'], summary, '');
    const output = tree.find(item => item.path === 'output');
    assert.ok(output, '深层目录必须建树');
    assert.equal(output.count, 4, '主计数必须采用含子目录递归总数');
    assert.equal(output.selfCount, 2, '本层徽标必须只算本层直接文件');
    const sub = output.children.find(item => item.path === 'output/sub');
    assert.ok(sub, '深层目录必须挂在正确父节点下');
    assert.equal(sub.count, 2);
    assert.equal(sub.selfCount, 1);
    const deeper = sub.children.find(item => item.path === 'output/sub/deeper');
    assert.ok(deeper, '任意深度都可入树');
    assert.equal(deeper.count, 1);
    assert.equal(deeper.selfCount, 1);

    // 分类看板：total_count 是全类型口径，分类计数必须沿用 categories 祖先累加
    const categoryTree = buildTree([], ['output'], summary, 'image');
    const categoryOutput = categoryTree.find(item => item.path === 'output');
    assert.equal(categoryOutput.count, 4, '分类计数经祖先累加得出该分类递归总数');
    assert.equal(categoryOutput.selfCount, 2);
});

test('AC4: 非 admin 403 显式提示，不再静默', () => {
    const disk = functionSource('loadAssetRackDiskFolders');
    assert.match(disk, /response\.status === 403/,
        '被拒（403）必须显式识别，不得与普通失败混同');
    assert.match(disk, /registryDiskFolderPermissionDenied = true/,
        '403 必须登记权限提示状态');
    assert.match(disk, /registryDiskFolderLoading\.has\(key\)\) return registryDiskFolderLoading\.get\(key\)/,
        '在途请求必须等待而非早返回，保证展开后重绘拿到结果');

    const folders = functionSource('renderAssetRackFolders');
    assert.match(folders, /registryDiskFolderPermissionDenied/,
        '目录层级页必须消费权限提示状态');
    assert.match(folders, /磁盘子目录浏览需要管理员权限/, '提示文案必须明确说明权限原因');

    const registered = functionSource('loadAssetRackRegisteredFolders');
    assert.match(registered, /registryDiskFolderPermissionNoticed = true/,
        '403 后必须强制重绘一次让提示条出现');
    const boardDirs = functionSource('loadCustomBoardFolderDirectories');
    assert.match(boardDirs, /registryDiskFolderPermissionNoticed = true/,
        '看板态同样必须在 403 后强制重绘一次');
});

test('AC5: 测试登记 node-files.txt 并完成卫生准入分片', () => {
    assert.match(NODE_FILES, /^tests\/frontend\/qin-542-registry-disk-folders\.test\.mjs$/m,
        '新增测试必须登记到 node-files.txt');
    assert.ok(existsSync(SHARD_URL), '缺少卫生准入分片 issue-598.json');
    const shard = JSON.parse(readFileSync(SHARD_URL, 'utf8'));
    assert.equal(shard.schema, 'gw.cleanroom-admission-shard/v1');
    assert.equal(shard.issue, 598);
    const paths = (shard.files || []).map(entry => String(entry));
    for(const required of [
        'web/js/modules/asset-manager.js',
        'gw/asset_registry/repository.py',
        'tests/api/test_qin_542_folder_counts.py',
        'tests/frontend/qin-542-registry-disk-folders.test.mjs',
        'tests/frontend/node-files.txt',
        'tests/hygiene/fixtures/admissions/issue-598.json',
    ]){
        assert.ok(paths.includes(required), `准入分片必须声明可改文件：${required}`);
    }
    const accepted = (shard.accepted_test_files || []).map(entry => entry.path);
    assert.ok(
        accepted.includes('tests/frontend/qin-542-registry-disk-folders.test.mjs'),
        '新增前端测试必须在准入分片登记'
    );
});
