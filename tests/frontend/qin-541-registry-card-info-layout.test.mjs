/**
 * QIN-541（GitHub #593）：资产库标准卡片信息层级重排。
 *
 * 断言面覆盖票面 AC1～AC7 的前端部分：
 * - AC1～AC6 仅标准模式正文收敛为「文件名 + 大小·尺寸」两行结构，类型/时间/来源文字行
 *   全部移除，格式角标与删除按钮保留；
 * - AC2 文件名单行省略（两行钳制规则移除、白色基线保留）；
 * - AC3 第二行灰色单行「大小 · 尺寸」，无值字段与整行省略，正文行距收敛；
 * - AC4 卡片不显示修改时间；
 * - AC5 来源默认隐藏，仅外来/URL/多库资产显示小图标（行为矩阵）；
 * - AC6 标准卡正文高度估算收敛到 44px，简洁模式保持 0；
 * - AC7 方形、简洁、列表模式保持不变，测试登记自证。
 */
import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const CARD_CSS = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');
const NODE_FILES = readFileSync(new URL('./node-files.txt', import.meta.url), 'utf8');
const SHARD = new URL('../hygiene/fixtures/admissions/issue-593.json', import.meta.url);

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

// 沙箱替身与真实函数同口径：source_names 去重、缺失回落「统一资产」；
// 离线判定只看素材自身的 root_availability 字段（真实函数的第一分支）。
const namesStub = item => Array.isArray(item.source_names)
    ? [...new Set(item.source_names.map(String).filter(Boolean))]
    : [item.storage_root_name || '统一资产'];
const offlineStub = item => item.root_availability === 'offline';

function buildBadge(boardSel, navFolder, rootId){
    return new Function(
        'registrySourceNames', 'registryRootIsOffline', 'escapeAttr',
        'boardSourceSelection', 'registryNavFolder', 'registryRootId',
        `${functionSource('registryCardSourceBadge')}
         return registryCardSourceBadge;`
    )(namesStub, offlineStub, value => value, boardSel, navFolder, rootId);
}

const sourceBadge = buildBadge({}, {rootId: '', path: ''}, '');
const sourceBadgeScoped = buildBadge({}, {rootId: 'root-b', path: ''}, '');

function buildCardHeight(simple, aspect='original'){
    return new Function(
        'registryVisualType', 'registryAspect', 'assetSupportsImagePreview', 'registrySimple',
        `${functionSource('registryVirtualCardHeight')}
         return registryVirtualCardHeight;`
    )(item => String(item?.kind || 'file'), aspect, () => true, simple);
}

function buildCardRenderer(aspect){
    return new Function(
        'registryAspect', 'registryVisualType', 'registryAssetExtension', 'registryMediaAspectStyle',
        'findMatchedVideoSegment', 'formatVideoSegmentRange', 'escapeAttr', 'escapeHtml',
        'deferRegistryPreviewThumb', 'assetThumb', 'renderRegistryRecycleButton', 'assetBoardById',
        'formatFileSize', 'formatMediaDuration', 'registryAssetTypeLabel', 'formatDate',
        `const registrySelectedIds = new Set();
         const selectedRegistryAssetId = '';
         const boardReferenceCanvasSelection = {registry: ''};
         const activeTab = 'registry';
         const registryQuery = '';
         const boardSourceSelection = {};
         const registryNavFolder = {rootId: '', path: ''};
         const registryRootId = '';
         const registryRootIsOffline = item => item?.root_availability === 'offline';
         ${functionSource('registrySourceNames')}
         ${functionSource('registryCardSourceBadge')}
         ${functionSource('renderRegistryCard')}
         return renderRegistryCard;`
    )(
        aspect,
        item => String(item?.kind || 'file'),
        () => 'png',
        () => '',
        () => null,
        () => '',
        value => String(value ?? ''),
        value => String(value ?? ''),
        value => value,
        () => '<img alt="preview">',
        () => '<button class="registry-card-recycle"></button>',
        () => ({id: 'registry', system: true}),
        size => `${Number(size || 0)} B`,
        duration => `${Number(duration || 0)} sec`,
        () => '图片',
        value => value ? `DATE:${value}` : '—'
    );
}

