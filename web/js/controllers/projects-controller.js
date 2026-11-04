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
 * Gods' Workbench - 项目中心控制器 (projects-controller.js)
 * 提供看板模式 (Kanban) 与列表模式 (Table) 双视图切换、分类筛选、实时检索与后端项目 CRUD 对接
 */

window.WorkbenchProjects = (function () {

  // 统一「无后端时显式降级」工具（复用 window.GWDegradation；页面已先加载该脚本）。
  const degradationApi = () => window.GWDegradation || null;

  const degradationMessage = (kind, status) => {
    const api = degradationApi();
    if (api) {
      if (kind === 'not_integrated') return `${api.NOT_INTEGRATED_MESSAGE}（HTTP ${status}）`;
      if (kind === 'service_unavailable') return `${api.SERVICE_UNAVAILABLE_MESSAGE}（HTTP ${status}）`;
    }
    if (kind === 'not_integrated') return `该功能尚未接入后端（未纳入当前切片）（HTTP ${status}）`;
    if (kind === 'service_unavailable') return `后端服务暂时不可用，请稍后重试（HTTP ${status}）`;
    return `请求失败（HTTP ${status}）`;
  };

  async function classifyFetchFailure(res) {
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    const api = degradationApi();
    if (api && typeof api.classifyResponse === 'function') {
      const verdict = api.classifyResponse(res, data);
      return { kind: verdict.kind, status: verdict.status, message: verdict.message, detail: data };
    }
    const detailValue = data ? data.detail : undefined;
    const isEnvelope = Boolean(detailValue) && typeof detailValue === 'object';
    const isDefaultText = typeof detailValue === 'string' && /^(not found|not implemented)$/i.test(detailValue.trim());
    let kind = 'error';
    if (res.status === 503) kind = 'service_unavailable';
    else if ([404, 501].indexOf(res.status) !== -1 && !isEnvelope && !(typeof detailValue === 'string' && detailValue.trim() && !isDefaultText)) kind = 'not_integrated';
    return { kind: kind, status: res.status, message: degradationMessage(kind, res.status), detail: data };
  }

  // 提取服务端响应或异常中的中文可理解错误文本，支持标准错误包、字符串与状态码语义
  function extractErrorMessage(error, res) {
    if (error) {
      if (typeof error.detail === 'string' && error.detail.trim()) {
        return error.detail.trim();
      }
      if (error.detail && typeof error.detail === 'object') {
        if (Array.isArray(error.detail)) {
          const msgs = error.detail.map(item => item.msg || item.type).filter(Boolean);
          if (msgs.length) return msgs.join('；');
        } else if (typeof error.detail.message === 'string' && error.detail.message.trim()) {
          return error.detail.message.trim();
        }
      }
      if (typeof error.message === 'string' && error.message.trim()) {
        return error.message.trim();
      }
    }
    const api = degradationApi();
    if (res) {
      if (res.status === 403) {
        return (api?.FORBIDDEN_MESSAGE || '权限不足，已切换为只读模式') + '（HTTP 403）';
      }
      if (res.status === 409) {
        return (api?.CONFLICT_MESSAGE || '版本冲突，请刷新并解决并发差异后重试') + '（HTTP 409）';
      }
      return `项目排期保存失败（HTTP ${res.status}）`;
    }
    return '项目排期保存失败';
  }

  function degradationNoticeHtml(degradation, extraHint) {
    const api = degradationApi();
    const hint = extraHint ? `<div class="mt-1 gw-type-body-sm text-slate-500">${escHtml(extraHint)}</div>` : '';
    if (api && typeof api.noticeHtml === 'function') {
      return api.noticeHtml(degradation.kind, degradation.message) + hint;
    }
    return `<div class="bay-inset p-4 rounded-xl border border-white/10 text-center gw-type-body-sm font-mono text-slate-400" role="status" data-gw-degradation="${escHtml(degradation.kind)}"><span class="block">${escHtml(degradation.message)}</span>${hint}</div>`;
  }

  // HTML 转义（页面渲染占位文案时使用；幂等，不改变已有渲染路径）。
  function escHtml(value) {
    return String(value === undefined || value === null ? '' : value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // 立项能力状态位：只按真实创建结果写，绝不断言未验证的端点存在性。
  function setDispatchStatus(kind, detail) {
    const label = document.getElementById('projectDispatchStatus');
    const led = document.getElementById('projectDispatchLed');
    if (!label) return;
    const map = {
      ok: { text: '已接入', cls: 'gw-type-body-sm font-mono text-emerald-400', led: 'w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1' },
      not_integrated: { text: '未接入', cls: 'gw-type-body-sm font-mono text-amber-300', led: 'w-1.5 h-1.5 rounded-full bg-amber-400 mr-1 animate-pulse' },
      service_unavailable: { text: '暂不可用', cls: 'gw-type-body-sm font-mono text-amber-300', led: 'w-1.5 h-1.5 rounded-full bg-amber-400 mr-1 animate-pulse' }
    };
    const view = map[kind] || { text: '待命', cls: 'gw-type-body-sm font-mono text-slate-400', led: 'w-1.5 h-1.5 rounded-full bg-slate-500 mr-1' };
    label.className = view.cls;
    label.textContent = view.text;
    if (detail) label.title = detail;
    if (kind) label.setAttribute('data-gw-degradation', kind);
    else label.removeAttribute('data-gw-degradation');
    if (led) led.className = view.led;
  }

  'use strict';

  const state = {
    projects: [],
    filterType: 'all',
    filterScope: 'active', // 'active' | 'archived' | 'trash'
    searchQuery: '',
    currentView: 'kanban',
    activeProjectId: null,
    pendingAction: null,
    counts: { active: 0, archived: 0, trash: 0 },
    projectsDegradation: null
  };

  // 全局统一回收站数据状态池（工程、素材资产、工程画布集中隔离）
  const globalTrashState = {
    currentTab: 'projects', // 'projects' | 'assets' | 'canvases'
    projects: [],
    assets: [],
    canvases: [],
    registryRevision: null,
    initialized: false,
    // 显式降级状态：null 表示治理总览已如实应答；否则记 { kind, message }。
    degradation: null
  };

  const esc = str => String(str ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // 原始项目日期路由使用毫秒时间戳；datetime-local 不带时区，按本地时间转换。
  const dateInputTimestamp = value => {
    const text = String(value || '').trim();
    if (!text) return null;
    const numeric = Number(text);
    if (Number.isFinite(numeric) && numeric > 0) return numeric;
    const timestamp = new Date(text).getTime();
    return Number.isFinite(timestamp) ? timestamp : null;
  };
  const dateInputValue = value => {
    const timestamp = Number(value || 0);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
    const date = new Date(timestamp < 1e12 ? timestamp * 1000 : timestamp);
    if (Number.isNaN(date.getTime())) return '';
    const pad = number => String(number).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  const formatTime = ts => {
    if (!ts) return '近期';
    const date = new Date(typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : ts);
    return isNaN(date.getTime()) ? '近期' : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  };

  // 黑金拟物 Toast 提示消息
  function showToast(message, type = 'info') {
    const container = document.getElementById('workbenchToastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    const isSuccess = type === 'success';
    const isWarning = type === 'warning';
    const isDanger = type === 'danger' || type === 'error';

    const borderColor = isSuccess ? 'border-emerald-500/40' : (isWarning ? 'border-amber-500/40' : (isDanger ? 'border-red-500/40' : 'border-[#dfc384]/40'));
    const glowColor = isSuccess ? 'rgba(52,211,153,0.2)' : (isWarning ? 'rgba(245,158,11,0.2)' : (isDanger ? 'rgba(239,68,68,0.25)' : 'rgba(223,195,132,0.2)'));
    const iconName = isSuccess ? 'check-circle' : (isWarning ? 'alert-circle' : (isDanger ? 'alert-triangle' : 'info'));
    const textColor = isSuccess ? 'text-emerald-400' : (isWarning ? 'text-amber-400' : (isDanger ? 'text-red-400' : 'text-[#eddab3]'));

    const text = (typeof message === 'object' && message !== null)
      ? (message.message || message.detail || JSON.stringify(message))
      : String(message ?? '');

    toast.className = `bay-inset p-2.5 px-3.5 rounded-xl border ${borderColor} gw-type-body-sm font-mono flex items-center space-x-2.5 shadow-2xl transition-all duration-300 pointer-events-auto opacity-0 translate-y-2`;
    toast.style.boxShadow = `0 8px 24px rgba(0,0,0,0.9), 0 0 12px ${glowColor}`;
    toast.style.background = 'linear-gradient(135deg, #181b24 0%, #0c0d12 100%)';
    toast.innerHTML = `
      <i data-lucide="${iconName}" class="w-4 h-4 ${textColor} shrink-0"></i>
      <span class="text-slate-200">${esc(text)}</span>
    `;
    container.appendChild(toast);
    window.lucide?.createIcons();

    // 入场动画
    requestAnimationFrame(() => {
      toast.classList.remove('opacity-0', 'translate-y-2');
      toast.classList.add('opacity-100', 'translate-y-0');
    });

    // 2.8秒后自动淡出并移除
    setTimeout(() => {
      toast.classList.remove('opacity-100', 'translate-y-0');
      toast.classList.add('opacity-0', '-translate-y-2');
      setTimeout(() => toast.remove(), 300);
    }, 2800);
  }

  // 1. 加载项目数据与状态统计
  async function load() {
    window.GWProjectArtifactLinks?.invalidate();
    try {
      let targetUrl = '/api/asset-registry/projects?archived=false';
      if (state.filterScope === 'trash') {
        targetUrl = '/api/asset-registry/projects?deleted=true';
      } else if (state.filterScope === 'archived') {
        targetUrl = '/api/asset-registry/projects?archived=true';
      }

      const res = await fetch(targetUrl, { credentials: 'same-origin' });
      if (!res.ok) {
        // 显式降级：接口未接入 / 服务不可用 / 请求失败一律如实标记并清空，
        // 绝不回退 getDemoProjects() 之类的伪造数据。
        const failure = await classifyFetchFailure(res);
        state.projects = [];
        state.projectsDegradation = { kind: failure.kind, message: failure.message };
      } else {
        const data = await res.json();
        const list = data?.projects || data;
        if (Array.isArray(list) && list.length > 0) {
          // 契约对齐：项目中心 API 的稳定实体 ID 字段为 project_id（见 docs/contracts/PROJECTS-HUB-INTERFACE-CATALOG.yaml
          // 与 docs/fixtures/projects-hub-list-active.json）；此处统一归一化为前端内部使用的 id，
          // 避免后端返回 project_id 时 p.id 为 undefined 导致渲染报错。
          state.projects = list.map(p => ({ ...p, id: p.id || p.project_id }));
          state.projectsDegradation = null;
        } else {
          // 后端如实应答「确实为空」：保留空态，不伪造数据。
          state.projects = [];
          state.projectsDegradation = null;
        }
      }

      // 异步读取活跃、已归档与回收站计数
      fetchCounts();
    } catch (e) {
      // 网络异常：服务不可用（可恢复），同样不得装载离线伪造数据。
      console.warn('项目中心读取失败（网络异常）:', e);
      state.projects = [];
      state.projectsDegradation = { kind: 'service_unavailable', message: degradationMessage('service_unavailable', 0) };
      updateCountBadges(null, null, null);
    }

    // URL 中的稳定项目 ID 优先，保证从排期等入口进入时选中对应项目。
    try {
      const requestedProjectId = new URLSearchParams(window.location.search).get('project_id');
      const savedActive = localStorage.getItem('workspace_project_id');
      if (requestedProjectId && state.projects.some(p => String(p.id) === requestedProjectId)) {
        state.activeProjectId = requestedProjectId;
        localStorage.setItem('workspace_project_id', requestedProjectId);
      } else if (savedActive && state.projects.some(p => String(p.id) === savedActive)) {
        state.activeProjectId = savedActive;
      } else if (state.projects.length > 0) {
        state.activeProjectId = state.projects[0].id;
      } else {
        state.activeProjectId = null;
      }
    } catch (e) {}

    // 项目中心是选择器而非项目视图：消费完入口参数后即刻移除，地址栏不谎报上下文。
    stripOwnProjectParam();

    render();
  }

  function stripOwnProjectParam() {
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has('project_id')) return;
      url.searchParams.delete('project_id');
      window.history.replaceState({}, '', url);
    } catch (e) {}
  }

  async function fetchCounts() {
    try {
      const [resAct, resArc, resTra] = await Promise.all([
        fetch('/api/asset-registry/projects?archived=false', { credentials: 'same-origin' }),
        fetch('/api/asset-registry/projects?archived=true', { credentials: 'same-origin' }),
        fetch('/api/asset-registry/projects?deleted=true', { credentials: 'same-origin' })
      ]);
      const okAll = [resAct, resArc, resTra].every(r => r && r.ok);
      if (!okAll) {
        // 显式降级：任一端点缺失或失败都分类后显示「—」，不得静默吞掉。
        const failed = [resAct, resArc, resTra].find(r => r && !r.ok);
        const failure = failed ? await classifyFetchFailure(failed) : null;
        state.countsDegradation = failure
          ? { kind: failure.kind, message: failure.message }
          : { kind: 'service_unavailable', message: degradationMessage('service_unavailable', 0) };
        updateCountBadges(null, null, null);
        return;
      }
      state.countsDegradation = null;
      const actData = await resAct.json();
      const arcData = await resArc.json();
      const traData = await resTra.json();

      const actLen = (actData?.projects || actData || []).length || 0;
      const arcLen = (arcData?.projects || arcData || []).length || 0;
      const traLen = (traData?.projects || traData || []).length || 0;

      updateCountBadges(actLen, arcLen, traLen);
    } catch (e) {
      // 网络异常同样显式降级：显示「—」，不估算、不伪造。
      state.countsDegradation = { kind: 'service_unavailable', message: degradationMessage('service_unavailable', 0) };
      updateCountBadges(null, null, null);
    }
  }

  function updateCountBadges(actLen, arcLen, traLen) {
    // 显式降级：null 代表计数不可用，显示「—」而不是伪造的 0/估算值。
    const display = v => (v === null || v === undefined ? '—' : v);
    state.counts = { active: actLen, archived: arcLen, trash: traLen };
    const elAct = document.getElementById('countActive');
    const elArc = document.getElementById('countArchived');
    const elTra = document.getElementById('countTrash');
    if (elAct) elAct.textContent = display(actLen);
    if (elArc) elArc.textContent = display(arcLen);
    if (elTra) elTra.textContent = display(traLen);

    // 同步顶栏拟物垃圾桶红点数字与抽屉项目数量（计数不可用时不显示红点）。
    const projTrash = (traLen === null || traLen === undefined) ? 0 : traLen;
    const totalTrashCount = projTrash + (globalTrashState.assets?.length || 0) + (globalTrashState.canvases?.length || 0);
    const elTopBadge = document.getElementById('topbarTrashBadge');
    if (elTopBadge) {
      elTopBadge.textContent = totalTrashCount;
      elTopBadge.style.display = totalTrashCount > 0 ? 'inline-block' : 'none';
    }
    const elDrawerCountProj = document.getElementById('trashCountProjects');
    if (elDrawerCountProj) elDrawerCountProj.textContent = display(traLen);
  }

  function getProjectTypeMeta(type) {
    switch (type) {
      case 'film':
        return { label: '院线长片', colorClass: 'text-amber-400', iconName: 'clapperboard' };
      case 'series':
        return { label: '连续短剧', colorClass: 'text-cyan-400', iconName: 'tv' };
      case 'other':
      default:
        return { label: '概念PV', colorClass: 'text-emerald-400', iconName: 'sparkles' };
    }
  }

  // 数值解析（显式降级，安全关键）：
  // 旧实现多处使用 `p.progress || 10` / `Number(p.progress) || 60` / `p.scenes || 24`：
  // `0 || 10 === 10` 会把真实的 0 进度 / 0 场景数改写成伪造值。
  // 现在：仅当字段存在且为有限数时返回数值；否则返回 null，由调用方显式降级。
  function rawNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function progressMeta(project) {
    const value = rawNumber(project && project.progress);
    return value === null
      ? { value: null, degraded: true,
          html: '<span data-gw-degradation="not_integrated" title="后端未返回该项目的 progress 字段，本页不伪造进度">未接入</span>' }
      : { value: Math.max(0, Math.min(100, value)), degraded: false, html: `<span>${Math.max(0, Math.min(100, value))}%</span>` };
  }
  function getProjectStage(p) {
    if (p.stage) return p.stage;
    const progress = p.progress || 0;
    if (progress < 25) return 'planning';
    if (progress < 50) return 'storyboard';
    if (progress < 85) return 'production';
    return 'review';
  }

  // 3. 过滤数据
  function getFilteredProjects() {
    return state.projects.filter(p => {
      // 本地生命周期状态二次校验 (若后端接口未过滤)
      if (state.filterScope === 'trash' && !p.deleted_at && !state.projects.every(item => item.deleted_at != null)) {
        // 如果是从后端特定接口获取，不做严格排他
      }
      // 类别过滤
      if (state.filterType !== 'all' && p.project_type !== state.filterType) {
        return false;
      }
      // 检索词
      if (state.searchQuery) {
        const q = state.searchQuery.toLowerCase();
        const matchName = (p.name || '').toLowerCase().includes(q);
        const matchType = (p.project_type || '').toLowerCase().includes(q);
        if (!matchName && !matchType) return false;
      }
      return true;
    });
  }

  // 单击选中项目
  function selectProject(id) {
    if (!id) return;
    state.activeProjectId = id;
    try {
      localStorage.setItem('workspace_project_id', id);
    } catch (e) {}

    // 局部更新卡片 active 样式
    document.querySelectorAll('.project-card-item').forEach(card => {
      if (card.getAttribute('data-project-id') === id) {
        card.classList.add('active');
      } else {
        card.classList.remove('active');
      }
    });
  }

  // 单个卡片 HTML 渲染器 (固定高度 240px，包含 72px 紧凑海报、双轨总进度条与【编辑】按键)
  function renderCardHtml(p) {
    const isSelected = state.activeProjectId === p.id;
    const isDeleted = Boolean(p.deleted_at || state.filterScope === 'trash');
    const isArchived = Boolean(p.archived_at || state.filterScope === 'archived');
    const typeMeta = getProjectTypeMeta(p.project_type);
    const stageBadge = p.stage === 'review' ? '待审阅' : (p.stage === 'storyboard' ? '分镜中' : (p.stage === 'planning' ? '概念规划' : '制作中'));
    const progress = Number(p.progress) || 0;
    const coverUrl = String(p.cover_media_url || p.cover_url || '').trim();

    // 双轨总进度条计算规则
    const scriptProgress = Math.min(100, Math.round(progress * 1.25));
    const prodProgress = Math.max(0, Math.min(100, Math.round(progress * 0.85)));

    // 64px * 64px 紧凑拟物方形封面
    const thumbnailHtml = coverUrl
      ? `<div class="w-[64px] h-[64px] rounded-lg overflow-hidden bg-black/80 border border-white/10 shrink-0 relative shadow-inner">
           <img src="${esc(coverUrl)}" alt="${esc(p.name)}" class="w-full h-full object-cover" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'w-full h-full flex items-center justify-center bg-black/60\\'><i data-lucide=\\'${typeMeta.iconName}\\' class=\\'w-5 h-5 ${typeMeta.colorClass}\\'></i></div>'; window.lucide?.createIcons();">
           <div class="absolute top-0.5 left-0.5 bg-black/75 backdrop-blur-[2px] p-0.5 rounded border border-white/10">
             <i data-lucide="${typeMeta.iconName}" class="w-2.5 h-2.5 ${typeMeta.colorClass}"></i>
           </div>
         </div>`
      : `<div class="w-[64px] h-[64px] rounded-lg bg-[#07090e] border border-white/10 shrink-0 relative flex items-center justify-center shadow-inner">
           <i data-lucide="${typeMeta.iconName}" class="w-5 h-5 ${typeMeta.colorClass}"></i>
           <div class="absolute top-0.5 left-0.5 bg-black/75 backdrop-blur-[2px] p-0.5 rounded border border-white/10">
             <i data-lucide="${typeMeta.iconName}" class="w-2.5 h-2.5 ${typeMeta.colorClass}"></i>
           </div>
         </div>`;

    // 状态徽章
    let statusBadgeHtml = `
      <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 flex items-center space-x-0.5 shrink-0">
        <span class="w-1 h-1 rounded-full bg-emerald-400 shadow-[0_0_4px_#34d399] animate-pulse"></span>
        <span>${stageBadge}</span>
      </span>
    `;
    if (isDeleted) {
      statusBadgeHtml = `
        <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded-full bg-red-500/15 text-red-400 border border-red-500/30 flex items-center space-x-0.5 shrink-0">
          <span class="w-1 h-1 rounded-full bg-red-400 shadow-[0_0_4px_#ef4444]"></span>
          <span>统一回收站</span>
        </span>
      `;
    } else if (isArchived) {
      statusBadgeHtml = `
        <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/30 flex items-center space-x-0.5 shrink-0">
          <span class="w-1 h-1 rounded-full bg-amber-400 shadow-[0_0_4px_#f59e0b]"></span>
          <span>已归档只读</span>
        </span>
      `;
    }

    return `
    <!-- PROJECT CARD: 严格固定高度 240px，黑金拟物机架 -->
    <div class="champagne-card project-card-item flex flex-col justify-between p-2.5 carbon-mesh-bg hover:border-[#dfc384]/60 transition group relative overflow-hidden ${isSelected ? 'active' : ''} ${isDeleted ? 'opacity-85' : ''}"
         style="height: 240px; max-height: 240px; min-height: 240px; box-sizing: border-box;"
         data-project-id="${esc(p.id)}"
         onclick="WorkbenchProjects.selectProject('${esc(p.id)}')"
         ondblclick="WorkbenchProjects.openProject('${esc(p.id)}', 'workshop')"
         title="单击选中工程，双击或按 [ · ] 键进入影视工坊">

      <!-- 1. 顶部：64px微型海报 + 标题、状态、四格技术嵌槽 + 优先级旋钮 -->
      <div class="flex items-start justify-between pb-1 border-b border-white/5 shrink-0">
        <div class="flex items-start space-x-2 min-w-0 flex-1">
          ${thumbnailHtml}

          <div class="min-w-0 flex-1 pr-1">
            <div class="flex items-center space-x-1 flex-wrap gap-y-0.5">
              <h3 class="gw-type-section text-slate-100 group-hover:text-[#eddab3] truncate max-w-[150px]">${esc(p.name)}</h3>
              ${statusBadgeHtml}
              <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded bg-black/60 border border-white/10 text-[#fae2c8] shrink-0">ACEScg</span>
            </div>

            <!-- 故事描述（1行截断以适应240px高度） -->
            <p class="gw-type-body-md text-slate-300 mt-0.5 font-normal">
              ${esc(p.description || '剧本大纲与镜头工程深度贯通，支持实时微调与母版渲染。')}
            </p>

            <!-- 四格微型嵌槽 -->
            <div class="grid grid-cols-4 gap-1 mt-1">
              <div class="bay-inset px-1 py-0.5 rounded flex flex-col justify-center">
                <span class="gw-type-body-sm font-mono text-slate-400 leading-tight">镜头规模</span>
                <span class="gw-type-body-sm font-mono font-bold text-[#eddab3] leading-tight">${(rawNumber(p.scenes) === null || rawNumber(p.shots) === null) ? '—' : `${rawNumber(p.scenes)}场/${rawNumber(p.shots)}镜`}</span>
              </div>
              <div class="bay-inset px-1 py-0.5 rounded flex flex-col justify-center">
                <span class="gw-type-body-sm font-mono text-slate-400 leading-tight">资产规模</span>
                <span class="gw-type-body-sm font-mono font-bold text-slate-500 leading-tight" data-gw-asset-scale data-gw-degradation="not_integrated" title="真实数据源：GET /api/asset-registry/assets 的 total（全库已登记素材）">未接入</span>
              </div>
              <div class="bay-inset px-1 py-0.5 rounded flex flex-col justify-center">
                <span class="gw-type-body-sm font-mono text-slate-400 leading-tight">算力集群</span>
                <span class="gw-type-body-sm font-mono font-bold text-slate-500 leading-tight" data-gw-gpu-cluster data-gw-degradation="not_integrated" title="真实数据源：GET /api/observability/health 的 gpu_telemetry">未接入</span>
              </div>
              <div class="bay-inset px-1 py-0.5 rounded flex flex-col justify-center">
                <span class="gw-type-body-sm font-mono text-slate-400 leading-tight">更新时间</span>
                <span class="gw-type-body-sm font-mono font-bold text-slate-300 leading-tight">${formatTime(p.updated_at)}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 优先级旋钮 -->
        <div class="hw-mini-knob-box shrink-0 pl-1 flex flex-col items-center justify-center">
          <div class="hw-mini-knob w-6 h-6">
            <div class="hw-mini-knob-arc" style="transform: rotate(${Math.min(180, Math.round(progress * 1.8))}deg);"></div>
            <div class="hw-mini-knob-inner w-[1.125rem] h-[1.125rem] gw-type-body-sm">S${(p.id.slice(-1) || '1')}</div>
          </div>
          <span class="gw-type-body-sm font-mono text-[#dfc384] font-bold mt-0.5">PRIORITY</span>
        </div>
      </div>

      <!-- 2. 中部：双轨总进度条（剧本分镜进度 + 制片进度） -->
      <div class="my-0.5 py-1 px-2 rounded-lg bg-[#06080c]/95 border border-white/5 shrink-0">
        <div class="flex items-center justify-between mb-0.5 gw-type-body-sm font-mono">
          <div class="flex items-center space-x-1">
            <i data-lucide="sliders" class="w-2.5 h-2.5 text-[#dfc384]"></i>
            <span class="font-bold text-[#eddab3]r">双轨流水线电平</span>
          </div>
          <span class="text-slate-400">综合: <strong class="text-emerald-400 font-bold">${progress}%</strong></span>
        </div>

        <div class="grid grid-cols-2 gap-2">
          <!-- 轨道 1: 剧本分镜进度 -->
          <div>
            <div class="flex items-center justify-between gw-type-body-sm font-mono mb-0.5">
              <span class="text-slate-400">① 剧本分镜进度</span>
              <span class="text-[#eddab3] font-bold">${scriptProgress}%</span>
            </div>
            <div class="hw-fader-track-horizontal w-full h-1 rounded-full bg-[#040508] border border-white/10 overflow-hidden relative">
              <div class="hw-fader-glow-bar h-full" style="width: ${scriptProgress}%; background: linear-gradient(90deg, #dfc384 0%, #f59e0b 100%);"></div>
            </div>
          </div>

          <!-- 轨道 2: 制片进度 -->
          <div>
            <div class="flex items-center justify-between gw-type-body-sm font-mono mb-0.5">
              <span class="text-slate-400">② 制片进度</span>
              <span class="text-cyan-300 font-bold">${prodProgress}%</span>
            </div>
            <div class="hw-fader-track-horizontal w-full h-1 rounded-full bg-[#040508] border border-white/10 overflow-hidden relative">
              <div class="hw-fader-glow-bar h-full" style="width: ${prodProgress}%; background: linear-gradient(90deg, #38bdf8 0%, #2dd4bf 100%);"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- 3. 底部：主创席位 + 操作按钮组（工坊、监视、编辑、归档、删除/恢复） -->
      <div class="pt-0.5 border-t border-white/5 gw-card-footer flex items-center justify-between gap-1 min-w-0 shrink-0">
        <div class="flex items-center space-x-1 min-w-0 overflow-hidden">
          <span class="gw-type-body-sm font-mono text-slate-400 font-semibold whitespace-nowrap shrink-0" data-gw-card-footer-optional>主创:</span>
          <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded bg-[#0a0c10] border border-white/10 text-slate-200 min-w-0 truncate" data-gw-card-footer-owner title="${esc(p.owner || 'admin')}">${esc(p.owner || 'admin')}</span>
          <span class="gw-type-body-sm font-mono px-1 py-0.5 rounded bg-[#0a0c10] border border-white/10 text-[#eddab3] shrink-0" data-gw-card-footer-optional>
            AURA
          </span>
          <span data-gw-artifact-version-slot data-project-id="${esc(p.id)}" class="flex items-center shrink-0" hidden></span>
        </div>

        <div class="flex items-center space-x-1 shrink-0">
          <!-- 生产入口组：非回收站项目可用 -->
          ${!isDeleted ? `
            <button class="hw-btn hw-btn-sm hw-btn-gold flex items-center space-x-0.5 cursor-pointer transition"
                    onclick="event.stopPropagation(); WorkbenchProjects.openProject('${esc(p.id)}', 'workshop')"
                    title="进入影视工坊">
              <i data-lucide="film" class="w-2.5 h-2.5"></i>
              <span data-gw-card-footer-label>工坊</span>
            </button>
            <button class="hw-btn hw-btn-sm flex items-center space-x-0.5 cursor-pointer transition"
                    onclick="event.stopPropagation(); WorkbenchProjects.openProject('${esc(p.id)}', 'production')"
                    title="进入制片监视器">
              <i data-lucide="sliders" class="w-2.5 h-2.5 text-cyan-300"></i>
              <span data-gw-card-footer-label>监视</span>
            </button>
            <!-- 新增：编辑按钮 -->
            <button class="hw-btn hw-btn-sm flex items-center space-x-0.5 cursor-pointer transition"
                    onclick="event.stopPropagation(); WorkbenchProjects.openEditProject('${esc(p.id)}')"
                    title="编辑项目信息与配置参数">
              <i data-lucide="edit-3" class="w-2.5 h-2.5 text-[#dfc384]"></i>
              <span data-gw-card-footer-label>编辑</span>
            </button>
            <div class="h-2.5 w-px bg-white/10"></div>
          ` : ''}

          <!-- 生命周期治理组 -->
          ${isDeleted ? `
            <!-- 回收站恢复 -->
            <button class="hw-btn hw-btn-sm hw-btn-gold flex items-center space-x-0.5 cursor-pointer transition"
                    onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('restore', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})"
                    title="从统一回收站安全恢复该工程">
              <i data-lucide="rotate-ccw" class="w-2.5 h-2.5"></i>
              <span>恢复</span>
            </button>
          ` : `
            <!-- 归档 / 取消归档 -->
            ${isArchived ? `
              <button class="hw-btn hw-btn-sm flex items-center space-x-0.5 cursor-pointer transition"
                      onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('unarchive', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})"
                      title="取消归档">
                <i data-lucide="archive-restore" class="w-2.5 h-2.5 text-[#dfc384]"></i>
                <span>取消归档</span>
              </button>
            ` : `
              <button class="hw-btn hw-btn-sm flex items-center space-x-0.5 cursor-pointer transition"
                      onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('archive', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})"
                      title="归档项目">
                <i data-lucide="archive" class="w-2.5 h-2.5 text-slate-400"></i>
                <span>归档</span>
              </button>
            `}

            <!-- 删除到统一回收站 -->
            <button class="hw-btn hw-btn-sm hw-btn-danger flex items-center space-x-0.5 cursor-pointer transition"
                    onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('trash', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})"
                    title="删除到统一回收站（非物理直接删除）">
              <i data-lucide="trash-2" class="w-2.5 h-2.5"></i>
              <span>删除</span>
            </button>
          `}
        </div>
      </div>
    </div>
    `;
  }

  // 4. 渲染视图 (默认横排展示前 3 个项目，第三个以后的项目分成三排展示，卡片高度不变)
  function render() {
    const list = getFilteredProjects();

    // 更新总线及胶囊计数
    const countAll = document.getElementById('countAll');
    if (countAll) countAll.textContent = state.projects.length;

    // 渲染卡片流
    const stream = document.getElementById('projectsCardsStream');
    if (stream) {
      if (list.length === 0) {
        // 显式降级：区分「未接入 / 服务不可用 / 请求失败」与「确实没有匹配工程」。
        stream.innerHTML = state.projectsDegradation
          ? degradationNoticeHtml(state.projectsDegradation, '未取到工程列表；本页不会展示任何伪造的示例工程。')
          : `
          <div class="champagne-card p-8 text-center text-slate-500 font-mono gw-type-body-sm w-full">
            <i data-lucide="inbox" class="w-8 h-8 mx-auto mb-2 text-slate-600"></i>
            <div>未检索到匹配的制片工程</div>
          </div>
        `;
      } else {
        if (!state.activeProjectId && list[0]) {
          state.activeProjectId = list[0].id;
        }

        // 核心规则：前 3 个项目默认横排排布；第 3 个以后的项目分成三排展示 (3列网格流)
        const top3 = list.slice(0, 3);
        const rest = list.slice(3);

        let cardsHtml = `
          <!-- 第一排：前 3 个横排工程 -->
          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 w-full">
            ${top3.map(p => renderCardHtml(p)).join('')}
          </div>
        `;

        // 第 3 个以后的项目：分成三排展示 (采用 3 列网格，每排 3 个)
        if (rest.length > 0) {
          cardsHtml += `
            <div class="flex items-center space-x-2 my-3 w-full">
              <div class="h-px bg-white/10 flex-1"></div>
              <span class="gw-type-body-sm font-mono text-[#eddab3]/80r flex items-center space-x-1.5 px-2 py-0.5 rounded bg-black/40 border border-white/5">
                <i data-lucide="layers" class="w-3 h-3 text-[#dfc384]"></i>
                <span>储备与推进中工程 (${rest.length})</span>
              </span>
              <div class="h-px bg-white/10 flex-1"></div>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 w-full">
              ${rest.map(p => renderCardHtml(p)).join('')}
            </div>
          `;
        }

        stream.innerHTML = cardsHtml;
        window.GWProjectArtifactLinks?.hydrate(stream);
      }
    }

    // 渲染表格视图
    const tbody = document.getElementById('projectTableBody');
    if (tbody) {
      tbody.innerHTML = list.map(p => {
        const isDeleted = Boolean(p.deleted_at || state.filterScope === 'trash');
        const isArchived = Boolean(p.archived_at || state.filterScope === 'archived');
        const typeMeta = getProjectTypeMeta(p.project_type);
        const stageName = isDeleted ? '已在回收站' : (isArchived ? '已归档只读' : ({
          planning: '1. 概念立项',
          storyboard: '2. 故事板',
          production: '3. 生成制片',
          review: '4. 审核定剪'
        }[getProjectStage(p)] || '1. 概念立项'));

        return `
          <tr class="hover:bg-white/5 transition cursor-pointer ${isDeleted ? 'opacity-80' : ''}" onclick="WorkbenchProjects.selectProject('${esc(p.id)}')">
            <td class="py-2.5 font-bold text-slate-100 flex items-center space-x-2">
              <i data-lucide="${typeMeta.iconName}" class="w-4 h-4 ${typeMeta.colorClass} shrink-0"></i>
              <span>${esc(p.name)}</span>
            </td>
            <td class="py-2.5 text-[#eddab3]">${typeMeta.label}</td>
            <td class="py-2.5 ${isDeleted ? 'text-red-400' : (isArchived ? 'text-amber-400' : 'text-cyan-300')}">${stageName}</td>
            <td class="py-2.5">
              <div class="flex items-center space-x-2">
                <div class="hw-fader-track-horizontal w-16 h-1">
                  <div class="hw-fader-glow-bar" style="width: ${progressMeta(p).value === null ? 0 : progressMeta(p).value}%;"></div>
                </div>
                ${progressMeta(p).html}
              </div>
            </td>
            <td class="py-2.5 text-slate-400">${rawNumber(p.scenes) === null ? '—' : rawNumber(p.scenes)} / ${rawNumber(p.shots) === null ? '—' : rawNumber(p.shots)}</td>
            <td class="py-2.5 text-slate-400">${formatTime(p.updated_at)}</td>
            <td class="py-2.5 text-right">
              <div class="inline-flex items-center space-x-1.5">
                ${!isDeleted ? `
                  <button onclick="event.stopPropagation(); WorkbenchProjects.openProject('${encodeURIComponent(p.id)}', 'workshop')" class="hw-btn hw-btn-sm hw-btn-gold" title="进入影视工坊">工坊</button>
                  <button onclick="event.stopPropagation(); WorkbenchProjects.openProject('${encodeURIComponent(p.id)}', 'production')" class="hw-btn hw-btn-sm" title="进入制片监视">制片</button>
                  <button onclick="event.stopPropagation(); WorkbenchProjects.openEditProject('${esc(p.id)}')" class="hw-btn hw-btn-sm" title="编辑工程">编辑</button>
                ` : ''}

                ${isDeleted ? `
                  <button onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('restore', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})" class="hw-btn hw-btn-sm hw-btn-gold" title="从回收站恢复">恢复</button>
                ` : `
                  ${isArchived ? `
                    <button onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('unarchive', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})" class="hw-btn hw-btn-sm" title="取消归档">恢复</button>
                  ` : `
                    <button onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('archive', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})" class="hw-btn hw-btn-sm" title="归档项目">归档</button>
                  `}
                  <button onclick="event.stopPropagation(); WorkbenchProjects.confirmAction('trash', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})" class="hw-btn hw-btn-sm hw-btn-danger" title="删除到统一回收站（非物理直接删除）">删除</button>
                `}
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    // 卡片流由本函数重建 innerHTML：真实读数必须在其之后写入，
    // 否则「资产规模」格会停留在静态初值「未接入」（假降级）。
    syncAssetScale();

    window.lucide?.createIcons();
    updateNavPillsProject();
  }

  // 4.1 单击选中工程
  function selectProject(id) {
    if (!id) return;
    state.activeProjectId = id;
    const found = state.projects.find(p => p.id === id);
    try {
      localStorage.setItem('workspace_project_id', id);
      if (found && found.name) {
        localStorage.setItem('workspace_project_name', found.name);
      }
    } catch (_) {}

    // 更新各卡片高亮选中状态
    document.querySelectorAll('#projectsContainer [data-project-id], #projectTableBody [data-project-id]').forEach(el => {
      if (el.getAttribute('data-project-id') === id) {
        el.classList.add('border-[#dfc384]', 'ring-1', 'ring-[#dfc384]/40');
      } else {
        el.classList.remove('border-[#dfc384]', 'ring-1', 'ring-[#dfc384]/40');
      }
    });

    // 动态同步顶栏各导航按钮的 project_id
    updateNavPillsProject(id);
  }

  // 同步更新顶栏胶囊导航的 project_id
  // 只有真正消费项目上下文的页面才携带参数；项目中心/控制台/设置不带（见 BEHAVIOR-SPEC 4.5）。
  const PROJECT_CONTEXT_PAGES = new Set([
    'workshop.html', 'production.html', 'storyboard.html', 'canvas.html',
    'agents.html', 'workflow.html', 'collab.html'
  ]);

  function updateNavPillsProject(targetId) {
    const pId = targetId || state.activeProjectId || (state.projects[0] && state.projects[0].id) || localStorage.getItem('workspace_project_id');
    if (!pId) return;
    document.querySelectorAll('#navPillsGroup a').forEach(a => {
      const href = a.getAttribute('href');
      if (href && !href.startsWith('http') && !href.startsWith('#')) {
        const url = new URL(href, window.location.origin);
        const page = url.pathname.split('/').pop();
        if (PROJECT_CONTEXT_PAGES.has(page)) {
          url.searchParams.set('project_id', pId);
        } else {
          url.searchParams.delete('project_id');
        }
        a.setAttribute('href', page + url.search);
      }
    });
  }

  // 5. 统一打开/进入项目 (直通生产路由或分镜排期)
  function openProject(id, target = 'production') {
    const targetId = id || state.activeProjectId || (state.projects[0] && state.projects[0].id) || 'proj-01';
    const foundProj = state.projects.find(p => p.id === targetId);
    try {
      localStorage.setItem('workspace_project_id', targetId);
      if (foundProj && foundProj.name) {
        localStorage.setItem('workspace_project_name', foundProj.name);
      }
    } catch (e) {
      console.warn('保存项目上下文失败:', e);
    }

    if (target === 'storyboard') {
      window.location.href = `/static/pages/storyboard.html?project_id=${encodeURIComponent(targetId)}`;
    } else if (target === 'workshop') {
      window.location.href = `/static/pages/workshop.html?project_id=${encodeURIComponent(targetId)}`;
    } else {
      window.location.href = `/static/pages/production.html?project_id=${encodeURIComponent(targetId)}`;
    }
  }

  const VIEW_MODE_KEY = 'workbench_projects_view_mode';
  const LEGACY_VIEW_MODE_KEY = 'v2_projects_view_mode';
  function readViewMode() {
    try {
      const current = localStorage.getItem(VIEW_MODE_KEY);
      if (current === 'grid' || current === 'table') return current;
      const legacy = localStorage.getItem(LEGACY_VIEW_MODE_KEY);
      if (legacy === 'grid' || legacy === 'table') {
        localStorage.setItem(VIEW_MODE_KEY, legacy);
        return legacy;
      }
    } catch (_) {}
    return null;
  }

  // 6. 视图切换 (网格 vs 列表)
  function switchView(view) {
    if (view !== 'grid' && view !== 'table') view = 'grid';
    state.currentView = view;
    try {
      localStorage.setItem(VIEW_MODE_KEY, view);
    } catch (e) {}

    const grid = document.getElementById('projectsGridViewContainer');
    const table = document.getElementById('projectTableView');
    const btnG = document.getElementById('viewBtnGrid');
    const btnT = document.getElementById('viewBtnTable');

    if (btnG) btnG.setAttribute('aria-pressed', String(view === 'grid'));
    if (btnT) btnT.setAttribute('aria-pressed', String(view !== 'grid'));
    if (view === 'grid') {
      if (grid) grid.classList.remove('hidden');
      if (table) table.classList.add('hidden');
      if (btnG) btnG.classList.add('active');
      if (btnT) btnT.classList.remove('active');
    } else {
      if (grid) grid.classList.add('hidden');
      if (table) table.classList.remove('hidden');
      if (btnT) btnT.classList.add('active');
      if (btnG) btnG.classList.remove('active');
    }
  }

  function syncFilters() {
    const sidebar = document.querySelector('[data-project-type]')?.closest('aside');
    sidebar?.querySelectorAll('.tree-node-card').forEach(card => card.classList.remove('active'));
    document.querySelectorAll('#filterCapsulesGroup .filter-pill-btn').forEach(button => {
      const selected = button.dataset.type ? button.dataset.type === state.filterType : button.dataset.scope === state.filterScope && state.filterType === 'all';
      button.classList.toggle('engaged', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    document.querySelectorAll('aside [data-project-type], aside [data-project-scope]').forEach(card => {
      const selected = card.dataset.projectType ? card.dataset.projectType === state.filterType : card.dataset.projectScope === state.filterScope;
      card.classList.toggle('active', selected);
      card.setAttribute('aria-pressed', String(selected));
    });
  }

  // 6. 分类筛选
  function filterType(type, btn) {
    state.filterType = type;
    const reload = state.filterScope !== 'active';
    state.filterScope = 'active';
    syncFilters();
    if (reload) load(); else render();
  }

  // 7. 生命周期与作用域筛选 (全部 active / 已归档 archived / 统一回收站 trash)
  function filterScope(scope, btn) {
    state.filterScope = scope;
    state.filterType = 'all';

    syncFilters();

    load();
  }

  // 8. 拟物化防误触二次确认弹窗唤起
  function confirmAction(type, projectId, projectName, version) {
    state.pendingAction = {
      type,
      projectId,
      projectName: projectName || projectId,
      version: version || 1
    };

    const modal = document.getElementById('projectActionModal');
    const titleEl = document.getElementById('actionModalTitle');
    const nameEl = document.getElementById('actionModalProjectName');
    const descEl = document.getElementById('actionModalDesc');
    const safetyNotice = document.getElementById('actionModalSafetyNotice');
    const confirmBtn = document.getElementById('actionModalConfirmBtn');

    if (nameEl) {
      nameEl.textContent = `项目：${projectName || projectId}`;
    }

    if (type === 'trash') {
      if (titleEl) titleEl.textContent = '移入统一回收站';
      if (descEl) {
        descEl.innerHTML = `确认将工程 <strong class="text-[#eddab3]">${esc(projectName || projectId)}</strong> 移入统一回收站？<br><span class="text-slate-400">移入后工程将进入隔离保护状态，原工程记录与关联资产完好保留，可在【统一回收站】视图中随时安全找回并一键恢复。</span>`;
      }
      if (safetyNotice) safetyNotice.classList.remove('hidden');
      if (confirmBtn) {
        confirmBtn.className = 'hw-btn hw-btn-danger cursor-pointer';
        confirmBtn.textContent = '确认移入回收站';
      }
    } else if (type === 'archive') {
      if (titleEl) titleEl.textContent = '归档制片工程';
      if (descEl) {
        descEl.innerHTML = `确认归档工程 <strong class="text-[#eddab3]">${esc(projectName || projectId)}</strong>？<br><span class="text-slate-400">归档后工程转为只读存档保护，防止生产中被误修改。可随时在【已归档】视图中取消归档并重新激活。</span>`;
      }
      if (safetyNotice) safetyNotice.classList.add('hidden');
      if (confirmBtn) {
        confirmBtn.className = 'hw-btn hw-btn-gold cursor-pointer';
        confirmBtn.textContent = '确认归档';
      }
    } else if (type === 'unarchive') {
      if (titleEl) titleEl.textContent = '取消归档工程';
      if (descEl) {
        descEl.innerHTML = `确认取消归档工程 <strong class="text-[#eddab3]">${esc(projectName || projectId)}</strong>？<br><span class="text-slate-400">取消归档后工程将立即恢复为活跃制片状态，重新开启镜头、剧本与分镜流水线。</span>`;
      }
      if (safetyNotice) safetyNotice.classList.add('hidden');
      if (confirmBtn) {
        confirmBtn.className = 'hw-btn hw-btn-gold cursor-pointer';
        confirmBtn.textContent = '取消归档并激活';
      }
    } else if (type === 'restore') {
      if (titleEl) titleEl.textContent = '从统一回收站安全恢复';
      if (descEl) {
        descEl.innerHTML = `确认从统一回收站恢复工程 <strong class="text-[#eddab3]">${esc(projectName || projectId)}</strong>？<br><span class="text-slate-400">工程将从回收站隔离池迁出，恢复至归档/活动流，所有场景与剧集资产完好如初。</span>`;
      }
      if (safetyNotice) safetyNotice.classList.add('hidden');
      if (confirmBtn) {
        confirmBtn.className = 'hw-btn hw-btn-gold cursor-pointer';
        confirmBtn.textContent = '确认安全恢复';
      }
    }

    if (window.lucide) window.lucide.createIcons();
    HardwareDeck.openModal('projectActionModal');
  }

  // 9. 执行二次确认的操作
  async function executeConfirmedAction() {
    if (!state.pendingAction) {
      HardwareDeck.closeModal('projectActionModal');
      return;
    }

    const { type, projectId, version } = state.pendingAction;
    HardwareDeck.closeModal('projectActionModal');
    state.pendingAction = null;

    if (type === 'archive') {
      await archiveProject(projectId, version);
    } else if (type === 'unarchive') {
      await unarchiveProject(projectId, version);
    } else if (type === 'trash') {
      await trashProject(projectId, version);
    } else if (type === 'restore') {
      await restoreTrashedProject(projectId, version);
    }
  }

  // 10. 归档项目 (DELETE /api/asset-registry/projects/{project_id})
  async function archiveProject(projectId, version) {
    // 显式降级：只有后端确认成功才提示成功；失败一律如实报错，
    // 绝不本地改写状态后谎称「已成功归档」。
    try {
      const res = await fetch(`/api/asset-registry/projects/${encodeURIComponent(projectId)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ expected_version: version || 1 })
      });

      if (res.ok) {
        showToast('项目已成功安全归档', 'success');
        await fetchCounts();
        await load();
        return;
      }
      const failure = await classifyFetchFailure(res);
      showToast(failure.kind === 'not_integrated' ? '归档接口未接入' : ('归档失败：' + failure.message), 'error');
    } catch (e) {
      console.warn('归档请求异常（网络异常）:', e);
      showToast(degradationMessage('service_unavailable', 0), 'error');
    }
  }

  // 11. 取消归档 (POST /api/asset-registry/governance/projects/{project_id}/restore)
  async function unarchiveProject(projectId, version) {
    // 显式降级：失败不本地改写状态，也不谎称成功。
    try {
      const res = await fetch(`/api/asset-registry/governance/projects/${encodeURIComponent(projectId)}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ expected_version: version || 1 })
      });

      if (res.ok) {
        showToast('项目已取消归档并恢复为活跃状态', 'success');
        await fetchCounts();
        await load();
        return;
      }
      const failure = await classifyFetchFailure(res);
      showToast(failure.kind === 'not_integrated' ? '取消归档接口未接入' : ('取消归档失败：' + failure.message), 'error');
    } catch (e) {
      console.warn('取消归档请求异常（网络异常）:', e);
      showToast(degradationMessage('service_unavailable', 0), 'error');
    }
  }

  // 12. 删除到统一回收站 (非物理直接删除，支持随时安全恢复)
  // 后端硬约束：仅已归档项目可移入回收站。如果项目当前未归档，先归档后移入回收站。
  async function trashProject(projectId, version) {
    let currentVersion = version || 1;
    const targetProject = state.projects.find(p => p.id === projectId);

    // 显式降级：只有后端确认成功才提示成功；失败不得本地伪造删除状态。
    try {
      // 若尚未归档，先安全归档
      if (!targetProject || !targetProject.archived_at) {
        const archRes = await fetch(`/api/asset-registry/projects/${encodeURIComponent(projectId)}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ expected_version: currentVersion })
        });
        if (archRes.ok) {
          const archData = await archRes.json();
          currentVersion = archData?.project?.version || (currentVersion + 1);
        } else {
          const failure = await classifyFetchFailure(archRes);
          showToast(failure.kind === 'not_integrated' ? '归档接口未接入，无法移入回收站' : ('归档失败：' + failure.message), 'error');
          return;
        }
      }

      // 移入统一回收站
      const trashRes = await fetch(`/api/asset-registry/projects/${encodeURIComponent(projectId)}/trash`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ expected_version: currentVersion })
      });

      if (trashRes.ok) {
        showToast('项目已安全停放至统一回收站（非物理删除）', 'success');
        await fetchCounts();
        await load();
        return;
      }
      const failure = await classifyFetchFailure(trashRes);
      showToast(failure.kind === 'not_integrated' ? '回收站接口未接入' : ('移入回收站失败：' + failure.message), 'error');
    } catch (e) {
      console.warn('移入回收站请求异常（网络异常）:', e);
      showToast(degradationMessage('service_unavailable', 0), 'error');
    }
  }

  // 13. 从统一回收站安全恢复 (POST /api/asset-registry/projects/{project_id}/trash/restore)
  async function restoreTrashedProject(projectId, version) {
    // 显式降级：失败不本地改写状态，也不谎称成功。
    try {
      const res = await fetch(`/api/asset-registry/projects/${encodeURIComponent(projectId)}/trash/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ expected_version: version || 1 })
      });

      if (res.ok) {
        showToast('工程已从统一回收站安全恢复', 'success');
        await fetchCounts();
        await load();
        return;
      }
      const failure = await classifyFetchFailure(res);
      showToast(failure.kind === 'not_integrated' ? '回收站恢复接口未接入' : ('恢复失败：' + failure.message), 'error');
    } catch (e) {
      console.warn('回收站恢复请求异常（网络异常）:', e);
      showToast(degradationMessage('service_unavailable', 0), 'error');
    }
  }

  // 14. 实时搜索
  function search(val) {
    state.searchQuery = (val || '').trim();
    render();
  }

  // 创建门时保存请求键直到服务端确认，网络结果未知时相同payload可安全重试。
  function projectCreateRequestId(payload) {
    const signature = JSON.stringify(payload);
    let stored = null;
    try {
      stored = JSON.parse(window.sessionStorage.getItem('gw.pending-project-create') || 'null');
    } catch (e) {
      throw new Error('\u6d4f\u89c8\u5668\u65e0\u6cd5\u5b89\u5168\u4fdd\u5b58\u521b\u5efa\u8bf7\u6c42\u952e\uff0c\u8bf7\u542f\u7528\u4f1a\u8bdd\u5b58\u50a8\u540e\u91cd\u8bd5');
    }
    if (stored && stored.signature === signature && /^[A-Za-z0-9_.:-]{8,128}$/.test(stored.client_request_id || '')) {
      return stored.client_request_id;
    }
    if (!window.crypto?.getRandomValues) throw new Error('\u6d4f\u89c8\u5668\u7f3a\u5c11\u5b89\u5168\u968f\u673a\u6e90\uff0c\u65e0\u6cd5\u521b\u5efa\u5e42\u7b49\u8bf7\u6c42\u952e');
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    const clientRequestId = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    try {
      window.sessionStorage.setItem('gw.pending-project-create', JSON.stringify({ signature, client_request_id: clientRequestId }));
    } catch (e) {
      throw new Error('\u6d4f\u89c8\u5668\u65e0\u6cd5\u4fdd\u5b58\u521b\u5efa\u8bf7\u6c42\u952e\uff0c\u8bf7\u542f\u7528\u4f1a\u8bdd\u5b58\u50a8\u540e\u91cd\u8bd5');
    }
    return clientRequestId;
  }

  function clearProjectCreateRequestId(clientRequestId) {
    if (!clientRequestId) return true;
    try {
      const key = 'gw.pending-project-create';
      const stored = JSON.parse(window.sessionStorage.getItem(key) || 'null');
      if (stored?.client_request_id === clientRequestId) window.sessionStorage.removeItem(key);
      return true;
    } catch (e) {
      return false;
    }
  }

  function verifiedProjectGateSummary(gates) {
    const states = { pending: '\u5f85\u5ba1\u6838', approved: '\u5df2\u901a\u8fc7', changes_requested: '\u9700\u8865\u5145' };
    const sample = gates.slice(0, 3).map(gate => `${gate.name} [${states[gate.state] || gate.state} v${gate.version}]`);
    const more = gates.length > sample.length ? `\uff1b\u53e6\u6709${gates.length - sample.length}\u9879` : '';
    return `\u5df2\u56de\u8bfb\u9a8c\u8bc1${gates.length}\u4e2a\u9636\u6bb5\u95e8\uff1a${sample.join('\uff1b')}${more}`;
  }
  // 9. 处理新建项目提交
  async function handleCreateProject(event) {
    event.preventDefault();
    const nameInput = document.getElementById('newProjectNameInput');
    const typeSelect = document.getElementById('newProjectTypeSelect');
    const descInput = document.getElementById('newProjectDescInput');
    const startAtInput = document.getElementById('newProjectStartAt');
    const dueAtInput = document.getElementById('newProjectDueAt');
    const gatesInput = document.getElementById('newProjectGatesInput');

    const name = nameInput?.value?.trim();
    if (!name) return;

    const payload = {
      name,
      project_type: typeSelect?.value || 'film',
      description: descInput?.value?.trim() || '',
      start_at: dateInputTimestamp(startAtInput?.value),
      due_at: dateInputTimestamp(dueAtInput?.value)
    };

    const gateLines = String(gatesInput?.value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (gateLines.length > 32) {
      showToast('初始阶段门最多32项', 'error');
      return;
    }
    if (gateLines.length) {
      const gates = [];
      for (const [index, line] of gateLines.entries()) {
        const divider = line.indexOf('|');
        if (divider < 1 || !line.slice(divider + 1).trim()) {
          showToast(`第${index + 1}行阶段门格式错误，请使用 code|阶段名称`, 'error');
          return;
        }
        gates.push({ code: line.slice(0, divider).trim(), name: line.slice(divider + 1).trim() });
      }
      payload.gates = gates;
      try {
        payload.client_request_id = projectCreateRequestId(payload);
      } catch (e) {
        showToast(e.message || '无法创建幂等请求键', 'error');
        return;
      }
    }

    try {
      const res = await fetch('/api/asset-registry/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const resData = await res.json();
        const raw = resData.project || resData;
        // 契约只回传稳定ID与版本；补齐项目卡片渲染所需名称。
        const created = { ...raw, id: raw.id || raw.project_id, name: raw.name || payload.name };
        const existingIndex = state.projects.findIndex(item => (item.id || item.project_id) === created.id);
        if (existingIndex >= 0) state.projects[existingIndex] = { ...state.projects[existingIndex], ...created };
        else state.projects.unshift(created);
        if (created.id) {
          localStorage.setItem('workspace_project_id', created.id);
          localStorage.setItem('workspace_project_name', created.name || created.title || payload.name);
          selectProject(created.id);
        }

        let verifiedGates = null;
        if (Array.isArray(payload.gates)) {
          try {
            const gatesRes = await fetch(`/api/asset-registry/projects/${encodeURIComponent(created.id)}/gates`, {
              method: 'GET', credentials: 'same-origin'
            });
            const gatesData = await gatesRes.json().catch(() => null);
            const gates = gatesData?.gates;
            const matchesRequest = Array.isArray(gates) && gates.length === payload.gates.length &&
              gates.every((gate, index) => gate.project_id === created.id &&
                gate.code === String(payload.gates[index].code).trim().toLowerCase() &&
                gate.name === payload.gates[index].name && gate.state === 'pending' && gate.version === 1);
            if (!gatesRes.ok || gatesData?.project_id !== created.id || !matchesRequest) {
              throw new Error('gate readback did not match the submitted project');
            }
            verifiedGates = gates;
          } catch (e) {
            console.warn('\u9879\u76ee\u521b\u5efa\u5df2\u786e\u8ba4\uff0c\u9636\u6bb5\u95e8\u56de\u8bfb\u5c1a\u672a\u9a8c\u8bc1:', e);
            setDispatchStatus('service_unavailable', 'GET /api/asset-registry/projects/{project_id}/gates');
            showToast('\u9879\u76ee\u5df2\u521b\u5efa\uff0c\u4f46\u9636\u6bb5\u95e8\u56de\u8bfb\u672a\u9a8c\u8bc1\uff1b\u8bf7\u4fdd\u6301\u5185\u5bb9\u76f8\u540c\u91cd\u8bd5\uff0c\u5e42\u7b49\u952e\u4f1a\u9632\u6b62\u91cd\u590d\u521b\u5efa', 'warning');
            render();
            return;
          }
        }

        const requestKeyCleared = clearProjectCreateRequestId(payload.client_request_id);
        if (!requestKeyCleared) {
          showToast('\u9879\u76ee\u5df2\u521b\u5efa\uff0c\u4f46\u6d4f\u89c8\u5668\u672a\u80fd\u6e05\u7406\u5e42\u7b49\u952e\uff1b\u8bf7\u5237\u65b0\u9875\u9762\u540e\u518d\u521b\u5efa\u540c\u540d\u9879\u76ee', 'warning');
        }
        showToast(verifiedGates ? verifiedProjectGateSummary(verifiedGates) : '\u9879\u76ee\u5df2\u6210\u529f\u521b\u5efa\u5e76\u521d\u59cb\u5316\u672c\u5730\u5de5\u7a0b\u76ee\u5f55', 'success');
        setDispatchStatus('ok', 'POST /api/asset-registry/projects \u771f\u5b9e\u521b\u5efa\u6210\u529f');
        HardwareDeck.closeModal('newProjectModal');
        nameInput.value = '';
        if (descInput) descInput.value = '';
        if (gatesInput) gatesInput.value = '';
        window.GWProjectDateRange?.setRange(document.querySelector('#newProjectModal [data-project-date-range]'), '', '');
        render();
      } else {
        const errJson = await res.json().catch(() => ({}));
        if (res.status === 503) setDispatchStatus('service_unavailable', 'HTTP ' + res.status);
        else if (res.status === 404 || res.status === 501) setDispatchStatus('not_integrated', 'HTTP ' + res.status);
        const errMsg = extractErrorMessage(errJson, res) || '新建项目失败';
        showToast(errMsg, 'error');
      }
    } catch (e) {
      console.error('新建项目请求失败:', e);
      showToast('网络连接异常，新建项目失败', 'error');
    }
  }

  // 15. 打开编辑工程模态框
  function openEditProject(id) {
    if (!id) return;
    const p = state.projects.find(item => item.id === id);
    if (!p) {
      showToast('未找到对应工程数据', 'error');
      return;
    }

    const idInput = document.getElementById('editProjectId');
    const verInput = document.getElementById('editProjectVersion');
    const nameInput = document.getElementById('editProjectNameInput');
    const typeSelect = document.getElementById('editProjectTypeSelect');
    const stageSelect = document.getElementById('editProjectStageSelect');
    const scenesInput = document.getElementById('editProjectScenesInput');
    const shotsInput = document.getElementById('editProjectShotsInput');
    const progressInput = document.getElementById('editProjectProgressInput');
    const descInput = document.getElementById('editProjectDescInput');
    const startAtInput = document.getElementById('editProjectStartAt');
    const dueAtInput = document.getElementById('editProjectDueAt');

    if (idInput) idInput.value = p.id;
    if (verInput) verInput.value = p.version || 1;
    if (nameInput) nameInput.value = p.name || '';
    if (typeSelect) typeSelect.value = p.project_type || 'film';
    if (stageSelect) stageSelect.value = getProjectStage(p);
    if (scenesInput) scenesInput.value = rawNumber(p.scenes) === null ? 0 : rawNumber(p.scenes);
    if (shotsInput) shotsInput.value = rawNumber(p.shots) === null ? 0 : rawNumber(p.shots);
    if (progressInput) progressInput.value = rawNumber(p.progress) === null ? '' : rawNumber(p.progress);
    if (descInput) descInput.value = p.description || '';
    const dateRange = document.querySelector('#editProjectModal [data-project-date-range]');
    window.GWProjectDateRange?.setRange(dateRange, p.start_at, p.due_at);

    HardwareDeck.openModal('editProjectModal');
  }

  // 16. 处理编辑工程提交 (PATCH /api/asset-registry/projects/{id})
  async function handleUpdateProject(event) {
    event.preventDefault();
    const idInput = document.getElementById('editProjectId');
    const verInput = document.getElementById('editProjectVersion');
    const nameInput = document.getElementById('editProjectNameInput');
    const typeSelect = document.getElementById('editProjectTypeSelect');
    const stageSelect = document.getElementById('editProjectStageSelect');
    const scenesInput = document.getElementById('editProjectScenesInput');
    const shotsInput = document.getElementById('editProjectShotsInput');
    const progressInput = document.getElementById('editProjectProgressInput');
    const descInput = document.getElementById('editProjectDescInput');
    const startAtInput = document.getElementById('editProjectStartAt');
    const dueAtInput = document.getElementById('editProjectDueAt');

    const projectId = idInput?.value;
    if (!projectId) return;

    const payload = {
      name: nameInput?.value?.trim() || '未命名工程',
      stage: stageSelect?.value || 'planning',
      scenes: Number.isFinite(parseInt(scenesInput?.value, 10)) ? parseInt(scenesInput?.value, 10) : 0,
      shots: Number.isFinite(parseInt(shotsInput?.value, 10)) ? parseInt(shotsInput?.value, 10) : 0,
      progress: parseInt(progressInput?.value, 10) || 0,
      description: descInput?.value?.trim() || '',
      start_at: dateInputTimestamp(startAtInput?.value),
      due_at: dateInputTimestamp(dueAtInput?.value),
      expected_version: parseInt(verInput?.value, 10) || 1
    };

    try {
      const res = await fetch(`/api/asset-registry/projects/${encodeURIComponent(projectId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const error = await res.json().catch(() => ({}));
        const message = extractErrorMessage(error, res);
        showToast(message, 'error');
        return;
      }
      showToast('项目工程配置已成功更新并持久化', 'success');
      HardwareDeck.closeModal('editProjectModal');
      await load();
    } catch (e) {
      console.warn('后端更新工程失败:', e);
      showToast('网络连接异常，项目排期未保存', 'error');
    }
  }

  // 17. 全局统一回收站操作 (工程、素材库资产、工程画布统一治理)
  function openGlobalTrashDrawer() {
    HardwareDeck.openDrawer('unifiedGlobalTrashDrawer');
    refreshGlobalTrash();
  }

  function switchGlobalTrashTab(tab) {
    globalTrashState.currentTab = tab;
    [['trashTabAll', 'all'], ['trashTabProjects', 'projects'], ['trashTabAssets', 'assets'], ['trashTabCanvases', 'canvases']].forEach(([id, value]) => {
      document.getElementById(id)?.setAttribute('aria-pressed', String(tab === value));
    });
    const btnAll = document.getElementById('trashTabAll');
    const btnProj = document.getElementById('trashTabProjects');
    const btnAssets = document.getElementById('trashTabAssets');
    const btnCanvas = document.getElementById('trashTabCanvases');

    if (btnAll) btnAll.className = tab === 'all' ? 'hw-btn btn-size-sm engaged px-2.5 py-0.5 cursor-pointer filter-pill-btn' : 'hw-btn btn-size-sm px-2.5 py-0.5 text-slate-400 cursor-pointer filter-pill-btn';
    if (btnProj) btnProj.className = tab === 'projects' ? 'hw-btn btn-size-sm engaged px-2.5 py-0.5 cursor-pointer filter-pill-btn' : 'hw-btn btn-size-sm px-2.5 py-0.5 text-slate-400 cursor-pointer filter-pill-btn';
    if (btnAssets) btnAssets.className = tab === 'assets' ? 'hw-btn btn-size-sm engaged px-2.5 py-0.5 cursor-pointer filter-pill-btn' : 'hw-btn btn-size-sm px-2.5 py-0.5 text-slate-400 cursor-pointer filter-pill-btn';
    if (btnCanvas) btnCanvas.className = tab === 'canvases' ? 'hw-btn btn-size-sm engaged px-2.5 py-0.5 cursor-pointer filter-pill-btn' : 'hw-btn btn-size-sm px-2.5 py-0.5 text-slate-400 cursor-pointer filter-pill-btn';

    renderGlobalTrash();
  }

  async function refreshGlobalTrash() {
    try {
      const response = await fetch('/api/asset-registry/governance/overview', { credentials: 'same-origin' });
      if (!response.ok) {
        // 显式降级：治理总览未接入 / 不可用时清空并如实标记，不保留旧数据假装成功。
        const failure = await classifyFetchFailure(response);
        globalTrashState.projects = [];
        globalTrashState.assets = [];
        globalTrashState.canvases = [];
        globalTrashState.degradation = { kind: failure.kind, message: failure.message };
        ['trashCountAll', 'trashCountProjects', 'trashCountAssets', 'trashCountCanvases'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = '—';
        });
        const badge = document.getElementById('topbarTrashBadge');
        if (badge) badge.style.display = 'none';
        renderGlobalTrash();
        return;
      }
      const data = await response.json();
      const recycleResponse = await fetch('/api/asset-registry/recycle-bin', { credentials: 'same-origin' });
      if (!recycleResponse.ok) throw new Error('素材回收站读取失败');
      const recycleData = await recycleResponse.json();
      globalTrashState.registryRevision = recycleData.revision;
      globalTrashState.projects = (Array.isArray(data.projects) ? data.projects : [])
        .filter(item => item && (item.deleted_at != null || item.deleted_entity_id));
      globalTrashState.assets = (Array.isArray(recycleData.assets) ? recycleData.assets : []).map(entry => ({
        ...entry.payload,
        id: entry.entry_id,
        deleted_at: entry.deleted_at,
        registry_recycle: true
      }));
      globalTrashState.canvases = Array.isArray(data.canvases) ? data.canvases : [];
      globalTrashState.degradation = null;

      globalTrashState.initialized = true;

      // 更新数量徽章
      const countProj = globalTrashState.projects.length;
      const countAssets = globalTrashState.assets.length;
      const countCanvases = globalTrashState.canvases.length;
      const totalTrash = countProj + countAssets + countCanvases;
      const elDrawerCountAll = document.getElementById('trashCountAll');
      const elDrawerCountProj = document.getElementById('trashCountProjects');
      const elDrawerCountAssets = document.getElementById('trashCountAssets');
      const elDrawerCountCanvases = document.getElementById('trashCountCanvases');
      const elTopBadge = document.getElementById('topbarTrashBadge');

      if (elDrawerCountProj) elDrawerCountProj.textContent = countProj;
      if (elDrawerCountAll) elDrawerCountAll.textContent = totalTrash;
      if (elDrawerCountAssets) elDrawerCountAssets.textContent = countAssets;
      if (elDrawerCountCanvases) elDrawerCountCanvases.textContent = countCanvases;
      if (elTopBadge) {
        elTopBadge.textContent = totalTrash;
        elTopBadge.style.display = totalTrash > 0 ? 'inline-block' : 'none';
      }

      renderGlobalTrash();
    } catch (e) {
      console.warn('全局回收站刷新失败（网络异常）:', e);
      globalTrashState.projects = [];
      globalTrashState.assets = [];
      globalTrashState.canvases = [];
      globalTrashState.degradation = { kind: 'service_unavailable', message: degradationMessage('service_unavailable', 0) };
      ['trashCountAll', 'trashCountProjects', 'trashCountAssets', 'trashCountCanvases'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
      });
      renderGlobalTrash();
    }
  }

  function renderGlobalTrash() {
    const container = document.getElementById('globalTrashContainer');
    if (!container) return;

    const tab = globalTrashState.currentTab;

    // 显式降级：治理总览未接入 / 不可用时，不得显示「回收站为空」这类结论。
    if (globalTrashState.degradation) {
      container.innerHTML = degradationNoticeHtml(
        globalTrashState.degradation,
        '治理总览不可用；无法确认回收站内容，请勿据此判断「没有删除项」。'
      );
      window.lucide?.createIcons();
      return;
    }

    if (tab === 'all') {
      const groups = [['projects', '项目', globalTrashState.projects], ['canvases', '画布', globalTrashState.canvases], ['assets', '资产', globalTrashState.assets]];
      container.innerHTML = groups.map(([kind, label, list]) => `<section class="space-y-1.5"><h4 class="gw-type-body-md font-mono text-slate-400">${label} · ${list.length}</h4>${list.length ? `<div class="space-y-1.5">${list.map(item => `<div class="bay-inset p-2.5 rounded-xl border border-white/5 flex items-center justify-between"><span class="gw-type-body-md text-slate-200 truncate">${esc(item.name || item.title || item.id)}</span><span class="gw-type-body-md font-mono text-slate-500">${formatTime(item.deleted_at)}</span></div>`).join('')}</div>` : '<div class="gw-type-body-md text-slate-600 font-mono">暂无删除内容</div>'}</section>`).join('');
    } else if (tab === 'projects') {
      const list = globalTrashState.projects;
      if (list.length === 0) {
        container.innerHTML = `
          <div class="bay-inset p-8 text-center text-slate-500 font-mono gw-type-body-sm rounded-xl">
            <i data-lucide="check-circle-2" class="w-7 h-7 mx-auto mb-2 text-emerald-400/60"></i>
            <div>制片工程隔离池为空，暂无已移入工程</div>
          </div>
        `;
      } else {
        container.innerHTML = list.map(p => {
          const typeMeta = getProjectTypeMeta(p.project_type);
          const restoreAction = p.recycle_entry_id
            ? `WorkbenchProjects.restoreProjectContentFromTrash('${esc(p.recycle_entry_id)}')`
            : `WorkbenchProjects.confirmAction('restore', '${esc(p.id)}', '${esc(p.name)}', ${p.version || 1})`;
          return `
            <div class="bay-inset p-3 rounded-xl border border-white/5 hover:border-red-500/30 transition flex items-center justify-between">
              <div class="min-w-0 flex-1 pr-3">
                <div class="flex items-center space-x-2">
                  <span class="w-1.5 h-1.5 rounded-full bg-red-400"></span>
                  <h4 class="gw-type-section text-slate-100 truncate">${esc(p.name)}</h4>
                  <span class="gw-type-body-sm font-mono px-1.5 py-0.5 rounded bg-red-500/15 text-red-400 border border-red-500/30">工程隔离</span>
                  <span class="gw-type-body-sm font-mono text-slate-400">${typeMeta.label}</span>
                </div>
                <div class="gw-type-body-sm font-mono text-slate-400 mt-1 flex items-center space-x-3">
                  <span>镜头: ${(rawNumber(p.scenes) === null || rawNumber(p.shots) === null) ? '—' : `${rawNumber(p.scenes)}场/${rawNumber(p.shots)}镜`}</span>
                  <span>隔离时间: ${formatTime(p.deleted_at)}</span>
                  <span>ID: ${esc(p.id)}</span>
                </div>
              </div>
              <button class="hw-btn hw-btn-sm hw-btn-gold flex items-center space-x-1 cursor-pointer transition shrink-0"
                      onclick="${restoreAction}"
                      title="一键安全恢复该工程">
                <i data-lucide="rotate-ccw" class="w-3 h-3"></i>
                <span>安全恢复</span>
              </button>
            </div>
          `;
        }).join('');
      }
    } else if (tab === 'assets') {
      const list = globalTrashState.assets;
      if (list.length === 0) {
        container.innerHTML = `
          <div class="bay-inset p-8 text-center text-slate-500 font-mono gw-type-body-sm rounded-xl">
            <i data-lucide="check-circle-2" class="w-7 h-7 mx-auto mb-2 text-emerald-400/60"></i>
            <div>素材库隔离池为空，暂无软删除资产</div>
          </div>
        `;
      } else {
        container.innerHTML = list.map(a => `
          <div class="bay-inset p-3 rounded-xl border border-white/5 hover:border-[#dfc384]/30 transition flex items-center justify-between">
            <div class="min-w-0 flex-1 pr-3">
              <div class="flex items-center space-x-2">
                <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>
                <h4 class="gw-type-section text-slate-100 truncate">${esc(a.name)}</h4>
                <span class="gw-type-body-sm font-mono px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">${esc(a.type || 'RAW')}</span>
              </div>
              <div class="gw-type-body-sm font-mono text-slate-400 mt-1 flex items-center space-x-3">
                <span>大小: ${esc(a.size || '未知')}</span>
                <span>隔离时间: ${formatTime(a.deleted_at)}</span>
                <span class="truncate max-w-[120px]">哈希: ${esc(a.hash || 'sha256:...')}</span>
              </div>
            </div>
            <button class="hw-btn hw-btn-sm hw-btn-gold flex items-center space-x-1 cursor-pointer transition shrink-0"
                    onclick="WorkbenchProjects.restoreAssetFromTrash('${esc(a.id)}')"
                    title="恢复此素材到资产库">
              <i data-lucide="rotate-ccw" class="w-3 h-3"></i>
              <span>恢复素材</span>
            </button>
          </div>
        `).join('');
      }
    } else if (tab === 'canvases') {
      const list = globalTrashState.canvases;
      if (list.length === 0) {
        container.innerHTML = `
          <div class="bay-inset p-8 text-center text-slate-500 font-mono gw-type-body-sm rounded-xl">
            <i data-lucide="check-circle-2" class="w-7 h-7 mx-auto mb-2 text-emerald-400/60"></i>
            <div>工程画布隔离池为空，暂无软删除画布</div>
          </div>
        `;
      } else {
        container.innerHTML = list.map(c => `
          <div class="bay-inset p-3 rounded-xl border border-white/5 hover:border-purple-500/30 transition flex items-center justify-between">
            <div class="min-w-0 flex-1 pr-3">
              <div class="flex items-center space-x-2">
                <span class="w-1.5 h-1.5 rounded-full bg-purple-400"></span>
                <h4 class="gw-type-section text-slate-100 truncate">${esc(c.name)}</h4>
                <span class="gw-type-body-sm font-mono px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-300 border border-purple-500/30">画布节点</span>
              </div>
              <div class="gw-type-body-sm font-mono text-slate-400 mt-1 flex items-center space-x-3">
                <span>节点规模: ${rawNumber(c.nodes_count) === null ? '未接入' : `${rawNumber(c.nodes_count)} Nodes`}</span>
                <span>隔离时间: ${formatTime(c.deleted_at)}</span>
              </div>
            </div>
            <button class="hw-btn hw-btn-sm hw-btn-gold flex items-center space-x-1 cursor-pointer transition shrink-0"
                    onclick="WorkbenchProjects.restoreCanvasFromTrash('${esc(c.id)}')"
                    title="恢复此画布到分镜画布台">
              <i data-lucide="rotate-ccw" class="w-3 h-3"></i>
              <span>恢复画布</span>
            </button>
          </div>
        `).join('');
      }
    }

    window.lucide?.createIcons();
  }

  // 18. 恢复素材库资产
  async function restoreAssetFromTrash(id) {
    // 显式降级：失败不本地伪造成功。
    try {
      const res = await fetch(`/api/asset-registry/recycle-bin/${encodeURIComponent(id)}/restore`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: globalTrashState.registryRevision })
      });
      if (!res.ok) {
        const failure = await classifyFetchFailure(res);
        showToast(failure.kind === 'not_integrated' ? '素材恢复接口未接入' : ('恢复失败：' + failure.message), 'error');
        return;
      }
    } catch (e) {
      showToast(degradationMessage('service_unavailable', 0), 'error');
      return;
    }

    globalTrashState.assets = globalTrashState.assets.filter(a => a.id !== id);
    showToast('素材已安全恢复至素材库', 'success');
    refreshGlobalTrash();
  }

  async function restoreProjectContentFromTrash(entryId) {
    try {
      const response = await fetch(`/api/asset-registry/project-recycle/${encodeURIComponent(entryId)}/restore`, { method: 'POST', credentials: 'same-origin' });
      if (!response.ok) throw new Error('项目内容恢复失败');
      showToast('项目内容已恢复', 'success');
      await refreshGlobalTrash();
    } catch (error) {
      showToast(error.message || '项目内容恢复失败', 'danger');
    }
  }

  // 19. 恢复工程画布
  async function restoreCanvasFromTrash(id) {
    // 显式降级：失败不本地伪造成功。
    try {
      const res = await fetch(`/api/canvases/${encodeURIComponent(id)}/restore`, {
        method: 'POST',
        credentials: 'same-origin'
      });
      if (!res.ok) {
        const failure = await classifyFetchFailure(res);
        showToast(failure.kind === 'not_integrated' ? '画布恢复接口未接入' : ('恢复失败：' + failure.message), 'error');
        return;
      }
    } catch (e) {
      showToast(degradationMessage('service_unavailable', 0), 'error');
      return;
    }

    globalTrashState.canvases = globalTrashState.canvases.filter(c => c.id !== id);
    showToast('工程画布已安全恢复至分镜台', 'success');
    refreshGlobalTrash();
  }

  // 10. 键盘快捷键监听：按“·”（Backquote 点号键）进入当前选中项目的影视工坊
  function setupKeyboardShortcuts() {
    window.addEventListener('keydown', (e) => {
      // 正在输入框中键入时忽略
      const target = e.target;
      if (target && (target.matches('input, textarea, select') || target.isContentEditable)) {
        return;
      }

      // 支持中英文按键输入下的反引号点号键
      if (e.key === '`' || e.key === '·' || e.code === 'Backquote') {
        e.preventDefault();
        const activeId = state.activeProjectId || (state.projects[0] && state.projects[0].id) || 'proj-01';
        openProject(activeId, 'workshop');
      }
    });
  }

  function rebind() {
    if (!document.getElementById('projectsCardsStream')) return;
    render();
    window.lucide?.createIcons();
  }

  // 项目卡「资产规模」格：消费真实 GET /api/asset-registry/assets 的 total（全库已登记素材数）。
  // 端点不可用时如实回退「未接入」，绝不显示伪造数字。
  async function syncAssetScale() {
    const nodes = document.querySelectorAll('[data-gw-asset-scale]');
    if (!nodes.length) return;
    try {
      const res = await fetch('/api/asset-registry/assets?limit=1', { credentials: 'same-origin' });
      const body = await res.json().catch(() => null);
      if (res.ok && body && Number.isFinite(Number(body.total))) {
        nodes.forEach(el => {
          el.textContent = Number(body.total) + ' 项';
          el.className = 'gw-type-body-sm font-mono font-bold text-cyan-300 leading-tight';
          el.removeAttribute('data-gw-degradation');
          el.title = '真实已登记素材总数（GET /api/asset-registry/assets 的 total）';
        });
      } else {
        nodes.forEach(el => {
          el.textContent = '未接入';
          el.setAttribute('data-gw-degradation', res.status === 503 ? 'service_unavailable' : 'not_integrated');
          el.title = '素材注册表端点不可用（HTTP ' + res.status + '）';
        });
      }
    } catch (e) {
      nodes.forEach(el => {
        el.textContent = '暂不可用';
        el.setAttribute('data-gw-degradation', 'service_unavailable');
        el.title = '素材注册表请求异常';
      });
    }
  }

  function init() {
    if (!document.getElementById('projectsCardsStream')) return;
    syncFilters();
    // 不在初始化阶段读资产规模：此时卡片尚未由 render() 生成，会命中 0 个节点。
    load();
    setupKeyboardShortcuts();
    if (new URLSearchParams(location.search).get('openTrash') === '1') {
      setTimeout(openGlobalTrashDrawer, 0);
    }
    try {
      const savedView = readViewMode();
      if (savedView === 'table' || savedView === 'grid') {
        switchView(savedView);
      }
    } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    load,
    render,
    rebind,
    switchView,
    filterType,
    filterScope,
    toggleArchived: btn => filterScope(state.filterScope === 'archived' ? 'active' : 'archived', btn),
    search,
    selectProject,
    openProject,
    openEditProject,
    handleUpdateProject,
    openGlobalTrashDrawer,
    switchGlobalTrashTab,
    refreshGlobalTrash,
    restoreAssetFromTrash,
    restoreProjectContentFromTrash,
    restoreCanvasFromTrash,
    handleCreateProject,
    confirmAction,
    executeConfirmedAction,
    archiveProject,
    unarchiveProject,
    trashProject,
    restoreTrashedProject,
    showToast
  };
})();
