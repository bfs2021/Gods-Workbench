import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// QIN-563（GitHub #636）：统计条「其他」与「画布」同排等宽、单击「画布」直接筛选全部画布资产。
// 静态契约：断言渲染模板与生效样式表的结构事实。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = relativePath => readFileSync(path.join(root, relativePath), 'utf8');

const js = read('web/js/modules/asset-manager.js');
const vaultCss = read('web/css/pages/asset-vault.css');

// ---- AC1：统计条同排等宽 ----

// 10 列网格：主卡 2fr，其余 9 张类型卡统一 .95fr（与画布卡同宽），「其他」不再换行。
const statsBlock = vaultCss.match(/body\.gw-signal\.gw-page--asset-manager \.registry-overview-stats \{([\s\S]*?)\n\}/);
assert.ok(statsBlock, 'asset-vault.css 必须定义概览统计网格');
assert.match(statsBlock[1], /grid-template-columns:\s*2fr repeat\(9, \.95fr\);/, '统计条应为 10 列：主卡 2fr + 9 张 .95fr 类型卡');
assert.match(statsBlock[1], /--registry-stat-gap-count: 9;/, '10 列应对应 9 个间距');
// QIN-531 口径不变：8px 间距令牌与 2/3 宽度公式保持原样。
assert.match(statsBlock[1], /--registry-stat-gap: 8px;/, '卡间距令牌应固定为 8px');
assert.match(statsBlock[1], /gap: var\(--registry-stat-gap\);/, '卡间距必须沿用 8px 令牌');
assert.match(
  statsBlock[1],
  /width: calc\(\(100% - var\(--registry-stat-gap\) \* var\(--registry-stat-gap-count\)\) \* 2 \/ 3 \+ var\(--registry-stat-gap\) \* var\(--registry-stat-gap-count\)\);/,
  'QIN-531 的 2/3 宽度公式必须保持不变'
);

// 统计卡数组顺序：其他 紧随 画布。
const statsArray = js.match(/const stats = \[([\s\S]*?)\];\s*return `<section class="registry-overview-stats"/);
assert.ok(statsArray, '必须能定位统计卡数组');
const canvasPos = statsArray[1].indexOf("{id:'reference-canvas', name:'画布'");
const otherPos = statsArray[1].indexOf("{id:'other', name:'其他'");
assert.ok(canvasPos >= 0, '统计卡数组必须包含画布卡');
assert.ok(otherPos >= 0, '统计卡数组必须包含其他卡');
assert.ok(otherPos > canvasPos, '「其他」必须排在「画布」之后');

// ---- AC2：单击「画布」筛选全部画布资产 ----

// 单击分支直接进入画布资产视图并选「全部画布」，不再要求双击。
const statClickBlock = js.slice(
  js.indexOf("const registryStat = target.closest?.('[data-registry-stat]')"),
  js.indexOf("const registryFeature = target.closest?.('[data-registry-feature-toggle]')")
);
assert.ok(statClickBlock, '必须存在概览统计卡点击分支');
assert.match(statClickBlock, /category === 'reference-canvas'/, '单击分支必须识别画布统计卡');
assert.match(statClickBlock, /registryStatCategory = '';/, '离开统计条时必须清空统计分类过滤');
assert.match(statClickBlock, /activeTab = 'canvas-assets'/, '单击画布卡片应进入画布资产视图');
assert.match(statClickBlock, /activeCanvasAssetCategory = 'all';/, '单击画布卡片应筛选全部画布');
assert.match(statClickBlock, /activeCanvasAssetCanvasId = '';/, '进入全部画布时必须清空画布选择');
assert.doesNotMatch(js, /双击「画布」/, '双击提示必须移除');

// dblclick 监听不得再处理统计卡。
const dblClickOpen = js.slice(js.indexOf("root.addEventListener('dblclick'"), js.indexOf("const groupCard = event.target.closest?.('[data-registry-structure-card][data-registry-structure-type=\"group\"]')"));
assert.ok(dblClickOpen, '必须能定位 dblclick 监听');
assert.doesNotMatch(dblClickOpen, /data-registry-stat/, '双击监听不得再处理统计卡');

// 「全部画布」虚拟分类：顶置、跳过类型过滤、聚合全部画布、默认落地跳过。
assert.match(js, /const all = \{\s*\n\s*id:'all',\s*\n\s*name:'全部画布',/, '画布资产分类必须定义「全部画布」虚拟分类');
assert.match(js, /return \[all, \.\.\.cats\];/, '「全部画布」必须顶置在分类列表首位');
assert.match(js, /activeCanvasAssetCategory !== 'all' && \(item\.canvas_kind \|\| 'classic'\) !== activeCanvasAssetCategory/, '全部画布不得按画布类型过滤资产');
assert.match(js, /if\(categoryId === 'all'\)\{\s*\n\s*return \(Array\.isArray\(canvasAssetsData\.canvases\)/, '全部画布必须返回全部画布列表');
assert.match(js, /const cats = canvasAssetCategories\(\)\.filter\(cat => cat\.id !== 'all'\);/, '默认落地分类必须跳过「全部画布」');
