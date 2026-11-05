/** QIN-548：上传、目录树与文件夹改名的前后端调用契约。 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);

function functionSource(name){
    let start = MANAGER.indexOf(`function ${name}(`);
    if(start < 0) start = MANAGER.indexOf(`async function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const open = MANAGER.indexOf('{', MANAGER.indexOf(')', start));
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

test('AC1 multipart 上传携带来源路径、文件夹、类型与文件', () => {
    const source = functionSource('uploadLocalAssets');
    assert.match(source, /form\.append\('path',\s*activeLocalLibraryPath\(\)\)/);
    assert.equal(MANAGER.match(/form\.append\('path',\s*activeLocalLibraryPath\(\)\)/g)?.length, 2);
    assert.match(source, /form\.append\('folder',\s*activeLocalUploadFolder\s*\|\|\s*''\)/);
    assert.match(source, /form\.append\('kind',\s*activeLocalLibraryKind\(\)\)/);
    assert.match(source, /form\.append\('files',\s*file\)/);
    assert.match(source, /assetManagerApi\.uploadLocalAssets\(form\)/);
});

test('AC2 页面从列表响应读取并渲染目录树', () => {
    const loader = functionSource('loadLocalAssets');
    const renderer = functionSource('renderLocalUploadFolderBranch');
    assert.match(loader, /localUploadTree\s*=\s*data\.tree/);
    assert.match(renderer, /folder\.children/);
    assert.match(renderer, /folder\.path/);
});

test('AC3 文件夹改名发送后端接收的旧名与新名字段', () => {
    const source = functionSource('renameLocalUploadFolder');
    assert.match(source, /root_path:\s*activeLocalLibraryPath\(\)/);
    assert.match(source, /name:\s*currentFolderPath/);
    assert.match(source, /currentFolderPath\s*=\s*String\(activeLocalUploadFolder\)/);
    assert.match(source, /new_name:\s*name/);
    assert.match(source, /updateLocalAssetFolder\(/);
});

function escapeMarkup(value=''){
    return String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

function renderLocalManagerMarkup({tree, activeFolder='', classFilter=''}={}){
    const renderFolderPanel = new Function(
        'localUploadTree',
        'activeLocalUploadFolder',
        'activeLocalUploadClassFilter',
        'escapeAttr',
        'escapeHtml',
        `${functionSource('renderLocalUploadFolderBranch')}\n${functionSource('localUploadFolderContainsActive')}\n${functionSource('renderLocalUploadFolderPanel')}\nreturn renderLocalUploadFolderPanel;`
    )(tree, activeFolder, classFilter, escapeMarkup, escapeMarkup);
    const context = {
        root: {innerHTML: ''},
        normalizeLocalCaptionSettings: () => {},
        localUploadItems: () => [],
        selectedLocalUploadId: '',
        findLocalUpload: () => null,
        selectedLocalUploadImageItems: () => [],
        localAssetsTotal: 0,
        localAssets: [],
        localUploadFolderTitle: () => '根目录',
        localUploadViewSubtitle: () => '',
        localUploadQuery: '',
        localUploadManageMode: false,
        selectedLocalUploadIds: new Set(),
        renderLocalUploadFolderPanel: renderFolderPanel,
        renderLocalCaptionTools: () => '',
        renderLocalUploadClipboardBar: () => '',
        renderLocalUploadAddCard: () => '',
        renderLocalUploadCard: () => '',
        renderLocalAssetsPagination: () => '',
        renderLocalUploadDetail: () => '',
        escapeHtml: escapeMarkup,
        escapeAttr: escapeMarkup,
    };
    new Function(...Object.keys(context), `${functionSource('renderLocalManager')}\nreturn renderLocalManager;`)(...Object.values(context))();
    return context.root.innerHTML;
}

test('AC2 上传资源页面实际渲染来源目录树及新建、重命名入口', () => {
    const tree = {
        id: '__root__', path: '', name: '根目录', count: 1, children: [{
            path: 'incoming', name: 'incoming', count: 1, children: [{
                path: 'incoming/nested', name: 'nested', count: 1, children: [],
            }],
        }],
    };
    const markup = renderLocalManagerMarkup({tree, activeFolder: 'incoming/nested'});
    const renameButton = markup.match(/<button\b(?=[^>]*data-localup-folder-rename)[^>]*>/)?.[0];

    assert.match(markup, /aria-label="上传文件夹树"/);
    assert.match(markup, /data-localup-folder="incoming"/);
    assert.match(markup, /data-localup-folder="incoming\/nested"/);
    assert.match(markup, /class="asset-tree-node is-selected is-path"/);
    assert.match(markup, /data-localup-folder-new/);
    assert.ok(renameButton, '应渲染文件夹重命名按钮');
    assert.doesNotMatch(renameButton, /\bdisabled\b/);
    assert.doesNotMatch(markup, /<\/aside>}\s*<section/);
    assert.doesNotMatch(markup, /系统资源/);
});

test('AC2 根目录和智能分类状态下禁用文件夹重命名', () => {
    const tree = {id: '__root__', path: '', name: '根目录', count: 0, children: []};
    for(const state of [{}, {classFilter: 'dimension:tag'}]){
        const markup = renderLocalManagerMarkup({tree, ...state});
        const renameButton = markup.match(/<button\b(?=[^>]*data-localup-folder-rename)[^>]*>/)?.[0];
        assert.ok(renameButton, '应渲染文件夹重命名按钮');
        assert.match(renameButton, /\bdisabled\b/);
    }
});
