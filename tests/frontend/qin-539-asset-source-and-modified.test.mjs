/**
 * QIN-539（GitHub #592）：资产卡来源名与修改时间未下发。
 *
 * 断言面覆盖票面 AC2～AC5 的前端部分：
 * - AC2 卡片来源行优先使用后端下发的真实来源库名，多来源仍渲染「N 库」；
 * - AC3 资产详情「更新时间」改名「修改时间」，取文件修改时间，无数据回落「—」；
 * - AC4 卡片不显示修改时间（不引用 modified_at / registryModifiedTime）；
 * - AC5 测试登记到 node-files.txt 并完成卫生准入分片。
 */
import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);
const NODE_FILES = readFileSync(
    new URL('./node-files.txt', import.meta.url),
    'utf8'
);
const SHARD = new URL('../hygiene/fixtures/admissions/issue-592.json', import.meta.url);

function functionSource(name){
    let start = MANAGER.indexOf(`function ${name}(`);
    if(start < 0) start = MANAGER.indexOf(`async function ${name}(`);
    assert.ok(start >= 0, `缺少函数：${name}`);
    const parenOpen = MANAGER.indexOf('(', start);
    let parenDepth = 0;
    let cursor = parenOpen;
    for(; cursor < MANAGER.length; cursor += 1){
        if(MANAGER[cursor] === '(') parenDepth += 1;
        else if(MANAGER[cursor] === ')'){
            parenDepth -= 1;
            if(parenDepth === 0) break;
        }
    }
    const open = MANAGER.indexOf('{', cursor);
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

const sourceNames = new Function(`${functionSource('registrySourceNames')}\nreturn registrySourceNames;`)();
const modifiedTime = new Function(`${functionSource('registryModifiedTime')}\nreturn registryModifiedTime;`)();
const modifiedLabelBuild = (formatter) => new Function(
    'formatDate',
    `${functionSource('registryModifiedTime')}
${functionSource('registryModifiedLabel')}
return registryModifiedLabel;`
)(formatter);
const modifiedLabel = modifiedLabelBuild(value => String(value));
const metaRows = new Function(
    'assetFileExtension',
    'detailViewerTypeLabel',
    'detailViewerUrl',
    'detailViewerSourceLabel',
    'formatFileSize',
    'formatDate',
    'detailViewerState',
    `${functionSource('registryModifiedTime')}
${functionSource('registryModifiedLabel')}
${functionSource('detailViewerMetaRows')}
return detailViewerMetaRows;`
)(
    item => String(item?.name || '').split('.').pop() || '',
    type => String(type || 'file'),
    () => '',
    () => '统一资产',
    size => `${Number(size || 0)} B`,
    value => String(value),
    {modelInfo: null, items: [], index: 0}
);

test('AC2 卡片来源行使用后端下发的真实来源库名', () => {
    assert.deepEqual(sourceNames({source_names: ['托管资产'], storage_root_name: '托管资产'}), ['托管资产']);
    assert.deepEqual(sourceNames({storage_root_name: '项目资产'}), ['项目资产']);
    assert.deepEqual(sourceNames({source_names: ['output', 'output']}), ['output']);
    // 两者都缺失时才回落到旧的合并叫法，不再覆盖已下发的真实库名。
    assert.deepEqual(sourceNames({}), ['统一资产']);
});

test('AC2 多来源保留「N 库」标记', () => {
    const names = sourceNames({source_names: ['托管资产', '项目资产']});
    assert.equal(names.length, 2);
    const card = functionSource('renderRegistryCard');
    assert.match(
        card,
        /registrySourceNames\(item\)\.length > 1 \? `<em>\$\{registrySourceNames\(item\)\.length\} 库<\/em>`/
    );
    assert.match(card, /<span>\$\{escapeHtml\(registrySourceNames\(item\)\.join\(' · '\)\)\}/);
});

test('AC3 详情行改名为「修改时间」并取文件修改时间', () => {
    const rows = metaRows('registry', {name: '静帧.png', size: 12, modified_at: '2023-11-14T22:13:20Z'}, 'image');
    const labels = rows.map(row => row[0]);
    assert.ok(labels.includes('修改时间'), `详情行缺少「修改时间」：${labels.join(',')}`);
    assert.ok(!labels.includes('更新时间'), '「更新时间」应已改名为「修改时间」');
    assert.equal(rows.find(row => row[0] === '修改时间')[1], String(Date.parse('2023-11-14T22:13:20Z')));
});

test('AC3 无修改时间时回落「—」而不是「未知」', () => {
    const rows = metaRows('registry', {name: '仅登记素材.txt', size: 0}, 'document');
    assert.equal(rows.find(row => row[0] === '修改时间')[1], '—');
    const source = functionSource('detailViewerMetaRows');
    assert.doesNotMatch(source, /'未知'/);
});

test('AC4 卡片不显示修改时间', () => {
    const card = functionSource('renderRegistryCard');
    assert.doesNotMatch(card, /modified_at/);
    assert.doesNotMatch(card, /registryModifiedTime/);
    assert.doesNotMatch(card, /修改时间/);
});

test('列表视图的修改时间列复用同一口径', () => {
    assert.match(MANAGER, /escapeHtml\(registryModifiedLabel\(item\)\)/);
    assert.match(MANAGER, /listCells\[4\]\.textContent = registryModifiedLabel\(item\)/);
    assert.equal(modifiedTime({modified_at: '2023-11-14T22:13:20Z'}), 1700000000000);
    assert.equal(modifiedTime({lastModified: 1700000000000}), 1700000000000);
    assert.equal(modifiedTime({}), 0);
    assert.equal(modifiedLabel({modified_at: '2023-11-14T22:13:20Z'}), '1700000000000');
    assert.equal(modifiedLabel({}), '—');
});

test('AC5 前端测试已登记到 node-files.txt 与卫生准入分片', () => {
    assert.match(NODE_FILES, /tests\/frontend\/qin-539-asset-source-and-modified\.test\.mjs/);
    assert.ok(existsSync(SHARD), '缺少 tests/hygiene/fixtures/admissions/issue-592.json');
    const shard = JSON.parse(readFileSync(SHARD, 'utf8'));
    assert.equal(shard.issue, 592);
    assert.ok(
        shard.files.includes('tests/frontend/qin-539-asset-source-and-modified.test.mjs'),
        '分片 files 未登记本票前端测试'
    );
});
