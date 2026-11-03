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

// AC5：排序/分组收口在同一胶囊内（前缀标签 + 当前值），下拉改为自绘 listbox。
assert.match(
  js,
  /renderRegistrySelectCapsule\(\{kind:'sort', icon:'arrow-down-wide-narrow', label:'排序'\}\)/,
  '排序控件应由统一胶囊渲染函数产出'
);
assert.match(
  js,
  /renderRegistrySelectCapsule\(\{kind:'group', icon:'layers', label:'分组'\}\)/,
  '分组控件应由统一胶囊渲染函数产出'
);
assert.match(
  js,
  /<label class="hw-btn registry-toolbar-select" data-registry-select-capsule="\$\{kind\}"><i data-lucide="\$\{icon\}"><\/i><span>\$\{label\}<\/span>/,
  '胶囊应保留 hw-btn 材质、图标在前、标签在后的结构'
);
assert.match(js, /\$\{kind === 'sort' \? 'registrySort' : 'registryGroupMode'\}/, '排序/分组 id 必须保留');
assert.match(js, /class="registry-select-menu" role="listbox"/, '下拉应为自绘 listbox 菜单');
assert.doesNotMatch(js, /registry-select-caret/, '向下三角箭头必须删除');
assert.doesNotMatch(js, /class="asset-sort-select"/, '原生 select 必须移除（系统下拉无法控制圆角与蓝底）');
assert.match(js, /toggleRegistrySelectMenu\(/, '点击胶囊应由自绘菜单开关接管');
assert.doesNotMatch(js, /showPicker/, '不得再调起原生选择菜单');
assert.match(
  managerCss,
  /\.registry-toolbar-select \{[\s\S]*?border: 1px solid var\(--vault-line\) !important;[\s\S]*?border-radius: 7px !important;[\s\S]*?background: linear-gradient\(180deg,var\(--vault-top\),var\(--vault-bottom\)\) !important;/,
  '胶囊应与工具栏其它按钮同用扁平底板（与概览卡材质对调）'
);
assert.match(
  managerCss,
  /\.registry-toolbar-select \.registry-select-trigger \{[\s\S]*?border: 0 !important;[\s\S]*?background: transparent !important;/,
  '触发器必须去框透明，视觉上并入胶囊'
);
assert.match(
  managerCss,
  /\.registry-toolbar-select \.registry-select-menu \{[\s\S]*?border-radius: 10px;/,
  '下拉菜单必须为圆角'
);
assert.match(
  managerCss,
  /\.registry-select-option:is\(:hover,:focus-visible\) \{\s*background: rgb\(223 195 132 \/ \.14\) !important;/,
  '选项悬浮应为淡金底，不得出现系统蓝'
);
assert.match(
  managerCss,
  /:is\(\.registry-select-trigger,\.registry-select-option\):is\(:focus,:focus-visible\) \{[\s\S]*?outline: none !important;/,
  '触发器与选项不得出现焦点方框'
);
assert.doesNotMatch(managerCss, /registry-select-caret/, '三角箭头样式必须移除');

// AC7：概览卡与工具栏按钮互换材质——卡片接管键帽（上亮下暗 + 顶部高光 + 落影），
// 工具栏按钮与下拉胶囊接管卡片原本的扁平底板（细边框 + 单像素内高光 + 极轻落影）。
assert.match(statBlock[1], /border-top-color: rgb\(255 255 255 \/ \.18\) !important;/, '概览卡应带键帽顶部高光边');
assert.match(
  statBlock[1],
  /background: linear-gradient\(180deg,#1e222d 0%,#11131a 100%\) !important;/,
  '概览卡应使用键帽上亮下暗渐变'
);
assert.match(
  statBlock[1],
  /box-shadow: inset 0 1px 0 rgb\(255 255 255 \/ \.14\), 0 4px 8px rgb\(0 0 0 \/ \.75\)/,
  '概览卡应带键帽内高光与落影'
);
assert.doesNotMatch(
  statBlock[1],
  /background: linear-gradient\(180deg,var\(--vault-top\),var\(--vault-bottom\)\)/,
  '概览卡不得再使用扁平底板（已与工具栏按钮对调）'
);
const toolbarButtonBlock = vaultCss.match(/\.registry-toolbar \.hw-btn:not\(\.btn-segmented-item\) \{([\s\S]*?)\n\}/);
assert.ok(toolbarButtonBlock, 'asset-vault.css 必须定义工具栏按钮的扁平底板');
assert.match(toolbarButtonBlock[1], /border: 1px solid var\(--vault-line\) !important;/, '工具栏按钮应用细边框');
assert.match(
  toolbarButtonBlock[1],
  /background: linear-gradient\(180deg,var\(--vault-top\),var\(--vault-bottom\)\) !important;/,
  '工具栏按钮应使用概览卡原本的扁平渐变底'
);
assert.match(toolbarButtonBlock[1], /box-shadow: inset 0 1px rgb\(255 255 255 \/ \.045\), 0 1px 2px/, '工具栏按钮应保留单像素内高光与轻落影');
assert.doesNotMatch(toolbarButtonBlock[1], /#1e222d/, '工具栏按钮不得再使用键帽渐变（已与概览卡对调）');
assert.match(
  vaultCss,
  /html\[data-theme="light"\] body\.gw-signal\.gw-page--asset-manager \.registry-stat-card \{\n\s*border: 1px solid rgb\(31 41 55 \/ \.26\) !important;/,
  '浅色主题下概览卡应同样换成有边界的立体卡面'
);

// AC6：「画布」卡片改为双击跳转，单击只做选中反馈。
const statClickBlock = js.slice(
  js.indexOf("const registryStat = target.closest?.('[data-registry-stat]')"),
  js.indexOf("const registryFeature = target.closest?.('[data-registry-feature-toggle]')")
);
assert.ok(statClickBlock, '必须存在概览卡片点击分支');
assert.match(statClickBlock, /双击「画布」进入画布资产库/, '单击画布卡片应提示双击跳转');
assert.doesNotMatch(statClickBlock, /activeTab = 'canvas-assets'/, '单击画布卡片不得直接跳转');
const dblClickOpen = js.slice(js.indexOf("root.addEventListener('dblclick'"), js.indexOf("const groupCard = event.target.closest?.('[data-registry-structure-card][data-registry-structure-type=\"group\"]')"));
assert.match(dblClickOpen, /\[data-registry-stat="reference-canvas"\]/, '双击监听应识别画布卡片');
assert.match(dblClickOpen, /activeTab = 'canvas-assets'/, '双击画布卡片应跳转到画布资产库');

console.log('QIN-531 asset topbar/toolbar contracts passed');
