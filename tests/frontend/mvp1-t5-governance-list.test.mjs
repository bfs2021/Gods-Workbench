// Copyright 2026 Gods-Workbench Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// MVP1-T5 / QIN-549 治理建议抽屉静态契约：asset-manager.js 顶层即触 DOM，
// 无法整体 import，因此按既有 mvp1-t0 模式读取源码做结构断言。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
    'utf8',
);

test('AC1: 治理建议入口位于顶栏并打开可访问抽屉，左栏不再保留入口', () => {
    assert.doesNotMatch(source, /\{id:'governance', name:'治理建议'/);
    assert.doesNotMatch(source, /activeTab === 'governance'/);
    assert.match(source, /data-governance-open/);
    assert.match(source, /data-governance-total/);
    assert.match(source, /function openGovernanceDrawer\(trigger\)/);
    assert.match(source, /role="dialog" aria-modal="true" aria-labelledby="governanceDrawerTitle"/);
    assert.match(source, /function renderRegistryToolbar\([\s\S]*?data-governance-open/);
    assert.match(source, /loadGovernanceSuggestions\(\);\s*loadAll\(\)\.then/);
});

test('suggestion view only calls the two implemented governance endpoints', () => {
    const apiCalls = [...source.matchAll(/['"]\/api\/asset-registry\/governance\/suggestions[^'"]*['"]/g)]
        .map(match => match[0].replace(/['"]/g, ''));
    assert.ok(apiCalls.includes('/api/asset-registry/governance/suggestions'));
    assert.ok(apiCalls.includes('/api/asset-registry/governance/suggestions/confirm'));
    for (const call of apiCalls) {
        assert.match(call, /^\/api\/asset-registry\/governance\/suggestions(\/confirm)?$/);
    }
});

test('confirm flow sends CAS expected_version and surfaces conflicts', () => {
    assert.match(
        source,
        /JSON\.stringify\(\{suggestion_id:id, expected_version:revision\}\)/,
    );
    // apiJsonResponse 统一处理 401 跳转与 409 VERSION_CONFLICT 提示。
    assert.match(source, /async function apiJsonResponse\(response\)/);
    assert.match(source, /VERSION_CONFLICT_MESSAGE/);
});

test('AC2/AC3: 六类建议有分组说明、处理方式、计数与素材操作', () => {
    for (const kind of ['naming', 'similar_names', 'duplicate_files', 'duplicate_shots', 'vector_duplicates', 'tagging']) {
        assert.match(source, new RegExp(`\\{id:'${kind}', label:'[^']+', icon:'[^']+', description:'[^']+', action:'[^']+'\\}`));
    }
    assert.match(source, /governance-suggestion-group-count/);
    assert.match(source, /governance-suggestion-group-assets/);
    assert.match(source, /source\.map\(entry => \(\{\.\.\.entry,kind:group\.id\}\)\)/);
    assert.match(source, /data-governance-suggestion=/);
    assert.match(source, /data-governance-confirm=/);
    assert.match(source, /data-governance-open-asset=/);
    assert.match(source, /registryVisualType\(asset\)/);
    assert.match(source, /data-governance-asset-type=/);
});

test('similar-name entries are informational and never auto-confirmable', () => {
    assert.match(source, /GOVERNANCE_INFORMATIONAL_KINDS = new Set\(\['similar_names'\]\)/);
    assert.match(source, /仅提示，不提供写入动作/);
});

test('AC2/AC5: 无问题分组折叠并显示状态，空清单与有清单可区分', () => {
    assert.match(source, /\(empty \? '' : ' open'\)/);
    assert.match(source, /<em class="governance-suggestion-no-issue">无问题<\/em>/);
    assert.match(source, /role="list" aria-label="治理建议清单"/);
    assert.match(source, /data-governance-empty/);
    assert.match(source, /暂无待处理建议/);
    assert.match(source, /if\(!governanceSuggestionsData\)/);
});

test('AC4/AC5: 403 权限错误与其他读取错误均显示错误态并支持重试', () => {
    assert.match(source, /governanceSuggestionsError\.status === 403/);
    assert.match(source, /需要治理权限/);
    assert.match(source, /当前账户需要 admin 或 governor 角色/);
    assert.match(source, /治理建议读取失败/);
    assert.match(source, /data-governance-error/);
    assert.match(source, /data-governance-retry/);
    assert.match(source, /await refreshGovernanceSuggestions\(\)/);
});

test('degraded index state is surfaced instead of silently hiding duplicates', () => {
    assert.match(source, /data-governance-degraded/);
    assert.match(source, /查重候选可能不完整/);
});

test('governance drawer exposes no asset deletion endpoint', () => {
    // 关闭抽屉会移除界面节点；此处约束只针对资产删除 API。
    const viewStart = source.indexOf('MVP1-T5 / QIN-549 治理建议：顶栏入口与分组抽屉');
    assert.ok(viewStart > 0, '缺少治理视图代码段');
    const viewEnd = source.indexOf('function render(){', viewStart);
    const view = source.slice(viewStart, viewEnd);
    assert.doesNotMatch(view, /['"]\/api\/asset-registry\/[^'"]*(?:delete|remove|unlink)[^'"]*['"]/i);
    assert.match(view, /系统永不自动删除，也不改源文件/);
});
