/**
 * QIN-525：预览缓存交互收口（两按钮 + 分类删除）结构契约。
 *
 * 断言面：
 * - AC5 设置面板只剩【全部缓存/重建缓存】与【删除缓存】两个按钮，旧入口与「当前视图 N 项」消失；
 * - AC3 覆盖完整后按钮自动变【重建缓存】，点击带 mode=rebuild；
 * - AC4 删除面板按「图像/视频/分镜/其他」四类勾选，提交 scope=all + categories；
 * - AC2 文案声明覆盖全库素材，类型占位图由后端渲染（前端不按类型过滤）。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const MANAGER = readFileSync(
    new URL('../../web/js/modules/asset-manager.js', import.meta.url),
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

test('AC5: 合并后只剩两个按钮，旧入口与「当前视图」口径全部消失', () => {
    const body = functionSource('renderAssetPreviewSettingsBody');
    assert.match(body, /data-preview-cache-delete/, '必须保留删除缓存按钮');
    assert.match(body, /data-preview-all-start/, '必须保留全部缓存按钮');

    assert.doesNotMatch(MANAGER, /data-board-thumbnail-run/, '旧的「后台重新生成」入口必须移除');
    assert.doesNotMatch(MANAGER, /data-board-thumbnail-delete/, '旧的「删除缩略图」入口必须移除');
    assert.doesNotMatch(MANAGER, /后台重新生成/, '旧按钮文案必须移除');
    assert.doesNotMatch(MANAGER, /删除缩略图/, '旧按钮文案必须移除');
    assert.doesNotMatch(MANAGER, /renderPreviewAllCacheSettingsBody/, '旧的全库区块必须并入主区块');
});

test('AC3: 覆盖完整后按钮切换为「重建缓存」并携带 mode=rebuild', () => {
    const body = functionSource('renderAssetPreviewSettingsBody');
    assert.match(body, /const complete = !running && stats\.totalAssets > 0 && stats\.cachedAssets >= stats\.totalAssets/,
        '完成判定必须基于真实缓存覆盖，而不是上一次任务状态');
    assert.match(body, /'重建缓存'/, '完成态按钮文案必须是「重建缓存」');
    assert.match(body, /'全部缓存'/, '未完成态按钮文案必须是「全部缓存」');
    assert.match(body, /data-preview-all-mode="\$\{complete \? 'rebuild' : 'fill'\}"/,
        '必须把当前按钮语义带给点击派发');

    const start = functionSource('startPreviewAllCache');
    assert.match(start, /const mode = assetPreviewAllState\.confirmMode === 'rebuild' \? 'rebuild' : 'fill'/);
    assert.match(start, /JSON\.stringify\(\{scope:'library', mode\}\)/);
});

test('AC4: 删除面板四类勾选，提交 scope=all + categories', () => {
    const panel = functionSource('renderPreviewCacheDeletePanel');
    for(const id of ['image', 'video', 'storyboard', 'other']){
        assert.match(panel, new RegExp(`data-preview-delete-toggle="\\$\\{row\\.id\\}"`));
        break;
    }
    assert.match(panel, /code:'image'|id:'image'/, '图像预览分类必须存在');
    assert.match(panel, /id:'video'/, '视频预览分类必须存在');
    assert.match(panel, /id:'storyboard'/, '视频分镜分类必须存在');
    assert.match(panel, /id:'other'/, '其他分类必须存在');
    assert.match(panel, /data-preview-delete-toggle="all"/, '必须有全选开关');
    assert.match(panel, /data-preview-delete-confirm/, '必须有确认删除按钮');
    assert.match(panel, /素材源文件、代理与无损转码产物不受影响/, '必须声明不碰素材与转码产物');

    const confirm = functionSource('confirmPreviewCacheDelete');
    assert.match(confirm, /'\/api\/asset-thumbnails\/delete'/);
    assert.match(confirm, /scope:'all'/);
    assert.match(confirm, /categories:selection\.categories/);
    assert.match(confirm, /if\(!selection\.categories\.length\) return;/, '未勾选任何分类时不得发请求');
    assert.match(confirm, /refreshThumbnailCacheStats\(\)/, '删除后必须刷新缓存统计');
});

test('AC2: 文案声明覆盖全库素材，前端不按类型过滤', () => {
    const body = functionSource('renderAssetPreviewSettingsBody');
    assert.match(body, /为全库素材后台生成预览图/);
    assert.match(body, /已缓存的素材会自动跳过/);
    assert.doesNotMatch(body, /图片\/视频/, '不能再把范围写成「图片/视频」');
});
