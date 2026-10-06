/* Shared frontend helpers: API access, navigation, formatting, UI primitives. */

const API = '/api';

/* ---- constants (kept in sync with backend/models.py) ---------------- */
const RESOURCE_TYPES = ['personnel', 'equipment', 'time'];
const OBJECTIVE_TYPES = ['makespan', 'total_completion', 'weighted_completion',
  'tardiness', 'cost', 'custom'];
const HARD_CONSTRAINT_TYPES = ['precedence', 'time_window', 'fixed_start',
  'resource_capacity', 'non_overlap', 'max_concurrent', 'resource_assignment'];
const SOFT_CONSTRAINT_TYPES = ['due_date', 'preferred_window', 'min_gap',
  'resource_balance', 'setup_time', 'max_makespan'];
const SOLVER_NAMES = ['lp', 'ip', 'genetic', 'simulated_annealing', 'greedy'];

/* Monday == 0 ... Sunday == 6, matching backend WEEKDAY_*. */
const WEEKDAYS = [
  { index: 0, label: '周一' }, { index: 1, label: '周二' },
  { index: 2, label: '周三' }, { index: 3, label: '周四' },
  { index: 4, label: '周五' }, { index: 5, label: '周六' },
  { index: 6, label: '周日' },
];
const DAY_PLAN_CLOSED = 'closed';
const DAY_PLAN_WEEKDAY = 'weekday';

const SOLVER_LABELS = {
  lp: '线性规划（松弛）',
  ip: '整数规划（分支定界）',
  genetic: '遗传算法',
  simulated_annealing: '模拟退火',
  greedy: '贪心（优先规则）',
};

const OBJECTIVE_LABELS = {
  makespan: '最小完工时间',
  total_completion: '总完工时间',
  weighted_completion: '加权完工时间',
  tardiness: '加权拖期',
  cost: '资源成本',
  custom: '自定义组合',
};

const RESOURCE_TYPE_LABELS = {
  personnel: '人员',
  equipment: '设备',
  time: '时间',
};

const HARD_CONSTRAINT_LABELS = {
  precedence: '先后顺序',
  time_window: '时间窗口',
  fixed_start: '固定开始时间',
  resource_capacity: '资源容量',
  non_overlap: '互斥（不重叠）',
  max_concurrent: '最大并发数',
  resource_assignment: '资源指派',
};

const SOFT_CONSTRAINT_LABELS = {
  due_date: '截止日期（拖期）',
  preferred_window: '偏好窗口',
  min_gap: '最小间隔',
  resource_balance: '资源均衡',
  setup_time: '准备/切换时间',
  max_makespan: '最大完工时间',
};

const STATUS_LABELS = {
  optimal: '最优',
  feasible: '可行',
  infeasible: '不可行',
  timeout: '超时',
  error: '错误',
};

function objectiveLabel(t) { return OBJECTIVE_LABELS[t] || t; }
function resourceTypeLabel(t) { return RESOURCE_TYPE_LABELS[t] || t; }
function constraintLabel(t) {
  return HARD_CONSTRAINT_LABELS[t] || SOFT_CONSTRAINT_LABELS[t] || t;
}
function solverLabel(n) { return SOLVER_LABELS[n] || n; }
function statusLabel(s) { return STATUS_LABELS[s] || s; }

const PALETTE = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
  '#edc949', '#af7aa1', '#ff9da7', '#9c755f', '#bab0ab', '#86bcb6', '#b07aa1'];

/* ---- current problem selection --------------------------------------- */
function currentProblemId() {
  const q = new URLSearchParams(location.search).get('id');
  if (q) { localStorage.setItem('activeProblem', q); return q; }
  return localStorage.getItem('activeProblem') || null;
}

function setProblemParam(id) {
  const url = new URL(location.href);
  if (id) {
    localStorage.setItem('activeProblem', id);
    url.searchParams.set('id', id);
  } else {
    localStorage.removeItem('activeProblem');
    url.searchParams.delete('id');
  }
  history.replaceState(null, '', url);
}

/* ---- API ------------------------------------------------------------- */
async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const msg = (body && (body.error || (body.details && body.details.join('; ')))) || res.statusText;
    throw new Error(msg);
  }
  return body;
}