test('AC1 卡片正文收敛为两行结构，类型/时间/来源文字行全部移除', () => {
    const card = functionSource('renderRegistryCard');
    const standard = buildCardRenderer('original')({
        id: 'asset-1', name: '长文件名.png', kind: 'image', size: 1024,
        width: 1920, height: 1080, created_at: '2026-11-04',
        source_names: ['托管资产', '项目资产'],
    });
    assert.ok(standard.includes('class="registry-card-format"'), '格式角标仍渲染');
    assert.ok(standard.includes('class="asset-card-check"'), '勾选框仍渲染，出现时机交由 CSS');
    assert.ok(standard.includes('class="registry-card-recycle"'), '删除按钮仍渲染');
    assert.ok(standard.includes('class="registry-card-caption"><span class="registry-card-subline">'), '标准模式第二行结构为 subline + 来源小图标');
    assert.ok(standard.includes('title="托管资产 · 项目资产"'), '标准模式来源图标 title 保留真实库名');
    assert.ok(!standard.includes('asset-card-meta'), '标准模式移除类型/时间文字行');
    assert.ok(!standard.includes('图片'), '标准模式移除类型字段');
    assert.ok(!standard.includes('DATE:2026-11-04'), '标准模式移除创建时间');
    assert.ok(!standard.includes('1024 B</span></div>'), '标准模式不显示旧单独大小行');
    assert.ok(card.includes('registryAspect === \'square\''), '方形模式有独立的旧正文分支');
});

test('AC2 文件名单行省略（两行钳制规则已移除）', () => {
    assert.ok(!CARD_CSS.includes('.registry-aspect-original .registry-card .asset-card-name { min-height:2.5em'), '标准模式移除两行钳制规则');
    assert.ok(CARD_CSS.includes('.registry-aspect-square .registry-card .asset-card-name { min-height:2.5em'), '方形模式保留两行钳制规则');
    assert.ok(CARD_CSS.includes('.asset-card-name { color:var(--text);'), '文件名白色基线保留');
});

test('AC3 第二行灰色「大小 · 尺寸」，无值字段与整行省略', () => {
    const card = functionSource('renderRegistryCard');
    assert.ok(card.includes('if(Number(item.size || 0) > 0) measureParts.push(formatFileSize(item.size))'), '无大小则省略');
    assert.ok(card.includes('measureParts.push(`${sublineWidth}×${sublineHeight}`)'), '尺寸为 width×height');
    assert.ok(card.includes('measureParts.length ? `<span class="registry-card-subline">'), '整行无值时省略');
    assert.ok(card.includes('const sublineWidth = Number(item.width || item.metadata?.width || 0)'), '尺寸取值与高度估算同源');
    assert.ok(card.includes('const sublineHeight = Number(item.height || item.metadata?.height || 0)'), '尺寸取值与高度估算同源');
    assert.ok(CARD_CSS.includes('.registry-aspect-original .registry-card .asset-card-body { gap:0; }'), '仅标准模式正文行距收敛为 0（44px 组成）');
    assert.ok(CARD_CSS.includes('color:var(--faint); font-family:var(--gw-font-mono); font-size:var(--gw-type-label-md); line-height:17px;'), '第二行灰色单行省略');
    assert.ok(CARD_CSS.includes('.registry-aspect-original .registry-card .registry-card-caption { display:flex; align-items:center; gap:5px; min-width:0; }'), '第二行单行结构只作用于标准模式');
});

test('AC4 卡片不显示修改时间', () => {
    const card = functionSource('renderRegistryCard');
    assert.ok(!card.includes('modified_at'), '不引用 modified_at');
    assert.ok(!card.includes('registryModifiedTime'), '不引用修改时间函数');
    assert.ok(!card.includes('修改时间'), '不出现修改时间文案');
});

test('用户指示 2026-11-05：视频详情摘要不再显示「未知」，时间行与详情口径一致', () => {
    const videoSummary = new Function(
        'detailViewerType', 'detailVideoStoryboardAssetId', 'registryAssetLinkedBoards', 'detailViewerSourceLabel',
        'assetFileExtension', 'formatFileSize', 'formatDate', 'registryModifiedTime',
        `${functionSource('renderDetailVideoSummary')}
         return renderDetailVideoSummary;`
    )(
        () => 'video', () => '', () => [], () => '统一资产',
        item => String(item?.name || '').split('.').pop() || '',
        size => `${Number(size || 0)} B`,
        value => String(value),
        null
    );
    // registryModifiedTime 也注入真实函数源码，保证口径与详情一致。
    const withRealModifiedTime = new Function(
        'detailViewerType', 'detailVideoStoryboardAssetId', 'registryAssetLinkedBoards', 'detailViewerSourceLabel',
        'assetFileExtension', 'formatFileSize', 'formatDate', 'registryModifiedTime', 'escapeHtml', 'escapeAttr',
        `${functionSource('registryModifiedTime')}
         ${functionSource('renderDetailVideoSummary')}
         return renderDetailVideoSummary;`
    )(
        () => 'video', () => '', () => [], () => '统一资产',
        item => String(item?.name || '').split('.').pop() || '',
        size => `${Number(size || 0)} B`,
        value => String(value),
        undefined,
        value => String(value ?? ''),
        value => String(value ?? '')
    );
    const html = withRealModifiedTime({kind: 'video', name: 'a.mp4', modified_at: '2023-11-14T22:13:20Z'}, 'registry');
    assert.ok(html.includes('修改时间'), '时间行改名为「修改时间」');
    assert.ok(html.includes(String(Date.parse('2023-11-14T22:13:20Z'))), '优先显示文件修改时间');
    assert.ok(!html.includes('未知'), '不再出现「未知」');
    const empty = withRealModifiedTime({kind: 'video', name: 'a.mp4'}, 'registry');
    assert.ok(empty.includes('>—</dd>'), '无数据回落「—」');
    assert.ok(!empty.includes('未知'), '无数据也不出现「未知」');
});

