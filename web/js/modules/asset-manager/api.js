/**
 * Copyright 2026 Gods-Workbench Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import assetManagerHttp from './http.js';

function jsonHeaders(headersInit) {
    if (typeof Headers !== 'undefined' && headersInit instanceof Headers) {
        return Object.fromEntries(headersInit.entries());
    }
    if (Array.isArray(headersInit)) {
        return Object.fromEntries(headersInit);
    }
    return {...(headersInit || {})};
}

function jsonInit(payload, init = {}) {
    const headers = jsonHeaders(init.headers);
    if (!Object.keys(headers).some(name => name.toLowerCase() === 'content-type')) {
        headers['Content-Type'] = 'application/json';
    }
    return {...init, headers, body: JSON.stringify(payload)};
}

function versionQuery(expectedVersion) {
    return new URLSearchParams({expected_version: String(expectedVersion)}).toString();
}

function resolveHttp(http) {
    const candidate = http || assetManagerHttp;
    if (!candidate || typeof candidate.request !== 'function') {
        throw new TypeError('An asset-manager HTTP transport with request(url, init) is required');
    }
    return candidate;
}

/**
 * API boundary for asset-manager media operations. The page retains response
 * parsing, authentication projection, Blob handling, and asset-library state.
 */
export function createAssetManagerApi(http) {
    const transport = resolveHttp(http);

    return {
        uploadAiFiles(formData, init = {}) {
            return transport.request('/api/ai/upload', {
                ...init,
                method: 'POST',
                body: formData,
                credentials: 'same-origin',
            });
        },
        downloadCanvasAssets(payload, init = {}) {
            return transport.request('/api/canvas-assets/download', jsonInit(payload, {...init, method: 'POST'}));
        },
        archiveRegistryAssets(payload, init = {}) {
            return transport.request('/api/asset-registry/assets/archive', jsonInit(payload, {...init, method: 'POST'}));
        },
        exportRegistryPdf(payload, init = {}) {
            return transport.request('/api/asset-registry/assets/export-pdf', jsonInit(payload, {...init, method: 'POST'}));
        },
        downloadAssetContentPdf(assetId, init = {}) {
            return transport.request(`/api/asset-content/pdf?asset_id=${encodeURIComponent(assetId)}`, {...init, method: 'GET', credentials: 'same-origin'});
        },
        getAssetContent(query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/asset-content${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        getAssetContentVersions(query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/asset-content/versions${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        getAssetContentVersion(versionId, query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/asset-content/versions/${encodeURIComponent(versionId)}${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        updateAssetContent(payload, init = {}) {
            return transport.request('/api/asset-content', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        updateAssetContentVersion(versionId, payload, init = {}) {
            return transport.request(`/api/asset-content/versions/${encodeURIComponent(versionId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteAssetContentVersion(versionId, query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            const {body: _body, ...requestInit} = init;
            return transport.request(`/api/asset-content/versions/${encodeURIComponent(versionId)}${suffix ? `?${suffix}` : ''}`, {
                ...requestInit,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        restoreAssetContentVersion(versionId, payload, init = {}) {
            return transport.request(`/api/asset-content/versions/${encodeURIComponent(versionId)}/restore`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getMedia(url, init = {}) {
            return transport.request(url, init);
        },
        getAudioWaveformData(query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/audio-waveform-data${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        getAssetFileInfo(query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/asset-file-info${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        reindexAssetSource(payload, init = {}) {
            return transport.request('/api/asset-registry/reindex', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        registerAssetRoot(payload, init = {}) {
            return transport.request('/api/asset-registry/reindex', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getRootAliasSuggestions(payload, init = {}) {
            return transport.request('/api/asset-registry/root-aliases/suggestions', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        confirmRootAlias(payload, init = {}) {
            return transport.request('/api/asset-registry/root-aliases/confirm', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getWorkspaceJob(jobId, init = {}) {
            return transport.request(`/api/asset-registry/workspace-jobs/${encodeURIComponent(jobId)}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        getStorageSettings(init = {}) {
            return transport.request('/api/storage-settings', {...init, credentials: 'same-origin'});
        },
        updateStorageSettings(payload, init = {}) {
            return transport.request('/api/storage-settings', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        getProjectDirectoryTemplates(projectType = '', init = {}) {
            const query = new URLSearchParams();
            if (String(projectType || '').trim()) query.set('project_type', String(projectType).trim());
            const suffix = query.toString();
            return transport.request(`/api/asset-registry/project-directory-templates${suffix ? `?${suffix}` : ''}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        createProjectDirectoryTemplate(payload, init = {}) {
            return transport.request('/api/asset-registry/project-directory-templates', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateProjectDirectoryTemplate(templateId, payload, init = {}) {
            return transport.request(`/api/asset-registry/project-directory-templates/${encodeURIComponent(templateId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        archiveProjectDirectoryTemplate(templateId, payload = {}, init = {}) {
            return transport.request(`/api/asset-registry/project-directory-templates/${encodeURIComponent(templateId)}/archive`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        setDefaultProjectDirectoryTemplate(templateId, payload, init = {}) {
            return transport.request(`/api/asset-registry/project-directory-templates/${encodeURIComponent(templateId)}/default`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateThumbnailSettings(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/settings', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        getThumbnailSettings(init = {}) {
            return transport.request('/api/asset-thumbnails/settings', {
                ...init,
                credentials: 'same-origin',
            });
        },
        switchThumbnailCache(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/cache/switch', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getRegistryStatus(init = {}) {
            return transport.request('/api/asset-registry/status', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getRegistryAssets(query = '', init = {}) {
            const suffix = String(query || '').replace(/^\?/, '');
            return transport.request(`/api/asset-registry/assets${suffix ? `?${suffix}` : ''}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getRegistryAsset(assetId, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getRegistryVideoStoryboard(assetId, version = '', init = {}) {
            const suffix = version === '' || version === null || version === undefined
                ? ''
                : `?version=${encodeURIComponent(version)}`;
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/video/storyboard${suffix}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        updateRegistryAsset(assetId, payload, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        createAssetStructure(payload, init = {}) {
            return transport.request('/api/asset-registry/asset-structures', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        listAssetStructures(assetIds, init = {}) {
            const ids = [...new Set((Array.isArray(assetIds) ? assetIds : [assetIds])
                .map(value => String(value || '').trim())
                .filter(Boolean))];
            const query = new URLSearchParams();
            ids.forEach(assetId => query.append('asset_ids', assetId));
            return transport.request(`/api/asset-registry/asset-structures?${query.toString()}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getAssetStructure(structureId, init = {}) {
            return transport.request(`/api/asset-registry/asset-structures/${encodeURIComponent(structureId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        setAssetStructureCurrent(structureId, payload, init = {}) {
            return transport.request(`/api/asset-registry/asset-structures/${encodeURIComponent(structureId)}/current`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteAssetStructure(structureId, expectedVersion, init = {}) {
            const query = new URLSearchParams({expected_version: String(expectedVersion)});
            return transport.request(`/api/asset-registry/asset-structures/${encodeURIComponent(structureId)}?${query.toString()}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        importRegistryAsset(formData, init = {}) {
            return transport.request('/api/asset-registry/assets/import', {
                ...init,
                method: 'POST',
                body: formData,
                credentials: 'same-origin',
            });
        },
        resolveRegistryAssetReference(payload, init = {}) {
            return transport.request('/api/asset-registry/assets/resolve-reference', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        openRegistryAssetLocal(assetId, init = {}) {
            const {body: _body, ...requestInit} = init;
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/open-local`, {
                ...requestInit,
                method: 'POST',
                credentials: 'same-origin',
            });
        },
        getRegistryImageVersions(assetId, limit = 500, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/image-versions?limit=${encodeURIComponent(limit)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        createRegistryImageVersion(assetId, payload, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/image-versions`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteRegistryAsset(assetId, expectedVersion, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}`, {
                ...jsonInit({expected_version: expectedVersion}, {...init, method: 'DELETE'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryFeature(featureId, payload, init = {}) {
            return transport.request(`/api/asset-registry/settings/features/${encodeURIComponent(featureId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryIndexAutomation(payload, init = {}) {
            return transport.request('/api/asset-registry/settings/index-automation', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        getRegistryFolders(rootId, init = {}) {
            return transport.request(`/api/asset-registry/folders?root_id=${encodeURIComponent(rootId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        createRegistryFolder(payload, init = {}) {
            return transport.request('/api/asset-registry/folders', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryImageVersion(assetId, versionId, payload, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/image-versions/${encodeURIComponent(versionId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteRegistryImageVersion(assetId, versionId, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/image-versions/${encodeURIComponent(versionId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        revealAssetFile(payload, init = {}) {
            return transport.request('/api/asset-file-reveal', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        createAssetLibraryItemsBatch(payload, init = {}) {
            return transport.request('/api/asset-library/items/batch', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        moveAssetLibraryItems(payload, init = {}) {
            return transport.request('/api/asset-library/items/move', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getAssetLibrary(init = {}) {
            return transport.request('/api/asset-library', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getWorkflowDocument(workflowId, init = {}) {
            return transport.request(`/api/god_workflow/item/${encodeURIComponent(workflowId)}`, {...init, credentials:'same-origin'});
        },
        exportWorkflowDocument(workflowId, init = {}) {
            return transport.request(`/api/god_workflow/export/${encodeURIComponent(workflowId)}?format=litegraph`, {...init, credentials:'same-origin'});
        },
        getWorkflowFile(url, init = {}) {
            return transport.request(url, {...init, credentials:'same-origin'});
        },
        renameWorkflowDocument(workflowId, payload, init = {}) {
            return transport.request(`/api/god_workflow/item/${encodeURIComponent(workflowId)}/name`, {...jsonInit(payload, {...init, method:'PUT'}), credentials:'same-origin'});
        },
        deleteWorkflowDocument(workflowId, expectedVersion, init = {}) {
            return transport.request(`/api/god_workflow/item/${encodeURIComponent(workflowId)}`, {...jsonInit({expected_version:expectedVersion}, {...init, method:'DELETE'}), credentials:'same-origin'});
        },
        createAssetLibrary(payload, init = {}) {
            return transport.request('/api/asset-library/libraries', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        renameAssetLibrary(libraryId, payload, init = {}) {
            return transport.request(`/api/asset-library/libraries/${encodeURIComponent(libraryId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteAssetLibrary(libraryId, expectedVersion, init = {}) {
            const query = versionQuery(expectedVersion);
            return transport.request(`/api/asset-library/libraries/${encodeURIComponent(libraryId)}?${query}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        createAssetLibraryCategory(payload, init = {}) {
            return transport.request('/api/asset-library/categories', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        renameAssetLibraryCategory(categoryId, payload, init = {}) {
            return transport.request(`/api/asset-library/categories/${encodeURIComponent(categoryId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteAssetLibraryCategory(categoryId, {libraryId = '', expectedVersion, ...init} = {}) {
            const query = new URLSearchParams({expected_version: String(expectedVersion)});
            if (libraryId) query.set('library_id', libraryId);
            const suffix = `?${query.toString()}`;
            return transport.request(`/api/asset-library/categories/${encodeURIComponent(categoryId)}${suffix}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        uploadAssetLibraryWorkflows(formData, init = {}) {
            return transport.request('/api/asset-library/workflows/upload', {
                ...init,
                method: 'POST',
                body: formData,
                credentials: 'same-origin',
            });
        },
        moveLocalAssets(payload, init = {}) {
            return transport.request('/api/local-assets/move', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateAssetLibraryItem(itemId, payload, init = {}) {
            return transport.request(`/api/asset-library/items/${encodeURIComponent(itemId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deleteAssetLibraryItem(itemId, expectedVersion, init = {}) {
            const query = versionQuery(expectedVersion);
            return transport.request(`/api/asset-library/items/${encodeURIComponent(itemId)}?${query}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        deleteAssetLibraryItems(payload, init = {}) {
            return transport.request('/api/asset-library/items/delete', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        registerAssetAvatar(itemId, payload, init = {}) {
            return transport.request(`/api/asset-library/items/${encodeURIComponent(itemId)}/register-avatar`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        checkAssetAvatarStatus(itemId, payload, init = {}) {
            return transport.request(`/api/asset-library/items/${encodeURIComponent(itemId)}/avatar-status`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getPromptLibraries(init = {}) {
            return transport.request('/api/prompt-libraries', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getPromptLibraryItems(libraryId = '', init = {}) {
            const query = new URLSearchParams();
            if (libraryId) query.set('library_id', libraryId);
            const suffix = query.toString() ? `?${query.toString()}` : '';
            return transport.request(`/api/prompt-libraries/items${suffix}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        createPromptLibrary(payload, init = {}) {
            return transport.request('/api/prompt-libraries', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        renamePromptLibrary(libraryId, payload, init = {}) {
            return transport.request(`/api/prompt-libraries/${encodeURIComponent(libraryId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deletePromptLibrary(libraryId, expectedVersion, init = {}) {
            const query = versionQuery(expectedVersion);
            return transport.request(`/api/prompt-libraries/${encodeURIComponent(libraryId)}?${query}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        createPromptLibraryCategory(payload, init = {}) {
            return transport.request('/api/prompt-libraries/categories', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        renamePromptLibraryCategory(categoryId, payload, init = {}) {
            return transport.request(`/api/prompt-libraries/categories/${encodeURIComponent(categoryId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deletePromptLibraryCategory(categoryId, expectedVersion, init = {}) {
            const query = versionQuery(expectedVersion);
            return transport.request(`/api/prompt-libraries/categories/${encodeURIComponent(categoryId)}?${query}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        createPromptLibraryItem(payload, init = {}) {
            return transport.request('/api/prompt-libraries/items', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updatePromptLibraryItem(itemId, payload, init = {}) {
            return transport.request(`/api/prompt-libraries/items/${encodeURIComponent(itemId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        deletePromptLibraryItem(itemId, expectedVersion, init = {}) {
            const query = versionQuery(expectedVersion);
            return transport.request(`/api/prompt-libraries/items/${encodeURIComponent(itemId)}?${query}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        deletePromptLibraryItems(payload, init = {}) {
            return transport.request('/api/prompt-libraries/items/delete', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        uploadLocalAssets(formData, init = {}) {
            return transport.request('/api/local-assets/upload', {
                ...init,
                method: 'POST',
                body: formData,
                credentials: 'same-origin',
            });
        },
        deleteLocalAssets(payload, init = {}) {
            return transport.request('/api/local-assets/delete', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateLocalAsset(payload, init = {}) {
            return transport.request('/api/local-assets/items', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        createLocalAssetFolder(payload, init = {}) {
            return transport.request('/api/local-assets/folders', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateLocalAssetFolder(payload, init = {}) {
            return transport.request('/api/local-assets/folders', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        captionLocalAssets(payload, init = {}) {
            return transport.request('/api/local-assets/caption', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        saveLocalAssetCaption(payload, init = {}) {
            return transport.request('/api/local-assets/caption', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        classifyLocalAssets(payload, init = {}) {
            return transport.request('/api/local-assets/classify', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        classifyAssetLibraryItems(payload, init = {}) {
            return transport.request('/api/asset-library/items/classify', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        createRegistryProject(payload, init = {}) {
            return transport.request('/api/asset-registry/projects', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        createRegistryProjectEntity(projectId, payload, init = {}) {
            return transport.request(`/api/asset-registry/projects/${encodeURIComponent(projectId)}/entities`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryProject(projectId, payload, init = {}) {
            return transport.request(`/api/asset-registry/projects/${encodeURIComponent(projectId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryProjectGate(gateId, payload, init = {}) {
            return transport.request(`/api/asset-registry/project-gates/${encodeURIComponent(gateId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        updateRegistryProjectEntity(entityId, payload, init = {}) {
            return transport.request(`/api/asset-registry/project-entities/${encodeURIComponent(entityId)}`, {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        linkRegistryProjectAssets(projectId, payload, init = {}) {
            return transport.request(`/api/asset-registry/projects/${encodeURIComponent(projectId)}/assets`, {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        createRegistryRemoteAsset(payload, init = {}) {
            return transport.request('/api/asset-registry/remote-assets', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteRegistryRemoteAsset(assetId, init = {}) {
            return transport.request(`/api/asset-registry/remote-assets/${encodeURIComponent(assetId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        getRegistryFacets(init = {}) {
            return transport.request('/api/asset-registry/facets', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getRegistryPresets(init = {}) {
            return transport.request('/api/asset-registry/presets', {
                ...init,
                credentials: 'same-origin',
            });
        },
        createRegistryPreset(payload, init = {}) {
            return transport.request('/api/asset-registry/presets', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteRegistryPreset(presetId, init = {}) {
            return transport.request(`/api/asset-registry/presets/${encodeURIComponent(presetId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        getRegistryRecycleBin(init = {}) {
            return transport.request('/api/asset-registry/recycle-bin', {
                ...init,
                credentials: 'same-origin',
            });
        },
        restoreRegistryRecycleEntry(entryId, expectedVersion, init = {}) {
            return transport.request(`/api/asset-registry/recycle-bin/${encodeURIComponent(entryId)}/restore`, {
                ...jsonInit({expected_version: expectedVersion}, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        addRegistryAssetTags(payload, init = {}) {
            return transport.request('/api/asset-registry/assets/tags', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteRegistryAssetTag(assetId, tagId, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/tags/${encodeURIComponent(tagId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        relateRegistryAssets(payload, init = {}) {
            return transport.request('/api/asset-registry/assets/relations', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        removeRegistryAssetRelation(assetId, relatedAssetId, init = {}) {
            return transport.request(`/api/asset-registry/assets/${encodeURIComponent(assetId)}/relations/${encodeURIComponent(relatedAssetId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        getCanvases(init = {}) {
            return transport.request('/api/canvases', {
                ...init,
                credentials: 'same-origin',
            });
        },
        attachRegistryAssetsToCanvas(payload, init = {}) {
            return transport.request('/api/canvases/assets', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getRegistryProjects(init = {}) {
            return transport.request('/api/asset-registry/projects', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getRegistryProject(projectId, init = {}) {
            // 项目详情 GET 使用冻结的平台读取接口；注册表单项路径仅供 PATCH/DELETE。
            return transport.request(`/api/projects/${encodeURIComponent(projectId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getCanvasAssets(init = {}) {
            return transport.request('/api/canvas-assets', {
                ...init,
                credentials: 'same-origin',
            });
        },
        saveReferenceCanvas(payload, init = {}) {
            return transport.request('/api/reference-canvases', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getProviders(init = {}) {
            return transport.request('/api/providers', {
                ...init,
                credentials: 'same-origin',
            });
        },
        getAssetClassificationPrompt(init = {}) {
            return transport.request('/api/asset-classification-prompt', {
                ...init,
                credentials: 'same-origin',
            });
        },
        updateAssetClassificationPrompt(payload, init = {}) {
            return transport.request('/api/asset-classification-prompt', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
        startAssetClassificationBackground(payload, init = {}) {
            return transport.request('/api/asset-classification/background', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        stopAssetClassificationBackground(init = {}) {
            return transport.request('/api/asset-classification/background', {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        getAssetClassificationJob(taskId, init = {}) {
            return transport.request(`/api/asset-classification/jobs/${encodeURIComponent(taskId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getThumbnailJob(taskId, init = {}) {
            return transport.request(`/api/asset-thumbnails/jobs/${encodeURIComponent(taskId)}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        generateThumbnails(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/generate', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        generateThumbnailsBackground(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/generate-background', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteThumbnails(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/delete', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteThumbnailStoryboards(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/delete-storyboards', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        generateThumbnailStoryboards(payload, init = {}) {
            return transport.request('/api/asset-thumbnails/generate-storyboards', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getStorageFiles(kind, offset, limit, init = {}) {
            return transport.request(
                `/api/storage-files?kind=${encodeURIComponent(kind)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(limit)}`,
                {...init, credentials: 'same-origin'},
            );
        },
        deleteStorageFiles(payload, init = {}) {
            return transport.request('/api/storage-files/delete', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getSharedFolders(init = {}) {
            return transport.request('/api/shared-folders', {
                ...init,
                credentials: 'same-origin',
            });
        },
        createSharedFolder(payload, init = {}) {
            return transport.request('/api/shared-folders', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        deleteSharedFolder(folderId, init = {}) {
            return transport.request(`/api/shared-folders/${encodeURIComponent(folderId)}`, {
                ...init,
                method: 'DELETE',
                credentials: 'same-origin',
            });
        },
        getSharedFolderTree(folderId, init = {}) {
            return transport.request(`/api/shared-folders/${encodeURIComponent(folderId)}/tree`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        importSharedFolderItems(payload, init = {}) {
            return transport.request('/api/shared-folders/import', {
                ...jsonInit(payload, {...init, method: 'POST'}),
                credentials: 'same-origin',
            });
        },
        getLocalAssets(kind, init = {}) {
            const {limit, cursor, ...requestInit} = init;
            const query = new URLSearchParams();
            if(kind) query.set('kind', kind);
            if(limit != null) query.set('limit', String(limit));
            if(cursor) query.set('cursor', cursor);
            const params = query.toString();
            const suffix = params ? `?${params}` : '';
            return transport.request(`/api/local-assets${suffix}`, {
                ...requestInit,
                credentials: 'same-origin',
            });
        },
        getEpisodePipelines(projectId = '', init = {}) {
            const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
            return transport.request(`/api/episode-pipelines${query}`, {
                ...init,
                credentials: 'same-origin',
            });
        },
        getAssetProxySettings(projectId = '', init = {}) {
            const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
            return transport.request(`/api/asset-proxy/settings${query}`, {
                ...init,
                method: 'GET',
                credentials: 'same-origin',
            });
        },
        updateAssetProxySettings(payload, init = {}) {
            return transport.request('/api/asset-proxy/settings', {
                ...jsonInit(payload, {...init, method: 'PATCH'}),
                credentials: 'same-origin',
            });
        },
    };
}

const assetManagerApi = Object.freeze(createAssetManagerApi());

export default assetManagerApi;
