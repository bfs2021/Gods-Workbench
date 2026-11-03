// QIN-485: 资产库新增资源库弹窗错误回显与交互状态契约测试
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../../web/js/modules/asset-manager.js', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../../web/css/pages/asset-manager.css', import.meta.url), 'utf8');
const controller = readFileSync(new URL('../../web/js/controllers/assets-controller.js', import.meta.url), 'utf8');
const shell = readFileSync(new URL('../../web/pages/assets.html', import.meta.url), 'utf8');

test('新建资源入口嵌入总览卡片并沿用独立标准硬件按钮', () => {
    assert.match(shell, /<div class="asset-shell-overview-card"[\s\S]*?id="assetSidebarOverview"[\s\S]*?id="assetSidebarCreate" class="hw-btn hw-btn-icon" onclick="WorkbenchAssets\.openCreateAssetLibrary\(\)"[\s\S]*?<nav class="gw-side-bus-list[^\"]*" id="assetCategories"/);
    assert.match(controller, /async function openCreateAssetLibrary\(\)[\s\S]*?button\.click\(\)/);
    assert.doesNotMatch(controller, /asset-shell-nav-create/);
});

test('新增资源库弹窗取消类型预设，只按名称、路径两行填写并使用紧凑居中标准卡片', () => {
    assert.doesNotMatch(source, /board-preset-grid|data-board-preset/);
    assert.match(source, /\{id:'other', name:'其他资源'[\s\S]*?category:'', kinds:\[\], extensions:\[\]\}/);
    assert.match(source, /board-create-form board-create-form-path/);
    assert.match(source, /<label><span>资源库名称<\/span><input id="boardCreateName"/);
    assert.match(source, /<label class="board-create-path-field"><span>资源路径（可选）<\/span><div class="directory-input-row"><input id="boardCreatePath"/);
    assert.match(styles, /\.board-create-modal\s*\{[^}]*width:min\(620px, 94vw\)/);
    assert.match(styles, /\.board-create-form\s*\{[^}]*grid-template-columns:minmax\(0,1fr\)/);
    assert.doesNotMatch(styles, /board-preset-grid|board-preset-card/);
    assert.match(source, /board\.preset === 'other'[\s\S]*?暂无可见资源/);
});

