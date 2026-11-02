/**
 * QIN-519：资产设置「缓存全部预览」按钮与全量后台预生成任务。
 *
 * 断言面覆盖票面 AC2 / AC3：
 * - AC2 资产设置面板（renderAssetPreviewSettingsBody）出现「缓存全部预览」入口与离线预热说明；
 * - AC3 两段式确认后才提交，提交走 generate-background 的 scope=library，
 *   忙态显示「正在缓存 processed/total…」，完成通知带新增/跳过/耗时；
 * - 回归护栏：不得静默预热（该入口只能由点击触发），运行中不得重复提交。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);

function functionSource(name){
    const start = MANAGER.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const open = MANAGER.indexOf('{', start);
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

test('AC2: 资产设置面板挂载「缓存全部预览」入口与离线预热说明', () => {
    const body = functionSource('renderAssetPreviewSettingsBody');
    assert.match(body, /renderPreviewAllCacheSettingsBody\(\)/, '设置面板必须挂载全库预览缓存区块');

    const section = functionSource('renderPreviewAllCacheSettingsBody');
    assert.match(section, /缓存全部预览/, '按钮文案必须是「缓存全部预览」');
    assert.match(section, /为全库素材后台生成预览图，适合离线前预热/, '必须给出离线预热说明');
    assert.match(section, /data-preview-all-start/, '入口按钮必须带 data-preview-all-start');
    // 全库口径来自已加载的注册表分类计数，避免误报总数。
    assert.match(section, /registryCategoryCount\('image'\)/);
    assert.match(section, /registryCategoryCount\('video'\)/);
});

test('AC3: 两段式确认后才提交，提交走 scope=library', () => {
    const dispatchIndex = MANAGER.indexOf("[data-preview-all-start]");
    assert.ok(dispatchIndex > 0, '点击派发必须登记 data-preview-all-start');
    assert.match(MANAGER, /if\(target\.closest\?\.\('\[data-preview-all-start\]'\)\)\{ requestPreviewAllConfirm\(\); return; \}/);
    assert.match(MANAGER, /if\(target\.closest\?\.\('\[data-preview-all-confirm\]'\)\)\{ await startPreviewAllCache\(\); return; \}/);
    assert.match(MANAGER, /if\(target\.closest\?\.\('\[data-preview-all-cancel\]'\)\)\{ cancelPreviewAllConfirm\(\); return; \}/);

    const confirm = functionSource('requestPreviewAllConfirm');
    assert.match(confirm, /confirming:true/, '首次点击只进入确认态，不能直接提交');
    assert.match(confirm, /renderStorageSettingsModal\(\)/);

    const start = functionSource('startPreviewAllCache');
    assert.match(start, /'\/api\/asset-thumbnails\/generate-background'/);
    assert.match(start, /scope:'library'/, '必须显式声明全库模式');
    assert.match(start, /method:'POST'/);
});

test('AC3: 忙态显示真实进度，完成通知带新增/跳过/耗时', () => {
    const section = functionSource('renderPreviewAllCacheSettingsBody');
    assert.match(section, /正在缓存 \$\{processed\}\/\$\{total\}…/, '忙态必须显示「正在缓存 120/950…」样式进度');
    assert.match(section, /disabled/, '运行中按钮必须禁用，避免重复提交');

    const poll = functionSource('pollPreviewAllJob');
    assert.match(poll, /\/api\/asset-thumbnails\/jobs\/\$\{encodeURIComponent\(jobId\)\}/);
    assert.match(poll, /全库预览缓存完成：新增/, '完成通知必须回报实际新增数量');
    assert.match(poll, /跳过/, '完成通知必须回报跳过数量（幂等证明）');
    assert.match(poll, /耗时/);
    assert.match(poll, /setStatus\(notice\)/, '完成通知必须同步到状态栏');
});

test('回归护栏：全库预热只能由创作者显式触发', () => {
    const startCalls = MANAGER.match(/startPreviewAllCache\(/g) || [];
    // 定义一次 + 点击派发一次，不允许其他任何调用点（无自动启动）。
    assert.equal(startCalls.length, 2, `startPreviewAllCache 只允许定义与点击派发，实际 ${startCalls.length} 处`);
    assert.doesNotMatch(MANAGER, /setInterval\([^)]*startPreviewAllCache/);
    const start = functionSource('startPreviewAllCache');
    assert.match(start, /if\(assetPreviewAllState\.submitting\) return;/, '提交中必须早退，避免重复任务');
});