test('AC5 来源默认隐藏：仅外来/URL/多库资产显示小图标', () => {
    // 单库资产：无多库/URL/外来特征，默认隐藏。
    assert.equal(sourceBadge({storage_root_name: '托管资产'}), '', '单库资产默认隐藏来源');
    // 多库资产：显示小图标，title 显示全部来源名。
    const multi = sourceBadge({source_names: ['托管资产', '项目资产']});
    assert.ok(multi.includes('class="registry-card-source"'), '多库资产显示来源小图标');
    assert.ok(multi.includes('data-lucide="database"'), '多库用 database 图标');
    assert.ok(multi.includes('title="托管资产 · 项目资产"'), 'title 显示来源名');
    // URL 采集资产：link 图标，title 为采集 URL。
    const urlBadge = sourceBadge({metadata: {collect_sources: [{url: 'https://example.com/a.png'}]}});
    assert.ok(urlBadge.includes('data-lucide="link"'), 'URL 资产用 link 图标');
    assert.ok(urlBadge.includes('title="https://example.com/a.png"'), 'URL 资产 title 为采集 URL');
    // 已登记素材库看板：绑定根内素材隐藏，外来根素材显示。
    const sourceBoard = {id: 'b-root', sourceBoard: true, rootId: 'root-a', system: false};
    assert.equal(sourceBadge({storage_root_id: 'root-a', storage_root_name: '托管资产'}, sourceBoard), '', '绑定根内素材默认隐藏来源');
    const foreign = sourceBadge({storage_root_id: 'root-b', storage_root_name: '项目资产'}, sourceBoard);
    assert.ok(foreign.includes('class="registry-card-source"'), '外来根素材显示来源小图标');
    assert.ok(foreign.includes('data-lucide="database"'), '外来根素材 database 图标');
    assert.ok(foreign.includes('title="项目资产"'), 'title 显示来源名');
    // 自定义库看板：素材根不在未隐藏来源内显示；机架来源选择收窄后进一步收紧。
    const customBoard = {id: 'b-custom', system: false, automaticHidden: false, sources: [{id: 'root-a', hidden: false}]};
    assert.equal(sourceBadge({storage_root_id: 'root-a'}, customBoard), '', '根在绑定来源内默认隐藏');
    assert.ok(sourceBadge({storage_root_id: 'root-x'}, customBoard).includes('class="registry-card-source"'), '根不在绑定来源内显示');
    assert.ok(buildBadge({'b-custom': 'root-b'}, {rootId: '', path: ''}, '')({storage_root_id: 'root-a'}, customBoard).includes('registry-card-source'), '机架来源选择收窄后显示');
    // 自动归档素材：看板收下时视为当前范围，隐藏时视为外来。
    assert.equal(sourceBadge({storage_root_id: ''}, customBoard), '', '看板收下自动归档时视为当前范围');
    assert.ok(buildBadge({}, {rootId: '', path: ''}, '')({storage_root_id: ''}, {id: 'b-custom', system: false, automaticHidden: true, sources: [{id: 'root-a', hidden: false}]}).includes('registry-card-source'), '看板隐藏自动归档时显示');
    // 系统聚合看板：未选库时无「当前库」可比默认隐藏；目录层级选中其他库时外来素材显示。
    const registryBoard = {id: 'registry', system: true};
    assert.equal(sourceBadge({storage_root_id: 'root-c'}, registryBoard), '', '聚合看板未选库默认隐藏');
    assert.ok(sourceBadgeScoped({storage_root_id: 'root-a'}, registryBoard).includes('registry-card-source'), '聚合看板目录层级选中其他库时外来显示');
    // 离线来源：title 尾部标注「离线」。
    const offlineMulti = sourceBadge({source_names: ['托管资产', '项目资产'], root_availability: 'offline'});
    assert.ok(offlineMulti.includes('title="托管资产 · 项目资产 · 离线"'), '离线资产 title 尾部标注离线');
});

test('AC6 标准卡正文高度估算收敛到 44px，简洁模式保持 0', () => {
    const height = buildCardHeight(false, 'original');
    assert.equal(height({kind: 'image', width: 100, height: 100}), 232, '标准卡图片：媒体 174 + 正文 44 + 上下留白 14');
    assert.equal(height({kind: 'video'}), 156, '标准卡视频按 16:9 估算');
    assert.equal(buildCardHeight(true)({kind: 'image', width: 100, height: 100}), 188, '简洁模式正文隐藏，正文估算为 0');
});

