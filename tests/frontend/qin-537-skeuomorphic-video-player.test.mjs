import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// QIN-537（GitHub #589）：拟物视频播放器重构
// 按 Stitch 原型实现黑金硬件机箱、高光时间线与触感按键
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = relativePath => readFileSync(path.join(root, relativePath), 'utf8');

const js = read('web/js/modules/asset-manager.js');
const managerCss = read('web/css/pages/asset-manager.css');
const vaultCss = read('web/css/pages/asset-vault.css');

// =========================================================================
// AC1: 视频播放器外层黑金硬件机箱底盘 (Player Chassis) 与视口金属边框 (Viewport Bezel)
// =========================================================================
assert.match(js, /class="asset-detail-video-dock player-chassis"/, '播放器控制底座应具有 player-chassis 类');
assert.match(js, /class="chassis-screw chassis-screw-tl"/, '机箱应具备左上角内六角螺钉装饰');
assert.match(js, /class="chassis-screw chassis-screw-tr"/, '机箱应具备右上角内六角螺钉装饰');
assert.match(js, /class="chassis-screw chassis-screw-bl"/, '机箱应具备左下角内六角螺钉装饰');
assert.match(js, /class="chassis-screw chassis-screw-br"/, '机箱应具备右下角内六角螺钉装饰');
assert.match(js, /class="asset-detail-video-telemetry"/, '机箱应具备顶栏广播规格标牌');
assert.match(js, /viewport-bezel/, '视频视口节点应具备 viewport-bezel 类');

