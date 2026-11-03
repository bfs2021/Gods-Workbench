import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// QIN-475（GitHub #450）：目录卡片间距、API 列表底色和设置页标题行操作。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = relativePath => readFileSync(path.join(root, relativePath), 'utf8');
const directoryCss = read('web/css/pages/directory-settings.css');
const apiCss = read('web/css/pages/api-settings.css');
const apiModule = read('web/js/modules/api-settings.js');
const assetManager = read('web/js/modules/asset-manager.js');

// 1. 目录设置五卡分两列独立堆叠；每列卡间固定 12px，窄屏折为一列。
assert.match(directoryCss, /\.directory-cards-grid\s*\{[\s\S]*?display:\s*grid[\s\S]*?grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
assert.match(directoryCss, /\.directory-cards-column\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column[^}]*gap:\s*12px/s);
assert.match(directoryCss, /\.directory-cards-column\s*>\s*\.asset-pref-section\s*\{[^}]*margin:\s*0[^}]*min-width:\s*0/s);
assert.match(directoryCss, /@media\s*\(max-width:\s*700px\)[\s\S]*?\.directory-cards-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
assert.match(assetManager, /class="directory-cards-column directory-cards-column--left"[\s\S]*data-board-directories-settings[\s\S]*<span>上传与生成<\/span>[\s\S]*renderAssetThumbnailCacheDirectoryBody\(\)[\s\S]*class="directory-cards-column directory-cards-column--right"[\s\S]*renderAllowedRootsSettings\(\)[\s\S]*renderProjectWorkspaceSettingsBody\(\)/);
assert.doesNotMatch(assetManager, /<span>回收站目录<\/span>/);

// 2. 仅设置页嵌入态改用设置卡片黑底，并隐藏重复的 iframe 详情头。
assert.match(apiCss, /\.workspace-embedded\.settings-host\s+\.api-settings-page\s+\.sidebar\s*\{[^}]*background:\s*var\(--gw-settings-card-background\)/s);
assert.match(apiCss, /\.workspace-embedded\.settings-host\s+\.api-settings-page\s+\.content-head\s*\{[^}]*display:\s*none/s);

// 3. 同源设置页宿主把原删除/保存按钮移到外层标题栏，并继续调用原动作。
assert.match(apiModule, /function mountSettingsApiToolbar\(\)[\s\S]*?endsWith\('\/pages\/settings\.html'\)[\s\S]*?hostActions\.appendChild\(toolbar\)/);
assert.match(apiModule, /\[deleteButton, 'delete', deleteProvider\]/);
assert.match(apiModule, /\[saveButton, 'save', saveProviders\]/);
assert.match(apiModule, /button\.addEventListener\('click', async \(\) => \{[\s\S]*?await action\(\)/);

console.log('QIN-475 settings UI polish contracts passed');