test('资源路径支持手输及右侧目录选择，先选路径时默认补齐末级目录名', () => {
    assert.match(source, /class="hw-btn hw-btn-icon directory-picker-btn" data-directory-picker="\$\{BOARD_CREATE_PATH_FIELD_KEY\}"/);
    assert.match(source, /async function fillServerDirectoryFromNative\(key, pathValue\)\{\s*if\(key === BOARD_CREATE_PATH_FIELD_KEY\)\{\s*updateBoardCreatePath\(pathValue\);/);
    assert.match(source, /createPathSelection \? '选择此目录' : '选择并挂载'/);
    assert.match(source, /createPathSelection \? '选择当前目录' : '选择当前目录并挂载'/);
    assert.match(source, /仅显示服务器文件夹；选择后返回创建表单，点击创建时再登记路径/);
    assert.ok(source.includes("const nameManuallyEdited = changedField === 'boardCreateName' || boardCreateState.nameManuallyEdited;"));
    assert.ok(source.includes("name:boardCreateState.nameManuallyEdited ? boardCreateState.name : deriveBoardCreateNameFromPath(path),"));
    assert.ok(source.includes("syncBoardCreateDraftFromDom(event.target.id);"));
    const pathFunctions = source.match(/function deriveBoardCreateNameFromPath\(pathValue\)\{[\s\S]*?\n\}\nfunction updateBoardCreatePath\(pathValue\)\{[\s\S]*?\n\}/)?.[0];
    assert.ok(pathFunctions, '路径命名辅助函数应存在');
    const pathFirst = {boardCreateState:{name:'', nameManuallyEdited:false, path:''}, pathValue:['D:', 'Assets', 'Brand', ''].join(String.fromCharCode(92))};
    runInNewContext([pathFunctions, 'updateBoardCreatePath(pathValue);'].join('\n'), pathFirst);
    assert.equal(pathFirst.boardCreateState.name, 'Brand');
    const nameFirst = {boardCreateState:{name:'品牌素材', nameManuallyEdited:true, path:''}, pathValue:['D:', 'Assets', 'Brand'].join(String.fromCharCode(92))};
    runInNewContext([pathFunctions, 'updateBoardCreatePath(pathValue);'].join('\n'), nameFirst);
    assert.equal(nameFirst.boardCreateState.name, '品牌素材');
});

test('新建资源库折叠右侧详情栏后释放网格宽度，不保留空白列', () => {
    assert.match(source, /function syncRegistryDetailGridWidth\(\)\{\s*root\?\.style\.setProperty\('--registry-detail-width',`\$\{registryDetailPanelCollapsed \? 44 : registryDetailPanelWidth\}px`\);\s*\}/);
    assert.match(source, /root\.classList\.toggle\('registry-detail-collapsed'[\s\S]*?syncRegistryDetailGridWidth\(\);/);
    assert.match(source, /root\.className = `asset-manager-root[\s\S]*?syncRegistryDetailGridWidth\(\);/);
});

test('管理员创建时将未登记路径加入允许根，非管理员沿用现有权限校验', () => {
    assert.match(source, /async function ensureCreateLibraryPathAllowedRoot\(path\)\{\s*await loadRegistryPrincipal\(\);\s*if\(!canManageAllowedRoots\(\)\) return true;/);
    assert.match(source, /body:JSON\.stringify\(\{path, expected_version:Number\(snapshot\.revision \|\| 1\)\}\)/);
    assert.match(source, /const allowed = await ensureCreateLibraryPathAllowedRoot\(path\);[\s\S]*?source = await attachPathToAssetBoard\('', `\$\{name\}素材`, path\)/);
});

test('新建资源库命中已移除旧根时确认恢复并继续创建', () => {
    assert.match(source, /function confirmRestoreRemovedAllowedRoot\(root\)[\s\S]*?旧根已移除，是否恢复？[\s\S]*?data-root-restore-confirm[^>]*>恢复并继续/);
    assert.match(source, /data-root-restore-cancel/);
    assert.match(source, /surface:'asset-root-restore-confirm'[\s\S]*?close:\(\) => close\(false\)/);
    assert.match(source, /filter\(root => root\.active === false && assetPathWithinAllowedRoot\(path, root\.path\)\)[\s\S]*?sort\(\(left, right\) => pathCompareKey\(right\.path\)\.length - pathCompareKey\(left\.path\)\.length\)/);
    assert.match(source, /`\/api\/storage-settings\/roots\/\$\{encodeURIComponent\(removedRoot\.id\)\}\/restore`[\s\S]*?body:JSON\.stringify\(\{expected_version:Number\(snapshot\.revision \|\| 1\)\}\)/);
    assert.match(source, /if\(!await confirmRestoreRemovedAllowedRoot\(removedRoot\)\) return false;/);
    assert.match(source, /const allowed = await ensureCreateLibraryPathAllowedRoot\(path\);\s*if\(!allowed\)\{[\s\S]*?资源库未创建[\s\S]*?return;\s*\}\s*source = await attachPathToAssetBoard/);
});

test('旧根确认后只恢复最具体祖先，取消不写入允许根', async () => {
    const pathHelperStart = source.indexOf('function assetPathWithinAllowedRoot(');
    const pathHelperEnd = source.indexOf('\nfunction confirmRestoreRemovedAllowedRoot(', pathHelperStart);
    const ensureStart = source.indexOf('async function ensureCreateLibraryPathAllowedRoot(');
    const ensureEnd = source.indexOf('\n// 用户显式指定缓存目录', ensureStart);
    assert.ok(pathHelperStart >= 0 && pathHelperEnd > pathHelperStart && ensureStart >= 0 && ensureEnd > ensureStart);
    const pathHelper = source.slice(pathHelperStart, pathHelperEnd);
    const ensureHelper = source.slice(ensureStart, ensureEnd);
    const simulate = async confirmed => {
        const calls = [];
        const snapshot = {
            revision:7,
            roots:[
                {id:'removed-parent', path:'D:\\Vault', active:false},
                {id:'removed-child', path:'D:\\Vault\\assets', active:false},
            ],
        };
        const context = {
            pathCompareKey:value => String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase(),
            loadRegistryPrincipal:async () => {},
            canManageAllowedRoots:() => true,
            confirmRestoreRemovedAllowedRoot:async () => confirmed,
            applyAllowedRootSettingsData:() => {},
            assetManagerHttp:{request:(url, options={}) => ({url, ...options})},
            apiJsonResponse:async request => {
                calls.push(request);
                if(request.method === 'GET') return snapshot;
                if(request.url.endsWith('/restore')) return {...snapshot, revision:8, roots:snapshot.roots.map(root => ({...root, active:root.id === 'removed-child'}))};
                throw new Error(`unexpected request ${request.method} ${request.url}`);
            },
        };
        const result = await runInNewContext(`${pathHelper}\n${ensureHelper}\nensureCreateLibraryPathAllowedRoot('D:/Vault/assets/new')`, context);
        return {result, calls};
    };

    const accepted = await simulate(true);
    assert.equal(accepted.result, true);
    const restoreCall = accepted.calls.find(call => call.url.endsWith('/restore'));
    assert.match(restoreCall.url, /removed-child\/restore$/);
    assert.deepEqual(JSON.parse(restoreCall.body), {expected_version:7});
    assert.equal(accepted.calls.filter(call => call.url === '/api/storage-settings/roots' && call.method === 'POST').length, 0);

    const cancelled = await simulate(false);
    assert.equal(cancelled.result, false);
    assert.equal(cancelled.calls.some(call => call.url.endsWith('/restore') || call.method === 'POST'), false);
});

test('新增资源库弹窗模板包含错误回显容器与状态图标', () => {
    assert.match(source, /boardCreateState\.error \? `<div class="board-create-error" role="alert">/);
    assert.match(source, /data-lucide="alert-triangle"/);
    assert.match(source, /escapeHtml\(boardCreateState\.error\)/);
});

test('新增资源库确认与取消按钮在 saving 状态下禁用防重并展示 loading', () => {
    assert.match(source, /data-board-create-close \$\{boardCreateState\.saving \? 'disabled' : ''\}/);
    assert.match(source, /data-board-create-confirm \$\{boardCreateState\.saving \? 'disabled' : ''\}/);
    assert.match(source, /boardCreateState\.saving \? 'loader' : 'plus'/);
    assert.match(source, /boardCreateState\.saving \? '正在创建…' : '创建资源库'/);
});

test('createPresetAssetBoard 捕获异常后将错误同步到 boardCreateState.error 并重新渲染弹窗', () => {
    assert.match(source, /boardCreateState\.saving = false;\s*boardCreateState\.error = err\.message/);
    assert.match(source, /renderCreateLibraryModal\(\);\s*markIndexState\('failed'\)/);
});

test('attachPathToAssetBoard 对别名建议查询具备容错保护', () => {
    assert.match(source, /suggestionData = await apiJsonResponse\(assetManagerApi\.getRootAliasSuggestions\(\{path:cleanPath\}\)\);\s*\} catch\(_\) \{\s*suggestionData = null;/);
});

test('asset-manager.css 中包含 .board-create-error 样式规则', () => {
    assert.match(styles, /\.board-create-error\s*\{[^}]*color:\s*#ef4444/);
    assert.match(styles, /\.board-create-error\s*svg\s*\{[^}]*stroke:\s*#ef4444/);
});

test('新资源库追加到列表下方并自动激活，来源索引异步跟踪 (QIN-487 / QIN-501)', () => {
    assert.match(source, /ASSET_LIBRARY_PRESETS\.find\(item => item\.id === 'other'\)/);
    assert.match(source, /state\.customBoards\.push\(\{id, name, preset:preset\.id/);
    assert.match(source, /state\.order\.push\(id\);\s*if\(source\) boardSourceSelection\[id\] = source\.id;\s*saveBoardNavigationState\(\);\s*closeCreateLibraryModal\(\);\s*renderBoardNavigation\(\);/);
    assert.match(source, /await activateAssetBoard\(id\);/);
    assert.match(source, /waitForAssetIndexJob\(source\.job_id, source\.name\)\.catch\(\(\) => \{\}\);/);
});

test('waitForAssetIndexJob 超时友好提示且不抛出阻断异常 (QIN-487)', () => {
    assert.match(source, /setStatus\(`素材库“\$\{sourceName\}”索引仍在后台运行，可稍后刷新查看`\);\s*return null;/);
    assert.doesNotMatch(source, /throw new Error\(`素材库“\$\{sourceName\}”索引仍在后台运行/);
});

test('attachPathToAssetBoard 返回带有 job_id 的 source 且不阻塞等待 (QIN-487)', () => {
    assert.match(source, /if\(accepted\?\.job_id\)\{\s*source\.job_id = accepted\.job_id;\s*\}/);
    assert.doesNotMatch(source, /await waitForAssetIndexJob\(accepted\?\.job_id/);
});