assert.match(managerCss, /\.player-chassis/, 'asset-manager.css 必须定义 player-chassis 样式');
assert.match(managerCss, /linear-gradient\(175deg,\s*#181a22\s*0%,\s*#101217\s*60%,\s*#0a0b0e\s*100%\)/, '机箱底盘必须具备深邃金属渐变');
assert.match(managerCss, /border-top:\s*1px\s*solid\s*rgba\(250,\s*226,\s*200,\s*0\.45\)/, '机箱顶部必须具备高光金线');
assert.match(managerCss, /\.chassis-screw/, 'asset-manager.css 必须定义 chassis-screw 螺钉样式');
assert.match(managerCss, /\.viewport-bezel/, 'asset-manager.css 必须定义 viewport-bezel 内凹金属框');
assert.match(managerCss, /inset\s*0\s*3px\s*14px\s*rgba\(0,\s*0,\s*0,\s*0\.98\)/, '视口内凹框必须具备内阴影金属质感');
assert.match(vaultCss, /\.player-chassis/, 'asset-vault.css 必须同步定义 player-chassis 样式');

// =========================================================================
// AC2: 重构中央悬停播放键为黑金拟物触感大微球
// =========================================================================
assert.match(js, /class="asset-detail-video-center-play-socket"/, '中央播放键必须具有内凹底槽容器');
assert.match(js, /class="asset-detail-video-center-play hw-play-capsule"/, '中央播放键必须升级为 hw-play-capsule');
assert.match(managerCss, /\.asset-detail-video-center-play-socket/, '样式表中必须定义播放键暗黑微凹底槽');
assert.match(managerCss, /\.asset-detail-media-node-clip \.asset-detail-video-center-play/, '必须定义中央拟物播放按键');
assert.match(
    managerCss,
    /radial-gradient\(circle at 45% 45%,\s*#ffffff\s*0%,\s*#fff2dc\s*25%,\s*#f5cf92\s*55%,\s*#dfac64\s*80%,\s*#b8823f\s*100%\)/,
    '中央播放按键必须采用香槟钛金拟物光学微球渐变'
);
assert.match(managerCss, /\.asset-detail-media-node\.is-video-playing \.asset-detail-video-center-play-socket/, '播放时平滑隐藏播放微球');
assert.match(managerCss, /\.asset-detail-media-node\.is-video-playing:hover \.asset-detail-video-center-play-socket/, '悬停时平滑显现播放微球');

// =========================================================================
// AC3: 底部控制区沉降式金属凹槽 (Transport Bar Trough) 与激光高光进度条
// =========================================================================
assert.match(js, /class="asset-detail-video-control-panel transport-bar-trough"/, '控制面板必须具备 transport-bar-trough 类');
assert.match(js, /class="asset-detail-video-progress-wrap scrubber-track"/, '进度条容器必须具备 scrubber-track 类');
assert.match(js, /class="scrubber-loaded"/, '进度条必须具备缓冲轨 scrubber-loaded');
assert.match(js, /class="scrubber-fill" data-detail-video-fill/, '进度条必须具备高光渐变填充轨道 scrubber-fill');
assert.match(managerCss, /\.transport-bar-trough/, '样式表必须定义沉降式金属凹槽');
assert.match(
    managerCss,
    /linear-gradient\(90deg,\s*#c9ad6e\s*0%,\s*#dfc384\s*60%,\s*#fae2c8\s*100%\)/,
    '进度条必须采用渐变高光填充 linear-gradient(90deg, #c9ad6e, #dfc384, #fae2c8)'
);
assert.match(managerCss, /\.asset-detail-video-progress::-webkit-slider-thumb/, '滑块必须升级为拟物金属旋钮');
assert.match(js, /const fill = controls\.querySelector\('\[data-detail-video-fill\]'\);/, '播放同步逻辑必须驱动 scrubber-fill 宽度');

// =========================================================================
// AC4: 阴刻数码管时间框 (Timecode Recessed) 与立体声动态 VU 电平表
// =========================================================================
assert.match(js, /class="asset-detail-video-time timecode-recessed"/, '时间显示框必须具备 timecode-recessed 类');
assert.match(managerCss, /\.timecode-recessed/, '样式表必须定义 timecode-recessed 样式');
assert.match(managerCss, /text-shadow:\s*0\s*0\s*8px\s*rgba\(223,\s*195,\s*132,\s*0\.35\)/, '时间码文字必须具备琥珀金背光发光');
assert.match(js, /class="asset-detail-video-vu-meter"/, '播放器必须集成双声道 VU 电平指示表');
assert.match(js, /class="vu-bar-fill vu-bar-left"/, 'VU 表必须包含左声道');
assert.match(js, /class="vu-bar-fill vu-bar-right"/, 'VU 表必须包含右声道');
assert.match(managerCss, /vu-bounce-left/, '样式表必须定义播放状态下动态摆动动画');

// =========================================================================
// AC5: 硬件金属微动按键 (hw-tactile-btn) 与微胶囊剪辑工具栏 (hw-capsule-btn)
// =========================================================================
// 票面 AC5 明确要求「播放/暂停大键与上一帧/下一帧」同为 hw-tactile-btn；
// AC2 的金色拟物微球只属于画面中央悬停键，两者不得混用。
assert.match(js, /class="asset-detail-video-play hw-tactile-btn"/, '走带播放键必须具备 hw-tactile-btn 类（票面 AC5）');
assert.match(js, /class="hw-tactile-btn-inner hw-play-tactile-inner"/, '走带播放键必须具备双层倒角内核');
assert.doesNotMatch(js, /class="asset-detail-video-play hw-play-capsule"/, '走带播放键不得改用中央微球的金色材质');
assert.match(js, /class="asset-detail-video-center-play hw-play-capsule"/, '画面中央悬停播放键保留香槟钛金微球材质（AC2）');
assert.match(js, /class="hw-tactile-btn"[^>]*data-detail-video-action="previous-frame"/, '上一帧按键必须具备 hw-tactile-btn 类');
assert.match(js, /class="hw-tactile-btn"[^>]*data-detail-video-action="next-frame"/, '下一帧按键必须具备 hw-tactile-btn 类');
assert.match(js, /class="asset-detail-video-mute hw-tactile-btn"/, '静音按键必须具备 hw-tactile-btn 类');
assert.match(js, /class="hw-tactile-btn-inner"/, '微动按键必须具有双层倒角内核 hw-tactile-btn-inner');
assert.match(js, /class="is-labeled hw-capsule-btn"/, '剪辑工具栏操作按键必须具备 hw-capsule-btn 微胶囊规范');
assert.match(managerCss, /\.hw-tactile-btn/, '样式表必须定义 hw-tactile-btn 触感微动按键');
assert.match(managerCss, /\.hw-capsule-btn/, '样式表必须定义 hw-capsule-btn 微胶囊按键');

// =========================================================================
// 回归防线：真实渲染中已修的两处塌陷（静态契约 + 浏览器复验双覆盖）
// =========================================================================
// 1) 机箱必须承接既有「位于素材节点下方、宽度铺满节点」的走带几何。
//    新机箱规则与 `.asset-detail-media-node > .asset-detail-video-dock` 同为 2 个类选择器，
//    若写成 width:auto，再叠加 container-type:inline-size，宽度会塌成 padding 宽度（30px）。
assert.match(
    managerCss,
    /\.asset-detail-video-dock\.player-chassis\s*\{[^}]*top:\s*calc\(100%\s*\+\s*12px\)[^}]*width:\s*100%/s,
    '机箱必须显式写回 top:calc(100% + 12px) 与 width:100%，避免宽度塌成 padding 宽'
);
// 2) 走带滑块必须保持透明底，否则 base/theme.css 的
//    `body.studio-theme-dark input{background:var(--soft)!important}` 会盖住自绘轨与高光填充。
assert.match(
    managerCss,
    /\.studio-theme-dark\s+\.asset-detail-video-dock\s+input\.asset-detail-video-progress[^{]*\{[^}]*background:\s*transparent\s*!important/s,
    '走带滑块必须按容器作用域覆盖为透明底，保证 scrubber 轨与高光填充可见'
);

console.log('✔ QIN-537 所有验收标准 (AC1 ~ AC5) 静态契约与断言验证通过！');
