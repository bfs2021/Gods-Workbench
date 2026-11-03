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

/**
 * C4-AC5b「未归属」收件箱：GET /api/assets/unassigned 只返回当前账户自己的无项目资产；
 * 无主资产只出现在管理员收件箱，后端标 source_unknown/origin_label。
 * 「移入项目」POST /api/assets/{asset_id}/move-to-project {project_id, expected_asset_version}。
 * expected_asset_version 取该条目的 asset_version（资产自身版本 CAS，其他资产变动不再冲突）。
 * 409 VERSION_CONFLICT（detail.asset_id）：重新读取该条目最新 asset_version，仍未归属则自动重试一次；
 * 409 ASSET_ALREADY_ASSIGNED：刷新列表并提示。匿名/无权（401/403）不显示入口。
 */
window.GWAssetJobStatus = (() => {
  'use strict';
  const NEXT_ACTION_LABEL = Object.freeze({
    poll_job: '等待任务状态更新',
    resume_index: '恢复索引任务',
    review_result: '查看索引结果',
    retry_index: '修复来源后重试索引',
    reconcile_index_job: '人工核对索引任务',
  });

  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function view(job = {}) {
    const status = String(job.status || 'unknown').trim().toLowerCase();
    const errorCode = String(job.error_code || '').trim();
    if (status === 'succeeded') return {status, kind: '', dataStatus: 'ok', label: '索引已完成', nextAction: 'review_result', resultUnknown: false, errorCode, message: '索引已完成'};
    if (status === 'queued' || status === 'running') return {status, kind: 'pending', dataStatus: 'degraded', label: status === 'queued' ? '索引排队中' : '索引执行中', nextAction: 'poll_job', resultUnknown: false, errorCode, message: status === 'queued' ? '索引排队中' : '索引执行中'};
    if (status === 'paused') return {status, kind: 'blocked', dataStatus: 'degraded', label: '索引已暂停', nextAction: 'resume_index', resultUnknown: false, errorCode, message: '索引已暂停'};
    if (status === 'failed') {
      const message = errorCode ? `索引失败：${errorCode}` : '索引失败';
      return {status, kind: 'degraded', dataStatus: 'degraded', label: '索引失败', nextAction: 'retry_index', resultUnknown: false, errorCode, message};
    }
    if (status === 'cancelled') return {status, kind: 'degraded', dataStatus: 'degraded', label: '索引已取消', nextAction: 'retry_index', resultUnknown: false, errorCode, message: '索引已取消'};
    if (status === 'interrupted') {
      const message = errorCode ? `索引中断，结果未知：${errorCode}` : '索引中断，结果未知';
      return {status, kind: 'unknown', dataStatus: 'degraded', label: '索引中断，结果未知', nextAction: 'reconcile_index_job', resultUnknown: true, errorCode, message};
    }
    return {status: 'unknown', kind: 'unknown', dataStatus: 'degraded', label: '索引状态未知', nextAction: 'reconcile_index_job', resultUnknown: true, errorCode, message: '索引状态未知'};
  }

  function render(job) {
    const result = view(job);
    const degradation = result.kind ? ` data-gw-degradation="${result.kind}"` : '';
    return `<div role="status" data-asset-job-status="${escapeText(result.status)}"${degradation}><strong>${escapeText(result.message)}</strong><span>下一步：${escapeText(NEXT_ACTION_LABEL[result.nextAction] || result.nextAction)}</span></div>`;
  }

  return Object.freeze({view, render});
})();

