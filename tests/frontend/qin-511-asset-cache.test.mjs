import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

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

test('预览 URL 携带缓存所需参数：宽度 + 资产身份 + 版本（AC5）', () => {
    const preview = functionBody('assetPreviewUrl');
    // 宽度入参：卡片请求的宽度必须进入 URL，后端按宽度分桶命中缓存。
    assert.match(preview, /new URLSearchParams\(\{w:String\(width\)\}\)/);
    assert.match(preview, /Math\.max\(64, Math\.min\(2048, Math\.round\(Number\(w\) \|\| 256\)\)\)/);
    // 资产身份 + 版本：缺了 asset_id 后端就无法按稳定 ID 复用预生成产物。
    assert.match(preview, /params\.set\('asset_id', String\(item\.id\)\)/);
    assert.match(preview, /params\.set\('v', String\(Number\(item\?\.updated_at \|\| item\?\.indexed_at \|\| 0\)\)\)/);
    assert.match(preview, /params\.set\('rv', ASSET_PREVIEW_RENDER_VERSION\)/);
    assert.match(preview, /return `\/api\/media-preview\?\$\{params\.toString\(\)\}`/);
});

test('卡片走预览接口，不直连文件系统也不出现裸缓存路径（AC5）', () => {
    const card = functionBody('assetThumb');
    // 图片 / 视频 / 音频卡片一律经 assetPreviewUrl 拼出预览接口地址。
    assert.match(card, /assetPreviewUrl\(item\.url, 256, item\)/);
    assert.match(card, /assetPreviewUrl\(item\.url, 384, item\)/);
    // 通用图片分支（卡片主体）：宽度按类型取 256 / 384，且必须过预览接口。
    assert.match(card, /const width = kind === 'text' \? 384 : 256;/);
    assert.match(card, /assetPreviewUrl\(item\.url, width, item\)/);
    assert.doesNotMatch(card, /media_output/);
    assert.doesNotMatch(card, /file:\/\//);
    // 整个前端都不把受管缓存目录当 URL 用：缓存位置是服务端的事。
    assert.doesNotMatch(source, /media_output/);
});

test('只有受支持的本地路径才走缓存预览，其余原样返回（AC5）', () => {
    const preview = functionBody('assetPreviewUrl');
    assert.match(preview, /const local = path\.startsWith\('\/output\/'\)/);
    assert.match(preview, /if\(!local\) return raw;/);
    // 视频走转码地址，且不复用图片预览的宽度分桶。
    assert.match(functionBody('assetVideoUrl'), /return `\/api\/media-transcode\?\$\{params\.toString\(\)\}`/);
});

test('批量生成与删除的载荷形状是 asset_ids（与后端一致）', () => {
    // 后端同时接受 asset_id / asset_ids；前端真实路径必须用批量形状。
    assert.match(source, /payload:\{asset_ids:ids, mode, directory, width:256, transcode_videos:transcodeVideos, replace_existing:true\}/);
    assert.match(functionBody('generateBoardAssetThumbnails'), /assetManagerApi\.generateThumbnails\(payload\)/);
    assert.match(source, /deleteThumbnails\(\{asset_ids:ids\}\)/);
    assert.match(source, /generateThumbnailStoryboards\(\{asset_ids:ids\}\)/);
    assert.match(source, /deleteThumbnailStoryboards\(\{asset_ids:ids\}\)/);
});

test('缓存目录切换后仍以已保存设置落盘，界面目录不直接当缓存根（AC5）', () => {
    const request = functionBody('thumbnailGeneratorRequest');
    // 界面目录与已保存目录不一致时先弹确认框，不直接拿去写盘。
    assert.match(request, /openThumbnailCacheSwitchDialog\(mode, directory\)/);
    assert.match(request, /payload:\{asset_ids:ids, mode, directory/);
});
