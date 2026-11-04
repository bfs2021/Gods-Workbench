/**
 * QIN-534（GitHub #583）：统一资产库多选交互 —— 右键菜单与底部批量栏按素材状态切换「合并 / 解除」。
 *
 * 断言面覆盖第四轮反馈：
 * - 菜单三项在「已存在版本结构 / 组结构 / 已关联」时切换为对应解除入口；
 * - 结构解除只把选中素材移出结构，其余成员保留；剩余不足两个时整体删除；
 * - 关联解除按单向关系逐条删除，本就没有关联的对（404）不影响整体操作；
 * - 底部批量栏与右键菜单同口径切换，且用独立属性避免重复触发。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8'
);

function functionSource(name){
    // async 定义必须整体取走：只匹配 "function name(" 会丢掉 async 关键字，
    // 抽出的函数体再用 vm 执行时会因 await 语法报错。
    let start = -1;
    for(const candidate of [`async function ${name}(`, `function ${name}(`]){
        const index = MANAGER.indexOf(candidate);
        if(index >= 0){ start = index; break; }
    }
    assert.ok(start >= 0, `缺少函数：${name}`);
    const parenStart = MANAGER.indexOf('(', start);
    let parenDepth = 0;
    let bodyStart = -1;
    for(let index = parenStart; index < MANAGER.length; index += 1){
        const ch = MANAGER[index];
        if(ch === '(') parenDepth += 1;
        else if(ch === ')'){
            parenDepth -= 1;
            if(parenDepth === 0){ bodyStart = MANAGER.indexOf('{', index); break; }
        }
    }
    assert.ok(bodyStart > 0, `未定位函数体：${name}`);
    let depth = 0;
    for(let index = bodyStart; index < MANAGER.length; index += 1){
        if(MANAGER[index] === '{') depth += 1;
        else if(MANAGER[index] === '}'){
            depth -= 1;
            if(depth === 0) return MANAGER.slice(start, index + 1);
        }
    }
    assert.fail(`函数未闭合：${name}`);
}

const SLICES = [
    'isRegistryStructureImage',
    'isRegistryStructureActiveImage',
    'registryRelatedAssetIds',
    'registrySelectedAssetMap',
    'registrySelectionRelationState',
    'registryOwningStructures',
    'renderRegistryStructureContextMenu',
    'releaseRegistryStructureMembers',
    'releaseRegistryRelations',
].map(functionSource).join('\n');

function buildContext(overrides = {}){
    const context = {
        registryAssets: [],
        registryAssetStructures: new Map(),
        registryStructureMutationId: '',
        registryVisualType: (item = {}) => String(item?.visual_type || item?.kind || 'image').trim().toLowerCase(),
        detailViewerState: {open: false, source: 'registry'},
        renderDetailViewer(){},
        setStatus(){},
        render(){},
        closeRegistryStructureMenu(){},
        reloadRegistryAssetStructure: async () => null,
        normalizeRegistryAssetStructure: (value = {}) => ({
            ...value,
            id: String(value.id || ''),
            members: Array.isArray(value.members) ? value.members : [],
        }),
        apiJsonResponse: async (response) => {
            const res = await response;
            const data = await res.json().catch(() => ({}));
            if(!res.ok){
                const detail = data?.detail;
                const error = new Error(detail?.message || '操作失败');
                error.status = res.status;
                error.code = detail?.code || null;
                throw error;
            }
            return data;
        },
        assetManagerApi: {
            deleteAssetStructure: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
            removeRegistryAssetRelation: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
        },
        assetManagerHttp: {
            request: async () => ({ok: true, status: 200, json: async () => ({})}),
        },
        ...overrides,
    };
    runInNewContext(SLICES, context);
    return context;
}

test('AC2-反馈 4: 未建立结构与关联时，菜单三项都是合并入口', () => {
    const context = buildContext();
    const menu = context.renderRegistryStructureContextMenu(
        ['a1', 'a2'],
        {imageOnly: true, owning: {version: null, group: null}, related: false}
    );
    assert.match(menu, /data-registry-structure-create="version"/);
    assert.match(menu, /合并为版本结构/);
    assert.match(menu, /data-registry-structure-create="relation"/);
    assert.match(menu, /合并为关联结构/);
    assert.match(menu, /data-registry-structure-create="group"/);
    assert.match(menu, /合并为组结构/);
    assert.doesNotMatch(menu, /data-registry-structure-release/);
});

test('AC2-反馈 4: 已存在版本结构时，版本项切换为解除入口', () => {
    const context = buildContext();
    const menu = context.renderRegistryStructureContextMenu(
        ['a1', 'a2'],
        {imageOnly: true, owning: {version: {id: 'strc_0001'}, group: null}, related: false}
    );
    assert.match(menu, /data-registry-structure-release="version"/);
    assert.match(menu, /解除版本结构/);
    assert.doesNotMatch(menu, /data-registry-structure-create="version"/);
    // 关联与组结构不受版本状态影响
    assert.match(menu, /data-registry-structure-create="relation"/);
    assert.match(menu, /data-registry-structure-create="group"/);
});

test('AC2-反馈 4: 已存在组结构时，组项切换为解除入口', () => {
    const context = buildContext();
    const menu = context.renderRegistryStructureContextMenu(
        ['a1', 'a2'],
        {imageOnly: true, owning: {version: null, group: {id: 'strc_0002'}}, related: false}
    );
    assert.match(menu, /data-registry-structure-release="group"/);
    assert.match(menu, /解除组结构/);
    assert.doesNotMatch(menu, /data-registry-structure-create="group"/);
});

test('AC2-反馈 4: 已关联时，关联项切换为解除入口', () => {
    const context = buildContext();
    const menu = context.renderRegistryStructureContextMenu(
        ['a1', 'a2'],
        {imageOnly: true, owning: {version: null, group: null}, related: true}
    );
    assert.match(menu, /data-registry-structure-release="relation"/);
    assert.match(menu, /解除关联结构/);
    assert.doesNotMatch(menu, /data-registry-structure-create="relation"/);
});

test('AC2-反馈 4: 非图片多选时版本与组入口禁用，解除态不受图片限制', () => {
    const context = buildContext();
    const disabled = context.renderRegistryStructureContextMenu(
        ['v1', 'v2'],
        {imageOnly: false, owning: {version: null, group: null}, related: false}
    );
    assert.match(disabled, /data-registry-structure-create="version"[^>]*disabled/);
    assert.match(disabled, /仅支持图片素材/);

    const releasing = context.renderRegistryStructureContextMenu(
        ['v1', 'v2'],
        {imageOnly: false, owning: {version: {id: 'strc_0001'}, group: {id: 'strc_0002'}}, related: true}
    );
    assert.doesNotMatch(releasing, /data-registry-structure-release="version"[^>]*disabled/);
    assert.doesNotMatch(releasing, /data-registry-structure-release="group"[^>]*disabled/);
});

test('AC2-反馈 4: 只有成员覆盖全部选中项的结构才算可解除', () => {
    const context = buildContext({
        registryAssetStructures: new Map([
            ['strc_0001', {
                id: 'strc_0001',
                structure_type: 'version',
                members: [{asset_id: 'a1'}, {asset_id: 'a2'}],
            }],
            ['strc_0002', {
                id: 'strc_0002',
                structure_type: 'group',
                members: [{asset_id: 'a1'}, {asset_id: 'a3'}],
            }],
        ]),
    });
    assert.equal(context.registryOwningStructures(['a1', 'a2']).version.id, 'strc_0001');
    assert.equal(context.registryOwningStructures(['a1', 'a3']).group.id, 'strc_0002');
    // 选中项分属不同结构时没有可整体解除的结构
    assert.equal(context.registryOwningStructures(['a2', 'a3']).version, null);
    assert.equal(context.registryOwningStructures(['a2', 'a3']).group, null);
    // 部分选中结构成员（3 项中选 2 项）时亦能正确识别归属结构以支持移出
    context.registryAssetStructures.set('strc_0003', {
        id: 'strc_0003',
        structure_type: 'version',
        members: [{asset_id: 'm1'}, {asset_id: 'm2'}, {asset_id: 'm3'}],
    });
    assert.equal(context.registryOwningStructures(['m1', 'm2']).version.id, 'strc_0003');
});

test('AC2-反馈 6: 普通本地未归档素材缺失 status 字段时仍正确识别为 active 图片', () => {
    const context = buildContext();
    // 显式 active
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image', status: 'active'}), true);
    // 本地索引素材 status 缺失，但未归档、未隐藏
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image'}), true);
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image', archived: false, hidden: false}), true);
    // 已归档或已隐藏的素材不能作为结构有效活跃图片
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image', archived: true}), false);
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image', hidden: true}), false);
    assert.equal(context.isRegistryStructureActiveImage({kind: 'image', status: 'archived'}), false);
    // 非图片类型
    assert.equal(context.isRegistryStructureActiveImage({kind: 'video', status: 'active'}), false);
});

test('AC2-反馈 4: 关联状态由素材的 related_assets 判定', () => {
    const context = buildContext({
        registryAssets: [
            {id: 'a1', related_assets: [{id: 'a2'}]},
            {id: 'a2', related_assets: []},
            {id: 'a3', related_assets: []},
        ],
    });
    assert.equal(context.registrySelectionRelationState(['a1', 'a2']), true);
    assert.equal(context.registrySelectionRelationState(['a2', 'a3']), false);
    assert.equal(context.registrySelectionRelationState(['a1']), false);
});

test('AC2-反馈 4: 解除结构只移出选中素材，其余成员保留', async () => {
    const requests = [];
    const context = buildContext({
        registryAssetStructures: new Map([
            ['strc_0001', {
                id: 'strc_0001',
                structure_type: 'version',
                version: 3,
                current_asset_id: 'a1',
                members: [{asset_id: 'a1'}, {asset_id: 'a2'}, {asset_id: 'a3'}],
            }],
        ]),
        assetManagerHttp: {
            request: async (url, init) => {
                requests.push({url, init});
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({
                        structure: {
                            id: 'strc_0001',
                            kind: 'version',
                            version: 4,
                            current_asset_id: 'a2',
                            members: [{asset_id: 'a2'}, {asset_id: 'a3'}],
                        },
                    }),
                };
            },
        },
    });

    await context.releaseRegistryStructureMembers(
        context.registryAssetStructures.get('strc_0001'),
        ['a1']
    );

    assert.equal(requests.length, 1);
    const [request] = requests;
    assert.equal(request.url, '/api/asset-registry/asset-structures/strc_0001');
    assert.equal(request.init.method, 'PATCH');
    const payload = JSON.parse(request.init.body);
    assert.deepEqual(payload.asset_ids, ['a2', 'a3']);
    assert.equal(payload.expected_version, 3);
    // 当前代表素材被移出后必须改指剩余成员，否则服务端按非法成员拒绝
    assert.equal(payload.current_asset_id, 'a2');
});

test('AC2-反馈 4: 剩余成员不足两个时整体删除结构', async () => {
    const deletes = [];
    const context = buildContext({
        registryAssetStructures: new Map([
            ['strc_0001', {
                id: 'strc_0001',
                structure_type: 'group',
                version: 5,
                current_asset_id: 'a1',
                members: [{asset_id: 'a1'}, {asset_id: 'a2'}],
            }],
        ]),
        assetManagerApi: {
            deleteAssetStructure: async (id, expectedVersion) => {
                deletes.push({id, expectedVersion});
                return {ok: true, status: 200, json: async () => ({deleted_structure_id: id, revision: 7})};
            },
            removeRegistryAssetRelation: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
        },
    });

    await context.releaseRegistryStructureMembers(
        context.registryAssetStructures.get('strc_0001'),
        ['a1']
    );

    assert.equal(deletes.length, 1);
    assert.deepEqual(deletes[0], {id: 'strc_0001', expectedVersion: 5});
    assert.equal(context.registryAssetStructures.has('strc_0001'), false);
});

test('AC2-反馈 4: 解除关联逐条删除，没有关联的对返回 404 时跳过', async () => {
    const attempted = [];
    const context = buildContext({
        registryAssets: [
            {id: 'a1', related_assets: [{id: 'a2'}]},
            {id: 'a2', related_assets: []},
        ],
        assetManagerApi: {
            deleteAssetStructure: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
            removeRegistryAssetRelation: async (from, to) => {
                attempted.push(`${from}->${to}`);
                if(from === 'a1' && to === 'a2') return {ok: true, status: 200, json: async () => ({revision: 3})};
                const error = new Error('关系不存在');
                error.status = 404;
                error.code = 'RELATION_NOT_FOUND';
                throw error;
            },
        },
    });

    await context.releaseRegistryRelations(['a1', 'a2']);

    assert.deepEqual(attempted.sort(), ['a1->a2', 'a2->a1']);
    // 已解除的对端从本地状态移除
    assert.deepEqual(context.registryAssets[0].related_assets, []);
});

test('AC2-反馈 4: 解除关联遇到非 404 错误必须上抛', async () => {
    const context = buildContext({
        assetManagerApi: {
            deleteAssetStructure: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
            removeRegistryAssetRelation: async () => {
                const error = new Error('权限不足');
                error.status = 403;
                error.code = 'FORBIDDEN';
                throw error;
            },
        },
    });
    await assert.rejects(
        () => context.releaseRegistryRelations(['a1', 'a2']),
        /权限不足/
    );
});

test('AC2-反馈 4: 只有 RELATION_NOT_FOUND 可跳过，其它 404 必须上抛', async () => {
    const context = buildContext({
        assetManagerApi: {
            deleteAssetStructure: async () => ({ok: true, status: 200, json: async () => ({revision: 2})}),
            removeRegistryAssetRelation: async () => {
                // 素材本身已不存在：同样是 404，但不能被当成「本来就没关联」吞掉。
                const error = new Error('素材不存在');
                error.status = 404;
                error.code = 'ASSET_NOT_FOUND';
                throw error;
            },
        },
    });
    await assert.rejects(
        () => context.releaseRegistryRelations(['a1', 'a2']),
        /素材不存在/
    );
});

test('AC2-反馈 4: 底部批量栏与右键菜单同口径切换且用独立属性', () => {
    const bar = functionSource('renderRegistrySelectionBar');
    assert.match(bar, /registryOwningStructures\(selectedIds\)/);
    assert.match(bar, /registrySelectionRelationState\(selectedIds\)/);
    // 解除态走 data-registry-bulk-release，与菜单的 data-registry-structure-release 互不命中
    assert.match(bar, /data-registry-\$\{owning\.version \? 'bulk-release' : 'structure-merge'\}/);
    assert.match(bar, /data-registry-\$\{related \? 'bulk-release' : 'relate'\}/);
    assert.match(bar, /解除版本/);
    assert.match(bar, /解除组/);
    assert.match(bar, /解除关联/);
});

test('AC2-反馈 4: 解除入口在点击分发中独占处理，不与合并入口混用', () => {
    assert.match(MANAGER, /data-registry-structure-release\]/);
    assert.match(MANAGER, /data-registry-bulk-release\]/);
    // 底部栏解除走根级处理，菜单解除走 document 级处理，避免同一次点击重复触发
    assert.match(MANAGER, /runRegistryStructureAction\(`release-\$\{type\}`, ids\)/);
    assert.match(MANAGER, /runRegistryStructureAction\(`release-\$\{releaseBtn\.dataset\.registryBulkRelease/);
});

test('AC2-反馈 4: 建立关联后把服务端返回的关系对端写回本地状态', () => {
    const source = functionSource('relateSelectedRegistryAssets');
    assert.match(source, /data\.relations/);
    assert.match(source, /from_asset_id/);
    assert.match(source, /related_assets/);
});