window.GWUnassignedInbox = (() => {
  'use strict';
  const INBOX = '/api/assets/unassigned';
  let snapshot = {assets: [], revision: null, total: 0};
  let projects = [];
  let busy = false;

  async function request(url, options = {}) {
    const res = await fetch(url, {credentials: 'same-origin', ...options});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data?.detail || {};
      const error = new Error(detail.message || `请求失败 (${res.status})`);
      error.status = res.status; error.code = detail.code;
      throw error;
    }
    return data;
  }

  /** 来源未知只信后端 source_unknown（无主资产，仅管理员收件箱可见），前端不做推断。 */
  function sourceUnknown(asset) { return asset?.source_unknown === true; }

  async function load() {
    const data = await request(`${INBOX}?limit=500`);
    snapshot = {assets: Array.isArray(data.assets) ? data.assets : (data.items || []), revision: data.revision, total: Number(data.total) || 0};
    return snapshot;
  }

  async function loadProjects() {
    const data = await request('/api/asset-registry/projects?archived=false');
    projects = (data.projects || []).filter(p => p && p.project_id && !p.archived_at && !p.deleted_at);
    return projects;
  }

  function entry() { return document.getElementById('btnUnassignedInbox'); }

  function clearEntryError(button) {
    document.getElementById('unassignedInboxEntryStatus')?.remove();
    button?.removeAttribute('aria-describedby');
    if (button?.dataset) delete button.dataset.unassignedLoadState;
  }

  function showEntryError(button) {
    let status = document.getElementById('unassignedInboxEntryStatus');
    if (!status) {
      status = document.createElement('div');
      status.id = 'unassignedInboxEntryStatus';
      status.dataset.unassignedEntryStatus = 'true';
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      status.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 8px;color:#fbbf24;font-size:var(--gw-type-body-sm, 12px);';
      button.insertAdjacentElement('afterend', status);
    }
    status.innerHTML = '<span>未归属资产暂时无法读取。</span><button type="button" class="hw-btn btn-size-sm" data-unassigned-retry>重试</button>';
    status.querySelector('[data-unassigned-retry]').addEventListener('click', async event => {
      event.currentTarget.disabled = true;
      await refreshEntry();
    });
    button.setAttribute('aria-describedby', status.id);
    if (button.dataset) button.dataset.unassignedLoadState = 'error';
  }

  async function refreshEntry() {
    const button = entry();
    if (!button) return 'missing';
    try {
      await load();
      clearEntryError(button);
      button.style.display = '';
      const count = button.querySelector('[data-unassigned-count]');
      if (count) count.textContent = String(snapshot.total);
      if (button.dataset) button.dataset.unassignedLoadState = 'ready';
      return 'shown';
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        clearEntryError(button);
        button.style.display = 'none';
        return 'hidden';
      }
      button.style.display = '';
      showEntryError(button);
      return 'error';
    }
  }

  function notify(message, isError = false) {
    const status = document.getElementById('unassignedInboxStatus');
    if (status) { status.textContent = message; status.dataset.error = isError ? 'true' : 'false'; }
    if (typeof window.showWorkbenchToast === 'function') window.showWorkbenchToast(message, isError, '', null, 5000);
  }

  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function render() {
    const dialog = document.getElementById('unassignedInboxDialog');
    const list = dialog?.querySelector('[data-unassigned-list]');
    if (!list) return;
    const options = projects.map(p => `<option value="${escapeText(p.project_id)}">${escapeText(p.name || p.project_id)}</option>`).join('');
    list.innerHTML = snapshot.assets.length ? snapshot.assets.map(asset => `
      <li class="flex items-center gap-2 py-1.5 border-b border-white/5" data-asset-id="${escapeText(asset.asset_id)}">
        <span class="flex-1 truncate" title="${escapeText(asset.asset_id)}">${escapeText(asset.name || asset.asset_id)}</span>
        ${sourceUnknown(asset) ? `<span class="text-amber-300 gw-type-body-sm" data-source-unknown="true" title="无可证明归属的资产，仅管理员可见">${escapeText(asset.origin_label || '来源未知')}</span>`
          : (asset.origin_label ? `<span class="text-slate-400 gw-type-body-sm" data-origin-label>${escapeText(asset.origin_label)}</span>` : '')}
        <select data-move-project style="background:#141720;color:#eddab3;border:1px solid rgba(223,195,132,.35);border-radius:6px;padding:2px 6px;" aria-label="目标项目" ${projects.length ? '' : 'disabled'}>${options || '<option value="">暂无可写项目</option>'}</select>
        <button type="button" class="hw-btn btn-size-sm" data-move-asset ${projects.length ? '' : 'disabled'}>移入项目</button>
      </li>`).join('') : '<li class="py-3 text-slate-400">没有未归属资产</li>';
    list.querySelectorAll('[data-move-asset]').forEach(button => button.addEventListener('click', () => {
      const row = button.closest('[data-asset-id]');
      moveToProject(row.dataset.assetId, row.querySelector('[data-move-project]')?.value);
    }));
    const count = entry()?.querySelector('[data-unassigned-count]');
    if (count) count.textContent = String(snapshot.total);
  }

  function ensureDialog() {
    let dialog = document.getElementById('unassignedInboxDialog');
    if (dialog) return dialog;
    dialog = document.createElement('div');
    dialog.id = 'unassignedInboxDialog';
    dialog.setAttribute('data-floating-surface', 'unassigned-inbox-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-label', '未归属资产');
    dialog.style.cssText = 'position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);';
    dialog.innerHTML = `<div data-floating-content class="bg-[#0b0d12] border border-[#dfc384]/35 rounded-xl p-4 text-[#eddab3] gw-type-body-sm" style="width:min(640px,92vw);max-height:80vh;overflow:auto;">
      <div class="flex items-center justify-between mb-2"><strong>未归属资产</strong>
        <button type="button" class="hw-btn btn-size-sm" data-floating-close data-close-inbox>关闭</button></div>
      <p class="text-slate-400 mb-2">这些资产尚未归属任何项目；系统不会自动归属，请手动选择项目移入。</p>
      <p id="unassignedInboxStatus" role="status" aria-live="polite" class="mb-1"></p>
      <ul data-unassigned-list></ul></div>`;
    dialog.querySelector('[data-close-inbox]').addEventListener('click', close);
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
    document.body.appendChild(dialog);
    return dialog;
  }

  async function open() {
    ensureDialog();
    try {
      await Promise.all([load(), loadProjects()]);
      render();
      return 'open';
    } catch (error) {
      notify(`无法读取未归属资产：${error.message}`, true);
      return 'error';
    }
  }

  function close() { document.getElementById('unassignedInboxDialog')?.remove(); }

  async function moveToProject(assetId, projectId) {
    if (busy) return 'busy';
    if (!projectId) { notify('请先选择目标项目', true); return 'no_project'; }
    busy = true;
    const versionOf = () => snapshot.assets.find(a => a.asset_id === assetId)?.asset_version;
    const post = assetVersion => request(`/api/assets/${encodeURIComponent(assetId)}/move-to-project`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project_id: projectId, expected_asset_version: assetVersion})});
    try {
      try {
        await post(versionOf());
      } catch (error) {
        if (error.status !== 409) throw error;
        await load();
        render();
        if (error.code === 'ASSET_ALREADY_ASSIGNED' || !snapshot.assets.some(a => a.asset_id === assetId)) {
          notify('该资产已被归属到项目，列表已刷新', true);
          return 'already_assigned';
        }
        try { await post(versionOf()); }
        catch (retryError) {
          if (retryError.status === 409) { await load().catch(() => {}); render(); notify('移入失败：该资产刚被其他操作修改，请稍后重试', true); return 'conflict'; }
          throw retryError;
        }
      }
      await load();
      render();
      const project = projects.find(p => p.project_id === projectId);
      notify(`已移入项目「${project?.name || projectId}」`);
      return 'moved';
    } catch (error) {
      notify(`移入项目失败：${error.message}`, true);
      return 'error';
    } finally {
      busy = false;
    }
  }

  return {open, close, refreshEntry, moveToProject, sourceUnknown, load, get snapshot() { return snapshot; }};
})();

