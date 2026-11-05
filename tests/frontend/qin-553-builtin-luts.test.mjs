import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const manager = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('../../web/js/modules/asset-manager/api.js', import.meta.url), 'utf8');

test('QIN-553：内置 LUT 登记与显示切换走服务端幂等接口', () => {
    assert.match(api, /ensureBuiltinLuts\(reveal = false, init = \{\}\)/);
    assert.match(api, /\/api\/asset-registry\/builtin-luts\/ensure\$\{suffix\}/);
    assert.match(api, /method: 'POST'/);
    assert.match(manager, /async function ensureBuiltinLutsRegistered\(options=\{\}\)/);
    assert.match(manager, /registryBuiltinHiddenIds = new Set\(Array\.isArray\(data\?\.hidden\) \? data\.hidden : \[\]\)/);
});

test('QIN-553：首次进入 LUT 库（统计胶囊与 lut 预设看板）触发内置登记', () => {
    // 统计胶囊：LUT 分类点击后先 ensure 再刷新列表。
    assert.match(manager, /if\(category === 'lut'\)\{[\s\S]{0,160}await ensureBuiltinLutsRegistered\(\)/);
    // LUT 预设看板（含 kinds 包含 lut 的自定义看板）同样触发。
    assert.match(manager, /if\(board\.preset === 'lut' \|\| \(board\.kinds \|\| \[\]\)\.includes\('lut'\)\)\{[\s\S]{0,160}await ensureBuiltinLutsRegistered\(\)/);
});

test('QIN-553：内置卡片提供隐藏开关而非删除，普通卡片保持回收', () => {
    assert.match(manager, /item\.metadata\?\.builtin === true \|\| item\.builtin === true/);
    assert.match(manager, /data-registry-builtin-hidden="\$\{escapeAttr\(item\.id \|\| ''\)\}"/);
    // 隐藏态图标切换。
    assert.match(manager, /<i data-lucide="\$\{hidden \? 'eye' : 'eye-off'\}"><\/i>/);
    // 内置分支必须先于回收按钮渲染。
    const builtinIndex = manager.indexOf('registry-card-builtin-hide');
    const recycleIndex = manager.indexOf('const disabled = !registryCanRecycle(item);');
    assert.ok(builtinIndex > 0 && builtinIndex < recycleIndex,
        '内置卡片分支应先于普通回收按钮逻辑渲染');
});

test('QIN-553：隐藏切换与恢复显示入口接线完整', () => {
    assert.match(manager, /async function toggleRegistryBuiltinHidden\(assetId, hidden\)/);
    assert.match(manager, /assetManagerApi\.updateRegistryAsset\(assetId, \{[\s\S]{0,120}hidden,[\s\S]{0,120}expected_version: requireExpectedVersion\(registryStatus\.revision, '资产注册表'\)/);
    // LUT 视图存在隐藏条目时渲染恢复入口。
    assert.match(manager, /data-registry-reveal-builtin>恢复显示内置 LUT/);
    assert.match(manager, /await ensureBuiltinLutsRegistered\(\{reveal:true\}\)/);
    // 点击委托先于普通回收处理拦截内置按钮。
    const hiddenIndex = manager.indexOf("const registryBuiltinHidden = target.closest?.('[data-registry-builtin-hidden]')");
    const recycleIndex = manager.indexOf("const registryRecycle = target.closest?.('[data-registry-recycle]')");
    assert.ok(hiddenIndex > 0 && recycleIndex > 0 && hiddenIndex < recycleIndex,
        '内置隐藏按钮委托应先于普通回收处理');
});