function toast(msg, type = 'ok') {
  let el = document.querySelector('.toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'toast ' + type;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3200);
}

/* ---- navigation ------------------------------------------------------ */
const NAV = [
  ['index.html', '仪表盘'],
  ['resources.html', '资源管理'],
  ['calendar.html', '班次/日历'],
  ['tasks.html', '任务与依赖'],
  ['constraints.html', '约束配置'],
  ['solvers.html', '求解器与参数'],
  ['gantt.html', '甘特图'],
  ['results.html', '结果与目标值'],
  ['sensitivity.html', '敏感性分析'],
  ['compare.html', '方案对比'],
  ['report.html', '报告生成'],
  ['history.html', '历史实例'],
];

function renderNav(active) {
  const nav = document.getElementById('nav');
  if (!nav) return;
  nav.innerHTML = NAV.map(([href, label]) =>
    `<a href="${href}${currentProblemId() ? '?id=' + encodeURIComponent(currentProblemId()) : ''}"
        class="${active === href ? 'active' : ''}">${label}</a>`).join('');
}

/* Wire up the shared sidebar (navigation + problem picker). */
function initSidebar(active, { staleBanner = true } = {}) {
  renderNav(active);
  const sel = document.getElementById('problem-picker');
  if (sel) {
    renderProblemPicker(sel, (id) => { setProblemParam(id); location.reload(); });
  }
  if (staleBanner && active !== 'index.html') {
    const pid = currentProblemId();
    if (pid) mountStaleBanner(pid);
  }
}

function renderProblemPicker(selectEl, onSelect, includeEmpty = true) {
  api('/problems').then(({ problems }) => {
    const cur = currentProblemId();
    let html = includeEmpty ? '<option value="">— 请选择问题实例 —</option>' : '';
    html += problems.map(p =>
      `<option value="${p.id}" ${p.id === cur ? 'selected' : ''}>${p.name || p.id}</option>`).join('');
    selectEl.innerHTML = html;
    if (onSelect) selectEl.addEventListener('change', () => onSelect(selectEl.value));
    // First visit only: adopt the first problem and reload exactly once so the
    // page loads it.  On later loads `cur` is set, so this never fires again —
    // that guard is what stops the infinite refresh loop.
    if (!cur && problems.length) {
      selectEl.value = problems[0].id;
      if (onSelect) onSelect(problems[0].id);
    }
  }).catch(e => toast('Failed to load problems: ' + e.message, 'error'));
}

/* ---- formatting ------------------------------------------------------ */
function fmt(v, digits = 2) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(digits);
  return String(v);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function statusBadge(status) {
  const cls = { optimal: 'ok', feasible: 'info', infeasible: 'bad',
    timeout: 'warn', error: 'bad' }[status] || 'muted';
  return `<span class="badge ${cls}">${escapeHtml(statusLabel(status))}</span>`;
}

/* ---- result freshness ----------------------------------------------- */
const FRESHNESS_LABELS = {
  current: '有效',
  stale: '已失效',
  unverified: '无法核对',
};

function freshnessInfo(item) {
  // Tolerate legacy items without provenance.
  if (!item.freshness) {
    return item.input_fingerprint == null
      ? { state: 'unverified', stale: true, reasons: ['结果生成于版本追溯功能上线前，无法自动核对'] }
      : { state: 'current', stale: false, reasons: [] };
  }
  return { state: item.freshness, stale: !!item.stale,
           reasons: item.stale_reasons || [] };
}

function freshnessBadge(item) {
  const info = freshnessInfo(item);
  const cls = { current: 'ok', stale: 'bad', unverified: 'warn' }[info.state] || 'muted';
  const title = info.reasons.length ? escapeHtml(info.reasons.join('\n')) : '输入未变，结果有效';
  return `<span class="badge ${cls} freshness-badge" title="${title}">${
      escapeHtml(FRESHNESS_LABELS[info.state] || info.state)}</span>`;
}

function freshnessReasons(reasons) {
  if (!reasons || !reasons.length) return '';
  return `<ul class="stale-reasons">${reasons.map(r =>
    `<li>${escapeHtml(r)}</li>`).join('')}</ul>`;
}

/* Fetch /api/problems/<id>/freshness once per page and render a dismissible
 * banner listing every artefact that needs re-running.  Used on every page
 * that shows solutions / analyses / reports. */
async function mountStaleBanner(problemId) {
  if (!problemId) return null;
  let summary;
  try { summary = await api(`/problems/${problemId}/freshness`); }
  catch (_) { return null; }
  const c = summary.counts;
  if (!c.stale_total) return summary;

  const groups = [
    ['排程方案', summary.solutions],
    ['敏感性分析', summary.sensitivity],
    ['报告', summary.reports],
  ];
  const items = [];
  for (const [label, rows] of groups) {
    for (const r of rows) {
      if (!r.stale) continue;
      const state = FRESHNESS_LABELS[r.freshness || 'stale'] || r.freshness;
      items.push(`<li><b>${label} ${escapeHtml(r.id)}</b>
        <span class="badge ${r.freshness === 'unverified' ? 'warn' : 'bad'}">${escapeHtml(state)}</span>
        ${freshnessReasons((r.stale_reasons || []).slice(0, 3))}</li>`);
    }
  }
  const banner = document.createElement('div');
  banner.className = 'stale-banner';
  banner.innerHTML = `
    <div class="stale-banner-head">
      ⚠️ 班次/输入已变更（实例 v${summary.problem_version}）：${c.stale_total} 个历史结果需要重跑
      （方案 ${c.solutions_stale}/${c.solutions_total}，
       分析 ${c.sensitivity_stale}/${c.sensitivity_total}，
       报告 ${c.reports_stale}/${c.reports_total}）
      <button class="btn sm danger" onclick="this.closest('.stale-banner').remove()">忽略本提示</button>
    </div>
    <ul class="stale-banner-list">${items.join('')}</ul>`;
  const main = document.querySelector('.main');
  if (main) main.prepend(banner);
  return summary;
}

/* ---- DOM helpers ----------------------------------------------------- */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    el.append(c.nodeType ? c : document.createTextNode(c));
  }
  return el;
}

function colorFor(key, i = 0) {
  let hash = 0;
  const s = String(key);
  for (let j = 0; j < s.length; j++) hash = (hash * 31 + s.charCodeAt(j)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}