/** The Workbench shell delegates all asset operations to the canonical asset manager. */
window.WorkbenchAssets = (() => {
  'use strict';
  let frame = null;
  let status = null;
  let categories = null;
  let createButton = null;
  let createButtonClickHandler = null;
  let requestedBoardId = '';
  const collapsedNavigationIds = new Set();
  let boundFrame = null;
  let frameLoadHandler = null;
  let observer;
  let revision = 0;
  let categorySnapshot = '';
  const SYSTEM_NAVIGATION_GROUPS = Object.freeze([
    {id: 'projects', name: '项目制片', children: ['scripts']},
    {id: 'assets', name: '托管资产', children: ['workflows']},
    {id: 'canvas-assets', name: '画布资产', children: []},
    {id: 'prompts', name: '提示词', children: []},
    {id: 'local', name: '上传资源', children: []},
    {id: 'governance', name: '治理建议', children: []},
  ]);
  const GROUPED_SYSTEM_BOARD_IDS = new Set(SYSTEM_NAVIGATION_GROUPS.flatMap(group => [group.id, ...group.children]));

  function manager() {
    try { return frame.contentDocument; } catch (_) { return null; }
  }

  // 父页状态条必须明说「未接入」并带结构化标记（用户 2026-09-21 裁决第 3 项）。
  function setVaultStatus(text) {
    const projected = text && typeof text === 'object' ? text : null;
    const value = projected?.message || text || '准备就绪';
    const kind = projected?.kind || (/未纳入当前切片/.test(value) ? 'not_integrated'
      : (/暂时不可用/.test(value) ? 'service_unavailable' : ''));
    if (!status) return;
    status.textContent = value;
    status.title = value;
    if (kind) status.setAttribute('data-gw-degradation', kind);
    else status.removeAttribute('data-gw-degradation');
    if (status.dataset) {
      if (projected?.nextAction) status.dataset.nextAction = projected.nextAction;
      else delete status.dataset.nextAction;
    }
  }

  // 把 iframe 内的显式降级同步到父页状态条，避免父页看起来「一切正常」。
  function mirrorFrameDegradation() {
    const doc = manager();
    if (!doc) return;
    const marked = doc.querySelector('[data-gw-degradation]');
    if (!marked) return;
    const kind = marked.getAttribute('data-gw-degradation') || 'not_integrated';
    const text = (marked.textContent || '').trim();
    status.textContent = text;
    status.title = text;
    status.setAttribute('data-gw-degradation', kind);
  }
  function waitForControl(selector) {
    const doc = manager();
    if (!doc) return Promise.reject(new Error('资产库暂不可访问，请刷新页面。'));
    const found = doc.querySelector(selector);
    if (found) return Promise.resolve(found);
    return new Promise((resolve, reject) => {
      const watch = new MutationObserver(() => {
        const control = doc.querySelector(selector);
        if (control) { clearTimeout(timer); watch.disconnect(); resolve(control); }
      });
      const timer = setTimeout(() => {
        watch.disconnect();
        reject(new Error('资产库尚未就绪，请检查登录状态或刷新页面。'));
      }, 10000);
      watch.observe(doc.body, {childList: true, subtree: true});
    });
  }

  function updateSidebarSelection(id) {
    const selectedId = String(id || '');
    categories?.querySelectorAll('[data-asset-board]').forEach(button => {
      const active = button.dataset.assetBoard === selectedId;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    const overview = document.getElementById('assetSidebarOverview');
    const overviewActive = selectedId === 'registry';
    overview?.classList.toggle('is-active', overviewActive);
    overview?.setAttribute('aria-pressed', String(overviewActive));
    if (overviewActive) overview?.setAttribute('aria-current', 'page');
    else overview?.removeAttribute('aria-current');
    overview?.closest('.asset-shell-overview-card')?.classList.toggle('is-active', overviewActive);
  }

  function toggleNavigationGroup(id, name, children, primaryButton, collapseButton) {
    const collapsed = !collapsedNavigationIds.has(id);
    if (collapsed) collapsedNavigationIds.add(id);
    else collapsedNavigationIds.delete(id);
    children.hidden = collapsed;
    const expanded = String(!collapsed);
    primaryButton.setAttribute('aria-expanded', expanded);
    collapseButton.setAttribute('aria-expanded', expanded);
    collapseButton.setAttribute('aria-label', `${collapsed ? '展开' : '折叠'}${name}子项`);
    collapseButton.title = collapsed ? `展开${name}子项` : `折叠${name}子项`;
  }

  async function activateBoard(id) {
    const selectedId = String(id || '');
    const current = ++revision;
    requestedBoardId = selectedId;
    updateSidebarSelection(selectedId);
    try {
      const button = await waitForControl(`#assetBoardTabs [data-tab="${CSS.escape(selectedId)}"]`);
      if (current !== revision) return;
      if (button.getAttribute('aria-selected') !== 'true') button.click();
      if (button.getAttribute('aria-selected') === 'true' && requestedBoardId === selectedId) requestedBoardId = '';
      syncCategories();
      setVaultStatus('');
    } catch (error) {
      if (current === revision && requestedBoardId === selectedId) {
        requestedBoardId = '';
        categorySnapshot = '';
        syncCategories();
      }
      setVaultStatus(error.message);
    }
  }

  function openRequestedAsset() {
    const assetId = new URLSearchParams(location.search).get('asset_id');
    if (!assetId || !frame?.contentWindow) return;
    frame.contentWindow.postMessage({ type: 'asset-global-search', asset_id: assetId }, location.origin);
  }

  function syncCategories() {
    const doc = manager();
    if (!categories || !doc) return;
    const tabs = [...doc.querySelectorAll('#assetBoardTabs .asset-board-tab')];
    const rows = tabs.map(button => ({
      id: button.dataset.tab || '',
      name: button.querySelector('span')?.textContent?.trim() || '',
      icon: button.querySelector('[data-lucide]')?.getAttribute('data-lucide') || 'folder',
      active: button.getAttribute('aria-selected') === 'true',
    })).filter(row => row.id && row.name);
    const frameSelectedId = rows.find(row => row.active)?.id || '';
    if (requestedBoardId && frameSelectedId === requestedBoardId) requestedBoardId = '';
    const selectedId = requestedBoardId || frameSelectedId;
    const sidebarRows = rows.map(row => ({...row, active: row.id === selectedId}));
    const snapshot = JSON.stringify(sidebarRows);
    if (categorySnapshot === snapshot) return;
    categorySnapshot = snapshot;
    const rowsById = new Map(sidebarRows.map(row => [row.id, row]));
    const systemEntries = SYSTEM_NAVIGATION_GROUPS.map(group => {
      const children = group.children.map(id => rowsById.get(id)).filter(Boolean);
      const source = rowsById.get(group.id) || children.shift();
      if (!source) return null;
      return {row: {...source, name: group.name}, children};
    }).filter(Boolean);
    const userRows = rows.filter(row => row.id !== 'registry' && !GROUPED_SYSTEM_BOARD_IDS.has(row.id));
    const renderGroup = (label, entries, emptyLabel = '') => {
      const group = document.createElement('div');
      group.className = 'asset-shell-nav-group';
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', label);
      const heading = document.createElement('h3');
      heading.className = 'asset-shell-nav-group-heading';
      heading.textContent = label;
      const items = document.createElement('div');
      items.className = 'asset-shell-nav-group-items';
      const createButton = (row, nested = false) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `asset-shell-nav-button tree-node-card${nested ? ' asset-shell-nav-button-sub' : ''}${row.active ? ' is-active' : ''}`;
        if (row.active) button.setAttribute('aria-current', 'page');
        button.title = row.name;
        button.dataset.assetBoard = row.id;
        button.dataset.assetBoardLevel = nested ? 'nested' : 'primary';
        const pin = document.createElement('div');
        pin.className = 'card-connector-pin';
        pin.setAttribute('aria-hidden', 'true');
        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', row.icon);
        icon.setAttribute('aria-hidden', 'true');
        const name = document.createElement('span');
        name.className = 'asset-shell-nav-label';
        name.textContent = row.name;
        button.append(pin, icon, name);
        button.addEventListener('click', () => activateBoard(row.id));
        return button;
      };
      if (entries.length) {
        for (const entry of entries) {
          const node = document.createElement('div');
          node.className = 'asset-shell-nav-node';
          const primaryButton = createButton(entry.row);
          if (entry.children.length) {
            const parentRow = document.createElement('div');
            parentRow.className = 'asset-shell-nav-parent-row';
            const collapseButton = document.createElement('button');
            const collapsed = collapsedNavigationIds.has(entry.row.id);
            const expanded = String(!collapsed);
            collapseButton.type = 'button';
            collapseButton.className = 'asset-shell-nav-collapse';
            collapseButton.dataset.navigationCollapse = entry.row.id;
            collapseButton.setAttribute('aria-expanded', expanded);
            collapseButton.setAttribute('aria-label', `${collapsed ? '展开' : '折叠'}${entry.row.name}子项`);
            collapseButton.title = collapsed ? `展开${entry.row.name}子项` : `折叠${entry.row.name}子项`;
            const collapseIcon = document.createElement('i');
            collapseIcon.setAttribute('data-lucide', 'chevron-down');
            collapseIcon.setAttribute('aria-hidden', 'true');
            collapseButton.append(collapseIcon);
            const children = document.createElement('div');
            children.className = 'asset-shell-nav-sublist';
            children.id = `asset-shell-nav-sublist-${entry.row.id}`;
            children.hidden = collapsed;
            children.setAttribute('role', 'group');
            children.setAttribute('aria-label', `${entry.row.name}子项`);
            primaryButton.className += ' asset-shell-nav-parent';
            primaryButton.setAttribute('aria-expanded', expanded);
            primaryButton.setAttribute('aria-controls', children.id);
            collapseButton.setAttribute('aria-controls', children.id);
            primaryButton.addEventListener('dblclick', () => toggleNavigationGroup(entry.row.id, entry.row.name, children, primaryButton, collapseButton));
            collapseButton.addEventListener('click', event => {
              event.stopPropagation();
              toggleNavigationGroup(entry.row.id, entry.row.name, children, primaryButton, collapseButton);
            });
            for (const child of entry.children) children.append(createButton(child, true));
            parentRow.append(primaryButton, collapseButton);
            node.append(parentRow, children);
          } else {
            node.append(primaryButton);
          }
          items.append(node);
        }
      } else if (emptyLabel) {
        const empty = document.createElement('span');
        empty.className = 'asset-shell-nav-empty';
        empty.textContent = emptyLabel;
        items.append(empty);
      }
      group.append(heading, items);
      return group;
    };
    const fragment = document.createDocumentFragment();
    fragment.append(renderGroup('系统预设', systemEntries));
    fragment.append(renderGroup('用户资源', userRows.map(row => ({row, children: []})), '尚无自定义资源'));
    const focused = categories.contains(document.activeElement)
      ? document.activeElement.dataset.assetBoard || null
      : null;
    categories.replaceChildren(fragment);
    const overview = document.getElementById('assetSidebarOverview');
    const registryActive = selectedId === 'registry';
    if (overview) {
      overview.classList.toggle('is-active', registryActive);
      overview.setAttribute('aria-pressed', String(registryActive));
      if (registryActive) overview.setAttribute('aria-current', 'page');
      else overview.removeAttribute('aria-current');
      overview.closest('.asset-shell-overview-card')?.classList.toggle('is-active', registryActive);
    }
    window.lucide?.createIcons();
    if (focused !== null) {
      categories.querySelector(`[data-asset-board="${CSS.escape(focused)}"]`)?.focus();
    }
  }

  async function openCreateAssetLibrary() {
    try {
      const button = await waitForControl('#assetBoardTabs [data-board-create]');
      button.click();
    } catch (error) { setVaultStatus(error.message); }
  }

  async function openAssetSettings() {
    try {
      const button = await waitForControl('#assetBoardTabs [data-board-settings-open]');
      button.click();
    } catch (error) { setVaultStatus(error.message); }
  }

  function toggleSmartFilter() {
    const doc = manager();
    const panel = doc?.getElementById('registryFacetPopover');
    const root = doc?.getElementById('assetManagerRoot');
    if (!panel || !root) { setVaultStatus('智能筛选尚未就绪，请刷新页面。'); return; }
    if (panel.hasAttribute('popover')) {
      if (panel.matches(':popover-open')) panel.hidePopover();
      else panel.showPopover();
      return;
    }
    root.classList.toggle('asset-registry-filters-collapsed');
  }

  async function uploadAsset() {
    try {
      const available = manager()?.querySelector('[data-localup-upload]');
      if (available) { available.click(); return; }
      const tab = await waitForControl('[data-tab="local"]');
      if (tab.getAttribute('aria-selected') !== 'true') tab.click();
      // Async navigation cannot retain a file picker's user activation.
      const upload = await waitForControl('[data-localup-upload]');
      setVaultStatus('上传资源已打开');
      upload.focus();
    } catch (error) { setVaultStatus(error.message); }
  }

  function initFrame() {
    observer?.disconnect();
    categorySnapshot = '';
    const doc = manager();
    if (!doc?.body) { setVaultStatus('资产库加载失败，请刷新页面。'); return; }
    const current = new URL(doc.URL);
    if (current.protocol === 'about:') return;
    if (current.origin === location.origin && current.pathname === '/static/episode-pipeline.html') {
      const target = new URL('/static/pages/workshop.html', location.origin);
      for (const key of ['project_id', 'pipeline_id']) {
        if (current.searchParams.has(key)) target.searchParams.set(key, current.searchParams.get(key));
      }
      target.searchParams.set('step', 'script');
      location.assign(target.href);
      return;
    }
    observer = new MutationObserver(syncCategories);
    observer.observe(doc.getElementById('assetBoardTabs') || doc.body, {childList: true, subtree: true});
    syncCategories();
    waitForControl('#assetBoardTabs [data-tab="registry"]').then(() => { syncCategories(); setVaultStatus(''); mirrorFrameDegradation(); openRequestedAsset(); }).catch(error => { setVaultStatus(error.message); });
  }
  function bindElements() {
    frame = document.getElementById('assetVaultFrame');
    status = document.getElementById('assetVaultStatus');
    categories = document.getElementById('assetCategories');
    const nextCreateButton = document.getElementById('assetSidebarCreate');
    if (nextCreateButton !== createButton) {
      createButton?.removeEventListener('click', createButtonClickHandler);
      createButton = nextCreateButton;
      createButtonClickHandler = createButton ? event => event.stopPropagation() : null;
      createButton?.addEventListener('click', createButtonClickHandler);
    }
    if (!frame) return false;
    if (boundFrame !== frame) {
      if (boundFrame && frameLoadHandler) boundFrame.removeEventListener('load', frameLoadHandler);
      frameLoadHandler = initFrame;
      frame.addEventListener('load', frameLoadHandler);
      boundFrame = frame;
    }
    return true;
  }

  function syncFrameSource() {
    if (!frame) return;
    const source = new URL(frame.dataset.src || '/static/asset-manager.html?embedded=1&vault=1', location.origin);
    const contextParams = new URLSearchParams(location.search);
    source.searchParams.delete('project_id');
    for (const key of ['pipeline_id', 'asset_id']) {
      if (contextParams.has(key)) source.searchParams.set(key, contextParams.get(key));
    }
    source.hash = location.hash;
    if (frame.dataset.gwBoundSrc !== source.href) {
      frame.dataset.gwBoundSrc = source.href;
      frame.src = source.href;
    }
  }

  // 资产库是全局入口，清除遗留工程参数，不读取或改写项目记忆。
  function settleProjectContext() {
    try {
      const url = new URL(location.href);
      if (!url.searchParams.has('project_id')) return;
      url.searchParams.delete('project_id');
      history.replaceState({}, '', url.toString());
    } catch (_) {}
  }

  function rebind() {
    window.GWUnassignedInbox?.refreshEntry();
    if (!bindElements()) return;
    categorySnapshot = '';
    mirrorFrameDegradation();
    syncFrameSource();
    void settleProjectContext();
    if (frame.contentDocument?.readyState === 'complete') initFrame();
  }

  function showIndexJobStatus(job) {
    const view = window.GWAssetJobStatus.view(job);
    setVaultStatus(view);
    return view;
  }

  // 首屏与壳层部分路由返回时都走同一个幂等绑定入口；不重复执行顶层脚本。
  rebind();
  window.addEventListener('pagehide', () => observer?.disconnect());
  return {uploadAsset, openAssetSettings, openCreateAssetLibrary, activateAssetBoard: activateBoard, toggleSmartFilter, rebind, showIndexJobStatus, indexJobStatusView: window.GWAssetJobStatus.view};
})();
