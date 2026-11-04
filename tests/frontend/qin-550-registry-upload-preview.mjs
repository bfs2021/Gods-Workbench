import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

// QIN-550：上传素材（仅 stored_name 登记）预览与回读地址修复的静态契约。
const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');

/** 截取具名函数的源码片段，用于断言分支内部行为。 */
function functionBody(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start > 0, `未找到函数 ${name}`);
    const next = source.indexOf('\nfunction ', start + 1);
    const fallback = source.indexOf('\nasync function ', start + 1);
    const end = Math.min(...[next, fallback].filter(index => index > 0));
    return source.slice(start, Number.isFinite(end) ? end : undefined);
}

test('AC3：normalizeRegistryAsset 对 stored_name 条目构造 /media 回读地址', () => {
    const body = functionBody('normalizeRegistryAsset');
    assert.match(body, /!item\.is_remote && assetId && \(item\.storage_root_id \|\| item\.stored_name\)/);
    assert.match(body, /`\/api\/asset-registry\/assets\/\$\{encodeURIComponent\(assetId\)\}\/media`/);
    assert.match(body, /QIN-550/);
});

test('AC3：assetPreviewUrl 的 asset_id 模式条件放宽为 storage_root_id || stored_name', () => {
    const body = functionBody('assetPreviewUrl');
    assert.match(body, /item\?\.id && \(item\?\.storage_root_id \|\| item\?\.stored_name\) && !item\?\.is_remote/);
    assert.match(body, /params\.set\('asset_id', String\(item\.id\)\)/);
});

test('AC3：assetVideoUrl 同族条件对齐，上传视频走 asset_id 转码模式', () => {
    const body = functionBody('assetVideoUrl');
    assert.match(body, /item\?\.id && \(item\?\.storage_root_id \|\| item\?\.stored_name\) && !item\?\.is_remote/);
    assert.match(body, /params\.set\('asset_id', String\(item\.id\)\)/);
});
