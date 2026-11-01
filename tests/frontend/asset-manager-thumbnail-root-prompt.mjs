import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');

/** 截取具名函数的源码片段，用于断言分支内部行为。 */
function functionBody(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start > 0, `未找到函数 ${name}`);
    const next = source.indexOf('\nfunction ', start + 1);
    const fallback = source.indexOf('\nasync function ', start + 1);
    const end = Math.min(...[next, fallback].filter(index => index > 0));
    return source.slice(start, Number.isFinite(end) ? end : undefined);
}

test('缓存目录被拒用专用码识别，并渲染目录 / 原因 / 动作提示块', () => {
    assert.match(source, /const THUMBNAIL_CACHE_BLOCK_CODES = \['CACHE_DIRECTORY_NOT_ALLOWED', 'CACHE_DIRECTORY_NOT_FOUND'\]/);
    assert.match(source, /data-thumbnail-cache-block/);
    assert.match(source, /无法写入 \$\{escapeHtml\(directory\)\}/);
    assert.match(styles, /\.thumbnail-cache-block \{/);
    assert.match(styles, /\.thumbnail-cache-block-actions \{/);
});

test('管理员确认后登记允许根并用原请求参数重试一次（AC4）', () => {
    const grant = functionBody('grantThumbnailCacheRootAndRetry');
    assert.match(grant, /registerThumbnailCacheAllowedRoot\(block\.directory\)/);
    assert.match(grant, /generateThumbnails\(block\.payload\)/);
    assert.match(source, /data-thumbnail-cache-grant/);
    assert.match(source, /添加到允许根并重试/);
    assert.match(source, /expected_version:Number\(snapshot\.revision \|\| 1\)/);
    assert.match(source, /'\/api\/storage-settings\/roots'/);
});

test('非管理员不展示添加按钮，只给允许根引导文案（AC6）', () => {
    assert.match(source, /canGrant:canManageAllowedRoots\(\)/);
    assert.match(source, /请管理员在 设置 › 允许根 中登记该目录后重试。/);
});

test('取消分支不发起登记请求、不重试、不改写设置项（AC5）', () => {
    const dismiss = functionBody('dismissThumbnailCacheBlock');
    assert.doesNotMatch(dismiss, /\/api\//);
    assert.doesNotMatch(dismiss, /generateThumbnails/);
    assert.doesNotMatch(dismiss, /patchThumbnailSettings|updateThumbnailSettings/);
    assert.match(dismiss, /cacheBlocked:null/);
    assert.match(source, /data-thumbnail-cache-cancel/);
});

test('允许根版本冲突只提示刷新，不静默重发（AC7）', () => {
    const grant = functionBody('grantThumbnailCacheRootAndRetry');
    assert.match(grant, /error\?\.code === 'VERSION_CONFLICT'/);
    assert.match(grant, /请刷新后重试（未自动重发）/);
});

test('缓存位置文案按模式区分受管目录与所选目录', () => {
    assert.match(source, /function thumbnailCacheTargetText\(\)/);
    assert.match(source, /真实写入所选目录下的 \$\{folder\} 子目录/);
    assert.match(source, /写入应用受管数据目录内的 \$\{folder\}/);
});

test('用户指定缓存目录即自动登记允许根，不需要先撞 403（QIN-515 返工）', () => {
    const ensure = functionBody('ensureThumbnailCacheRootAllowed');
    assert.match(ensure, /canManageAllowedRoots\(\)/);
    assert.match(ensure, /'\/api\/storage-settings\/roots'/);
    assert.match(ensure, /expected_version:Number\(snapshot\.revision \|\| 1\)/);
    assert.match(ensure, /ALLOWED_ROOT_ALREADY_REGISTERED/);
    assert.match(ensure, /VERSION_CONFLICT/);
    assert.match(functionBody('saveThumbnailCacheDirectory'), /ensureThumbnailCacheRootAllowed/);
    assert.match(functionBody('commitThumbnailSettingsChange'), /ensureThumbnailCacheRootAllowed/);
});

test('切换缓存目录弹出确认框，含迁移 / 删除 / 取消三个动作', () => {
    assert.match(source, /是否迁移原缓存/);
    assert.match(source, /data-thumbnail-cache-switch="migrate"/);
    assert.match(source, /data-thumbnail-cache-switch="delete"/);
    assert.match(source, /data-thumbnail-cache-switch="cancel"/);
    assert.match(source, /switchThumbnailCache\(\{mode:state\.toMode, directory:state\.toDirectory, cache_action:action\}\)/);
    assert.match(styles, /\.thumbnail-cache-switch-modal \{/);
});

test('取消切换不改设置，目录回退到已保存值', () => {
    const cancel = functionBody('cancelThumbnailCacheSwitch');
    assert.doesNotMatch(cancel, /switchThumbnailCache/);
    assert.match(cancel, /savedDirectory/);
    assert.match(cancel, /已取消切换/);
});

test('切换弹窗挂在 body 上，点击必须经文档级委托进入 handleClick', () => {
    // #assetManagerRoot 的 root.addEventListener 收不到 body 级浮层的点击，
    // 必须由文档级委托清单兜住，否则弹窗所有按钮点击都无效（QIN-515 返工 3）。
    const render = functionBody('renderThumbnailCacheSwitchDialog');
    assert.match(render, /document\.body\.appendChild\(overlay\)/);
    const delegation = source.match(/event\.target\.closest\?\.\('(#storageSettingsOverlay[^']*)'\)/);
    assert.ok(delegation, '未找到文档级的 body 浮层点击委托清单');
    assert.ok(
        delegation[1].split(',').includes('#thumbnailCacheSwitchOverlay'),
        '切换弹窗容器 id 不在文档级委托清单里，按钮点击会全部失效',
    );
});

test('目录与已保存值不同才询问；未确认前不发起生成', () => {
    const commit = functionBody('commitThumbnailSettingsChange');
    assert.match(commit, /openThumbnailCacheSwitchDialog\(mode, directory\)/);
    const request = functionBody('thumbnailGeneratorRequest');
    assert.match(request, /openThumbnailCacheSwitchDialog\(mode, directory\)/);
    assert.match(request, /缓存目录尚未切换/);
});
