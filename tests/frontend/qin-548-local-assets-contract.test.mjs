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
    assert.match(source, /name:\s*currentFolderName/);
    assert.match(source, /new_name:\s*name/);
    assert.match(source, /updateLocalAssetFolder\(/);
});
