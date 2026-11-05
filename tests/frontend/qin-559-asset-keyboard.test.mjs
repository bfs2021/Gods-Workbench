/** QIN-559：全屏浏览、视频帧步进与 Backquote 编辑手势。 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const COVERFLOW = readFileSync(new URL('../../web/js/modules/asset-manager/coverflow.js', import.meta.url), 'utf8');
const GUIDE = readFileSync(new URL('../../docs/guides/KEYBOARD-SHORTCUTS.md', import.meta.url), 'utf8');

function functionSource(source, name){
    let start = source.indexOf(`function ${name}(`);
    if(start < 0) start = source.indexOf(`async function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const open = source.indexOf('{', source.indexOf(')', start));
    let depth = 0;
    for(let index = open; index < source.length; index += 1){
        if(source[index] === '{') depth += 1;
        else if(source[index] === '}'){
            depth -= 1;
            if(depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`函数未闭合：${name}`);
}

function createDetailBackquoteRecognizer(){
    return new Function(`
        let now = 0;
        let lastDetailBackquoteTap = null;
        const DETAIL_BACKQUOTE_DOUBLE_TAP_MS = 500;
        const Date = {now:() => now};
        ${functionSource(MANAGER, 'isBackquoteKey')}
        ${functionSource(MANAGER, 'isBackquoteShortcut')}
        ${functionSource(MANAGER, 'detailBackquoteTappedTwice')}
        return {recognize:detailBackquoteTappedTwice, setTime:value => { now = value; }};
    `)();
}

function createCoverflowKeyHandler(callbacks){
    return new Function('viewItem', 'move', 'requestFullscreenClose', 'requestEdit', 'window', `
        let now = 0;
        let lastEditBackquoteTap = null;
        const EDIT_BACKQUOTE_WINDOW_MS = 500;
        const Date = {now:() => now};
        ${functionSource(COVERFLOW, 'isBackquoteKey')}
        ${functionSource(COVERFLOW, 'isBackquoteShortcut')}
        ${functionSource(COVERFLOW, 'handleKeydown')}
        return {
            handle:handleKeydown,
            setTime:value => { now = value; },
        };
    `)(() => ({id:'asset-1'}), () => {}, callbacks.close, callbacks.edit, {FloatingDismissal:null});
}

function keyEvent(key, code='Backquote'){
    return {
        key, code, repeat:false, ctrlKey:false, metaKey:false, altKey:false, isComposing:false,
        defaultPrevented:false, prevented:false, stopped:false,
        preventDefault(){ this.prevented = true; },
        stopPropagation(){ this.stopped = true; },
    };
}

test('双击识别限定同一详情与 500ms 窗口', () => {
    const recognizer = createDetailBackquoteRecognizer();
    const tap = keyEvent('·');
    assert.equal(recognizer.recognize(tap, 'registry:image-1'), false);
    recognizer.setTime(500);
    assert.equal(recognizer.recognize(tap, 'registry:image-1'), true);

    assert.equal(recognizer.recognize(tap, 'registry:image-1'), false);
    recognizer.setTime(1001);
    assert.equal(recognizer.recognize(tap, 'registry:image-1'), false);
    assert.equal(recognizer.recognize(tap, 'registry:image-2'), false);
});

test('全屏 Coverflow 双击 Backquote 进编辑，F 与 Escape 退出', () => {
    const requests = {edit:0, close:0};
    const handler = createCoverflowKeyHandler({
        close:() => { requests.close += 1; },
        edit:() => { requests.edit += 1; },
    });
    const view = {mode:'fullscreen', source:'registry', scope:'library'};
    const tap = keyEvent('`');
    handler.handle(view, tap);
    assert.equal(requests.edit, 0);
    assert.equal(tap.prevented, true);
    handler.setTime(500);
    handler.handle(view, keyEvent('`'));
    assert.equal(requests.edit, 1);

    handler.handle(view, keyEvent('f', 'KeyF'));
    assert.equal(requests.close, 1);
    handler.handle(view, keyEvent('Escape', 'Escape'));
    assert.equal(requests.close, 2);
    assert.match(COVERFLOW, /assets:\s*view\.items\.map\(value => value\.raw\)/);
});

test('画布 F 使用选择集，视频 A/D 逐帧且 F 切换全屏', () => {
    const selection = functionSource(MANAGER, 'detailMediaCanvasSelectedCoverflowAssets');
    const openSelection = functionSource(MANAGER, 'openDetailMediaCanvasSelectionCoverflow');
    const toggleFullscreen = functionSource(MANAGER, 'toggleDetailVideoFullscreen');
    assert.match(selection, /detailMediaCanvasExpandedObjectIds\(detailMediaCanvasSelectionOrder\(\)\)/);
    assert.match(openSelection, /mode:'fullscreen'/);
    assert.match(openSelection, /assets,/);
    assert.match(openSelection, /currentAssetId,/);
    assert.doesNotMatch(MANAGER, /hasCanvasFullscreenSelection/);
    assert.match(MANAGER, /stepDetailVideoFrame\(key === 'd' \? 1 : -1\)/);
    assert.match(MANAGER, /detailMediaCanvasSelectedCoverflowAssets\(\)\.length <= 1\)\)\{\s*event\.preventDefault\(\);\s*toggleDetailVideoFullscreen\(\)/);
    assert.match(MANAGER, /key === 'f'\)\{\s*event\.preventDefault\(\);\s*toggleDetailVideoFullscreen\(\)/);
    assert.match(toggleFullscreen, /document\.fullscreenElement === video/);
    assert.match(MANAGER, /title="上一帧（A）" aria-label="上一帧（A）"/);
    assert.match(MANAGER, /title="下一帧（D）" aria-label="下一帧（D）"/);
    assert.match(MANAGER, /<kbd>A<\/kbd><kbd>D<\/kbd> 上一帧 \/ 下一帧/);
    assert.match(MANAGER, /<kbd>F<\/kbd> 切换全屏/);
    assert.match(MANAGER, /data-detail-video-action="rate-reset" title="恢复原始速度（Z）"/);
    assert.match(MANAGER, /title="减速 0\.1 倍（X）"/);
    assert.match(MANAGER, /title="加速 0\.1 倍（C）"/);
});

test('编辑双击退出回到进入前视图，快捷键指南同步', () => {
    const closeEditor = functionSource(MANAGER, 'closeDetailImageEditor');
    const restoreView = functionSource(MANAGER, 'restoreDetailImageEditorCoverflow');
    const loadText = functionSource(MANAGER, 'loadDetailViewerText');
    assert.match(closeEditor, /restoreDetailImageEditorCoverflow\(state\)/);
    assert.match(restoreView, /gw:asset-coverflow-open/);
    assert.match(loadText, /detailViewerFileParams\(detailViewerState\.source, item\)/);
    assert.match(MANAGER, /if\(detailBackquoteTappedTwice\(event,context\)\)\{[\s\S]*?enterCurrentDetailEditableContent\(\)/);
    assert.match(MANAGER, /exitDetailTextEditFromBackquote\(editor,detailTextEditorBackquoteInput\)/);
    assert.match(MANAGER, /const registryFullscreenShortcut = event\.key === 'Enter'[\s\S]*?!event\.ctrlKey && !event\.metaKey && !event\.altKey/);
    assert.match(GUIDE, /`Enter` \/ `F`/);
    assert.match(GUIDE, /500ms 内双击/);
    assert.match(GUIDE, /`A` \/ `D`/);
});
