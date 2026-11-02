import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {pathCompareKey} from '../../web/js/modules/asset-manager/path-utils.js';

const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const hintText = '检测到缓存位于素材库内，系统已自动加入扫描排除名单，防止重复索引';

function functionSource(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `未找到函数 ${name}`);
    const next = source.indexOf('\nfunction ', start + 1);
    const asyncNext = source.indexOf('\nasync function ', start + 1);
    const ends = [next, asyncNext].filter(index => index >= 0);
    return source.slice(start, ends.length ? Math.min(...ends) : undefined);
}

function createHintHelpers(state, hintElement) {
    const assetPathHelper = functionSource('assetPathWithinAllowedRoot');
    const insideLibraryHelper = functionSource('thumbnailCacheDirectoryIsInsideLibrary');
    const updateHintHelper = functionSource('updateThumbnailCacheExclusionHint');
    return new Function(
        'thumbnailGeneratorState',
        'storageSettingsState',
        'localLibraries',
        'pathCompareKey',
        'hintElement',
        `const THUMBNAIL_CACHE_EXCLUSION_HINT = ${JSON.stringify(hintText)};
         const document = {querySelector: () => hintElement};
         ${assetPathHelper}
         ${insideLibraryHelper}
         ${updateHintHelper}
         return {thumbnailCacheDirectoryIsInsideLibrary, updateThumbnailCacheExclusionHint};`,
    )(state.thumbnail, state.settings, state.libraries, pathCompareKey, hintElement);
}

test('缓存路径在素材库内时显示指定提示，并按目录边界判断', () => {
    const hint = {textContent: '', hidden: true};
    const helpers = createHintHelpers({
        thumbnail: {mode: 'custom', directory: 'C:\\Media\\Library\\cache'},
        settings: {localLibraries: [{path: 'C:\\Media\\Library'}]},
        libraries: [],
    }, hint);

    assert.equal(helpers.thumbnailCacheDirectoryIsInsideLibrary(), true);
    helpers.updateThumbnailCacheExclusionHint();
    assert.equal(hint.textContent, hintText);
    assert.equal(hint.hidden, false);

    const sibling = createHintHelpers({
        thumbnail: {mode: 'custom', directory: 'C:\\Media\\Library-2\\cache'},
        settings: {localLibraries: [{path: 'C:\\Media\\Library'}]},
        libraries: [],
    }, {textContent: '', hidden: true});
    assert.equal(sibling.thumbnailCacheDirectoryIsInsideLibrary(), false);
});

test('非自定义缓存模式隐藏提示，设置区保留无障碍状态节点', () => {
    const hint = {textContent: hintText, hidden: false};
    const helpers = createHintHelpers({
        thumbnail: {mode: 'source', directory: 'C:\\Media\\Library\\cache'},
        settings: {localLibraries: [{path: 'C:\\Media\\Library'}]},
        libraries: [],
    }, hint);

    helpers.updateThumbnailCacheExclusionHint();
    assert.equal(hint.textContent, '');
    assert.equal(hint.hidden, true);
    assert.match(source, /data-thumbnail-cache-exclusion-hint role="status" aria-live="polite"/);
    assert.match(source, /document\.addEventListener\('input',[\s\S]*?event\.target\?\.id === 'thumbnailDirectory'[\s\S]*?updateThumbnailCacheExclusionHint\(\)/);
});