test('AC7 方形、简洁、列表视图保持原样', () => {
    const item = {
        id: 'square-1', name: '方形卡片.png', kind: 'image', size: 1024,
        width: 1920, height: 1080, created_at: '2026-11-04',
        source_names: ['托管资产', '项目资产'],
    };
    const square = buildCardRenderer('square')(item);
    assert.ok(square.includes('class="registry-card-measure">1024 B</span>'), '方形模式恢复原大小/时长行');
    assert.ok(square.includes('class="asset-card-meta"><span>图片</span><span>DATE:2026-11-04</span></div>'), '方形模式保留类型与日期行');
    assert.ok(square.includes('<span>托管资产 · 项目资产</span>'), '方形模式保留来源名称行');
    assert.ok(square.includes('<em>2 库</em>'), '方形模式保留多库徽标');
    assert.ok(!square.includes('registry-card-subline'), '方形模式不使用标准模式 subline');
    assert.equal(buildCardHeight(false, 'square')({kind: 'image', width: 400, height: 200}, 202), 306, '方形正文估算恢复 92px');
    assert.equal(buildCardHeight(true, 'square')({kind: 'image', width: 400, height: 200}, 202), 214, '方形模式缩略图仍为正方形，简洁正文隐藏');
    assert.equal(buildCardHeight(true, 'original')({kind: 'image', width: 400, height: 200}, 202), 114, '原比例简洁模式正文估算仍为 0');
    assert.ok(CARD_CSS.includes('.registry-aspect-square .registry-card .asset-card-name { min-height:2.5em; white-space:normal; display:-webkit-box;'), '方形模式恢复文件名两行规则');
    assert.ok(CARD_CSS.includes('.registry-aspect-square .registry-card .asset-card-meta { display:flex; align-items:center; justify-content:space-between; gap:7px; }'), '方形模式恢复类型/时间行布局');
    assert.ok(CARD_CSS.includes('.registry-aspect-square .registry-card .registry-card-source { margin-top:2px; min-width:0;'), '方形模式恢复完整来源行样式');
    const listRow = functionSource('renderRegistryListRow');
    assert.ok(listRow.includes('class="registry-list-type"'), '列表视图仍渲染类型列');
    assert.ok(listRow.includes('registryModifiedLabel(item)'), '列表视图仍渲染修改时间列');
    assert.ok(listRow.includes('registrySourceNames(item).join'), '列表视图仍渲染来源路径');
});

test('AC7 简洁模式正文隐藏，标准模式勾选框按需显现，测试登记自证', () => {
    assert.ok(CARD_CSS.includes('.registry-simple .registry-card .asset-card-body { display:none; }'), '简洁模式正文隐藏保留');
    assert.ok(CARD_CSS.includes('.registry-simple .registry-card .asset-card-body { display:none !important; }'), '简洁模式正文隐藏 !important 版本保留');
    assert.ok(CARD_CSS.includes('.registry-card.active .registry-card-subline'), '选中态高亮第二行 subline');
    assert.ok(CARD_CSS.includes('.registry-card.active .asset-card-meta'), '方形模式选中态继续高亮 meta 行');
    // 勾选框默认隐藏，悬停/键盘聚焦/已选中时显现。
    assert.ok(CARD_CSS.includes('.asset-content.registry-content.registry-aspect-original .registry-card .asset-card-check { display:inline-block; opacity:0; pointer-events:none; transition:opacity .14s ease; }'), '标准模式勾选框默认隐藏');
    assert.ok(CARD_CSS.includes('.asset-content.registry-content.registry-aspect-original .registry-card:hover .asset-card-check'), '标准模式悬停显现');
    assert.ok(CARD_CSS.includes('.asset-content.registry-content.registry-aspect-original .registry-card:focus-within .asset-card-check'), '标准模式键盘聚焦显现');
    assert.ok(CARD_CSS.includes('.asset-content.registry-content.registry-aspect-original .registry-card.selected .asset-card-check { opacity:1; pointer-events:auto; }'), '标准模式已选中显现');
    // 自证登记：node-files.txt 与卫生准入分片。
    assert.ok(NODE_FILES.includes('tests/frontend/qin-541-registry-card-info-layout.test.mjs'), '本票前端测试已登记 node-files.txt');
    assert.ok(existsSync(SHARD), '缺少 tests/hygiene/fixtures/admissions/issue-593.json');
    const shard = JSON.parse(readFileSync(SHARD, 'utf8'));
    assert.equal(shard.issue, 593);
    assert.ok(shard.files.includes('tests/frontend/qin-541-registry-card-info-layout.test.mjs'), '分片 files 未登记本票前端测试');
});
