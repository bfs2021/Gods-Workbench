import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const manager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');

test('QIN-552：LUT 库只匹配 .cube，并在资产分类与统计中单列', () => {
    assert.match(manager, /\{id:'lut', name:'LUT库',[^\n]*kinds:\['lut'\], extensions:\['cube'\]\}/);
    assert.match(manager, /lut:new Set\(\['cube'\]\)/);
    assert.match(manager, /id:'lut', name:'LUT',[^\n]*registryCategoryCount\('lut'\)/);
    assert.match(manager, /lut:'LUT'/);
    assert.match(manager, /cube:'Cube LUT 色彩查找表（\.cube）'/);
    assert.match(manager, /const isLut = String\(item\.kind \|\| item\.media_type \|\| item\.type \|\| ''\)\.toLowerCase\(\) === 'lut'\s*\|\| registryAssetExtension\(item\)\.toLowerCase\(\) === 'cube';/);
    assert.match(manager, /const format = isLut \? 'LUT'/);
});

test('QIN-552：索引失败提示可读，类型图标使用调色板', () => {
    assert.match(manager, /LUT_CUBE_INVALID:'LUT 文件格式无效，请检查文件头和采样数据'/);
    assert.match(manager, /LUT_CUBE_TOO_LARGE:'LUT 文件超过 64 MiB 上限'/);
    assert.match(manager, /lut:'palette'/);
});
