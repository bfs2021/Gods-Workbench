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

(function () {
  'use strict';
  const ROUTES = {
    'index.html': 'index.html', 'projects.html': 'projects.html', 'workshop.html': 'workshop.html',
    'production.html': 'production.html', 'storyboard.html': 'storyboard.html?view=canvas',
    'agents.html': 'agents.html', 'assets.html': 'assets.html', 'collab.html': 'collab.html',
    'workflow-workbench.html': 'workflow-workbench.html', 'settings.html': 'settings.html'
  };
  const STARTUP_PAGE_ROUTES = Object.freeze({
    index: 'index.html',
    projects: 'projects.html',
    storyboard: 'storyboard.html?view=canvas'
  });
  const WORKBENCH_PREFERENCES_KEY = 'workbench_preferences';
  // 部分路由会移除旧的 script 节点，但不会撤销浏览器已经建立的全局绑定。
  // 以稳定指纹去重，避免外部脚本重复副作用和内联 const 重复声明。
  const executedRouteScriptKeys = new Set();
  let workshopRouteBootstrapped = false;
  let globalTrashLoadPromise = null;
  let recyclerDirectoryClickTimer = 0;
  let recyclerDirectoryModalReturnFocus = null;
  const RECYCLER_DEFAULT_PATH = 'data/registry_uploads/god-recycler（系统受管默认目录）';
  // popstate 时 location 已经改变，保留实际仍挂载的控制器地址。
  let mountedRouteHref = location.href;

  function globalTrashDrawerMarkup() {
    return document.getElementById('unifiedGlobalTrashDrawer');
  }

  function setGlobalTrashDirectoryTitle(path) {
    const button = document.querySelector('[data-gw-recycler-change]');
    if (!button) return;
    const effective = path || RECYCLER_DEFAULT_PATH;
    button.title = `当前回收站路径：${effective}（双击按钮可恢复默认）`;
    button.setAttribute('aria-label', `更改回收站目录。当前路径：${effective}。双击恢复默认`);
  }

  async function loadGlobalTrashDirectoryTitle() {
    try {
      const response = await fetch('/api/storage-settings', {credentials: 'same-origin'});
      if (!response.ok) return;
      const settings = await response.json();
      const root = String(settings.dirs?.trash || '').trim();
      const separator = root.includes('\\') ? '\\' : '/';
      const mode = localStorage.getItem('gw_recycler_mode') || 'managed';
      const fallback = mode === 'source' ? '跟随素材来源盘符：<素材源目录>/god-recycler' : RECYCLER_DEFAULT_PATH;
      setGlobalTrashDirectoryTitle(root ? `${root.replace(/[\\/]+$/, '')}${separator}god-recycler` : fallback);
    } catch (_) {}
  }

  async function loadProjectsController() {
    if (window.WorkbenchProjects) return;
    const existing = [...document.scripts].find(script => script.src.includes('/static/js/controllers/projects-controller.js'));
    if (existing) {
      await new Promise(resolve => {
        if (window.WorkbenchProjects) return resolve();
        existing.addEventListener('load', resolve, {once: true});
        existing.addEventListener('error', resolve, {once: true});
      });
      return;
    }
    await new Promise(resolve => {
      const script = document.createElement('script');
      script.src = '/static/js/controllers/projects-controller.js?v=qin-526';
      script.dataset.gwGlobalTrashController = '1';
      script.onload = resolve;
      script.onerror = resolve;
      document.body.appendChild(script);
    });
  }

  async function ensureGlobalTrashDrawer() {
    if (globalTrashDrawerMarkup()) return true;
    if (!globalTrashLoadPromise) {
      globalTrashLoadPromise = (async () => {
        try {
          const response = await fetch('/static/pages/projects.html', {credentials: 'same-origin'});
          if (!response.ok) throw new Error(`回收站组件读取失败（HTTP ${response.status}）`);
          const html = await response.text();
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const drawer = doc.getElementById('unifiedGlobalTrashDrawer');
          const backdrop = doc.getElementById('unifiedGlobalTrashDrawer-backdrop');
          if (!drawer || !backdrop) throw new Error('回收站组件不完整');
          [backdrop, drawer].forEach(node => {
            node.dataset.gwGlobalTrashShared = '1';
            document.body.appendChild(document.importNode(node, true));
          });
          if (!document.querySelector('link[data-gw-global-trash-styles]')) {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = '/static/css/pages/projects.css';
            link.dataset.gwGlobalTrashStyles = '1';
            document.head.appendChild(link);
          }
          await loadProjectsController();
          return Boolean(window.WorkbenchProjects);
        } catch (error) {
          console.warn('[GW shell] global trash load failed', error);
          window.GWWorkbenchShell?.showMessage?.(error.message || '回收站暂时无法打开');
          return false;
        } finally {
          globalTrashLoadPromise = null;
        }
      })();
    }
    return globalTrashLoadPromise;
  }

  async function openGlobalTrashDrawer() {
    if (!await ensureGlobalTrashDrawer()) return false;
    if (!window.WorkbenchProjects || !window.HardwareDeck?.openDrawer) return false;
    window.WorkbenchProjects.openGlobalTrashDrawer();
    void loadGlobalTrashDirectoryTitle();
    return true;
  }

  async function storageJson(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', ...options});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail && typeof payload.detail === 'object' ? payload.detail : {};
      const error = new Error(detail.message || (typeof payload.detail === 'string' ? payload.detail : `请求失败（HTTP ${response.status}）`));
      error.status = response.status;
      error.code = detail.code || '';
      throw error;
    }
    return payload;
  }

  function createRecyclerModal(title, content, onDismiss) {
    document.getElementById('gwRecyclerModal')?.remove();
    recyclerDirectoryModalReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overlay = document.createElement('div');
    overlay.id = 'gwRecyclerModal';
    overlay.className = 'gw-recycler-modal-backdrop';
    overlay.innerHTML = `<section class="gw-recycler-modal" role="dialog" aria-modal="true" aria-labelledby="gwRecyclerModalTitle" tabindex="-1" data-floating-content>
      <header><h2 id="gwRecyclerModalTitle">${title}</h2><button type="button" data-recycler-dismiss aria-label="关闭">×</button></header>
      ${content}
    </section>`;
    document.body.appendChild(overlay);
    const dialog = overlay.querySelector('.gw-recycler-modal');
    let dismissed = false;
    const close = () => {
      if (dismissed) return;
      dismissed = true;
      document.removeEventListener('keydown', onKeydown, true);
      overlay.remove();
      recyclerDirectoryModalReturnFocus?.focus?.();
      recyclerDirectoryModalReturnFocus = null;
      onDismiss?.();
    };
    const onKeydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const items = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),[href],[tabindex]:not([tabindex="-1"])')];
      if (!items.length) { event.preventDefault(); dialog.focus(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    overlay.querySelectorAll('[data-recycler-dismiss]').forEach(button => button.addEventListener('click', close));
    document.addEventListener('keydown', onKeydown, true);
    requestAnimationFrame(() => (dialog?.querySelector('input,button') || dialog)?.focus());
    return {overlay, dialog, close};
  }

  async function applyRecyclerDirectory(path) {
    if (path) {
      let roots = await storageJson('/api/storage-settings/roots');
      try {
        await storageJson('/api/storage-settings/roots', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({path, expected_version: roots.revision})
        });
      } catch (error) {
        if (error.code !== 'ALLOWED_ROOT_ALREADY_REGISTERED') throw error;
        roots = await storageJson('/api/storage-settings/roots');
        const exactMatch = (roots.roots || []).some(root => root.active && String(root.path || '').replace(/[\\/]+$/, '') === path.replace(/[\\/]+$/, ''));
        if (!exactMatch) throw error;
      }
    }
    const current = await storageJson('/api/storage-settings');
    const dirs = {...(current.dirs || {}), trash: path};
    const updated = await storageJson('/api/storage-settings', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dirs, expected_version: current.revision})
    });
    await loadGlobalTrashDirectoryTitle();
    return updated;
  }

  async function changeRecyclerDirectory(options = {}) {
    let current;
    try {
      current = await storageJson('/api/storage-settings');
    } catch (error) {
      showRecyclerMessage('无法读取回收站设置', error.message);
      return null;
    }
    return new Promise(resolve => {
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        modal.close();
        resolve(value);
      };
      const modal = createRecyclerModal('更改回收站目录', `<div class="gw-recycler-modal-body">
        <label for="gwRecyclerPath">服务器上的绝对目录</label>
        <div class="gw-recycler-path-row"><input id="gwRecyclerPath" type="text" autocomplete="off" value="${String(current.dirs?.trash || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')}" placeholder="例如 /mnt/media/Trash 或 D:\\Media\\Trash"><button type="button" class="hw-btn" data-recycler-native>服务器文件夹选择器</button></div>
        <p class="gw-recycler-modal-help">确认后系统会先将目录登记到允许根，再保存“god-recycler”回收位置。远程会话或无图形环境可直接填写服务器目录。</p>
        <p class="gw-recycler-modal-error" role="alert" data-recycler-error hidden></p>
        <footer><button type="button" class="hw-btn" data-recycler-cancel>取消</button><button type="button" class="hw-btn hw-btn-gold" data-recycler-apply>确认更改</button></footer>
      </div>`, () => { if (!settled) { settled = true; resolve(null); } });
      const errorNode = modal.overlay.querySelector('[data-recycler-error]');
      modal.overlay.querySelector('[data-recycler-cancel]')?.addEventListener('click', () => finish(null));
      modal.overlay.querySelector('[data-recycler-native]')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await storageJson('/api/settings/directories/pick-native', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: '选择回收站目录', initial_dir: modal.overlay.querySelector('#gwRecyclerPath')?.value || null})
          });
          if (result.status === 'selected' && result.path) modal.overlay.querySelector('#gwRecyclerPath').value = result.path;
          if (result.status === 'unavailable') {
            errorNode.textContent = `服务器选择器不可用（${result.reason || '未知原因'}），请直接填写服务器绝对目录。`;
            errorNode.hidden = false;
          }
        } catch (error) {
          errorNode.textContent = `${error.message}；请直接填写服务器绝对目录。`;
          errorNode.hidden = false;
        } finally { button.disabled = false; }
      });
      modal.overlay.querySelector('[data-recycler-apply]')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        const path = String(modal.overlay.querySelector('#gwRecyclerPath')?.value || '').trim();
        if (!path || !(/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(path))) {
          errorNode.textContent = '请输入有效的服务器绝对目录。';
          errorNode.hidden = false;
          return;
        }
        button.disabled = true;
        errorNode.hidden = true;
        try {
          await applyRecyclerDirectory(path);
          finish({saved: true, path});
          if (!options.quiet) showRecyclerMessage('回收站目录已更新', `${path} 下的 god-recycler 将接收新软删除素材。`);
        } catch (error) {
          errorNode.textContent = `保存失败：${error.message}`;
          errorNode.hidden = false;
          button.disabled = false;
        }
      });
    });
  }

  function showRecyclerMessage(title, message, confirmAction = null) {
    let settled = false;
    const modal = createRecyclerModal(title, `<div class="gw-recycler-modal-body"><p>${String(message).replace(/&/g, '&amp;').replace(/</g, '&lt;')}</p><footer><button type="button" class="hw-btn hw-btn-gold" data-recycler-message-ok>${confirmAction ? '确认恢复默认' : '知道了'}</button></footer></div>`, () => {
      if (!settled) { settled = true; confirmAction?.(false); }
    });
    modal.overlay.querySelector('[data-recycler-message-ok]')?.addEventListener('click', async () => {
      if (settled) return;
      if (confirmAction) {
        const button = modal.overlay.querySelector('[data-recycler-message-ok]');
        button.disabled = true;
        try {
          await confirmAction(true);
          settled = true;
          modal.close();
        } catch (error) {
          const body = modal.overlay.querySelector('.gw-recycler-modal-body');
          const messageNode = body.querySelector('p');
          messageNode.textContent = `恢复失败：${error.message}`;
          button.disabled = false;
        }
      } else {
        settled = true;
        modal.close();
      }
    });
  }

  function confirmRecyclerDefault() {
    showRecyclerMessage('恢复默认回收站路径', '确认恢复默认回收站路径？将恢复为跟随素材源盘符。', async confirmed => {
      if (confirmed) {
        await applyRecyclerDirectory('');
        localStorage.setItem('gw_recycler_mode', 'source');
        localStorage.setItem('gw_recycler_setup_done', 'true');
        await loadGlobalTrashDirectoryTitle();
      }
    });
  }

  function bindGlobalTrash() {
    document.addEventListener('click', event => {
      const button = event.target.closest?.('#topbarUnifiedTrashBtn');
      if (button) {
        event.preventDefault();
        event.stopPropagation();
        void openGlobalTrashDrawer();
        return;
      }
      const directoryButton = event.target.closest?.('[data-gw-recycler-change]');
      if (!directoryButton) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.detail >= 2) {
        clearTimeout(recyclerDirectoryClickTimer);
        recyclerDirectoryClickTimer = 0;
        confirmRecyclerDefault();
      } else {
        clearTimeout(recyclerDirectoryClickTimer);
        recyclerDirectoryClickTimer = window.setTimeout(() => {
          recyclerDirectoryClickTimer = 0;
          void changeRecyclerDirectory();
        }, 250);
      }
    });
  }

  window.GWWorkbenchShell = Object.freeze({
    openGlobalTrashDrawer,
    changeRecyclerDirectory,
    showMessage: showRecyclerMessage,
    refreshRecyclerDirectoryTitle: loadGlobalTrashDirectoryTitle
  });
  function routeScriptKey(script, baseURI = document.baseURI) {
    const type = (script.getAttribute('type') || '').trim().toLowerCase();
    const src = script.getAttribute('src');
    if (src) return `${type}|src:${new URL(src, baseURI || location.href).href}`;
    return `${type}|inline:${script.textContent || ''}`;
  }
  function seedInitialRouteScripts() {
    document.querySelectorAll('body > script').forEach(script => {
      if (!script.src || !script.src.includes('/static/js/core/workbench-shell.js')) {
        executedRouteScriptKeys.add(routeScriptKey(script));
      }
    });
    const pageName = location.pathname.split('/').pop() || 'index.html';
    workshopRouteBootstrapped = pageName === 'workshop.html';
  }
  function readWorkbenchPreferences() {
    try {
      const parsed = JSON.parse(localStorage.getItem(WORKBENCH_PREFERENCES_KEY) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (_) {
      return {};
    }
  }
  function startupHref(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    const mapped = STARTUP_PAGE_ROUTES[raw] || raw;
    try {
      const url = new URL(mapped, location.href);
      if (url.origin !== location.origin) return '';
      const pageName = url.pathname.split('/').pop() || 'index.html';
      if (!ROUTES[pageName]) return '';
      return `${url.pathname}${url.search}${url.hash}`;
    } catch (_) {
      return '';
    }
  }
  function rememberLastVisitedPage(href = location.href) {
    const target = startupHref(href);
    if (!target) return;
    try {
      const preferences = readWorkbenchPreferences();
      preferences.last_visited_page = target;
      localStorage.setItem(WORKBENCH_PREFERENCES_KEY, JSON.stringify(preferences));
    } catch (_) {}
  }
  function applyStartupPreference() {
    const current = new URL(location.href);
    const currentPage = current.pathname.split('/').pop() || 'index.html';
    // Direct page links remain authoritative. Startup preferences only apply
    // when the configured entry point has loaded the plain home route.
    if (currentPage !== 'index.html' || current.search) return false;
    const preferences = readWorkbenchPreferences();
    const restore = preferences.restore_page !== false;
    const target = startupHref(restore ? preferences.last_visited_page : preferences.default_page || 'index');
    if (!target || target === `${current.pathname}${current.search}${current.hash}`) return false;
    void navigate(target, false);
    return true;
  }
  function standardRightDeck() {
    const deck = document.querySelector('.topbar-master-deck .recessed-deck-slot');
    if (!deck || deck.children.length < 2) return;
    const nav = deck.querySelector(':scope > .deck-nav-cluster') || deck.firstElementChild;
    if (!nav) return;
    const right = deck.querySelector(':scope > .gw-shell-right') || document.createElement('div');
    const needsMarkup = !right.classList.contains('gw-shell-right');
    right.className = 'gw-shell-right';
    right.innerHTML = '<div class="gw-shell-fader"><span>FLUX</span><div class="hw-fader-track-horizontal gw-shell-fader-track"><div class="hw-fader-glow-bar" data-gw-gpu-util-fill style="width:0%"></div><div class="hw-fader-thumb-3d" data-gw-gpu-util-thumb style="left:0%"></div></div><span class="text-amber-300" data-gw-gpu-util title="真实数据源：GET /api/observability/health 的 gpu_telemetry（读取中）">读取中…</span></div><div class="gw-shell-fader"><span>VRAM</span><div class="hw-fader-track-horizontal gw-shell-fader-track"><div class="hw-fader-glow-bar" data-gw-gpu-vram-fill style="width:0%;background:linear-gradient(90deg,#38bdf8,#2dd4bf)"></div><div class="hw-fader-thumb-3d" data-gw-gpu-vram-thumb style="left:0%"></div></div><span class="text-amber-300" data-gw-gpu-vram title="真实数据源：GET /api/observability/health 的 gpu_telemetry（读取中）">读取中…</span></div><span class="h-4 w-px bg-white/10"></span><span class="gw-type-body-md font-mono text-[#dfc384]">CF</span><button type="button" class="hw-slide-toggle" data-gw-comfy-toggle aria-pressed="false" aria-label="切换 CF 后台控制"><div class="hw-slide-peg"></div></button><span class="h-4 w-px bg-white/10"></span><button type="button" id="topbarUnifiedTrashBtn" class="gw-shell-trash btn-hw-round" title="统一回收站" aria-label="打开统一回收站"><span class="btn-hw-round-inner"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></span><span id="topbarTrashBadge" class="gw-shell-trash-badge" hidden>0</span></button><div class="h-4 w-px bg-white/10"></div><div class="hw-avatar-keycap btn-hw-round" title="认证状态未接入：点击打开认证中心查看真实登录状态" role="button" tabindex="0" aria-label="打开认证中心" data-gw-identity="unverified"><div class="hw-avatar-keycap-inner btn-hw-round-inner"><i data-lucide="shield-check" class="w-4 h-4 text-[#eddab3]"></i></div><span class="hw-avatar-keycap-status"></span></div>';
    if (needsMarkup) {
      [...deck.children].forEach(child => {
        if (child !== nav && child !== right) child.remove();
      });
      deck.appendChild(right);
    } else {
      [...deck.children].forEach(child => {
        if (child !== nav && child !== right) child.remove();
      });
    }
    // 顶栏推子是后注入的：用最近一次真实 GPU 读数立即回放，
    // 否则会一直停在「读取中」直到 15s 周期刷新（fake-degradation 空窗）。
    try {
      if (window.HardwareDeck && typeof window.HardwareDeck.applyGpuTelemetry === 'function') {
        window.HardwareDeck.applyGpuTelemetry(window.HardwareDeck.lastGpuCheck || null);
      }
    } catch (e) {}
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }
  function injectTrash() {
    const deck = document.querySelector('.topbar-master-deck .recessed-deck-slot');
    if (!deck || deck.querySelector('.gw-shell-trash') || document.querySelector('#topbarUnifiedTrashBtn')) return;
    const right = deck.lastElementChild;
    if (!right) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.id = 'topbarUnifiedTrashBtn';
    button.className = 'gw-shell-trash btn-hw-round';
    button.title = '统一回收站';
    button.setAttribute('aria-label', '打开统一回收站');
    button.innerHTML = '<span class="btn-hw-round-inner"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></span><span class="gw-shell-trash-badge" hidden>0</span>';
    const divider = document.createElement('span');
    divider.className = 'h-4 w-px bg-white/10';
    right.insertBefore(divider, right.firstChild);
    right.insertBefore(button, right.firstChild);
    if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons();
  }
  function syncNavPills(href = location.href) {
    const url = new URL(href, location.href);
    const activeName = url.pathname.split('/').pop() || 'index.html';
    const settingsActive = activeName === 'settings.html' || (activeName === 'index.html' && url.searchParams.get('view') === 'settings');
    document.querySelectorAll('.topbar-master-deck .nav-pill-btn').forEach(item => {
      const itemHref = item.getAttribute('href') || '';
      let active = false;
      if (item.id === 'navPillSettings') active = settingsActive;
      else if (item.id === 'navPillDashboard') active = !settingsActive && activeName === 'index.html';
      else if (itemHref) {
        const target = new URL(itemHref, location.href);
        const targetName = target.pathname.split('/').pop() || 'index.html';
        active = targetName === activeName;
        if (targetName === 'index.html' && target.searchParams.get('view') === 'settings') active = settingsActive;
      }
      item.classList.toggle('pill-capsule-active', active);
      item.classList.toggle('pill-capsule-inactive', !active);
      if (item.matches('button')) item.setAttribute('aria-pressed', String(active));
    });
  }

  let comfyBusy = false;
  let comfyLastState = null;
  let comfyLastFailure = null;
  let comfyPollTimer = null;
  function notifyComfy(message, isError) {
    if (window.showWorkbenchToast) { window.showWorkbenchToast(message, isError); return; }
    if (window.showToast) { window.showToast(message, isError); return; }
    let notice = document.getElementById('gwComfyNotice');
    if (!notice) { notice = document.createElement('div'); notice.id = 'gwComfyNotice'; notice.setAttribute('role','alert'); notice.style.cssText='position:fixed;top:90px;right:20px;z-index:9999;background:#151515;color:#dfc384;padding:12px;border:1px solid #dfc384'; document.body.appendChild(notice); }
    notice.textContent = message;
    notice.hidden = false;
    clearTimeout(notice._timer);
    notice._timer = setTimeout(() => { notice.hidden = true; }, 5000);
  }
  function authReady() { return Boolean(window.GWAuthGate?.isAuthenticated()); }
  function renderComfyState(data) {
    comfyLastState = data;
    const online = data.status === 'online' && data.highlight === true;
    document.querySelectorAll('[data-gw-comfy-toggle]').forEach(toggle => {
      toggle.classList.toggle('active', online);
      toggle.setAttribute('aria-pressed', String(online));
      // 权限由点击反馈和服务端检查处理；不要用 disabled 吞掉交互。
      toggle.disabled = false;
      toggle.setAttribute('aria-busy', String(comfyBusy));
      toggle.dataset.gwComfyStatus = data.status || 'unknown';
      toggle.title = data.code === 'COMFY_START_FAILED' ? `${data.message || 'CF 启动失败'}；点击重试启动` : data.status === 'starting' ? 'CF 正在启动，点击取消启动' : data.status === 'unknown' ? 'CF 状态未知' : online ? 'CF 运行中，点击关闭' : 'CF 未运行，点击后台启动';
      if (!authReady()) toggle.title = '请先登录后读取/控制 CF，点击头像打开认证中心';
      else if (window.HardwareDeck?.authState?.principal?.role !== 'admin') toggle.title = 'CF 状态只读；启停仅限管理员';
      const label = toggle.previousElementSibling;
      if (label && label.textContent.trim() === 'CF') label.classList.add('text-[#dfc384]');
    });
  }
  async function readComfyState() {
    if (!authReady()) {
      const error = new Error('请先登录后读取 CF 状态'); error.status = 401; throw error;
    }
    const authRevision = window.GWAuthGate?.revision;
    const response = await fetch('/api/god_workflow/comfy-control', {credentials:'same-origin'});
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) window.GWAuthGate?.invalidate(401, authRevision, '/api/god_workflow/comfy-control');
    if (!response.ok) { const error = new Error(data.detail?.message || `CF 状态读取失败（HTTP ${response.status}）`); error.status = response.status; throw error; }
    return data;
  }
  async function pollComfyState() {
    if (comfyBusy || !authReady() || window.GWAuthGate?.paused?.has('/api/god_workflow/comfy-control')) {
      if (!authReady()) renderComfyState({status:'unknown', highlight:false});
      return;
    }
    comfyBusy = true;
    try {
      const data = await readComfyState();
      renderComfyState(data);
      const failure = data.code === 'COMFY_START_FAILED' ? `${data.code}:${data.exit_code}` : null;
      if (failure && failure !== comfyLastFailure) notifyComfy(data.message || 'CF 启动进程已退出，请检查本地启动环境', true);
      comfyLastFailure = failure;
    }
    catch (error) {
      renderComfyState({status:'unknown', highlight:false});
      document.querySelectorAll('[data-gw-comfy-toggle]').forEach(t => { t.title = `CF 状态未知：${error.message || '读取失败'}；点击重试控制`; });
    }
    finally { comfyBusy = false; document.querySelectorAll('[data-gw-comfy-toggle]').forEach(t => { t.disabled = false; t.setAttribute('aria-busy', 'false'); }); }
  }
  async function comfyControl(toggle, requestedAction = null) {
    if (!authReady()) {
      if (!authReady()) { notifyComfy('请先登录后控制 CF', true); window.HardwareDeck?.openAccountModal?.(); }
      return;
    }
    if (window.HardwareDeck?.authState?.principal?.role !== 'admin') { notifyComfy('CF 启停仅限管理员', true); return; }
    if (comfyBusy) { notifyComfy('CF 正在读取状态或执行控制，请稍后重试', false); return; }
    window.GWAuthGate?.paused?.delete('/api/god_workflow/comfy-control');
    comfyBusy = true;
    document.querySelectorAll('[data-gw-comfy-toggle]').forEach(t => { t.disabled = false; t.setAttribute('aria-busy', 'true'); });
    try {
      // 点击前重新读服务端，不能以按钮残留样式决定启动/停止。
      const current = await readComfyState();
      const action = requestedAction === 'start'
        ? (current.highlight || current.status === 'starting' ? null : 'start')
        : current.highlight || current.status === 'starting' ? 'stop' : 'start';
      if (!action) { renderComfyState(current); return; }
      if (action === 'start') comfyLastFailure = null;
      let confirmation_token;
      if (action === 'stop' && !current.managed) {
        if (!current.can_stop || !current.confirmation_token) throw new Error('CF 进程身份未知，拒绝关闭');
        if (!window.confirm('CF 由外部启动。确认关闭已识别的本机 CF 实例？这将中断该实例正在执行的任务。')) return;
        confirmation_token = current.confirmation_token;
      }
      const authRevision = window.GWAuthGate?.revision;
      const response = await fetch('/api/god_workflow/comfy-control', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, confirmation_token})});
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) window.GWAuthGate?.invalidate(401, authRevision, '/api/god_workflow/comfy-control');
      if (data.status) renderComfyState(data);
      if (data.code === 'COMFY_START_FAILED') comfyLastFailure = `${data.code}:${data.exit_code}`;
      if (!response.ok || !data.ok) throw new Error(data.detail?.message || data.message || data.code || 'CF 控制失败');
      notifyComfy(data.status === 'starting' ? 'CF 正在启动，就绪后开关会亮起；再次点击可取消启动' : action === 'stop' ? 'CF 已确认关闭' : 'CF 已确认运行', false);
    } catch (error) {
      notifyComfy(error.message || 'CF 控制失败', true);
    } finally {
      comfyBusy = false;
      await pollComfyState();
    }
  }
  function bindComfyControl() {
    document.querySelectorAll('[data-gw-comfy-toggle]').forEach(toggle => {
      if (toggle.dataset.gwBound) return; toggle.dataset.gwBound = '1';
      toggle.addEventListener('click', () => comfyControl(toggle));
    });
    if (!comfyPollTimer) comfyPollTimer = setInterval(pollComfyState, 5000);
    pollComfyState();
    if (!window.__gwComfyAuthBound) {
      window.__gwComfyAuthBound = true;
      window.addEventListener('gw-auth-state', () => { bindComfyControl(); pollComfyState(); });
    }
  }
  // 工作流仅在用户明确点击“启动CF后继续”时请求启动，不操作顶栏DOM或误切到停止。
  window.WorkbenchComfy = Object.freeze({
    getStatus: () => authReady() ? comfyLastState?.status || 'unknown' : 'unknown',
    start: () => comfyControl(null, 'start'),
  });
  function loadWorkbenchShortcuts() {
    if (window.WorkbenchShortcuts?.bind) {
      window.WorkbenchShortcuts.bind();
      return;
    }
    if (document.querySelector('script[data-gw-shortcuts]')) return;
    const host = document.head || document.body;
    if (!host || !document.createElement) return;
    const script = document.createElement('script');
    script.src = '/static/js/modules/shortcuts.js';
    script.dataset.gwShortcuts = '1';
    script.onload = () => window.WorkbenchShortcuts?.bind?.();
    host.appendChild(script);
  }
  function loadWorkbenchOmnibar() {
    if (window.GWOmnibar?.init) {
      window.GWOmnibar.init();
      return;
    }
    const host = document.head || document.body;
    if (!host || !document.createElement) return;
    function appendOmnibarScript() {
      if (document.querySelector('script[data-gw-omnibar]')) return;
      const script = document.createElement('script');
      script.src = '/static/js/core/omnibar.js';
      script.async = false;
      script.dataset.gwOmnibar = '1';
      script.onload = () => window.GWOmnibar?.init?.();
      host.appendChild(script);
    }
    let stylesheet = document.querySelector('link[data-gw-omnibar-styles]');
    if (!stylesheet) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/static/css/components/omnibar.css';
      link.dataset.gwOmnibarStyles = '1';
      host.appendChild(link);
      stylesheet = document.querySelector('link[data-gw-omnibar-styles]');
    }
    if (stylesheet?.sheet) appendOmnibarScript();
    else if (stylesheet) {
      stylesheet.addEventListener('load', appendOmnibarScript, {once: true});
      stylesheet.addEventListener('error', appendOmnibarScript, {once: true});
    }
  }
  function init() {
    seedInitialRouteScripts();
    loadWorkbenchShortcuts();
    loadWorkbenchOmnibar();
    standardRightDeck();
    bindComfyControl();
    injectTrash();
    syncNavPills(location.href);
  }
  function shellWorkspace() { return targetWorkspace(document); }
  function targetWorkspace(doc) {
    return doc.querySelector('.app-shell-workspace')
      || doc.querySelector('#homeWorkspace, #workspace, #agentsWorkspace')
      || doc.querySelector('.topbar-master-deck')?.nextElementSibling
      || doc.querySelector('#homeWorkspace, #workspace, #agentsWorkspace, main, .app-shell-workspace')
      || doc.body;
  }
  function firstSidebar(workspace) { return workspace?.querySelector(':scope > aside') || null; }
  function syncRouteDeck(doc) {
    const currentHeader = document.querySelector('.topbar-master-deck');
    const nextHeader = doc.querySelector('.topbar-master-deck');
    const currentDeck = currentHeader?.querySelector(':scope > .recessed-deck-slot');
    const nextDeck = nextHeader?.querySelector(':scope > .recessed-deck-slot');
    if (!currentHeader || !nextHeader || !currentDeck || !nextDeck) return;

    // 顶栏上层同样包含页级元素（例如工程标题、首页专用渐变定义）。
    // 只替换上层和下层内容，保留 header 外壳，确保路由后的 deck 与整页加载一致。
    const currentUpper = [...currentHeader.children].find(node =>
      node !== currentDeck && !node.classList.contains('absolute')
    );
    const nextUpper = [...nextHeader.children].find(node =>
      node !== nextDeck && !node.classList.contains('absolute')
    );
    if (currentUpper && nextUpper) {
      currentUpper.replaceWith(document.importNode(nextUpper, true));
    }
    currentDeck.replaceWith(document.importNode(nextDeck, true));
  }
  function syncRouteBody(doc) {
    if (!doc.body) return;
    document.body.className = doc.body.className;
    [...document.body.attributes].forEach(attribute => {
      if (attribute.name.startsWith('data-')) document.body.removeAttribute(attribute.name);
    });
    [...doc.body.attributes].forEach(attribute => {
      if (attribute.name.startsWith('data-')) document.body.setAttribute(attribute.name, attribute.value);
    });
  }
  async function runRouteScripts(doc) {
    document.querySelectorAll('script[data-gw-route-script]').forEach(script => script.remove());
    const scripts = [...doc.querySelectorAll('body > script')].filter(script => {
      const src = script.getAttribute('src') || '';
      return (src && !src.includes('workbench-shell.js') && src.includes('/static/')) || (!src && script.textContent.trim());
    });
    for (const source of scripts) {
      const key = routeScriptKey(source, doc.baseURI || location.href);
      const src = source.getAttribute('src') || '';
      // 外部控制器/页面模块在工作区被替换后必须重新执行；否则返回旧页面时
      // 只有新 DOM，没有旧控制器的事件与状态绑定。内联脚本仍按指纹去重，
      // 避免重复声明 const/let 造成语法错误。
      const rebindOnRoute = Boolean(src) && (src.includes('/controllers/') || src.includes('/modules/'));
      if (!rebindOnRoute && executedRouteScriptKeys.has(key)) continue;
      const script = document.createElement('script');
      [...source.attributes].forEach(attribute => {
        if (attribute.name !== 'src') script.setAttribute(attribute.name, attribute.value);
      });
      script.dataset.gwRouteScript = '1';
      if (src) {
        script.src = new URL(src, doc.baseURI || location.href).href;
        script.async = false;
        const loaded = await new Promise(resolve => {
          script.onload = () => resolve(true);
          script.onerror = () => resolve(false);
          document.body.appendChild(script);
        });
        if (loaded && !rebindOnRoute) executedRouteScriptKeys.add(key);
      } else {
        script.textContent = source.textContent;
        document.body.appendChild(script);
        executedRouteScriptKeys.add(key);
      }
    }
  }
  function disposeRouteController(href, nextHref = href) {
    const pageName = new URL(href, location.href).pathname.split('/').pop() || 'index.html';
    const nextPageName = new URL(nextHref, location.href).pathname.split('/').pop() || 'index.html';
    if (pageName === 'workflow-workbench.html') {
      window.WorkbenchWorkflow?.dispose?.();
    }
    if (pageName === nextPageName) return;
    if (pageName === 'workshop.html' || pageName === 'collab.html') {
      try {
        if (pageName === 'workshop.html') window.WorkbenchWorkshop?.dispose?.();
        if (pageName === 'collab.html') window.WorkbenchCollab?.dispose?.();
      } catch (error) {
        console.warn('[GW shell] route controller dispose failed', pageName, error);
      }
    }
  }

  function refreshRouteController(href) {
    const pageName = new URL(href, location.href).pathname.split('/').pop() || 'index.html';
    const rebinders = {
      'index.html': window.WorkbenchHome?.rebind,
      'projects.html': window.WorkbenchProjects?.rebind,
      'production.html': window.WorkbenchProduction?.rebind,
      'workshop.html': window.WorkbenchWorkshop?.rebind,
      'storyboard.html': window.WorkbenchStoryboard?.rebind,
      'agents.html': window.WorkbenchAgents?.rebind,
      'assets.html': window.WorkbenchAssets?.rebind,
      'collab.html': window.WorkbenchCollab?.rebind,
      'workflow-workbench.html': window.WorkbenchWorkflow?.rebind,
    };
    try { rebinders[pageName]?.(); } catch (error) {
      console.warn('[GW shell] route controller rebind failed', pageName, error);
    }
    if (pageName === 'production.html') {
      window.WorkbenchProduction?.initRouting?.();
      window.WorkbenchProduction?.renderSceneCatalog?.();
      window.WorkbenchProduction?.renderShots?.();
    }
    if (pageName === 'workshop.html') {
      const title = document.getElementById('workshopProjectTitle');
      const projectName = localStorage.getItem('workspace_project_name');
      if (title && projectName) title.textContent = `${projectName} · 影视工坊流水线`;
    }
  }

  async function navigate(href, push) {
    const url = new URL(href, location.href);
    if (url.origin !== location.origin) return;
    const currentWorkspace = shellWorkspace();
    if (!currentWorkspace) { location.href = url.href; return; }
    try {
      const response = await fetch(url.href, { credentials: 'same-origin', headers: { 'X-GW-Partial': '1' } });
      if (!response.ok) throw new Error('route ' + response.status);
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const nextWorkspace = targetWorkspace(doc);
      syncRouteBody(doc);
      document.querySelectorAll('style[data-gw-route-style],link[data-gw-route-style]').forEach(node => node.remove());
    const routeStyles = [...doc.head.querySelectorAll('style,link[rel="stylesheet"]')];
      for (const node of routeStyles) {
        if (node.tagName === 'SCRIPT') continue;
        const copy = node.cloneNode(true); copy.dataset.gwRouteStyle = '1'; document.head.appendChild(copy);
      }
      // 页面使用 Tailwind utility class 时，强制刷新不会经过壳层的脚本
      // 重放路径；直接导航必须让本地 Tailwind 编译器在页面自己的 head 中运行。
      const tailwindScript = [...doc.head.querySelectorAll('script[src*="tailwindcss-cdn.js"]')][0];
      if (tailwindScript && !document.querySelector('script[data-gw-tailwind-route]')) {
        const copy = document.createElement('script');
        copy.src = new URL(tailwindScript.getAttribute('src'), doc.baseURI || location.href).href;
        copy.dataset.gwTailwindRoute = '1';
        document.head.appendChild(copy);
      }
      // 无论目标是 div 工作区还是直接 main，始终替换完整根节点。
      // 这样侧边栏、页面专属根 ID 与首页控制条都不会跨页残留。
      syncRouteDeck(doc);
      disposeRouteController(mountedRouteHref, url.href);
      mountedRouteHref = url.href;
      if (document.querySelector('[data-gw-global-trash-shared="1"]')) {
        nextWorkspace.querySelectorAll('#unifiedGlobalTrashDrawer, #unifiedGlobalTrashDrawer-backdrop').forEach(node => node.remove());
      }
      currentWorkspace.replaceWith(document.importNode(nextWorkspace, true));
      if (push) history.pushState({}, '', url.href);
      if (doc.title) document.title = doc.title;
      standardRightDeck();
      bindComfyControl();
      await runRouteScripts(doc);
      refreshRouteController(url.href);
      syncNavPills(url.href);
      window.dispatchEvent(new CustomEvent('gw:route-loaded', { detail: { href: url.href } }));
      if (window.lucide?.createIcons) window.lucide.createIcons();
      rememberLastVisitedPage(url.href);
    } catch (error) {
      console.warn('[GW shell] partial route failed, falling back to full navigation', error);
      location.href = url.href;
    }
  }
  function bindNavigation() {
    document.addEventListener('click', event => {
      const link = event.target.closest('.nav-pill-btn[href], .tree-node-card[href]');
      if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (link.matches('.tree-node-card') && !firstSidebar(shellWorkspace())?.contains(link)) return;
      const rawHref = link.getAttribute('href') || '';
      const targetUrl = new URL(rawHref, location.href);
      if (targetUrl.origin !== location.origin || !targetUrl.pathname.startsWith('/static/pages/') || !ROUTES[targetUrl.pathname.split('/').pop()] || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
      event.preventDefault();
      const target = targetUrl.pathname.endsWith('/index.html') && targetUrl.searchParams.get('view') === 'settings' ? 'settings.html' : link.href;
      navigate(target, true);
    });
    window.addEventListener('popstate', () => navigate(location.href, false));
  }
  function bindSelection() {
    const selector = '.tree-node-card';
    let observer;
    function prepare() {
      const aside = firstSidebar(shellWorkspace());
      if (!aside) return;
      aside.querySelectorAll(selector).forEach(item => {
        if (!item.matches('a, button')) {
          item.setAttribute('role', 'button');
          item.tabIndex = 0;
        }
        if (!item.matches('a')) {
          const pressed = String(item.classList.contains('active'));
          if (item.getAttribute('aria-pressed') !== pressed) item.setAttribute('aria-pressed', pressed);
        }
      });
    }
    function observeSidebar() {
      observer?.disconnect();
      prepare();
      const aside = firstSidebar(shellWorkspace());
      if (!aside) return;
      observer = new MutationObserver(prepare);
      observer.observe(aside, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    }
    document.addEventListener('click', event => {
      const target = event.target.closest(selector);
      const aside = firstSidebar(shellWorkspace());
      if (!target || !aside?.contains(target) || target.matches('[disabled], [aria-disabled="true"]') || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      aside.querySelectorAll(selector).forEach(item => {
        item.classList.toggle('active', item === target);
      });
    }, true);
    document.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const target = event.target.closest(selector);
      if (!target || event.target !== target || target.matches('a, button') || !firstSidebar(shellWorkspace())?.contains(target)) return;
      event.preventDefault();
      target.click();
    });
    window.addEventListener('gw:route-loaded', observeSidebar);
    observeSidebar();
  }
  const oldInit = init;
  init = function () {
    oldInit();
    const redirected = applyStartupPreference();
    if (!redirected) rememberLastVisitedPage();
    bindNavigation();
    bindSelection();
    bindGlobalTrash();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
