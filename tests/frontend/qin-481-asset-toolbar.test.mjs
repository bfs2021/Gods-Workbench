import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// QIN-481（GitHub #456）：资产库筛选列与工具栏按钮按硬件按钮规范重做、搜索框去黑色遮挡。
// 静态契约：工具栏复用 .hw-btn 族与 segmented deck，禁用/选中态来自 hardware-buttons.css
// 既有规则；asset-manager.css 覆盖块压回 vault 旧主题的 40px/内凹黑影外观。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = relativePath => readFileSync(path.join(root, relativePath), 'utf8');

const js = read('web/js/modules/asset-manager.js');
const css = read('web/css/pages/asset-manager.css');

// 1. 排序/分组：胶囊复用 .hw-btn 硬件材质并带图标；QIN-531 起改为自绘 listbox（无原生 select）。
assert.match(
  js,
  /<label class="hw-btn registry-toolbar-select" data-registry-select-capsule="\$\{kind\}"><i data-lucide="\$\{icon\}"><\/i><span>\$\{label\}<\/span>/,
  '排序/分组控件应复用 hw-btn 材质并带图标'
);
assert.match(
  js,
  /renderRegistrySelectCapsule\(\{kind:'sort', icon:'arrow-down-wide-narrow', label:'排序'\}\)/,
  '排序控件应传入排序图标与标签'
);
assert.match(
  js,
  /renderRegistrySelectCapsule\(\{kind:'group', icon:'layers', label:'分组'\}\)/,
  '分组控件应传入分组图标与标签'
);
assert.match(js, /id="\$\{kind === 'sort' \? 'registrySort' : 'registryGroupMode'\}"/, '排序/分组 id 必须保留');

// 2. 列表/方格视图切换使用既有 segmented deck，方格比例切换为 hw-btn。
assert.match(
  js,
  /class="registry-layout-switch btn-segmented-deck"/,
  '视图切换应使用既有 btn-segmented-deck 组'
);
assert.doesNotMatch(js, /registry-segment/, '旧的 registry-segment 扁平容器应移除');
assert.match(
  js,
  /<button class="hw-btn" type="button" data-registry-aspect-toggle/,
  '方格比例切换应为 hw-btn 按钮'
);

// 3. 选中/禁用态继续由 hardware-buttons.css 既有选择器承担（aria-pressed / disabled）。
assert.match(
  js,
  /data-registry-aspect-toggle aria-pressed="\$\{registryAspect === 'square'\}"/,
  '方格比例切换应携带 aria-pressed 选中态'
);
assert.match(
  js,
  /data-registry-group-selected \$\{selectedIds\.length \? '' : 'disabled'\}/,
  '加入分组应保留 disabled 禁用态（虚线灰字来源）'
);

// 4. AC4：外层搜索标签透明且无框，边框仅由输入框本身提供。
const searchWrapperStyle = css.match(
  /body\.gw-signal\.gw-page--asset-manager #assetManagerRoot \.registry-toolbar \.asset-search-wrap \{([^}]*)\}/s
);
assert.ok(searchWrapperStyle, '搜索框外层样式应由 asset-manager.css 明确控制');
assert.match(searchWrapperStyle[1], /height: 32px !important/);
assert.match(searchWrapperStyle[1], /border: 0 !important/);
assert.match(searchWrapperStyle[1], /background: transparent !important/);
assert.match(searchWrapperStyle[1], /box-shadow: none !important/);
assert.match(
  css,
  /\.registry-toolbar \.asset-search-wrap \.asset-search \{[^}]*border: 1px solid rgb\(223 195 132 \/ \.2\) !important[^}]*border-radius: 8px !important/s,
  '搜索框应只保留输入框自身的圆角边框'
);
assert.match(
  css,
  /body\.gw-signal\.gw-page--asset-manager #assetManagerRoot \.registry-toolbar \.registry-toolbar-select \{[^}]*height: 32px !important/s,
  '排序/分组标签应压回 32px 与按钮排对齐'
);
assert.match(
  css,
  /body\.gw-signal\.gw-page--asset-manager #assetManagerRoot \.registry-toolbar \.registry-layout-switch \{[^}]*padding: 3px !important/s,
  '视图切换 deck 应恢复 recessed 内衬间距'
);

// 5. 智能筛选列选中态：金色描边金字，不再整块填金。
assert.match(
  css,
  /\.registry-facet-basic\.active \{ border-color:var\(--strong\); background:var\(--soft\); color:var\(--strong\); \}/,
  '筛选行选中态应为金描边金字'
);
assert.match(
  css,
  /\.registry-tag-row\.include > button:first-child \{ border:1px solid var\(--strong\); background:var\(--soft\); color:var\(--strong\); \}/,
  '标签包含选中态应为金描边金字'
);

// 6. 浅色主题下筛选列标题不得残留近白硬编码字色。
assert.doesNotMatch(css, /#eef3ee/, 'asset-manager.css 不得残留 #eef3ee 硬编码字色');

console.log('QIN-481 asset toolbar hardware-button contracts passed');
