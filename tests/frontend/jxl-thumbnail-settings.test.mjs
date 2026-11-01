/**
 * QIN-513：缩略图格式设置控件、即时保存与 API 契约。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const ASSET_MANAGER = readFileSync(
  new URL('../../web/js/modules/asset-manager.js', import.meta.url),
  'utf8'
);
const ASSET_MANAGER_API = readFileSync(
  new URL('../../web/js/modules/asset-manager/api.js', import.meta.url),
  'utf8'
);
const DIRECTORY_SETTINGS_CSS = readFileSync(
  new URL('../../web/css/pages/directory-settings.css', import.meta.url),
  'utf8'
);

test('资产设置展示默认 JXL 与兼容 WebP 单选项', () => {
  assert.match(ASSET_MANAGER, /format:'jxl', revision:1/);
  assert.match(ASSET_MANAGER, /function renderThumbnailFormatOptions\(\)/);
  assert.match(ASSET_MANAGER, /value="jxl" data-thumbnail-format/);
  assert.match(ASSET_MANAGER, /value="webp" data-thumbnail-format/);
  assert.match(ASSET_MANAGER, /renderThumbnailFormatOptions\(\)/);
});

test('格式切换即时保存并携带设置版本', () => {
  assert.match(ASSET_MANAGER, /data-thumbnail-format/);
  assert.match(ASSET_MANAGER, /persistThumbnailStorageSettings\(\{format:nextFormat\}\)/);
  assert.match(ASSET_MANAGER, /expected_version:expectedVersion/);
  assert.match(ASSET_MANAGER, /revision:Number\(data\.revision \|\| expectedVersion \+ 1\)/);
});

test('资产管理 API 通过缩略图设置接口更新格式', () => {
  assert.match(ASSET_MANAGER_API, /updateThumbnailSettings\(payload, init = \{\}\)/);
  assert.match(ASSET_MANAGER_API, /request\('\/api\/asset-thumbnails\/settings'/);
  assert.match(DIRECTORY_SETTINGS_CSS, /\.directory-page \.thumbnail-format-options/);
  assert.match(DIRECTORY_SETTINGS_CSS, /\.directory-page \.thumbnail-format-option\.is-active/);
});
