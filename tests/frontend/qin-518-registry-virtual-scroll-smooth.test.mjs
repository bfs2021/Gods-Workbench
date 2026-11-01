/**
 * QIN-518：素材库跨虚拟化阈值原地升级、预览视口提前量与悬浮提示条。
 *
 * 断言面覆盖票面 AC1–AC3：
 * - AC1 翻页首次跨过虚拟窗口（100 → 200 条）时走原地升级，不调用全局 render()，
 *   并以首个可见卡片为滚动锚点恢复视口；
 * - AC2 预览预取带视口提前量，不再是 0px；
 * - AC3 新素材提示条为绝对定位胶囊，宿主 height:0，不撑开网格流。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);
const MANAGER_CSS = readFileSync(
    new URL('../../web/css/pages/asset-manager.css', import.meta.url),
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

test('AC1: 跨虚拟化阈值走原地升级而非整树 render()', () => {
    const promote = functionSource('promoteRegistryGridToVirtual');
    // 原地升级必须替换网格容器本身，而不是重建整个看板树。
    assert.match(promote, /grid\.replaceWith\(nextGrid\)/);
    const promoteCode = promote.replace(/\/\/[^\n]*/g, '');
    assert.doesNotMatch(promoteCode, /\brender\(\)/, '原地升级函数内严禁调用全局 render()');    // 保留滚动锚点：首个可见卡片 id + 相对视口偏移，升级后按锚点回填。
    assert.match(promote, /anchorId = String\(card\.dataset\.registryCard \|\| ''\)/);
    assert.match(promote, /anchorOffset = cardRect\.top - scrollRect\.top/);
    assert.match(promote, /restoreRegistryVirtualScroll\(scrollRoot, previousScrollTop, anchorId, anchorOffset\)/);
    // 升级前后预览图节点复用，避免图片重新解码造成白块闪烁。
    assert.match(promote, /captureRegistryPreviewImages\(\)/);
    assert.match(promote, /restoreRegistryPreviewImages\(registryPreviewCache\)/);

    // loadMore 在快路径失效前先判定"是否可原地升级"，并通过 promoteToVirtual 分支收尾。
    assert.match(
        MANAGER,
        /promoteToVirtual = shouldVirtualize && !appendTarget\.virtual && !structurePresentationChanged;/
    );
    assert.match(MANAGER, /if\(promoteToVirtual\)\{[\s\S]{0,400}?promoteRegistryGridToVirtual\(\)/);
    assert.match(MANAGER, /let promoteToVirtual = false;/);
});

test('AC1: 虚拟化阈值 120 条语义未被改动（100 普通网格、200 虚拟窗口）', () => {
    const enabled = new Function(
        'registryLayout',
        'registryGroupMode',
        'registryAssetStructures',
        'REGISTRY_VIRTUAL_WINDOW_SIZE',
        'activeTab',
        'assetBoardById',
        `${functionSource('registryVirtualizationEnabled')}\nreturn registryVirtualizationEnabled;`
    )('grid', 'none', new Map(), 120, 'registry', () => null);

    const items = count => Array.from({length:count}, (_, index) => ({id:`asset-${index}`}));
    assert.equal(enabled(items(100)), false, '首屏 100 条仍走普通网格快速追加');
    assert.equal(enabled(items(120)), false, '等于阈值仍不启用虚拟窗口');
    assert.equal(enabled(items(200)), true, '第二页累计 200 条启用虚拟窗口');
});

test('AC2: 预览预取带入视口提前量', () => {
    assert.match(MANAGER, /const REGISTRY_PREVIEW_ROOT_MARGIN = '200px 0px';/);
    assert.doesNotMatch(MANAGER, /REGISTRY_PREVIEW_ROOT_MARGIN = '0px 0px'/);
    assert.match(MANAGER, /\{root:scrollRoot, rootMargin:REGISTRY_PREVIEW_ROOT_MARGIN\}/);
});

test('AC3: 新素材提示条为悬浮胶囊，不撑开网格流', () => {
    // 宿主常驻且带 class，胶囊在其内部绝对定位。
    assert.match(
        MANAGER,
        /<div class="registry-fresh-notice" data-registry-fresh-notice><\/div>/g
    );
    assert.match(MANAGER, /class="hw-btn registry-fresh-capsule"/);
    // 提示消失时只清空内容，不再移除宿主（移除后整树渲染前提示条会永久失效）。
    assert.match(MANAGER, /if\(!registryPendingFreshCount\(\)\)\{ host\.innerHTML = ''; return; \}/);
    assert.doesNotMatch(functionSource('renderRegistryFreshNotice'), /host\.remove\(\)/);

    assert.match(MANAGER_CSS, /\.registry-fresh-notice \{ position:relative; height:0;/);
    assert.match(
        MANAGER_CSS,
        /\.registry-fresh-notice > \.registry-fresh-capsule \{[\s\S]*?position:absolute;[\s\S]*?pointer-events:auto;/
    );
});
