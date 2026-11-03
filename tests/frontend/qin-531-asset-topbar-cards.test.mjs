import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// QIN-531（GitHub #577）：统一资产库顶栏概览卡片双行紧凑化、画布全量整合、
// 排序/分组一体化下拉胶囊。静态契约：断言渲染模板与生效样式表的结构事实。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = relativePath => readFileSync(path.join(root, relativePath), 'utf8');

const js = read('web/js/modules/asset-manager.js');
const managerCss = read('web/css/pages/asset-manager.css');
const vaultCss = read('web/css/pages/asset-vault.css');

const statBlock = vaultCss.match(/\.registry-stat-card \{([\s\S]*?)\n\}/);
assert.ok(statBlock, 'asset-vault.css 必须定义概览卡片布局块');

// AC1：卡片收窄为 2 行紧凑网格，min-height 落在 42px~46px 区间。
assert.match(statBlock[1], /grid-template-rows:\s*auto auto;/, '概览卡片应为 2 行网格');
assert.match(
  statBlock[1],
  /min-height:\s*(4[2-6])px !important;/,
  '概览卡片 min-height 应落在 42px~46px 区间'
);
assert.doesNotMatch(statBlock[1], /grid-template-rows:\s*14px 25px 4px;/, '旧的 3 行堆叠必须移除');

// AC2：第 1 行 = 名称 + 占比，第 2 行 = 图标 + 数量 + 进度条。
assert.match(
  vaultCss,
  /\.registry-stat-card small \{ grid-area: 1 \/ 1 \/ 2 \/ 3;/,
  '名称应落在第 1 行左侧'
);
assert.match(
  vaultCss,
  /\.registry-stat-card em \{ grid-area: 1 \/ 3 \/ 2 \/ 4;[^}]*justify-self: end;/,
  '占比应靠第 1 行右侧'
);
assert.match(
  vaultCss,
  /\.registry-stat-icon \{ grid-area: 2 \/ 1 \/ 3 \/ 2;/,
  '图标应落在第 2 行行首'
);
assert.match(
  vaultCss,
  /\.registry-stat-card strong \{ grid-area: 2 \/ 2 \/ 3 \/ 3;[^}]*font-variant-numeric: tabular-nums;/,
  '数量应为第 2 行等宽数字'
);
assert.match(
  vaultCss,
  /\.registry-stat-meter \{ grid-area: 2 \/ 3 \/ 3 \/ 4;/,
  '进度条应落在第 2 行右侧并自适应拉伸'
);
// 资产总量卡片保留金底微型徽标
assert.match(js, /<em>\$\{stat\.primary \? '统一资产索引' : `\$\{ratio\}%`\}<\/em>/, '资产总量卡片应保留统一资产索引徽标');
assert.match(vaultCss, /\.registry-stat-card\.primary em \{[^}]*background: color-mix/s, '徽标应有金底背景');

// AC3：细进度条 + 深凹槽底轨 + 水平高光渐变填充。
const meterTrack = vaultCss.match(/\.registry-stat-meter \{([^}]*)\}/);
assert.ok(meterTrack, '必须定义进度条底轨');
assert.match(meterTrack[1], /height: 4px;/, '进度条高度应为 4px');
assert.match(meterTrack[1], /box-shadow: inset/, '底轨应有内凹阴影');
const meterFill = vaultCss.match(/\.registry-stat-meter > span \{([^}]*)\}/);
assert.ok(meterFill, '必须定义进度条填充');
assert.match(meterFill[1], /linear-gradient\(90deg/, '填充应为水平高光渐变');
assert.match(meterFill[1], /box-shadow: 0 0 6px/, '填充应带微光阴影');

// AC4：参考画布更名为画布、统计全量画布、恢复图标与进度条。
assert.match(js, /\{id:'reference-canvas', name:'画布', value:canvasTotal, icon:'map'\}/, '画布卡片应更名并统计全量画布');
assert.match(
  js,
  /const canvasTotal = Array\.isArray\(canvasAssetsData\.canvases\) \? canvasAssetsData\.canvases\.length : 0;/,
  '画布数量应取 canvasAssetsData.canvases 全量长度'
);
assert.doesNotMatch(js, /filter\(item => \(item\.kind \|\| 'classic'\) === 'reference'\)/, '不得再只过滤参考画布');
assert.doesNotMatch(
  vaultCss,
  /\[data-registry-stat="reference-canvas"\]/,
  '不得再对画布卡片隐藏图标与进度条'
);

// AC5：排序/分组收口在同一胶囊内（前缀标签 + 当前值 + ∨ 箭头）。
const sortCapsule = js.match(/<label class="hw-btn registry-toolbar-select"><i data-lucide="arrow-down-wide-narrow"><\/i><span>排序<\/span>[\s\S]*?<\/label>/);
assert.ok(sortCapsule, '排序控件应保留 hw-btn + registry-toolbar-select 胶囊');
assert.match(sortCapsule[0], /id="registrySort"/, '排序下拉 id 必须保留');
assert.match(sortCapsule[0], /class="registry-select-caret"/, '排序胶囊内应有 ∨ 箭头元素');
const groupCapsule = js.match(/<label class="hw-btn registry-toolbar-select"><i data-lucide="layers"><\/i><span>分组<\/span>[\s\S]*?<\/label>/);
assert.ok(groupCapsule, '分组控件应保留 hw-btn + registry-toolbar-select 胶囊');
assert.match(groupCapsule[0], /id="registryGroupMode"/, '分组下拉 id 必须保留');
assert.match(groupCapsule[0], /class="registry-select-caret"/, '分组胶囊内应有 ∨ 箭头元素');
assert.match(
  managerCss,
  /\.registry-toolbar-select \{[\s\S]*?border: 1px solid rgb\(223 195 132 \/ \.22\) !important;[\s\S]*?border-radius: 7px !important;[\s\S]*?background: linear-gradient\(180deg,#1a1d25,#0f1115\) !important;/,
  '胶囊外框应自带微边框与黑金渐变底'
);
assert.match(
  managerCss,
  /\.registry-toolbar-select \.asset-sort-select \{[\s\S]*?border: 0 !important;[\s\S]*?background: transparent !important;/,
  '内层原生 select 必须去框透明，视觉上并入胶囊'
);
assert.match(managerCss, /\.registry-select-caret \{[^}]*border-top: 4px solid #8d95a3;/, '∨ 箭头应由 CSS 三角绘制');
// 点击卡片任意区域调起原生选择菜单
assert.match(js, /const selectCapsule = target\.closest\?\.\('\.registry-toolbar-select'\);/, '点击委托应识别下拉胶囊');
assert.match(js, /select\.showPicker\(\)/, '点击胶囊任意区域应调起原生选择菜单');

console.log('QIN-531 asset topbar/toolbar contracts passed');
