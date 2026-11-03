/**
 * QIN-525：全库预览缓存的并发提速与「暂停 / 继续 / 取消」控制契约。
 *
 * 断言面：
 * - 运行中只显示【暂停】【取消】，不再把主按钮当进度展示；
 * - 暂停态同一按钮变【继续】，且轮询必须继续（否则点继续后拿不到后续进度）；
 * - 控制请求打到 /jobs/{id}/control 并带 action；
 * - 取消终态给出「已取消」提示并刷新缓存统计。
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

test('AC6: 运行中显示暂停与取消，主按钮让位', () => {
    const body = functionSource('renderAssetPreviewSettingsBody');
    assert.match(body, /data-preview-all-pause/, '运行中必须提供暂停按钮');
    assert.match(body, /data-preview-all-stop/, '运行中必须提供取消按钮');
    assert.match(body, /const running = Boolean\(job && \['queued','running','paused'\]\.includes\(job\.status\)\)/,
        '暂停也属于运行中，删除缓存等互斥操作要保持禁用');
    assert.match(body, /const paused = job\?\.status === 'paused'/);
    assert.match(body, /paused \? '继续' : '暂停'/, '暂停态同一按钮必须变「继续」');
    // 运行中不再渲染主按钮：进度改由 status 行与进度条表达。
    assert.match(body, /running\s*\n\s*\? `/, '运行中分支必须先于主按钮分支');
});

test('AC6: 控制请求走 /control 并带 action', () => {
    const control = functionSource('controlPreviewAllJob');
    assert.match(control, /\/api\/asset-thumbnails\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/control/,
        '必须调用任务控制端点');
    assert.match(control, /body:JSON\.stringify\(\{action\}\)/, '必须把 action 传给后端');
    assert.match(control, /action === 'cancel'/, '取消要有独立提示');
    assert.match(control, /controlling:false/, '请求结束后必须清掉控制中状态');
});

test('AC6: 暂停态继续轮询，取消后刷新统计', () => {
    const poll = functionSource('pollPreviewAllJob');
    assert.match(poll, /\['queued','running','paused'\]\.includes\(task\.status\)/,
        '暂停态必须继续轮询，否则点继续后收不到进度');
    assert.match(poll, /task\.status === 'cancelled'/, '必须处理取消终态');
    assert.match(poll, /已取消/, '取消终态要有明确文案');
    assert.match(poll, /refreshThumbnailCacheStats\(\)/, '取消后要刷新缓存覆盖统计');
});

test('AC6: 点击派发把暂停/继续语义接到当前任务状态', () => {
    const dispatch = MANAGER.slice(MANAGER.indexOf('[data-preview-all-pause]'));
    assert.match(dispatch, /assetPreviewAllState\.job\?\.status === 'paused' \? 'resume' : 'pause'/,
        '同一按钮按当前状态在暂停/继续之间切换');
    assert.match(dispatch, /data-preview-all-stop/, '取消按钮必须接入统一委托');
});
