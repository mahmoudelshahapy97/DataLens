/**
 * The operator console.
 *
 * Escaping, fetch, the CSRF header and the accessibility primitives come from
 * ../shared/core.js, which the workspace page imports too. This file used to carry
 * its own copies; two implementations of a security-relevant escape function is one
 * too many, and they had already begun to diverge.
 */

import {
  announce,
  api,
  applyTheme,
  esc,
  relative,
  roveFocus,
  setAuthFailureHandler,
  setHeaderProvider,
  toggleTheme,
  trapFocus,
} from './shared/core.js';
import { confirmSheet } from './shared/dialogs.js';


const STORAGE_KEY = 'vanna.identity';
const THEME_KEY   = 'vanna.theme';

let identity = null;
let me = null;
let tab = 'overview';
let scope = null;                      // tenant currently being administered
let cache = { examples: [], instructions: [], packs: [], tenants: [], users: [], starters: [], datasources: [], engines: [] };

/** Id of the rule currently open for editing, if any. */
let editingRule = null;

const el = (id) => document.getElementById(id);

// ------------------------------------------------------------ language ---
//
// The same mechanism as the main app, deliberately: one idea to learn, and the
// two pages share `vanna.locale` so following the "Admin console" link from an
// Arabic app does not land on an English page.
//
// Separate dictionaries though (`admin.en.json`, not `en.json`) -- the key sets
// barely overlap, and merging them would ship every console string to every
// user who only ever opens the chat.

const LOCALE_KEY = 'vanna.locale';
const LOCALES = { en: 'English', ar: 'العربية' };
const RTL = new Set(['ar']);

let locale = localStorage.getItem(LOCALE_KEY) || 'en';
let strings = {};

/** Translate a key, interpolating {placeholders}. Falls back to the key. */
function t(key, values) {
  let text = strings[key];
  if (text === undefined) {
    if (locale !== 'en') console.warn('[i18n] missing', locale, key);
    text = key;
  }
  if (values) {
    for (const name of Object.keys(values)) {
      text = text.split(`{${name}}`).join(values[name]);
    }
  }
  return text;
}

async function loadLocale(next) {
  locale = LOCALES[next] ? next : 'en';
  localStorage.setItem(LOCALE_KEY, locale);
  try {
    // /admin/locales/, beside the page that uses it -- not /locales/, which is the
    // workspace app's dictionary. They were one directory with `admin.` prefixed
    // filenames; when they were split, this path was not updated and every string
    // on this screen silently became its own lookup key.
    const response = await fetch(`/admin/locales/${locale}.json`, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`locale ${locale}: ${response.status}`);
    strings = await response.json();
  } catch (error) {
    // t() falls back to keys rather than blanking the page -- but say so. A screen
    // rendering `tab.billing` and `console.title` looks like a deliberate label
    // scheme until somebody opens the console and sees this.
    console.error('Could not load translations; falling back to raw keys.', error);
    strings = {};
  }
  applyLocale();
}

function applyLocale() {
  document.documentElement.setAttribute('lang', locale);
  document.documentElement.setAttribute('dir', RTL.has(locale) ? 'rtl' : 'ltr');

  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-attr]').forEach((node) => {
    node.dataset.i18nAttr.split(',').forEach((pair) => {
      const [attr, key] = pair.split(':');
      if (attr && key) node.setAttribute(attr.trim(), t(key.trim()));
    });
  });

  if (me) { paintTabs(); render(); }
}


function toast(message) {
  const node = el('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}



// ----------------------------------------------------------------- tabs ---

function tabsFor() {
  const list = [];
  // Every admin gets the overview and the audit trail: both surfaces scope
  // themselves server-side, so there is nothing to hide behind a tier here.
  list.push(['overview', t('tab.overview')]);
  if (me && me.is_platform_admin) list.push(['tenants', t('tab.tenants')]);
  if (me && me.is_platform_admin) list.push(['accounts', t('tab.accounts')]);
  list.push(['members', t('tab.members')]);
  list.push(['permissions', t('tab.permissions')]);
  list.push(['billing', t('tab.billing')]);
  list.push(['audit', t('tab.audit')]);
  list.push(['review', t('tab.review')]);
  list.push(['verified', t('tab.verified')]);
  list.push(['domains', t('tab.domains')]);
  list.push(['rules', t('tab.rules')]);
  list.push(['library', t('tab.library')]);
  list.push(['starters', t('tab.starters')]);
  list.push(['new', t('tab.add')]);
  return list;
}

function paintTabs() {
  el('tabs').innerHTML = tabsFor().map(([id, label]) => (
    // role=tab plus a managed tabindex: the container is one stop in the tab order
    // and arrows move within it, which is what a screen reader announces a set of
    // tabs as. A row of plain buttons is just a row of buttons.
    `<button type="button" role="tab" data-tab="${id}" ` +
    `aria-selected="${tab === id}" tabindex="${tab === id ? '0' : '-1'}">${esc(label)}</button>`
  )).join('');
  el('tabs').querySelectorAll('button').forEach((button) => {
    button.onclick = () => showTab(button.dataset.tab);
  });
}

// Registered once rather than on every repaint, which would stack a listener per
// render and fire the handler n times on the nth paint.
roveFocus(el('tabs'), 'button[role="tab"]');

function showTab(next) {
  // Without this the 30-second poll keeps firing against a panel that is no
  // longer on screen, and stacks another one every time the tab is revisited.
  if (next !== 'overview') stopAutoRefresh();
  tab = next;
  paintTabs();
  render();
  const label = (tabsFor().find(([id]) => id === next) || [null, next])[1];
  announce(label);
}

// -------------------------------------------------------------- loading ---

async function refresh() {
  el('content').innerHTML = '<div class="empty">Loading…</div>';
  try {
    me = await api('/api/vanna/v2/me');
  } catch (error) {
    el('content').innerHTML = `
      <div class="empty">
        ${esc(error.message)}<br /><br />
        <a href="/">${t('console.signInLink')}</a>${t('console.thenComeBack')}
      </div>`;
    return;
  }

  scope = scope || me.tenant.id;
  el('who').textContent = `${me.user.email} · ${me.tenant.name}`;
  el('who').className = 'tag ' + (me.is_admin ? 'admin' : '');

  if (!me.is_admin) {
    el('content').innerHTML =
      `<div class="empty">${t('console.notAdmin')}</div>`;
    el('tabs').innerHTML = '';
    return;
  }

  // Instructions and packs are tenant-scoped in the path. The library's own
  // `/admin/instructions` still exists but is served no store: its guard reads
  // group membership with no tenant to check it against, which refuses a
  // platform admin working on somebody else's workspace.
  const workspace = encodeURIComponent(scope);
  const [examples, instructions, packs] = await Promise.all([
    api('/api/vanna/v2/admin/examples').catch(() => ({ examples: [] })),
    api(`/api/vanna/v2/admin/tenants/${workspace}/instructions`)
      .catch(() => ({ instructions: [] })),
    api(`/api/vanna/v2/admin/tenants/${workspace}/instruction-packs`)
      .catch(() => ({ packs: [] })),
  ]);
  cache.examples = examples.examples || [];
  cache.instructions = instructions.instructions || [];
  cache.packs = packs.packs || [];

  if (me.is_platform_admin) {
    const [tenants, datasources, engines] = await Promise.all([
      api('/api/vanna/v2/admin/tenants').catch(() => ({ tenants: [] })),
      api('/api/vanna/v2/admin/datasources').catch(() => ({ datasources: [] })),
      api('/api/vanna/v2/admin/engines').catch(() => ({ engines: [] })),
    ]);
    cache.tenants = tenants.tenants || [];
    cache.datasources = datasources.datasources || [];
    cache.engines = engines.engines || [];
  }

  await loadScope();
  paintTabs();
  render();
}

async function loadScope() {
  const [users, starters] = await Promise.all([
    api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/users`).catch(() => ({ users: [] })),
    api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/starters`).catch(() => ({ starters: [] })),
  ]);
  cache.users = users.users || [];
  cache.starters = starters.starters || [];
}

// -------------------------------------------------------------- renders ---

function scopePicker() {
  // Only a platform admin can act on a workspace other than their own; for a
  // tenant admin there is nothing to pick.
  if (!me.is_platform_admin || cache.tenants.length < 2) {
    return `<p class="muted small">Workspace: <strong>${esc(scope)}</strong></p>`;
  }
  return `
    <label for="scope">Workspace</label>
    <select id="scope" style="max-width:340px">
      ${cache.tenants.map((ws) => (
        `<option value="${esc(ws.id)}" ${ws.id === scope ? 'selected' : ''}>${esc(ws.name)} (${esc(ws.id)})</option>`
      )).join('')}
    </select>`;
}

function wireScopePicker() {
  const picker = el('scope');
  if (!picker) return;
  picker.onchange = async () => {
    scope = picker.value;
    await loadScope();
    render();
  };
}

function render() {
  const target = el('content');

  if (tab === 'overview') return renderOverview(target);
  if (tab === 'audit')    return renderAudit(target);
  if (tab === 'tenants')  return renderTenants(target);
  if (tab === 'accounts') return renderAccounts(target);
  if (tab === 'members')  return renderMembers(target);
  if (tab === 'permissions') return renderPermissions(target);
  if (tab === 'billing')  return renderBilling(target);
  if (tab === 'starters') return renderStarters(target);
  if (tab === 'review')   return renderReview(target);
  if (tab === 'verified') return renderVerified(target);
  if (tab === 'domains')  return renderDomains(target);
  if (tab === 'rules')    return renderRules(target);
  if (tab === 'library')  return renderLibrary(target);
  return renderAdd(target);
}

// -- overview --------------------------------------------------------------
//
// The landing tab. Every number on it was already being collected -- the
// console simply never asked -- so this is a screen over existing data rather
// than new measurement.
//
// It fetches its own payload rather than reading `cache`, like renderBilling
// and renderPermissions do: refresh() reloads that on every tab switch, and the
// overview wants a different window and a different workspace from the one the
// rest of the console is scoped to.

/** Handle of the 30-second poll, when auto-refresh is on. */
let overviewTimer = null;

//: `tenant` is null until the first paint decides: a platform admin starts on
//: the whole platform, everybody else on the only workspace they can see.
let overviewState = { tenant: null, days: 30, auto: false };

function stopAutoRefresh() {
  if (overviewTimer) { clearInterval(overviewTimer); overviewTimer = null; }
}

function dashTenant() {
  if (overviewState.tenant === null) {
    overviewState.tenant = me && me.is_platform_admin ? '' : scope;
  }
  return overviewState.tenant;
}

/** Locale-aware integers. A KPI row of raw digits is hard to read at a glance. */
function num(value) {
  return new Intl.NumberFormat(locale).format(Number(value) || 0);
}

/**
 * A workspace row's counts.
 *
 * `list_tenants_with_usage` nests them under `usage` rather than putting them
 * beside `id` and `name`. Every field defaults to 0 here so one accessor covers
 * both that and a row the endpoint omitted them from.
 */
function usageOf(workspace) {
  const usage = (workspace && workspace.usage) || {};
  return {
    questions: Number(usage.questions) || 0,
    succeeded: Number(usage.succeeded) || 0,
    liked: Number(usage.liked) || 0,
    disliked: Number(usage.disliked) || 0,
    members: Number(usage.members) || 0,
    active_users: Number(usage.active_users) || 0,
    last_activity: usage.last_activity || null,
  };
}

/** A rate of `null` is "nothing was asked", which is not the same as zero. */
function pct(rate) {
  return rate === null || rate === undefined ? '—' : `${(rate * 100).toFixed(1)}%`;
}

function usd(dollars) {
  if (dollars === null || dollars === undefined) return '—';
  return new Intl.NumberFormat(locale, { style: 'currency', currency: 'USD' })
    .format(Number(dollars) || 0);
}

function kpiCard(key, value, note) {
  return `
    <div class="kpi">
      <div class="k">${esc(t(key))}</div>
      <div class="v">${esc(value)}</div>
      ${note ? `<div class="n">${esc(note)}</div>` : ''}
    </div>`;
}

/**
 * Put a Plotly figure in a card.
 *
 * Two things this has to get right, both of which fail silently otherwise.
 *
 * `data` and `layout` are element *properties*, not attributes -- the component
 * takes objects, and an attribute would arrive as the string "[object Object]".
 *
 * And they may only be assigned once the element has upgraded. Setting a
 * property on a custom element the browser has not defined yet creates an own
 * property that shadows the accessor the class installs a moment later, so the
 * chart renders empty with nothing in the console to say why.
 */
async function mountChart(hostId, data, layout) {
  if (!el(hostId)) return;
  await customElements.whenDefined('plotly-chart');

  const host = el(hostId);
  if (!host) return;              // the tab was repainted while we waited

  const chart = document.createElement('plotly-chart');
  chart.theme = document.documentElement.getAttribute('data-theme') === 'dark'
    ? 'dark' : 'light';
  chart.data = data;
  chart.layout = {
    height: 240,
    margin: { t: 12, r: 12, b: 34, l: 46 },
    // The card already paints a surface; an opaque plot background would sit on
    // it as a lighter rectangle in dark mode and a darker one in light.
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    showlegend: false,
    ...layout,
  };
  host.replaceChildren(chart);
}

/** The palette, read from the stylesheet so the charts follow the theme. */
function chartColors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name, fallback) => (style.getPropertyValue(name) || fallback).trim();
  return {
    primary: read('--primary', '#4f46e5'),
    good: read('--good', '#059669'),
    bad: read('--bad', '#dc2626'),
    muted: read('--muted', '#64748b'),
    border: read('--border', '#e2e8f0'),
  };
}

function dashScopePicker() {
  // Only a platform admin can look past their own workspace; for anybody else
  // there is nothing to pick and the server would refuse the attempt anyway.
  if (!me.is_platform_admin) return '';
  const tenant = dashTenant();
  return `
    <div>
      <label for="dash-scope">${esc(t('ov.workspace'))}</label>
      <select id="dash-scope">
        <option value="" ${tenant === '' ? 'selected' : ''}>${esc(t('ov.allWorkspaces'))}</option>
        ${cache.tenants.map((ws) => (
          `<option value="${esc(ws.id)}" ${ws.id === tenant ? 'selected' : ''}>${esc(ws.name)}</option>`
        )).join('')}
      </select>
    </div>`;
}

function wireDashScope(repaint) {
  const picker = el('dash-scope');
  if (!picker) return;
  picker.onchange = () => { overviewState.tenant = picker.value; repaint(); };
}

async function renderOverview(target) {
  // Repainting is what the poll does, so clear it first: otherwise a refresh
  // that lands while auto-refresh is on leaves two timers running.
  stopAutoRefresh();

  const params = new URLSearchParams({ days: String(overviewState.days) });
  const tenant = dashTenant();
  if (tenant) params.set('tenant_id', tenant);

  let data;
  try {
    data = await api(`/api/vanna/v2/admin/overview?${params}`);
  } catch (error) {
    if (tab !== 'overview') return;
    target.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  // Recorded before the guard below. The audit tab builds its filter from this
  // vocabulary, and dropping a response that already arrived just because the
  // operator has moved on left that filter with no options at all.
  overviewActions = data.actions || overviewActions;

  // The console opens on this tab, so its fetch is in flight while the operator
  // is already clicking somewhere else. Writing the result into #content
  // regardless would paint the overview over whichever tab they landed on --
  // and the tab strip would still say they were on that other one. Only the
  // *painting* is guarded; the data itself is fine to keep.
  if (tab !== 'overview') return;

  const kpis = data.kpis || {};
  const platformWide = data.scope === '';
  const windows = [7, 30, 90];

  target.innerHTML = `
    <div class="filters">
      ${dashScopePicker()}
      <div>
        <label for="dash-days">${esc(t('ov.window'))}</label>
        <select id="dash-days">
          ${windows.map((n) => (
            `<option value="${n}" ${n === overviewState.days ? 'selected' : ''}>${esc(t('ov.lastDays', { n }))}</option>`
          )).join('')}
        </select>
      </div>
      <div class="check">
        <input type="checkbox" id="dash-auto" ${overviewState.auto ? 'checked' : ''} />
        <label for="dash-auto">${esc(t('ov.autoRefresh'))}</label>
      </div>
    </div>

    <div class="kpi-row">
      ${platformWide
        ? kpiCard('kpi.workspaces', num(kpis.workspaces),
                  t('ov.activeOf', { n: num(kpis.active_workspaces) }))
        : kpiCard('kpi.members', num(kpis.members))}
      ${kpiCard('kpi.questions', num(kpis.questions), t('ov.lastDays', { n: data.window_days }))}
      ${kpiCard('kpi.successRate', pct(kpis.success_rate),
                t('ov.ofN', { n: num(kpis.succeeded) }))}
      ${kpiCard('kpi.activeUsers', num(kpis.active_users))}
      ${'cost_usd' in kpis ? kpiCard('kpi.spend', usd(kpis.cost_usd)) : ''}
    </div>

    <div class="split">
      <div class="card chart-card">
        <p class="q">${esc(t('ov.questionsPerDay'))}</p>
        <div id="ov-series"><p class="muted small">${esc(t('common.loading'))}</p></div>
      </div>
      <div class="card chart-card">
        <p class="q">${esc(platformWide ? t('ov.byWorkspace') : t('ov.feedback'))}</p>
        <div id="ov-breakdown"><p class="muted small">${esc(t('common.loading'))}</p></div>
      </div>
    </div>

    <div class="split">
      <div class="card">
        <p class="q">${esc(t('ov.recentActivity'))}</p>
        ${renderFeed(data.recent || [])}
      </div>
      <div class="card">
        <p class="q">${esc(t('ov.health'))}</p>
        ${renderHealth(data, platformWide)}
      </div>
    </div>

    ${platformWide ? renderWorkspaceTable(data.workspaces || []) : renderKnowledge(data)}
  `;

  paintOverviewCharts(data, platformWide);

  wireDashScope(() => renderOverview(target));
  el('dash-days').onchange = () => {
    overviewState.days = Number(el('dash-days').value) || 30;
    renderOverview(target);
  };
  el('dash-auto').onchange = () => {
    overviewState.auto = el('dash-auto').checked;
    if (overviewState.auto) startAutoRefresh(target); else stopAutoRefresh();
  };
  target.querySelectorAll('[data-goto]').forEach((button) => {
    button.onclick = () => showTab(button.dataset.goto);
  });

  if (overviewState.auto) startAutoRefresh(target);
}

function startAutoRefresh(target) {
  stopAutoRefresh();
  overviewTimer = setInterval(() => {
    // The tab can change without showTab being the thing that repaints, and a
    // poll that outlives its panel writes into a detached node.
    if (tab !== 'overview') { stopAutoRefresh(); return; }
    renderOverview(target);
  }, 30000);
}

function paintOverviewCharts(data, platformWide) {
  const colors = chartColors();
  const series = data.series || [];
  const days = series.map((row) => row.day);

  mountChart('ov-series', [
    {
      type: 'scatter', mode: 'lines', name: t('ov.questions'),
      x: days, y: series.map((row) => row.questions),
      line: { color: colors.primary, width: 2 },
      fill: 'tozeroy', fillcolor: 'rgba(79,70,229,.12)',
    },
    {
      type: 'scatter', mode: 'lines', name: t('ov.succeeded'),
      x: days, y: series.map((row) => row.succeeded),
      line: { color: colors.good, width: 1.5, dash: 'dot' },
    },
  ], { showlegend: true, legend: { orientation: 'h', y: -0.25 } });

  if (platformWide) {
    // Busiest first, and only as many as fit: a bar per workspace is unreadable
    // past a dozen and this panel is a ranking, not an inventory.
    // `list_tenants_with_usage` nests the counts under `usage`; read at the
    // top level every one of them is `undefined`, which `num()` renders as a
    // confident 0 -- indistinguishable from a workspace nobody used.
    const top = [...(data.workspaces || [])]
      .sort((a, b) => usageOf(b).questions - usageOf(a).questions)
      .slice(0, 8)
      .reverse();
    mountChart('ov-breakdown', [{
      type: 'bar', orientation: 'h',
      y: top.map((ws) => ws.name || ws.id),
      x: top.map((ws) => usageOf(ws).questions),
      marker: { color: colors.primary },
    }], { margin: { t: 12, r: 12, b: 34, l: 120 } });
    return;
  }

  const kpis = data.kpis || {};
  const liked = Number(kpis.liked) || 0;
  const disliked = Number(kpis.disliked) || 0;
  const unrated = Math.max(0, (Number(kpis.questions) || 0) - liked - disliked);
  mountChart('ov-breakdown', [{
    type: 'bar',
    x: [t('ov.liked'), t('ov.disliked'), t('ov.unrated')],
    y: [liked, disliked, unrated],
    // Green/red is the right reading here -- this is the one chart in the
    // product where the two colours mean approval and rejection literally.
    marker: { color: [colors.good, colors.bad, colors.muted] },
  }], {});
}

function renderFeed(events) {
  if (!events.length) return `<p class="muted small">${esc(t('ov.noActivity'))}</p>`;
  return `
    <ul class="feed">
      ${events.map((event) => `
        <li>
          <button type="button" data-goto="audit">
            <span class="action">${esc(event.action)}</span>
            ${event.target ? `<span class="mono small">${esc(event.target)}</span>` : ''}
            <span class="muted small">${esc(event.actor_email || '')}</span>
            <span class="when">${esc(relative(event.created_at, t, locale))}</span>
          </button>
        </li>`).join('')}
    </ul>`;
}

/** `last_ok` is tri-state, and "never checked" must not read as "fine". */
function healthTag(lastOk) {
  if (lastOk === true) return `<span class="tag active">${esc(t('ov.healthOk'))}</span>`;
  if (lastOk === false) return `<span class="tag inactive">${esc(t('ov.healthFailing'))}</span>`;
  return `<span class="tag candidate">${esc(t('ov.healthUnknown'))}</span>`;
}

function renderHealth(data, platformWide) {
  const sources = data.data_sources;
  // Omitted rather than empty: this is a platform-admin surface, and a
  // workspace admin should be told it is not theirs, not shown an empty list
  // that reads as "no databases".
  if (!sources) return `<p class="muted small">${esc(t('ov.healthPlatformOnly'))}</p>`;
  if (!sources.length) {
    return `<p class="muted small">${esc(platformWide ? t('ov.allHealthy') : t('ov.noSources'))}</p>`;
  }
  return `
    <div class="scroll-x">
      <table class="data">
        <thead><tr>
          ${platformWide ? `<th>${esc(t('ov.workspace'))}</th>` : ''}
          <th>${esc(t('ov.source'))}</th><th>${esc(t('ov.status'))}</th>
          <th>${esc(t('ov.checked'))}</th>
        </tr></thead>
        <tbody>
          ${sources.map((source) => `
            <tr>
              ${platformWide ? `<td class="mono small">${esc(source.tenant_id)}</td>` : ''}
              <td class="mono small">${esc(source.label || source.data_source_id)}</td>
              <td>
                ${healthTag(source.last_ok)}
                ${source.last_error ? `<div class="muted small">${esc(source.last_error)}</div>` : ''}
              </td>
              <td class="muted small">${source.last_checked_at
                ? esc(relative(source.last_checked_at, t, locale)) : esc(t('ov.never'))}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

function renderWorkspaceTable(rows) {
  if (!rows.length) return '';
  return `
    <div class="card scroll-x">
      <p class="q">${esc(t('ov.perWorkspace'))}</p>
      <table class="data">
        <thead><tr>
          <th>${esc(t('ov.workspace'))}</th><th>${esc(t('kpi.members'))}</th>
          <th>${esc(t('kpi.questions'))}</th><th>${esc(t('kpi.successRate'))}</th>
          <th>${esc(t('ov.feedback'))}</th><th>${esc(t('ov.lastActivity'))}</th>
        </tr></thead>
        <tbody>
          ${[...rows].sort((a, b) => usageOf(b).questions - usageOf(a).questions).map((ws) => `
            <tr>
              <td>
                ${esc(ws.name || ws.id)}
                ${ws.is_active === false ? `<span class="tag inactive">${esc(t('ov.inactive'))}</span>` : ''}
                <div class="mono small muted">${esc(ws.id)}</div>
              </td>
              <td>${num(usageOf(ws).members)}</td>
              <td>${num(usageOf(ws).questions)}</td>
              <td>${esc(pct(usageOf(ws).questions
                ? usageOf(ws).succeeded / usageOf(ws).questions : null))}</td>
              <td class="small">
                <span style="color:var(--good)">▲ ${num(usageOf(ws).liked)}</span>
                <span style="color:var(--bad)">▼ ${num(usageOf(ws).disliked)}</span>
              </td>
              <td class="muted small">${usageOf(ws).last_activity
                ? esc(relative(usageOf(ws).last_activity, t, locale)) : esc(t('ov.never'))}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

/**
 * Verified examples against the review queue.
 *
 * Counted from `cache`, which refresh() already loaded -- but that cache
 * describes the workspace the *console* is scoped to, so the panel is only
 * shown when the overview happens to be looking at the same one. Showing acme's
 * review queue under a globex heading is worse than showing nothing.
 */
function renderKnowledge(data) {
  if (data.scope !== scope) return '';
  const verified = cache.examples.filter((e) => e.status === 'verified').length;
  const candidates = cache.examples.filter((e) => e.status === 'candidate').length;
  const total = verified + candidates;
  return `
    <div class="card">
      <p class="q">${esc(t('ov.knowledge'))}</p>
      <span class="stat"><b>${num(verified)}</b>${esc(t('ov.verified'))}</span>
      <span class="stat"><b>${num(candidates)}</b>${esc(t('ov.candidates'))}</span>
      <span class="stat"><b>${num(cache.instructions.length)}</b>${esc(t('ov.instructions'))}</span>
      <div class="bar"><span style="width:${total ? (verified / total) * 100 : 0}%"></span></div>
      ${candidates
        ? `<div class="actions">
             <button class="act" type="button" data-goto="review">${esc(t('ov.reviewQueue'))}</button>
           </div>`
        : ''}
    </div>`;
}

// -- audit trail -----------------------------------------------------------
//
// `admin_audit` has recorded every privileged mutation since the beginning and
// nothing ever read it back: the trail was write-only, which is the same as not
// having one the first time somebody asks what happened.
//
// Two logs, because they answer different questions. The admin trail is what
// operators did to the system; the access log is what the agent did on behalf
// of users, and which of those attempts were refused.

//: The audit filter's vocabulary, served by the overview rather than hardcoded
//: here. Populated on the first overview paint, which is the landing tab.
let overviewActions = [];

/** Fetch the action vocabulary if nothing has painted the overview yet. */
async function ensureActions() {
  if (overviewActions.length) return;
  try {
    // days=1 -- this call is only wanted for `actions`, and there is no reason
    // to make the database roll up a month to answer it.
    const params = new URLSearchParams({ days: '1' });
    const tenant = dashTenant();
    if (tenant) params.set('tenant_id', tenant);
    const body = await api(`/api/vanna/v2/admin/overview?${params}`);
    overviewActions = body.actions || [];
  } catch (error) {
    // A filter with no options is a usable screen; a blank tab is not.
    console.warn('Could not load the audit action list', error);
  }
}

let auditState = {
  view: 'admin', action: '', actor: '', limit: 100, denied: false, expanded: null,
};

/** The workspace the audit tab is reading, following the overview's picker. */
function auditTenant() {
  return dashTenant();
}

function auditQuery() {
  const params = new URLSearchParams({ limit: String(auditState.limit) });
  const tenant = auditTenant();
  if (tenant) params.set('tenant_id', tenant);
  if (auditState.view === 'admin') {
    if (auditState.action) params.set('action', auditState.action);
    if (auditState.actor) params.set('actor_email', auditState.actor);
  } else if (auditState.denied) {
    params.set('denied_only', 'true');
  }
  return params;
}

async function renderAudit(target) {
  await ensureActions();
  const adminView = auditState.view === 'admin';

  // The access log is per-workspace by nature -- there is no platform-wide
  // reading of it -- so "all workspaces" falls back to the console's own scope,
  // and the panel says which workspace it is showing rather than implying all.
  const accessTenant = auditTenant() || scope;

  let rows = [];
  let failure = '';
  try {
    const params = auditQuery();
    if (!adminView) params.set('tenant_id', accessTenant);
    const path = adminView ? 'audit' : 'access-log';
    const body = await api(`/api/vanna/v2/admin/${path}?${params}`);
    rows = body.events || [];
  } catch (error) {
    failure = error.message;
  }

  // Same race as the overview: this awaits, and the tab can change under it.
  if (tab !== 'audit') return;

  target.innerHTML = `
    <div class="actions" style="margin:0 0 14px">
      <button class="act ${adminView ? 'primary' : ''}" type="button" data-view="admin">
        ${esc(t('audit.adminActions'))}
      </button>
      <button class="act ${adminView ? '' : 'primary'}" type="button" data-view="access">
        ${esc(t('audit.accessLog'))}
      </button>
    </div>

    ${adminView ? '' : `<div class="banner">${esc(t('audit.accessScope', { workspace: accessTenant }))}</div>`}

    <div class="filters">
      ${dashScopePicker()}
      ${adminView ? `
        <div>
          <label for="audit-action">${esc(t('audit.action'))}</label>
          <select id="audit-action">
            <option value="">${esc(t('audit.allActions'))}</option>
            ${overviewActions.map((action) => (
              `<option value="${esc(action)}" ${action === auditState.action ? 'selected' : ''}>${esc(action)}</option>`
            )).join('')}
          </select>
        </div>
        <div>
          <label for="audit-actor">${esc(t('audit.actor'))}</label>
          <input id="audit-actor" type="search" value="${esc(auditState.actor)}"
                 placeholder="${esc(t('audit.actorHint'))}" />
        </div>` : `
        <div class="check">
          <input type="checkbox" id="audit-denied" ${auditState.denied ? 'checked' : ''} />
          <label for="audit-denied">${esc(t('audit.deniedOnly'))}</label>
        </div>`}
      <div>
        <label for="audit-limit">${esc(t('audit.limit'))}</label>
        <select id="audit-limit">
          ${[50, 100, 250, 500].map((n) => (
            `<option value="${n}" ${n === auditState.limit ? 'selected' : ''}>${n}</option>`
          )).join('')}
        </select>
      </div>
      <div class="check">
        <button class="act" type="button" id="audit-reset">${esc(t('audit.reset'))}</button>
        ${adminView
          ? `<button class="act" type="button" id="audit-export">${esc(t('audit.export'))}</button>`
          : ''}
      </div>
    </div>

    ${failure
      ? `<div class="empty">${esc(failure)}</div>`
      : (adminView ? adminTable(rows) : accessTable(rows))}
  `;

  target.querySelectorAll('[data-view]').forEach((button) => {
    button.onclick = () => {
      auditState.view = button.dataset.view;
      auditState.expanded = null;
      renderAudit(target);
    };
  });
  wireDashScope(() => renderAudit(target));

  const action = el('audit-action');
  if (action) action.onchange = () => { auditState.action = action.value; renderAudit(target); };

  const actor = el('audit-actor');
  if (actor) {
    actor.onchange = () => { auditState.actor = actor.value.trim(); renderAudit(target); };
  }

  const denied = el('audit-denied');
  if (denied) denied.onchange = () => { auditState.denied = denied.checked; renderAudit(target); };

  el('audit-limit').onchange = () => {
    auditState.limit = Number(el('audit-limit').value) || 100;
    renderAudit(target);
  };
  el('audit-reset').onclick = () => {
    auditState = { ...auditState, action: '', actor: '', denied: false, expanded: null };
    renderAudit(target);
  };
  const exporter = el('audit-export');
  if (exporter) exporter.onclick = () => exportAudit();

  target.querySelectorAll('tr.expandable').forEach((row) => {
    row.onclick = () => {
      auditState.expanded = auditState.expanded === row.dataset.key ? null : row.dataset.key;
      renderAudit(target);
    };
  });
}

/** The expanded JSON under a row, or nothing. */
function detailRow(key, span, payload) {
  if (auditState.expanded !== key) return '';
  return `
    <tr class="detail">
      <td colspan="${span}"><pre>${esc(JSON.stringify(payload ?? {}, null, 2))}</pre></td>
    </tr>`;
}

function adminTable(rows) {
  if (!rows.length) return `<div class="empty">${esc(t('audit.none'))}</div>`;
  return `
    <div class="card scroll-x">
      <table class="data">
        <thead><tr>
          <th>${esc(t('audit.time'))}</th><th>${esc(t('audit.actor'))}</th>
          <th>${esc(t('audit.action'))}</th><th>${esc(t('ov.workspace'))}</th>
          <th>${esc(t('audit.target'))}</th><th>${esc(t('audit.ip'))}</th>
        </tr></thead>
        <tbody>
          ${rows.map((event) => `
            <tr class="expandable" data-key="${esc(event.id)}">
              <td class="muted small" title="${esc(event.created_at || '')}">
                ${esc(relative(event.created_at, t, locale))}
              </td>
              <td class="small">${esc(event.actor_email || '—')}</td>
              <td><span class="mono small">${esc(event.action)}</span></td>
              <td class="mono small">${esc(event.tenant_id || '—')}</td>
              <td class="mono small">${esc(event.target || '—')}</td>
              <td class="mono small muted">${esc(event.actor_ip || '—')}</td>
            </tr>
            ${detailRow(event.id, 6, event.details)}`).join('')}
        </tbody>
      </table>
    </div>`;
}

function accessTable(rows) {
  if (!rows.length) return `<div class="empty">${esc(t('audit.none'))}</div>`;
  return `
    <div class="card scroll-x">
      <table class="data">
        <thead><tr>
          <th>${esc(t('audit.time'))}</th><th>${esc(t('audit.actor'))}</th>
          <th>${esc(t('audit.event'))}</th><th>${esc(t('audit.tool'))}</th>
          <th>${esc(t('audit.outcome'))}</th>
        </tr></thead>
        <tbody>
          ${rows.map((event) => `
            <tr class="expandable" data-key="${esc(event.event_id)}">
              <td class="muted small" title="${esc(event.created_at || '')}">
                ${esc(relative(event.created_at, t, locale))}
              </td>
              <td class="small">${esc(event.user_email || '—')}</td>
              <td class="mono small">${esc(event.event_type || '—')}</td>
              <td class="mono small">${esc(event.tool_name || '—')}</td>
              <td>${event.access_granted === false
                ? `<span class="tag inactive">${esc(t('audit.denied'))}</span>`
                : `<span class="tag active">${esc(t('audit.granted'))}</span>`}</td>
            </tr>
            ${detailRow(event.event_id, 5, event.payload)}`).join('')}
        </tbody>
      </table>
    </div>`;
}

/**
 * Download the trail as a spreadsheet.
 *
 * Fetched and handed over as a blob rather than opened in a new tab: the export
 * carries the same filters as the table, and going through the normal fetch
 * path means a refusal arrives as a message instead of a blank window.
 */
async function exportAudit() {
  const tenant = auditTenant();
  try {
    const response = await fetch(`/api/vanna/v2/admin/audit.csv?${auditQuery()}`, {
      credentials: 'include',
      headers: tenant ? { 'X-Tenant-Id': tenant } : {},
    });
    if (!response.ok) throw new Error(`${response.status}`);

    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `audit-${tenant || 'platform'}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(t('audit.exported'));
  } catch (error) {
    toast(t('audit.exportFailed'));
    console.error('CSV export failed', error);
  }
}

// -- workspaces ------------------------------------------------------------

function renderTenants(target) {
  target.innerHTML = `
    <div class="card scroll-x">
      <table class="data">
        <thead><tr>
          <th>${t('ws.workspace')}</th><th>${t('ws.dataSource')}</th><th>${t('ws.writes')}</th><th>${t('ws.ownKey')}</th>
          <th>${t('tab.members')}</th>
          <th>${t('ws.questions30')}</th><th>${t('ws.rated')}</th><th>${t('ws.lastActivity')}</th><th></th>
        </tr></thead>
        <tbody>
          ${cache.tenants.map((ws, index) => `
            <tr>
              <td>
                <strong>${esc(ws.name)}</strong>
                <span class="tag ${ws.is_active ? 'active' : 'inactive'}">${ws.is_active ? 'active' : 'disabled'}</span>
                <div class="muted mono small">${esc(ws.id)}</div>
              </td>
              <td class="mono small">${esc(ws.data_source)}</td>
              <td><input type="checkbox" data-writes="${index}" ${ws.allow_writes ? 'checked' : ''}
                    title="Allow INSERT/UPDATE/DELETE. Also needs VANNA_ALLOW_WRITES on the server." /></td>
              <td><input type="checkbox" data-byo="${index}" ${ws.allow_byo_key !== false ? 'checked' : ''}
                    title="Allow members to answer on their own LLM API key." /></td>
              <td>${ws.usage ? ws.usage.members : '—'}</td>
              <td>${ws.usage ? ws.usage.questions : '—'}</td>
              <td class="small">
                ${ws.usage ? `<span style="color:var(--good)">▲ ${ws.usage.liked}</span>
                             <span style="color:var(--bad)">▼ ${ws.usage.disliked}</span>` : '—'}
              </td>
              <td class="muted small">${ws.usage && ws.usage.last_activity
                ? esc(new Date(ws.usage.last_activity).toLocaleString()) : 'never'}</td>
              <td style="white-space:nowrap">
                <button class="act" data-act="manage" data-i="${index}">${t('ws.manage')}</button>
                <button class="act" data-act="edit" data-i="${index}">${t('ws.dataSource')}</button>
                <button class="act" data-act="databases" data-i="${index}">${t('ws.databases')}</button>
                <button class="act danger" data-act="del" data-i="${index}">${t('common.delete')}</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin-top:0">${t('ws.create')}</h3>
      <p class="muted small" style="margin-top:0">
        A workspace is a tenant: its own database, its own members, its own
        knowledge. You become its first admin. Credentials are stored
        server-side and are never sent back to this page.
      </p>
      <div class="grid2">
        <div>
          <label>${t('ws.identifier')}</label>
          <input id="t-id" placeholder="acme" />
        </div>
        <div>
          <label>${t('ws.displayName')}</label>
          <input id="t-name" placeholder="Acme Corp" />
        </div>
      </div>
      <label>${t('ws.description')}</label>
      <input id="t-desc" placeholder="Sales analytics for the Acme account" />

      <label>${t('ws.connection')}</label>
      <select id="t-mode">
        <option value="existing">${t('ws.pickDb')}</option>
        <option value="fields">${t('ws.otherServer')}</option>
        <option value="default">${t('ws.serverDefault')}</option>
      </select>

      <div id="t-existing">
        ${datasourceField('t-url')}
      </div>

      <div id="t-fields" style="display:none">
        <label>${t('ws.engine')}</label>
        <select id="t-engine">
          ${(cache.engines || []).map((e) => (
            `<option value="${esc(e.name)}"${e.name === 'postgres' ? ' selected' : ''}>${esc(e.label)}</option>`
          )).join('')}
        </select>
        <!-- Fields are rendered from the engine registry, so the form always
             asks for exactly what the chosen driver needs. It used to be a
             fixed Postgres host/port/database set, which is why every workspace
             created here was a Postgres one whatever the operator intended. -->
        <div id="t-engine-fields"></div>
      </div>

      <div class="actions">
        <button class="act" id="t-test">${t('ws.test')}</button>
        <button class="act primary" id="t-create">${t('ws.createBtn')}</button>
        <span id="t-status" class="small muted"></span>
      </div>
    </div>`;

  target.querySelectorAll('[data-writes]').forEach((box) => {
    const tenant = cache.tenants[Number(box.dataset.writes)];
    box.onchange = async () => {
      // Writes are the one setting that widens what a workspace may do, so it
      // is confirmed rather than toggled, and it still needs VANNA_ALLOW_WRITES
      // set on the deployment before it has any effect.
      if (box.checked && !confirm(
        `Allow admins of ${tenant.name} to run INSERT, UPDATE and DELETE?

` +
        'DROP and TRUNCATE stay blocked. Every write is audited. This also ' +
        'requires VANNA_ALLOW_WRITES=true on the server.'
      )) { box.checked = false; return; }
      try {
        await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}`, {
          method: 'PATCH', body: JSON.stringify({ allow_writes: box.checked }),
        });
        toast(box.checked ? 'Writes enabled' : 'Writes disabled');
        await refresh();
      } catch (error) { toast(error.message); box.checked = !box.checked; }
    };
  });

  target.querySelectorAll('[data-byo]').forEach((box) => {
    const tenant = cache.tenants[Number(box.dataset.byo)];
    box.onchange = async () => {
      // Turning it *off* is the restrictive direction, so that is the one that
      // gets confirmed: it stops members who rely on their own key from asking
      // anything once the workspace quota is spent.
      if (!box.checked && !confirm(
        `Stop members of ${tenant.name} using their own LLM API key?\n\n` +
        'Their questions will be answered on the server key and counted against ' +
        'the workspace quota. Anyone relying on a personal key to work past the ' +
        'limit will be blocked at it.'
      )) { box.checked = true; return; }
      try {
        await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}`, {
          method: 'PATCH', body: JSON.stringify({ allow_byo_key: box.checked }),
        });
        toast(box.checked ? 'Personal keys allowed' : 'Personal keys blocked');
        await refresh();
      } catch (error) { toast(error.message); box.checked = !box.checked; }
    };
  });

  target.querySelectorAll('[data-act]').forEach((button) => {
    const tenant = cache.tenants[Number(button.dataset.i)];
    button.onclick = () => {
      if (button.dataset.act === 'manage') {
        scope = tenant.id;
        loadScope().then(() => showTab('members'));
        return;
      }
      if (button.dataset.act === 'edit') return editDataSource(tenant);
      if (button.dataset.act === 'databases') return manageDatabases(tenant);
      deleteTenant(tenant);
    };
  });

  wireDatasourceField('t-url');

  el('t-mode').onchange = () => {
    const mode = el('t-mode').value;
    el('t-existing').style.display = mode === 'existing' ? 'block' : 'none';
    el('t-fields').style.display = mode === 'fields' ? 'block' : 'none';
    el('t-status').textContent = '';
  };

  if (el('t-engine')) {
    paintEngineFields(el('t-engine').value);
    el('t-engine').onchange = () => {
      paintEngineFields(el('t-engine').value);
      el('t-status').textContent = '';   // a tested connection is not this one
    };
  }

  el('t-test').onclick = async () => {
    const status = el('t-status');
    status.textContent = 'Testing...';
    status.style.color = '';
    try {
      const result = await api('/api/vanna/v2/admin/datasources/test', {
        method: 'POST', body: JSON.stringify(newTenantConnection()),
      });
      status.textContent = result.ok ? `Connected to ${result.data_source}` : result.error;
      status.style.color = result.ok ? 'var(--good)' : 'var(--bad)';
    } catch (error) {
      status.textContent = error.message;
      status.style.color = 'var(--bad)';
    }
  };

  el('t-create').onclick = createTenant;
}

/** A select of databases found on the server, plus a free-text escape hatch. */
function datasourceField(id) {
  return `
    <select id="${id}-pick">
      <option value="">${t('ws.defaultConn')}</option>
      ${cache.datasources.map((d) => `<option value="${esc(d.url)}">${esc(d.label)}</option>`).join('')}
      <option value="__custom">${t('ws.customConn')}</option>
    </select>
    <input id="${id}" style="display:none;margin-top:8px"
           placeholder="postgresql://user:password@host:5432/database" />`;
}

function wireDatasourceField(id) {
  const picker = el(`${id}-pick`);
  const field = el(id);
  if (!picker || !field) return;
  picker.onchange = () => {
    const custom = picker.value === '__custom';
    field.style.display = custom ? 'block' : 'none';
    if (!custom) field.value = picker.value;
  };
}

function datasourceValue(id) {
  const picker = el(`${id}-pick`);
  return picker.value === '__custom' ? el(id).value.trim() : picker.value;
}

/**
 * Render the field set for one engine.
 *
 * Driven entirely by /admin/engines, which is the same registry the server uses
 * to build the URL -- so the form cannot ask for fields the driver ignores, or
 * omit ones it needs.
 */
function paintEngineFields(engineName) {
  const engine = (cache.engines || []).find((e) => e.name === engineName);
  const mount = el('t-engine-fields');
  if (!mount) return;
  if (!engine) { mount.innerHTML = ''; return; }

  // Two per row where they are short; the help text below each is where the
  // engine-specific gotchas live (a Snowflake account is not a URL, a SQLite
  // path is on the server, not on your machine).
  mount.innerHTML = `
    <div class="grid2">
      ${engine.fields.map((f) => `
        <div>
          <label>${esc(f.label)}${f.required ? ' <span style="color:var(--bad)">*</span>' : ''}</label>
          <input id="t-f-${esc(f.name)}"
                 type="${f.kind === 'password' ? 'password' : 'text'}"
                 value="${esc(f.default || '')}"
                 dir="ltr"
                 placeholder="${esc(f.help || '')}" />
          ${f.help ? `<div class="muted small">${esc(f.help)}</div>` : ''}
        </div>`).join('')}
    </div>
    ${engine.notes ? `<div class="banner" style="margin-top:12px">${esc(engine.notes)}</div>` : ''}`;
}

/** The connection half of the create form, in whichever mode is selected. */
function newTenantConnection() {
  const mode = el('t-mode').value;
  if (mode === 'default') return {};
  if (mode === 'existing') return { database_url: datasourceValue('t-url') || '' };

  const engineName = el('t-engine') ? el('t-engine').value : 'postgres';
  const engine = (cache.engines || []).find((e) => e.name === engineName);
  const values = { engine: engineName };
  (engine ? engine.fields : []).forEach((f) => {
    const input = el(`t-f-${f.name}`);
    // Passwords keep their whitespace; everything else is trimmed, because a
    // trailing space in a hostname is a typo and in a password is a character.
    if (input) values[f.name] = f.kind === 'password' ? input.value : input.value.trim();
  });
  return values;
}

async function createTenant() {
  const body = {
    id: el('t-id').value.trim().toLowerCase(),
    name: el('t-name').value.trim(),
    description: el('t-desc').value.trim(),
    ...newTenantConnection(),
  };
  if (!body.id || !body.name) return toast(t('ws.needIdName'));

  // Test before creating. A workspace bound to a database nobody has reached
  // is indistinguishable from a broken deployment for everyone invited to it,
  // and the person who can tell the difference is the one filling in this form.
  if (el('t-mode').value !== 'default') {
    try {
      const check = await api('/api/vanna/v2/admin/datasources/test', {
        method: 'POST', body: JSON.stringify(newTenantConnection()),
      });
      if (!check.ok) return toast(`Not created: ${check.error}`);
    } catch (error) { return toast(error.message); }
  }
  try {
    await api('/api/vanna/v2/admin/tenants', { method: 'POST', body: JSON.stringify(body) });
    toast(`Created ${body.name}`);
    await refresh();
  } catch (error) { toast(error.message); }
}

async function editDataSource(tenant) {
  // A prompt() for a raw connection string was the previous version of this.
  // It offered no way to check the connection before committing every member
  // of a workspace to it, and no hint about what a valid string looks like.
  const html = `
    <div class="card">
      <h3 style="margin-top:0">Data source for ${esc(tenant.name)}</h3>
      <p class="muted small" style="margin-top:0">
        Currently <span class="mono">${esc(tenant.data_source)}</span>.
        Credentials are stored server-side and are never sent back to this page.
      </p>
      <div class="grid2">
        <div><label>${t('ws.host')}</label><input id="ds-host" placeholder="db.example.com" /></div>
        <div><label>${t('ws.port')}</label><input id="ds-port" value="5432" /></div>
      </div>
      <div class="grid2">
        <div><label>${t('ws.database')}</label><input id="ds-db" placeholder="analytics" /></div>
        <div><label>${t('ws.sslmode')}</label>
          <select id="ds-ssl">
            <option value="">(driver default)</option>
            <option value="require">require</option>
            <option value="verify-full">verify-full</option>
            <option value="disable">disable</option>
          </select>
        </div>
      </div>
      <div class="grid2">
        <div><label>${t('ws.user')}</label><input id="ds-user" placeholder="readonly" /></div>
        <div><label>${t('ws.password')}</label><input id="ds-pass" type="password" /></div>
      </div>
      <label>${t('conn.orPaste')}</label>
      <input id="ds-url" placeholder="postgresql://user:password@host:5432/database" />
      <div class="actions">
        <button class="act" id="ds-test">${t('ws.test')}</button>
        <button class="act primary" id="ds-save" disabled>${t('common.save')}</button>
        <button class="act" id="ds-clear">${t('ws.useDefault')}</button>
        <span id="ds-status" class="small muted"></span>
      </div>
    </div>`;

  el('content').innerHTML = html + el('content').innerHTML;

  const payload = () => ({
    host: el('ds-host').value.trim(),
    port: el('ds-port').value.trim(),
    database: el('ds-db').value.trim(),
    username: el('ds-user').value.trim(),
    password: el('ds-pass').value,
    sslmode: el('ds-ssl').value,
    database_url: el('ds-url').value.trim(),
  });

  let tested = null;

  el('ds-test').onclick = async () => {
    const status = el('ds-status');
    status.textContent = 'Testing...';
    status.style.color = '';
    try {
      const result = await api('/api/vanna/v2/admin/datasources/test', {
        method: 'POST', body: JSON.stringify(payload()),
      });
      if (result.ok) {
        tested = payload();
        status.textContent = `Connected to ${result.data_source}`;
        status.style.color = 'var(--good)';
        el('ds-save').disabled = false;
      } else {
        tested = null;
        el('ds-save').disabled = true;
        status.textContent = result.error;
        status.style.color = 'var(--bad)';
      }
    } catch (error) {
      status.textContent = error.message;
      status.style.color = 'var(--bad)';
    }
  };

  el('ds-save').onclick = async () => {
    // Only a tested payload can be saved. Binding a workspace to a database
    // nobody has reached is how every member gets a broken app at once.
    if (!tested) return toast(t('ws.testFirst'));
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}`, {
        method: 'PATCH',
        // Structured fields go up as-is; the server composes the URL.
        body: JSON.stringify(tested),
      });
      toast(t('ws.dsUpdated'));
      await refresh();
    } catch (error) { toast(error.message); }
  };

  el('ds-clear').onclick = async () => {
    if (!confirm(`Point ${tenant.name} back at the server default connection?`)) return;
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}`, {
        method: 'PATCH', body: JSON.stringify({ database_url: null }),
      });
      toast(t('ws.usingDefault'));
      await refresh();
    } catch (error) { toast(error.message); }
  };
}

async function deleteTenant(tenant) {
  if (!confirm(
    `Delete ${tenant.name}?\n\nIts members, starters and saved queries go with it. ` +
    'The record of questions asked is kept.'
  )) return;
  try {
    await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}`, { method: 'DELETE' });
    toast(t('common.deleted'));
    if (scope === tenant.id) scope = me.tenant.id;
    await refresh();
  } catch (error) { toast(error.message); }
}

// -- accounts --------------------------------------------------------------
//
// Credentials, distinct from membership. An account can sign in; the Members tab
// decides which workspaces it can then see. Creating one here does not put it in
// any workspace -- that is a separate, deliberate act.

async function renderAccounts(target) {
  let accounts = [];
  try {
    ({ accounts } = await api('/api/vanna/v2/admin/accounts'));
  } catch (error) {
    target.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  target.innerHTML = `
    <div class="banner">
      An account is a login. Membership of a workspace is granted separately, on the
      Members tab -- so creating an account here gives someone a password and nothing
      else to look at until you add them somewhere.
    </div>

    <div class="card scroll-x">
      <table class="data">
        <thead><tr><th>${t('acc.account')}</th><th>${t('acc.status')}</th><th>${t('acc.lastSignIn')}</th><th></th></tr></thead>
        <tbody>
          ${accounts.length ? accounts.map((a, index) => `
            <tr>
              <td>
                <strong>${esc(a.full_name || a.email)}</strong>
                <div class="muted small">${esc(a.email)}</div>
              </td>
              <td>
                <span class="tag ${a.is_active ? 'active' : 'inactive'}">
                  ${a.is_active ? 'active' : 'disabled'}</span>
                ${a.must_change ? '<span class="tag candidate">temp password</span>' : ''}
              </td>
              <td class="muted small">${a.last_login_at
                ? esc(new Date(a.last_login_at).toLocaleString()) : 'never'}</td>
              <td style="white-space:nowrap">
                <button class="act" data-reset="${index}">${t('acc.reset')}</button>
                <button class="act ${a.is_active ? 'danger' : ''}" data-toggle="${index}">
                  ${a.is_active ? t('acc.disable') : t('acc.enable')}
                </button>
              </td>
            </tr>`).join('')
          : `<tr><td colspan="4" class="empty">${t('acc.none')}</td></tr>`}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin-top:0">${t('acc.create')}</h3>
      <p class="muted small" style="margin-top:0">
        A temporary password is generated and shown once. It is not stored in a form
        anyone can read back, and the holder must change it on first sign-in.
      </p>
      <div class="grid2">
        <div><label>${t('acc.email')}</label><input id="a-email" placeholder="ada@example.com" /></div>
        <div><label>${t('acc.name')}</label><input id="a-name" placeholder="Ada Lovelace" /></div>
      </div>
      <div class="actions"><button class="act primary" id="a-create">${t('common.create')}</button></div>
      <div id="a-result"></div>
    </div>`;

  target.querySelectorAll('[data-reset]').forEach((button) => {
    const account = accounts[Number(button.dataset.reset)];
    button.onclick = async () => {
      if (!confirm(`Reset the password for ${account.email}? Their current one stops working immediately.`)) return;
      try {
        const result = await api(
          `/api/vanna/v2/admin/accounts/${encodeURIComponent(account.email)}/reset`,
          { method: 'POST' }
        );
        showTemporaryPassword(result.email, result.temporary_password);
      } catch (error) { toast(error.message); }
    };
  });

  target.querySelectorAll('[data-toggle]').forEach((button) => {
    const account = accounts[Number(button.dataset.toggle)];
    button.onclick = async () => {
      const next = !account.is_active;
      if (!next && !confirm(
        `Disable ${account.email}? Their sessions and API tokens are revoked immediately.`
      )) return;
      try {
        await api(`/api/vanna/v2/admin/accounts/${encodeURIComponent(account.email)}`, {
          method: 'PATCH', body: JSON.stringify({ is_active: next }),
        });
        await refresh();
      } catch (error) { toast(error.message); }
    };
  });

  el('a-create').onclick = async () => {
    const email = el('a-email').value.trim();
    if (!email) return toast(t('acc.needEmail'));
    try {
      const result = await api('/api/vanna/v2/admin/accounts', {
        method: 'POST',
        body: JSON.stringify({ email, full_name: el('a-name').value.trim() }),
      });
      showTemporaryPassword(result.email, result.temporary_password);
      el('a-email').value = ''; el('a-name').value = '';
    } catch (error) { toast(error.message); }
  };
}

/** Show a generated password once, with the reason it will not be shown again. */
function showTemporaryPassword(email, password) {
  el('a-result').innerHTML = `
    <div class="card" style="border-color:var(--good)">
      <strong>Temporary password for ${esc(email)}</strong>
      <pre style="margin-top:8px">${esc(password)}</pre>
      <p class="muted small">
        Only its hash is stored, so this cannot be shown again. Send it over a channel
        you trust; they will be asked to change it on first sign-in.
      </p>
    </div>`;
}

// -- billing ---------------------------------------------------------------

/** Cents as money. Integer arithmetic throughout -- see the payments table. */
function money(cents, currency) {
  return `${(Number(cents || 0) / 100).toFixed(2)} ${String(currency || 'usd').toUpperCase()}`;
}

/** Where a limit came from, in words. */
const SOURCE_NOTE = {
  override: 'set explicitly for this workspace, which beats the plan',
  plan: 'from the plan',
  default: 'the deployment default, since this workspace has no subscription',
};

async function renderBilling(target) {
  let data;
  try {
    data = await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/billing`);
  } catch (error) {
    target.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  const sub = data.subscription;
  const limits = data.limits || {};
  const used = (data.usage || {}).questions || 0;
  const quota = limits.daily_quota || 0;
  // Usage is 30 days and the quota is daily, so this is a ratio of unlike
  // things -- shown as separate numbers rather than one misleading bar.
  const expires = sub && sub.expires_at ? new Date(sub.expires_at) : null;
  const expired = expires && expires < new Date();

  target.innerHTML = `
    ${scopePicker()}

    <div class="card">
      <h3 style="margin-top:0">
        ${esc((data.plan || 'free').toUpperCase())}
        ${sub ? `<span class="tag ${expired || sub.status !== 'active' ? 'inactive' : 'active'}">
          ${esc(expired ? 'expired' : sub.status)}</span>` : ''}
      </h3>
      <p class="muted small" style="margin-top:0">
        ${sub
          ? (expires
              ? `${expired ? 'Expired' : 'Renews or ends'} ${esc(expires.toLocaleDateString())}.
                 ${expired ? 'The workspace has fallen back to the free plan -- it is not locked out.' : ''}`
              : 'Open-ended, with no expiry date.')
          : 'No subscription. This workspace runs on the deployment defaults.'}
      </p>

      <div class="grid2">
        <div>
          <label>${t('bill.questionsPerDay')}</label>
          <div><strong>${quota.toLocaleString()}</strong>
            <span class="muted small">— ${esc(SOURCE_NOTE[limits.quota_source] || '')}</span></div>
        </div>
        <div>
          <label>${t('bill.rowsPerQuery')}</label>
          <div><strong>${(limits.max_rows || 0).toLocaleString()}</strong>
            <span class="muted small">— ${esc(SOURCE_NOTE[limits.rows_source] || '')}</span></div>
        </div>
      </div>
      <p class="muted small">
        ${used.toLocaleString()} question${used === 1 ? '' : 's'} in the last 30 days,
        across everyone in this workspace. The quota is counted per workspace per day,
        not per person.
      </p>
    </div>

    <div class="card">
      <h3 style="margin-top:0">${t('bill.changePlan')}</h3>
      <div class="grid2">
        <div>
          <label>${t('bill.plan')}</label>
          <select id="b-plan">
            ${(data.available_plans || []).map((p) => (
              `<option value="${esc(p.name)}" ${p.name === data.plan ? 'selected' : ''}>
                 ${esc(p.label)} — ${p.daily_quota.toLocaleString()}/day, ${p.max_rows.toLocaleString()} rows
               </option>`
            )).join('')}
          </select>
        </div>
        <div>
          <label>${t('bill.months')}</label>
          <input id="b-months" type="number" min="1" value="12" />
        </div>
      </div>
      <div class="actions">
        <button class="act primary" id="b-set">${t('bill.setPlan')}</button>
        ${sub && sub.status === 'active'
          ? `<button class="act danger" id="b-cancel">${t('common.cancel')}</button>` : ''}
      </div>
      <p class="muted small">
        Setting a plan here does not take payment. Use “Record a payment” below when
        money actually changed hands — that is what leaves an audit trail.
      </p>
    </div>

    <div class="card">
      <h3 style="margin-top:0">${t('bill.record')}</h3>
      <p class="muted small" style="margin-top:0">
        For a payment taken elsewhere — invoice, transfer, or an internal arrangement.
        The reference is what makes this safe to repeat: recording the same one twice
        changes nothing and extends nothing.
      </p>
      <div class="grid2">
        <div><label>${t('bill.reference')}</label><input id="b-ref" placeholder="INV-2026-014" /></div>
        <div><label>${t('bill.amount')}</label><input id="b-amount" type="number" min="0" step="0.01" placeholder="499.00" /></div>
        <div><label>${t('bill.monthsToAdd')}</label><input id="b-pay-months" type="number" min="1" value="12" /></div>
        <div><label>${t('bill.note')}</label><input id="b-desc" placeholder="Annual, paid by transfer" /></div>
      </div>
      <div class="actions"><button class="act primary" id="b-pay">${t('bill.recordBtn')}</button></div>
    </div>

    <div class="card scroll-x">
      <h3 style="margin-top:0">${t('bill.history')}</h3>
      <table class="data">
        <thead><tr>
          <th>${t('bill.date')}</th><th>${t('bill.amount')}</th><th>${t('bill.reference')}</th><th>${t('acc.status')}</th><th>${t('bill.note')}</th>
        </tr></thead>
        <tbody>
          ${(data.payments || []).length ? data.payments.map((p) => `
            <tr>
              <td class="small">${esc(new Date(p.created_at).toLocaleDateString())}</td>
              <td class="mono">${esc(money(p.amount_cents, p.currency))}</td>
              <td class="mono small">${esc(p.provider_ref)}</td>
              <td><span class="tag ${p.status === 'succeeded' ? 'active' : 'inactive'}">${esc(p.status)}</span></td>
              <td class="small">${esc(p.description || '')}</td>
            </tr>`).join('')
          : `<tr><td colspan="5" class="empty">${t('bill.nothing')}</td></tr>`}
        </tbody>
      </table>
    </div>`;

  wireScopePicker();

  el('b-set').onclick = async () => {
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/billing/plan`, {
        method: 'POST',
        body: JSON.stringify({
          plan: el('b-plan').value,
          months: Number(el('b-months').value) || 1,
        }),
      });
      toast(t('bill.planUpdated'));
      render();
    } catch (error) { toast(error.message); }
  };

  const cancel = el('b-cancel');
  if (cancel) cancel.onclick = async () => {
    if (!confirm(
      'Cancel this subscription? The workspace drops to the free plan — it keeps access to its data.'
    )) return;
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/billing/cancel`, {
        method: 'POST',
      });
      render();
    } catch (error) { toast(error.message); }
  };

  el('b-pay').onclick = async () => {
    const reference = el('b-ref').value.trim();
    if (!reference) return toast(t('bill.needReference'));
    try {
      const result = await api(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/billing/payments`,
        {
          method: 'POST',
          body: JSON.stringify({
            reference,
            // Cents on the wire. Money never becomes a float on the server.
            amount_cents: Math.round((Number(el('b-amount').value) || 0) * 100),
            months: Number(el('b-pay-months').value) || 1,
            plan: el('b-plan').value,
            description: el('b-desc').value.trim(),
          }),
        }
      );
      toast(result.recorded ? 'Payment recorded.' : result.note);
      render();
    } catch (error) { toast(error.message); }
  };
}

// -- members ---------------------------------------------------------------

function renderMembers(target) {
  target.innerHTML = `
    <div class="card">${scopePicker()}</div>

    <div class="card scroll-x">
      <table class="data">
        <thead><tr>
          <th>${t('mem.member')}</th><th>${t('mem.role')}</th><th>${t('acc.status')}</th><th>${t('mem.lastSeen')}</th><th></th>
        </tr></thead>
        <tbody>
          ${cache.users.length ? cache.users.map((u, index) => `
            <tr>
              <td>
                <strong>${esc(u.full_name || u.email)}</strong>
                <div class="muted small">${esc(u.email)}</div>
              </td>
              <td>
                <select data-role="${index}">
                  ${['admin', 'analyst', 'viewer'].map((role) => (
                    `<option value="${role}" ${u.role === role ? 'selected' : ''}>${role}</option>`
                  )).join('')}
                </select>
              </td>
              <td><span class="tag ${u.is_active ? 'active' : 'inactive'}">
                ${u.is_active ? 'active' : 'disabled'}</span></td>
              <td class="muted small">${u.last_seen_at
                ? esc(new Date(u.last_seen_at).toLocaleString()) : 'never'}</td>
              <td style="white-space:nowrap">
                <button class="act" data-toggle="${index}">${u.is_active ? t('acc.disable') : t('acc.enable')}</button>
                <button class="act danger" data-remove="${index}">${t('common.remove')}</button>
              </td>
            </tr>`).join('')
          : `<tr><td colspan="5" class="empty">${t('mem.none')}</td></tr>`}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin-top:0">${t('mem.invite')}</h3>
      <p class="muted small" style="margin-top:0">
        <strong>admin</strong> manages members and knowledge ·
        <strong>analyst</strong> asks questions and saves queries ·
        <strong>viewer</strong> reads only.
      </p>
      <div class="grid2">
        <div>
          <label>${t('acc.email')}</label>
          <input id="u-email" placeholder="ada@example.com" />
        </div>
        <div>
          <label>${t('acc.name')}</label>
          <input id="u-name" placeholder="Ada Lovelace" />
        </div>
      </div>
      <label>${t('mem.role')}</label>
      <select id="u-role">
        <option value="analyst">analyst</option>
        <option value="admin">admin</option>
        <option value="viewer">viewer</option>
      </select>
      <div class="actions">
        <button class="act primary" id="u-add">${t('mem.add')}</button>
      </div>
    </div>`;

  wireScopePicker();

  target.querySelectorAll('[data-role]').forEach((select) => {
    const user = cache.users[Number(select.dataset.role)];
    select.onchange = () => updateUser(user, { role: select.value });
  });
  target.querySelectorAll('[data-toggle]').forEach((button) => {
    const user = cache.users[Number(button.dataset.toggle)];
    button.onclick = () => updateUser(user, { is_active: !user.is_active });
  });
  target.querySelectorAll('[data-remove]').forEach((button) => {
    const user = cache.users[Number(button.dataset.remove)];
    button.onclick = () => removeUser(user);
  });

  el('u-add').onclick = addUser;
}

async function updateUser(user, changes) {
  try {
    await api(
      `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/users/${encodeURIComponent(user.id)}`,
      { method: 'PATCH', body: JSON.stringify(changes) }
    );
    toast(t('common.updated'));
  } catch (error) { toast(error.message); }
  await loadScope();
  render();
}

async function removeUser(user) {
  if (!confirm(`Remove ${user.email} from ${scope}?`)) return;
  try {
    await api(
      `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/users/${encodeURIComponent(user.id)}`,
      { method: 'DELETE' }
    );
    toast(t('common.removed'));
  } catch (error) { toast(error.message); }
  await loadScope();
  render();
}

async function addUser() {
  const body = {
    email: el('u-email').value.trim(),
    full_name: el('u-name').value.trim(),
    role: el('u-role').value,
  };
  if (!body.email) return toast(t('acc.needEmail'));
  try {
    await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/users`, {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`Added ${body.email}`);
  } catch (error) { return toast(error.message); }
  await loadScope();
  render();
}

// -- permissions -----------------------------------------------------------

// Three states, cycled in this order. Deliberately not four: insert, update and
// delete move together, because "may write" is how people describe the decision
// and a label reading "write" that quietly withholds delete is a worse surprise
// than one that includes it. Every change still needs human approval before it
// touches a row, and a workspace can require a second approver for destructive
// statements, so delete is gated twice more downstream.
const ACCESS_CYCLE = ['none', 'read', 'write'];

const ACCESS_FLAGS = {
  none:  { can_read: false, can_insert: false, can_update: false, can_delete: false },
  read:  { can_read: true,  can_insert: false, can_update: false, can_delete: false },
  write: { can_read: true,  can_insert: true,  can_update: true,  can_delete: true },
};

/** Which of the three states a stored grant row represents. */
function accessOf(grant) {
  if (!grant || !grant.can_read) return 'none';
  return (grant.can_insert || grant.can_update || grant.can_delete) ? 'write' : 'read';
}

function accessLabel(access) {
  return t(`perm.access.${access}`);
}

// Held here rather than in `cache`, which refresh() reloads on every tab
// switch -- this page fetches its own data, like renderAccounts and
// renderBilling. Defaults to `analyst` because that is the role whose access
// anyone actually comes here to tune; admins already have what they need.
let permState = { role: 'analyst', filter: '', expanded: null, data: null,
                  presets: [], policy: {} };

/** Fetch this role's grants, then paint. Mutations come back through here. */
async function renderPermissions(target) {
  target.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/grants`;
  try {
    permState.data = await api(`${base}?role=${encodeURIComponent(permState.role)}`);
  } catch (error) {
    permState.data = null;
    target.innerHTML = `<div class="card"><p class="muted">${esc(error.message)}</p></div>`;
    return;
  }
  // Presets and the workspace default need the control plane; a deployment
  // without one still gets the matrix rather than an error page.
  const [presets, policy] = await Promise.all([
    api(`${base}/presets`).catch(() => ({ presets: [] })),
    api(`${base}/policy`).catch(() => ({ roles: {} })),
  ]);
  permState.presets = presets.presets || [];
  permState.policy = policy.roles || {};
  paintPermissions(target);
}

/** The workspace default: what a role is granted before anybody edits it. */
function renderPolicyCard() {
  if (!permState.presets.length) return '';
  const role = permState.role;
  const current = permState.policy[role] || {};

  return `
    <div class="card">
      <h3 style="margin-top:0">${t('perm.defaultsTitle')}</h3>
      <p class="muted small">${t('perm.defaultsHelp', { role })}</p>
      <div class="row" style="gap:12px;flex-wrap:wrap;align-items:flex-end">
        <div style="min-width:220px">
          <label for="policy-preset">${t('perm.preset')}</label>
          <select id="policy-preset">
            ${permState.presets.map((p) => `
              <option value="${esc(p.name)}" ${p.name === (current.preset || 'none') ? 'selected' : ''}>
                ${esc(p.title)}
              </option>`).join('')}
          </select>
        </div>
        <label class="policy-check">
          <input type="checkbox" id="policy-new-tables"
                 ${current.apply_to_new_tables ? 'checked' : ''} />
          <span>${t('perm.applyNewTables')}</span>
        </label>
        <label class="policy-check">
          <input type="checkbox" id="policy-enforce-reads"
                 ${current.enforce_reads ? 'checked' : ''} />
          <span>${t('perm.enforceReads')}</span>
        </label>
      </div>
      <p class="muted small">${t('perm.enforceReadsHelp')}</p>
      <div class="actions">
        <button class="act" id="policy-save">${t('perm.saveDefault')}</button>
        <button class="act primary" id="policy-apply">${t('perm.saveAndApply')}</button>
      </div>
      ${current.last_applied_at
        ? `<div class="meta">${t('perm.lastApplied', {
            when: new Date(current.last_applied_at).toLocaleString(),
            version: current.last_applied_version,
          })}</div>`
        : ''}
    </div>`;
}

function wirePolicyCard(target) {
  const save = el('policy-save');
  const apply = el('policy-apply');
  if (!save || !apply) return;

  const body = (shouldApply) => ({
    roles: {
      [permState.role]: {
        preset: el('policy-preset').value,
        apply_to_new_tables: el('policy-new-tables').checked,
        enforce_reads: el('policy-enforce-reads').checked,
      },
    },
    apply: shouldApply,
    mode: 'fill',
  });

  const send = async (shouldApply) => {
    // Applying writes grant rows and moves the version, which re-authorizes any
    // write already approved but not yet run. Saving the intent alone does not,
    // so the two are separate buttons rather than one with a checkbox.
    if (shouldApply && !confirm(t('perm.confirmApply', { role: permState.role }))) return;
    try {
      await api(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/grants/policy`,
        { method: 'PUT', body: JSON.stringify(body(shouldApply)) },
      );
      toast(shouldApply ? t('perm.applied') : t('common.saved'));
      renderPermissions(target);
    } catch (error) { toast(error.message); }
  };

  save.onclick = () => send(false);
  apply.onclick = () => send(true);
}

/** Paint from the payload already held. Filtering and expanding use this, so a
 *  keystroke costs no round trip. */
function paintPermissions(target) {
  const data = permState.data;
  if (!data) return;

  const roles = data.roles || ['admin', 'analyst', 'viewer'];
  // Grants come back for the selected role only, so a flat lookup is enough.
  const tableGrants = {};
  (data.tables || []).forEach((g) => { tableGrants[g.table] = g; });
  const columnGrants = {};
  (data.columns || []).forEach((g) => { columnGrants[`${g.table}.${g.column}`] = g; });

  const needle = permState.filter.trim().toLowerCase();
  const resources = (data.resources || []).filter((r) => {
    if (!needle) return true;
    if (r.table.toLowerCase().includes(needle)) return true;
    return (r.columns || []).some((c) => c.name.toLowerCase().includes(needle));
  });

  target.innerHTML = `
    <div class="card">
      ${scopePicker()}
      <div class="row" style="margin-top:10px;gap:12px;align-items:flex-end">
        <div>
          <label for="perm-role">${t('perm.role')}</label>
          <select id="perm-role" style="max-width:200px">
            ${roles.map((role) => (
              `<option value="${esc(role)}" ${role === permState.role ? 'selected' : ''}>${esc(role)}</option>`
            )).join('')}
          </select>
        </div>
        <div class="grow">
          <label for="perm-filter">${t('perm.filter')}</label>
          <input id="perm-filter" value="${esc(permState.filter)}"
                 placeholder="${esc(t('perm.filterHint'))}" />
        </div>
      </div>
      <p class="muted small" style="margin-bottom:0">${t('perm.explain')}</p>
    </div>

    <div class="card scroll-x">
      <table class="data">
        <thead><tr>
          <th>${t('perm.table')}</th>
          <th>${t('perm.columns')}</th>
          <th>${t('perm.keys')}</th>
          <th>${t('perm.access')}</th>
        </tr></thead>
        <tbody>
          ${resources.length ? resources.map((r) => {
            const access = accessOf(tableGrants[r.table]);
            const next = ACCESS_CYCLE[(ACCESS_CYCLE.indexOf(access) + 1) % ACCESS_CYCLE.length];
            const keys = (r.columns || []).filter((c) => c.is_primary_key);
            const open = permState.expanded === r.table;
            return `
              <tr>
                <td>
                  <button type="button" class="linkish" data-expand="${esc(r.table)}"
                          aria-expanded="${open}">
                    <span class="mono">${esc(r.table)}</span>
                  </button>
                </td>
                <td class="muted small">${(r.columns || []).length}</td>
                <td class="mono small muted">
                  ${keys.length ? keys.map((c) => esc(c.name)).join(', ') : t('perm.noKey')}
                </td>
                <td>
                  <button type="button" class="act perm perm--${access}"
                          data-table="${esc(r.table)}" data-next="${next}"
                          aria-label="${esc(r.table)}: ${esc(accessLabel(access))}. ${esc(t('perm.clickTo', { state: accessLabel(next) }))}">
                    ${esc(accessLabel(access))}
                  </button>
                </td>
              </tr>
              ${open ? `
              <tr>
                <td colspan="4" style="background:var(--surface-2)">
                  ${renderColumnGrants(r, access, columnGrants)}
                </td>
              </tr>` : ''}`;
          }).join('')
          : `<tr><td colspan="4" class="empty">${t('perm.none')}</td></tr>`}
        </tbody>
      </table>
    </div>

    ${renderPolicyCard()}

    <div class="banner">${t('perm.defaultDeny')}</div>`;

  wireScopePicker();
  wirePermissions(target, tableGrants);
  wirePolicyCard(target);
}

/** The per-column write list, shown when a table row is expanded. */
//: The four things a grant can say about one column, in the order they are shown.
//:
//: `read` is the gate: the database refuses `can_filter` without `can_read`, and
//: so does the model, so clearing read clears the rest rather than producing a row
//: the server would reject.
const COLUMN_USES = ['can_read', 'can_filter', 'can_aggregate', 'can_write'];

function renderColumnGrants(resource, access, columnGrants) {
  if (access === 'none') {
    return `<p class="muted small" style="margin:0">${t('perm.columnsNeedRead')}</p>`;
  }

  const writable = access === 'write';
  return `
    <p class="muted small" style="margin-top:0">${t('perm.columnsExplain')}</p>
    <table class="data perm-cols">
      <thead>
        <tr>
          <th>${t('perm.column')}</th>
          ${COLUMN_USES.map((use) => `<th class="num" title="${esc(t(`perm.${use}Help`))}">${t(`perm.${use}`)}</th>`).join('')}
        </tr>
      </thead>
      <tbody>
        ${(resource.columns || []).map((column) => {
          const grant = columnGrants[`${resource.table}.${column.name}`];
          return `
            <tr>
              <td class="mono">
                ${esc(column.name)}
                ${column.is_primary_key ? `<span class="tag">${t('perm.key')}</span>` : ''}
                ${column.is_generated ? `<span class="tag">${t('perm.generatedShort')}</span>` : ''}
              </td>
              ${COLUMN_USES.map((use) => {
                // A generated column can never be assigned -- the database
                // computes it, so a write grant on it would only ever produce a
                // statement the validator refuses. Write also needs the table to
                // be writable; the other three are read-shaped and do not.
                const blocked =
                  (use === 'can_write' && (column.is_generated || !writable));
                const on = blocked ? false : !!(grant && grant[use]);
                const why = column.is_generated && use === 'can_write'
                  ? t('perm.generated')
                  : (use === 'can_write' && !writable ? t('perm.writeNeedsTable') : '');
                return `
                  <td class="num">
                    <input type="checkbox" data-col-use="${use}"
                           data-column="${esc(column.name)}"
                           data-col-table="${esc(resource.table)}"
                           title="${esc(why)}"
                           ${on ? 'checked' : ''} ${blocked ? 'disabled' : ''} />
                  </td>`;
              }).join('')}
            </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

function wirePermissions(target, tableGrants) {
  const roleSelect = el('perm-role');
  if (roleSelect) {
    roleSelect.onchange = () => {
      permState.role = roleSelect.value;
      permState.expanded = null;
      render();
    };
  }

  const filter = el('perm-filter');
  if (filter) {
    filter.oninput = () => {
      permState.filter = filter.value;
      // Re-filter from the payload already held; no refetch for a keystroke.
      const caret = filter.selectionStart;
      paintPermissions(target);
      const again = el('perm-filter');
      if (again) { again.focus(); again.setSelectionRange(caret, caret); }
    };
  }

  target.querySelectorAll('[data-expand]').forEach((button) => {
    button.onclick = () => {
      const name = button.dataset.expand;
      permState.expanded = permState.expanded === name ? null : name;
      paintPermissions(target);
    };
  });

  target.querySelectorAll('[data-table][data-next]').forEach((button) => {
    button.onclick = () => setTableAccess(button, tableGrants);
  });

  target.querySelectorAll('[data-col-use]').forEach((box) => {
    box.onchange = () => setColumnUse(box);
  });
}

async function setTableAccess(button, tableGrants) {
  const table = button.dataset.table;
  const next = button.dataset.next;
  const previous = accessOf(tableGrants[table]);

  if (next === 'write' && !confirm(t('perm.confirmWrite', { table }))) return;

  button.disabled = true;
  try {
    await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/grants/table`, {
      method: 'PUT',
      body: JSON.stringify({
        role: permState.role, table, ...ACCESS_FLAGS[next], autofill_columns: true,
      }),
    });
  } catch (error) {
    button.disabled = false;
    return toast(error.message);
  }
  // A cycling button reads as nothing useful without this.
  announce(`${table}: ${accessLabel(next)}`);
  toast(`${table} · ${accessLabel(next)}`);
  if (previous === 'write' && next !== 'write') permState.expanded = null;
  render();
}

async function setColumnUse(box) {
  const table = box.dataset.colTable;
  const column = box.dataset.column;
  const use = box.dataset.colUse;
  const wanted = box.checked;

  // The held grant, so the other three flags are sent as they already are --
  // this endpoint replaces the row rather than patching one field, so omitting
  // them would silently clear whatever else was granted.
  const columns = (permState.data && permState.data.columns) || [];
  const existing = columns.find((g) => g.table === table && g.column === column) || {};
  const next = {
    can_read: !!existing.can_read,
    can_filter: !!existing.can_filter,
    can_aggregate: !!existing.can_aggregate,
    can_write: !!existing.can_write,
  };
  next[use] = wanted;

  // Read is the gate, in both directions. The database CHECK constraints refuse
  // filter/aggregate/write without read, so clearing read has to clear them too
  // rather than send a row the server will reject; and ticking any of them
  // implies read, which is what the admin plainly means.
  if (use === 'can_read' && !wanted) {
    next.can_filter = next.can_aggregate = next.can_write = false;
  } else if (wanted) {
    next.can_read = true;
  }

  try {
    await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/grants/column`, {
      method: 'PUT',
      body: JSON.stringify({ role: permState.role, table, column, ...next }),
    });
  } catch (error) {
    box.checked = !wanted;   // the control must not claim a state the server refused
    return toast(error.message);
  }

  // Keep the held payload in step. Without this the checkbox is right until the
  // next repaint -- collapsing and re-expanding the table would read the stale
  // grant back and silently undo what the user just saw succeed.
  if (columns.includes(existing) || existing.table) Object.assign(existing, next);
  else columns.push({ role: permState.role, table, column, ...next });

  // Re-render the row: clearing read also cleared three other boxes, and leaving
  // them ticked would show a state the server does not have.
  paintPermissions(el('content'));

  const label = t(`perm.${use}`);
  announce(`${column} · ${label}: ${t(wanted ? 'perm.on' : 'perm.off')}`);
  toast(`${column} · ${label}: ${t(wanted ? 'perm.on' : 'perm.off')}`);
}

/**
 * Every database one workspace may be asked about.
 *
 * The workspace's *binding* -- the single `tenants.database_url` -- is still
 * edited by the button beside this one. This is the list on top of it: a
 * workspace can register several, the chat offers them in a picker, and the
 * permission matrix is per database because the grant tables have always been
 * keyed on it.
 */
async function manageDatabases(tenant) {
  let sources = [];
  try {
    const body = await api(
      `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}/datasources`
    );
    sources = body.data_sources || [];
  } catch (error) {
    return toast(error.message);
  }

  const html = `
    <div class="card">
      <h3 style="margin-top:0">${t('ws.databasesFor', { name: esc(tenant.name) })}</h3>
      <table class="data">
        <thead><tr>
          <th>${t('ws.database')}</th><th>${t('ws.default')}</th><th></th>
        </tr></thead>
        <tbody>
          ${sources.map((source) => `
            <tr>
              <td>
                <strong>${esc(source.label)}</strong>
                <div class="muted mono small">${esc(source.data_source_id)}</div>
              </td>
              <td>
                ${source.is_default
                  ? `<span class="tag active">${t('ws.default')}</span>`
                  : `<button class="act" data-db-default="${esc(source.data_source_id)}">${t('ws.makeDefault')}</button>`}
              </td>
              <td>
                <button class="act danger" data-db-remove="${esc(source.data_source_id)}"
                        ${sources.length < 2 ? 'disabled' : ''}
                        title="${esc(sources.length < 2 ? t('ws.needsOneDatabase') : '')}">
                  ${t('common.remove')}
                </button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>

      <h4>${t('ws.addDatabase')}</h4>
      <label for="db-url">${t('conn.url')}</label>
      <input id="db-url" class="mono" placeholder="postgresql://user:password@host:5432/dbname" />
      <label for="db-label">${t('ws.label')}</label>
      <input id="db-label" placeholder="${esc(t('ws.labelHint'))}" />
      <div class="row" style="margin-top:10px">
        <button class="go" id="db-add">${t('ws.addDatabase')}</button>
        <button class="btn" id="db-close">${t('common.close')}</button>
      </div>
      <p class="muted small" id="db-note">${t('ws.addDatabaseHelp')}</p>
    </div>`;

  openSheet(html);

  el('db-close').onclick = closeSheet;

  el('db-add').onclick = async () => {
    const url = el('db-url').value.trim();
    if (!url) return toast(t('ws.urlRequired'));
    el('db-note').textContent = t('ws.testing');
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}/datasources`, {
        method: 'POST',
        body: JSON.stringify({ database_url: url, label: el('db-label').value.trim() }),
      });
    } catch (error) {
      el('db-note').textContent = '';
      return toast(error.message);
    }
    toast(t('ws.databaseAdded'));
    manageDatabases(tenant);
  };

  document.querySelectorAll('[data-db-default]').forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.dbDefault;
      try {
        await api(
          `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}/datasources/${encodeURIComponent(id)}`,
          { method: 'PATCH', body: JSON.stringify({ is_default: true }) }
        );
      } catch (error) { return toast(error.message); }
      manageDatabases(tenant);
    };
  });

  document.querySelectorAll('[data-db-remove]').forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.dbRemove;
      // Grants written against it survive -- they are keyed on the data source,
      // so they neither apply elsewhere nor come back wrong if it is re-added.
      if (!confirm(t('ws.confirmRemoveDatabase', { name: id }))) return;
      try {
        await api(
          `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant.id)}/datasources/${encodeURIComponent(id)}`,
          { method: 'DELETE' }
        );
      } catch (error) { return toast(error.message); }
      manageDatabases(tenant);
    };
  });
}

// -- business domains -----------------------------------------------------

/**
 * What a slice of the database is *for*, and the words people use about it.
 *
 * Five endpoints have existed since the feature shipped and nothing reached them:
 * the only way to describe a domain was raw SQL against `business_domains`. The
 * model reads these -- name, description, terminology and membership all go into
 * the prompt -- so an unreachable editor meant the feature was effectively off.
 *
 * Membership is *not* an access control, which the store's own docstring is
 * emphatic about. It steers retrieval; grants decide what may be read. Said plainly
 * in the banner, because a screen that lists tables per group invites the other
 * reading.
 */
const domainState = { domains: [], resources: [], loaded: '' };

function domainsUrl(suffix = '') {
  return `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/domains${suffix}`;
}

async function renderDomains(target) {
  target.innerHTML = `<div class="empty">${t('common.loading')}</div>`;

  // The catalog comes from the grants route, which is the one place that already
  // assembles every table the workspace knows -- not only those with a grant row.
  const [listed, grants] = await Promise.all([
    api(domainsUrl()).catch((error) => ({ error: error.message })),
    api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/grants`)
      .catch(() => ({ resources: [] })),
  ]);

  if (listed.error) {
    target.innerHTML = `<div class="card"><p class="muted">${esc(listed.error)}</p></div>`;
    return;
  }
  domainState.domains = listed.domains || [];
  domainState.resources = grants.resources || [];
  domainState.loaded = scope;
  paintDomains(target);
}

function paintDomains(target) {
  const domains = domainState.domains;
  target.innerHTML = `
    <div class="card">${scopePicker()}</div>
    <div class="banner">${t('dom.banner')}</div>
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <h3 style="margin:0">${t('dom.title')}</h3>
        <button class="act primary" id="dom-new">${t('dom.new')}</button>
      </div>
      ${domains.length ? `
      <table class="data" style="margin-top:12px">
        <thead><tr>
          <th>${t('dom.name')}</th>
          <th>${t('dom.tables')}</th>
          <th>${t('dom.terms')}</th>
          <th>${t('dom.enabled')}</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${domains.map((domain, index) => `
            <tr>
              <td>
                <strong>${esc(domain.name)}</strong>
                ${domain.description
                  ? `<div class="muted small">${esc(domain.description)}</div>`
                  : ''}
              </td>
              <td>${(domain.tables || []).length}</td>
              <td>${Object.keys(domain.terminology || {}).length}</td>
              <td>
                <button class="act" data-dom-toggle="${index}"
                        aria-pressed="${!!domain.is_enabled}">
                  ${domain.is_enabled ? t('dom.on') : t('dom.off')}
                </button>
              </td>
              <td>
                <button class="act" data-dom-edit="${index}">${t('common.edit')}</button>
                <button class="act" data-dom-tables="${index}">${t('dom.editTables')}</button>
                <button class="act danger" data-dom-del="${index}">${t('common.delete')}</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`
      : `<div class="empty">${t('dom.empty')}</div>`}
    </div>`;

  wireScopePicker();
  el('dom-new').onclick = () => domainSheet(null);
  target.querySelectorAll('[data-dom-edit]').forEach((button) => {
    button.onclick = () => domainSheet(domains[Number(button.dataset.domEdit)]);
  });
  target.querySelectorAll('[data-dom-tables]').forEach((button) => {
    button.onclick = () => domainTablesSheet(domains[Number(button.dataset.domTables)]);
  });
  target.querySelectorAll('[data-dom-toggle]').forEach((button) => {
    button.onclick = () => toggleDomain(domains[Number(button.dataset.domToggle)]);
  });
  target.querySelectorAll('[data-dom-del]').forEach((button) => {
    button.onclick = () => deleteDomain(domains[Number(button.dataset.domDel)]);
  });
}

/** Terminology rows: the terms of art a schema cannot carry. */
function termRow(term = '', meaning = '') {
  return `
    <div class="row term-row" style="gap:8px;margin-bottom:6px">
      <input class="term-key" placeholder="${esc(t('dom.term'))}" value="${esc(term)}"
             aria-label="${esc(t('dom.term'))}" style="max-width:200px" />
      <input class="term-value" placeholder="${esc(t('dom.meaning'))}" value="${esc(meaning)}"
             aria-label="${esc(t('dom.meaning'))}" />
      <button class="act" data-term-remove type="button"
              aria-label="${esc(t('dom.removeTerm'))}">&times;</button>
    </div>`;
}

function domainSheet(domain) {
  const terms = Object.entries((domain && domain.terminology) || {});
  openSheet(`
    <div class="card">
      <h3 style="margin-top:0">${domain ? t('dom.edit') : t('dom.new')}</h3>
      <label for="dom-name">${t('dom.name')}</label>
      <input id="dom-name" value="${esc(domain ? domain.name : '')}"
             placeholder="${esc(t('dom.namePlaceholder'))}" />
      <label for="dom-desc">${t('dom.description')}</label>
      <textarea id="dom-desc" rows="3"
                placeholder="${esc(t('dom.descriptionPlaceholder'))}">${esc(domain ? domain.description : '')}</textarea>
      <h4>${t('dom.terms')}</h4>
      <p class="muted small">${t('dom.termsHelp')}</p>
      <div id="dom-terms">
        ${(terms.length ? terms : [['', '']]).map(([k, v]) => termRow(k, v)).join('')}
      </div>
      <button class="act" id="dom-add-term" type="button">${t('dom.addTerm')}</button>
      <div class="row" style="margin-top:14px">
        <button class="btn act" data-close>${t('common.cancel')}</button>
        <button class="act primary" id="dom-save">${t('common.save')}</button>
      </div>
    </div>`, { label: domain ? t('dom.edit') : t('dom.new') });

  const wireTerms = () => {
    el('dom-terms').querySelectorAll('[data-term-remove]').forEach((button) => {
      button.onclick = () => {
        // Never remove the last row: an editor with no fields offers no way back.
        const rows = el('dom-terms').querySelectorAll('.term-row');
        if (rows.length === 1) {
          rows[0].querySelectorAll('input').forEach((input) => { input.value = ''; });
          return;
        }
        button.closest('.term-row').remove();
      };
    });
  };
  wireTerms();
  el('dom-add-term').onclick = () => {
    el('dom-terms').insertAdjacentHTML('beforeend', termRow());
    wireTerms();
  };

  el('dom-save').onclick = async () => {
    const name = el('dom-name').value.trim();
    if (!name) return toast(t('dom.nameRequired'));

    const terminology = {};
    el('dom-terms').querySelectorAll('.term-row').forEach((row) => {
      const term = row.querySelector('.term-key').value.trim();
      const meaning = row.querySelector('.term-value').value.trim();
      if (term && meaning) terminology[term] = meaning;
    });
    const body = { name, description: el('dom-desc').value.trim(), terminology };

    try {
      if (domain) {
        await api(domainsUrl(`/${encodeURIComponent(domain.id)}`), {
          method: 'PATCH', body: JSON.stringify(body),
        });
      } else {
        await api(domainsUrl(), { method: 'POST', body: JSON.stringify(body) });
      }
    } catch (error) { return toast(error.message); }
    closeSheet();
    toast(t('common.saved'));
    renderDomains(el('content'));
  };
}

/**
 * Membership, against the workspace's catalog.
 *
 * Checkboxes rather than free text because the backend validates every table
 * against the catalog and refuses the whole call on one typo -- which, typed by
 * hand, is a form that rejects itself with no hint as to which line was wrong.
 */
function domainTablesSheet(domain) {
  const chosen = new Set((domain.tables || []).map((table) => table.toLowerCase()));
  const resources = domainState.resources;

  openSheet(`
    <div class="card">
      <h3 style="margin-top:0">${t('dom.tablesFor', { name: esc(domain.name) })}</h3>
      <p class="muted small">${t('dom.tablesHelp')}</p>
      ${resources.length ? `
      <label for="dom-filter" class="sr-only">${t('dom.filterTables')}</label>
      <input id="dom-filter" type="search" placeholder="${esc(t('dom.filterTables'))}" />
      <div id="dom-table-list" style="max-height:46vh;overflow:auto;margin-top:8px">
        ${resources.map((resource) => `
          <label class="row" style="gap:8px;padding:3px 0" data-table-row="${esc(resource.table.toLowerCase())}">
            <input type="checkbox" value="${esc(resource.table)}"
                   ${chosen.has(resource.table.toLowerCase()) ? 'checked' : ''} />
            <span class="mono small">${esc(resource.table)}</span>
          </label>`).join('')}
      </div>`
      : `<div class="empty">${t('dom.noCatalog')}</div>`}
      <div class="row" style="margin-top:14px">
        <button class="btn act" data-close>${t('common.cancel')}</button>
        <button class="act primary" id="dom-tables-save"
                ${resources.length ? '' : 'disabled'}>${t('common.save')}</button>
      </div>
    </div>`, { label: t('dom.editTables') });

  if (resources.length) {
    el('dom-filter').oninput = () => {
      const needle = el('dom-filter').value.trim().toLowerCase();
      el('dom-table-list').querySelectorAll('[data-table-row]').forEach((row) => {
        row.hidden = !!needle && !row.dataset.tableRow.includes(needle);
      });
    };
  }

  el('dom-tables-save').onclick = async () => {
    const tables = Array.from(
      el('dom-table-list').querySelectorAll('input[type=checkbox]')
    ).filter((box) => box.checked).map((box) => box.value);
    try {
      await api(domainsUrl(`/${encodeURIComponent(domain.id)}/tables`), {
        method: 'PUT', body: JSON.stringify({ tables }),
      });
    } catch (error) { return toast(error.message); }
    closeSheet();
    toast(t('common.saved'));
    renderDomains(el('content'));
  };
}

async function toggleDomain(domain) {
  try {
    await api(domainsUrl(`/${encodeURIComponent(domain.id)}`), {
      method: 'PATCH', body: JSON.stringify({ is_enabled: !domain.is_enabled }),
    });
  } catch (error) { return toast(error.message); }
  renderDomains(el('content'));
}

async function deleteDomain(domain) {
  const go = await confirmSheet(t, {
    title: t('dom.deleteTitle'),
    // Worth saying: `table_annotations.domain_id` is ON DELETE SET NULL, so the
    // descriptions somebody wrote for these tables survive the grouping.
    body: t('dom.deleteBody', { name: domain.name }),
    confirmLabel: t('common.delete'),
    danger: true,
  });
  if (!go) return;
  try {
    await api(domainsUrl(`/${encodeURIComponent(domain.id)}`), { method: 'DELETE' });
  } catch (error) { return toast(error.message); }
  toast(t('common.deleted'));
  renderDomains(el('content'));
}

// -- starters --------------------------------------------------------------

function renderStarters(target) {
  target.innerHTML = `
    <div class="card">${scopePicker()}</div>
    <div class="banner">
      Starter questions appear above the chat for everyone in this workspace.
      They are the fastest way to show new users what this data can answer.
    </div>
    ${cache.starters.length ? cache.starters.map((s, index) => `
      <div class="card">
        <div class="row">
          <p class="q grow" style="flex:1">${esc(s.question)}</p>
          <button class="act danger" data-del="${index}">${t('common.delete')}</button>
        </div>
      </div>`).join('')
    : `<div class="empty">${t('start.none')}</div>`}
    <div class="card">
      <h3 style="margin-top:0">${t('start.add')}</h3>
      <label>${t('start.question')}</label>
      <input id="s-text" placeholder="What was revenue by region last quarter?" />
      <div class="actions"><button class="act primary" id="s-add">${t('tab.add')}</button></div>
    </div>`;

  wireScopePicker();

  target.querySelectorAll('[data-del]').forEach((button) => {
    const starter = cache.starters[Number(button.dataset.del)];
    button.onclick = async () => {
      try {
        await api(
          `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/starters/${encodeURIComponent(starter.id)}`,
          { method: 'DELETE' }
        );
      } catch (error) { return toast(error.message); }
      await loadScope();
      render();
    };
  });

  el('s-add').onclick = async () => {
    const question = el('s-text').value.trim();
    if (!question) return toast(t('start.needQuestion'));
    try {
      await api(`/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/starters`, {
        method: 'POST',
        body: JSON.stringify({ question, sort_order: cache.starters.length }),
      });
    } catch (error) { return toast(error.message); }
    await loadScope();
    render();
  };
}

// -- knowledge -------------------------------------------------------------

function exampleCard(example) {
  const status = esc(example.status);
  return `
    <div class="card">
      <div class="row">
        <p class="q grow" style="flex:1">${esc(example.question)}</p>
        <span class="tag ${status}">${status}</span>
      </div>
      <pre>${esc(example.sql)}</pre>
      <div class="meta">
        ${example.tables && example.tables.length
          ? 'Tables: ' + esc(example.tables.join(', ')) + ' · ' : ''}
        ${example.created_by ? 'by ' + esc(example.created_by) : ''}
        ${example.verified_by ? ' · verified by ' + esc(example.verified_by) : ''}
      </div>
      <div class="actions">
        ${example.status === 'candidate' ? `
          <button class="act primary" data-status="verified" data-id="${esc(example.id)}">
            Promote to verified
          </button>
          <button class="act danger" data-status="rejected" data-id="${esc(example.id)}">
            Reject
          </button>` : ''}
        ${example.status === 'verified' ? `
          <button class="act" data-status="candidate" data-id="${esc(example.id)}">${t('rev.unverify')}</button>` : ''}
        <button class="act danger" data-delete="${esc(example.id)}">${t('common.delete')}</button>
      </div>
    </div>`;
}

function wireExampleCards(target) {
  target.querySelectorAll('[data-status]').forEach((button) => {
    button.onclick = () => setStatus(button.dataset.id, button.dataset.status);
  });
  target.querySelectorAll('[data-delete]').forEach((button) => {
    button.onclick = () => removeExample(button.dataset.delete);
  });
}

function renderReview(target) {
  const pending = cache.examples.filter((e) => e.status === 'candidate');
  target.innerHTML = pending.length
    ? `<div class="banner">
         These were captured automatically when a user rated an answer as
         correct. A thumbs-up is a signal, not a review — confirm the SQL
         actually answers the question before promoting it, because verified
         examples are shown to the model as patterns to imitate.
       </div>` + pending.map(exampleCard).join('')
    : `<div class="empty">${t('rev.none')}</div>`;
  wireExampleCards(target);
}

function renderVerified(target) {
  const verified = cache.examples.filter((e) => e.status === 'verified');
  target.innerHTML = verified.length
    ? verified.map(exampleCard).join('')
    : `<div class="empty">${t('rev.noVerified')}</div>`;
  wireExampleCards(target);
}

// Business rules, in three groups because they are owned by three different
// people. The platform's apply everywhere and cannot be deleted here; a starter
// pack's arrived as a copy and belong to this workspace now; the rest were
// written here. Showing them as one undifferentiated list is what made "why
// can't I delete this?" a support question rather than a visible fact.
function renderRules(target) {
  const rules = cache.instructions || [];
  const platform = rules.filter((r) => r.origin === 'platform');
  const fromPack = rules.filter((r) => r.origin === 'library');
  const own = rules.filter((r) => r.origin !== 'platform' && r.origin !== 'library');

  target.innerHTML = `
    ${section(t('rules.platform'), t('rules.platformHelp'), platform)}
    ${section(t('rules.fromLibrary'), t('rules.fromLibraryHelp'), fromPack)}
    ${section(t('rules.written'), t('rules.writtenHelp'), own)}
    ${rules.length ? '' : `<div class="empty">${t('rules.none')}</div>`}`;

  wireRules(target);
}

function section(title, help, rules) {
  if (!rules.length) return '';
  return `
    <h3 class="section">${esc(title)}</h3>
    <p class="muted small section-help">${esc(help)}</p>
    ${rules.map(ruleCard).join('')}`;
}

function ruleCard(rule) {
  if (editingRule === rule.id) return ruleEditor(rule);

  const locked = !!rule.locked;
  // A platform rule that may not be switched off has no control at all, rather
  // than a disabled one: an affordance that never works is worse than its
  // absence, and the help text above already says why.
  const canDisable = !locked || rule.disableable;

  return `
    <div class="card" data-rule="${esc(rule.id)}">
      <div class="row">
        <p class="q grow" style="flex:1">${esc(rule.text)}</p>
        <span class="tag ${rule.enabled ? 'verified' : 'rejected'}">
          ${rule.enabled ? t('rules.active') : t('rules.disabled')}
        </span>
      </div>
      <div class="meta">
        ${locked ? `<span class="tag">${t('rules.locked')}</span> ` : ''}
        ${rule.source_pack ? `<span class="tag">${esc(rule.source_pack)}</span> ` : ''}
        ${t('rules.scope')}: ${esc(rule.scope)}${rule.scope_ref ? ' → ' + esc(rule.scope_ref) : ''}
        · ${t('rules.priority')} ${esc(String(rule.priority))}
      </div>
      <div class="actions">
        ${canDisable ? `
          <button class="act" data-toggle="${esc(rule.id)}" data-enabled="${!rule.enabled}">
            ${rule.enabled ? t('acc.disable') : t('acc.enable')}
          </button>` : ''}
        ${locked ? '' : `
          <button class="act" data-editrule="${esc(rule.id)}">${t('rules.edit')}</button>
          <button class="act danger" data-delrule="${esc(rule.id)}">${t('common.delete')}</button>`}
      </div>
    </div>`;
}

function ruleEditor(rule) {
  const scopes = ['global', 'data_source', 'table', 'group'];
  return `
    <div class="card" data-rule="${esc(rule.id)}">
      <label>${t('rules.rule')}</label>
      <textarea id="edit-text">${esc(rule.text)}</textarea>
      <div class="row" style="gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:180px">
          <label>${t('rules.scope')}</label>
          <select id="edit-scope">
            ${scopes.map((s) => `
              <option value="${s}" ${s === rule.scope ? 'selected' : ''}>${s}</option>
            `).join('')}
          </select>
        </div>
        <div style="flex:1;min-width:180px">
          <label>${t('rules.scopeRef')}</label>
          <input id="edit-ref" value="${esc(rule.scope_ref || '')}"
                 placeholder="${t('rules.scopeRefPlaceholder')}"
                 ${rule.scope === 'global' ? 'disabled' : ''} />
        </div>
        <div style="width:120px">
          <label>${t('rules.priority')}</label>
          <input id="edit-priority" type="number" value="${esc(String(rule.priority))}" />
        </div>
      </div>
      <div class="actions">
        <button class="act primary" data-saverule="${esc(rule.id)}">${t('common.save')}</button>
        <button class="act" data-cancelrule="1">${t('common.cancel')}</button>
      </div>
    </div>`;
}

function wireRules(target) {
  target.querySelectorAll('[data-toggle]').forEach((button) => {
    button.onclick = () => toggleRule(button.dataset.toggle, button.dataset.enabled === 'true');
  });
  target.querySelectorAll('[data-delrule]').forEach((button) => {
    button.onclick = () => removeRule(button.dataset.delrule);
  });
  target.querySelectorAll('[data-editrule]').forEach((button) => {
    button.onclick = () => { editingRule = button.dataset.editrule; render(); };
  });
  target.querySelectorAll('[data-cancelrule]').forEach((button) => {
    button.onclick = () => { editingRule = null; render(); };
  });
  target.querySelectorAll('[data-saverule]').forEach((button) => {
    button.onclick = () => saveRule(button.dataset.saverule);
  });
  const scopeSelect = el('edit-scope');
  if (scopeSelect) {
    scopeSelect.onchange = (event) => {
      el('edit-ref').disabled = event.target.value === 'global';
    };
  }
}

// Starter packs: curated sets a workspace can take, and then owns.
function renderLibrary(target) {
  const packs = cache.packs || [];
  target.innerHTML = packs.length
    ? packs.map((pack) => `
        <div class="card">
          <div class="row">
            <h3 class="grow" style="flex:1;margin:0">${esc(pack.name)}</h3>
            ${pack.enabled ? `<span class="tag verified">${t('lib.enabled')}</span>` : ''}
          </div>
          <p class="muted">${esc(pack.description)}</p>
          <div class="meta">${t('lib.rulesCount', { n: pack.instruction_count })}</div>
          <ul class="preview">
            ${(pack.preview || []).map((line) => `<li>${esc(line)}</li>`).join('')}
          </ul>
          <div class="actions">
            ${pack.enabled
              ? `<button class="act danger" data-removepack="${esc(pack.id)}">${t('lib.remove')}</button>`
              : `<button class="act primary" data-addpack="${esc(pack.id)}">${t('lib.enable')}</button>`}
          </div>
        </div>`).join('')
    : `<div class="empty">${t('lib.none')}</div>`;

  target.querySelectorAll('[data-addpack]').forEach((button) => {
    button.onclick = () => enablePack(button.dataset.addpack);
  });
  target.querySelectorAll('[data-removepack]').forEach((button) => {
    button.onclick = () => removePack(button.dataset.removepack);
  });
}

function renderAdd(target) {
  target.innerHTML = `
    <div class="card">
      <h3 style="margin-top:0">${t('rev.addExample')}</h3>
      <label>${t('start.question')}</label>
      <input id="nq" placeholder="What was revenue by region last quarter?" />
      <label>SQL</label>
      <textarea id="ns" placeholder="SELECT region, SUM(amount) ..."></textarea>
      <div class="actions">
        <button class="act primary" id="add-example">${t('rev.saveVerified')}</button>
      </div>
    </div>
    <div class="card">
      <h3 style="margin-top:0">${t('rules.add')}</h3>
      <label>${t('rules.rule')}</label>
      <input id="rt" placeholder="${t('bill.inCents')}" />
      <label>${t('rules.scope')}</label>
      <select id="rs">
        <option value="global">global — applies to every query</option>
        <option value="data_source">data_source — one connection</option>
        <option value="table">table — only when that table is in scope</option>
        <option value="group">group — only for one user group</option>
      </select>
      <label>${t('rules.scopeRef')}</label>
      <input id="rr" disabled placeholder="${t('rules.scopeRefPlaceholder')}" />
      <label>${t('rules.priority')}</label>
      <input id="rp" type="number" value="0" />
      <p class="muted small">${t('rules.priorityHelp')}</p>
      <div class="actions">
        <button class="act primary" id="add-rule">${t('rules.save')}</button>
      </div>
    </div>`;

  el('rs').onchange = (event) => { el('rr').disabled = event.target.value === 'global'; };
  el('add-example').onclick = addExample;
  el('add-rule').onclick = addRule;
}

async function setStatus(id, status) {
  try {
    await api(`/api/vanna/v2/admin/examples/${encodeURIComponent(id)}/status`, {
      method: 'POST', body: JSON.stringify({ status }),
    });
    toast(`Marked ${status}`);
    refresh();
  } catch (error) { toast(error.message); }
}

async function removeExample(id) {
  if (!confirm('Delete this example permanently?')) return;
  try {
    await api(`/api/vanna/v2/admin/examples/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast(t('common.deleted'));
    refresh();
  } catch (error) { toast(error.message); }
}

async function addExample() {
  const question = el('nq').value.trim();
  const sql = el('ns').value.trim();
  if (!question || !sql) return toast(t('rev.needBoth'));
  try {
    await api('/api/vanna/v2/admin/examples', {
      method: 'POST', body: JSON.stringify({ question, sql }),
    });
    el('nq').value = ''; el('ns').value = '';
    toast(t('common.saved'));
    refresh();
  } catch (error) { toast(error.message); }
}

/** Instruction endpoints are scoped to the workspace being administered. */
function rulesUrl(suffix) {
  return `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/instructions${suffix || ''}`;
}

function packsUrl(suffix) {
  return `/api/vanna/v2/admin/tenants/${encodeURIComponent(scope)}/instruction-packs${suffix || ''}`;
}

async function addRule() {
  const text = el('rt').value.trim();
  const scopeKind = el('rs').value;
  const scopeRef = el('rr').value.trim();
  // Priority used to be hardcoded to 0 here, so every rule tied and the
  // (-priority, id) sort fell back to uuid order -- effectively random, and it
  // made "instructions survive truncation longest" mean nothing in particular.
  const priority = parseInt(el('rp').value, 10) || 0;
  if (!text) return toast(t('rules.needText'));
  if (scopeKind !== 'global' && !scopeRef) return toast(t('rules.needRef'));
  try {
    await api(rulesUrl(), {
      method: 'POST',
      body: JSON.stringify({ text, scope: scopeKind, scope_ref: scopeRef || null, priority }),
    });
    toast(t('common.saved'));
    tab = 'rules';
    refresh();
  } catch (error) { toast(error.message); }
}

async function saveRule(id) {
  const text = el('edit-text').value.trim();
  const scopeKind = el('edit-scope').value;
  const scopeRef = el('edit-ref').value.trim();
  const priority = parseInt(el('edit-priority').value, 10) || 0;
  if (!text) return toast(t('rules.needText'));
  if (scopeKind !== 'global' && !scopeRef) return toast(t('rules.needRef'));
  try {
    await api(rulesUrl(`/${encodeURIComponent(id)}`), {
      method: 'PUT',
      body: JSON.stringify({
        text, scope: scopeKind, scope_ref: scopeRef || null, priority,
      }),
    });
    editingRule = null;
    toast(t('common.saved'));
    refresh();
  } catch (error) { toast(error.message); }
}

async function toggleRule(id, enabled) {
  try {
    await api(rulesUrl(`/${encodeURIComponent(id)}/enabled`), {
      method: 'POST', body: JSON.stringify({ enabled }),
    });
    refresh();
  } catch (error) { toast(error.message); }
}

async function removeRule(id) {
  if (!confirm(t('rules.confirmDelete'))) return;
  try {
    await api(rulesUrl(`/${encodeURIComponent(id)}`), { method: 'DELETE' });
    toast(t('common.deleted'));
    refresh();
  } catch (error) { toast(error.message); }
}

async function enablePack(id) {
  try {
    const result = await api(packsUrl(`/${encodeURIComponent(id)}/enable`), {
      method: 'POST',
    });
    toast(t('lib.added', { n: result.added, skipped: result.skipped }));
    refresh();
  } catch (error) { toast(error.message); }
}

async function removePack(id) {
  if (!confirm(t('lib.confirmRemove'))) return;
  try {
    const result = await api(packsUrl(`/${encodeURIComponent(id)}`), { method: 'DELETE' });
    // `kept` is not a rounding error worth hiding: a rule somebody reworded is
    // theirs, and removing the pack it arrived in leaves it alone.
    toast(result.kept
      ? t('lib.removedKept', { n: result.removed, kept: result.kept })
      : t('lib.removed', { n: result.removed }));
    refresh();
  } catch (error) { toast(error.message); }
}


// -------------------------------------------------------------- dialogs ---

/** The active dialog's focus trap, so closing can release it. */
let releaseTrap = null;

function openSheet(html, { label = '' } = {}) {
  const sheet = el('sheet');
  sheet.innerHTML = html;
  el('overlay').classList.add('on');
  el('overlay').removeAttribute('aria-hidden');
  if (label) sheet.setAttribute('aria-label', label);
  sheet.querySelectorAll('[data-close]').forEach((button) => { button.onclick = closeSheet; });
  if (releaseTrap) releaseTrap();
  releaseTrap = trapFocus(sheet, { onEscape: closeSheet });
}

function closeSheet() {
  if (!el('overlay').classList.contains('on')) return;
  el('overlay').classList.remove('on');
  el('overlay').setAttribute('aria-hidden', 'true');
  el('sheet').innerHTML = '';
  if (releaseTrap) {
    releaseTrap();
    releaseTrap = null;
  }
}

// ----------------------------------------------------------------- boot ---

applyTheme();

try {
  identity = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
} catch (_) { identity = null; }

// The shared fetch helper needs to know which workspace a request is for, and what
// to do when the server stops accepting the session.
setHeaderProvider(() => (identity && identity.tenant ? { 'X-Tenant-Id': identity.tenant } : {}));
setAuthFailureHandler((status) => {
  if (status !== 401) return;
  // The console has no sign-in screen of its own -- the app owns that -- so send
  // the operator there rather than leaving them on a page of empty panels.
  location.href = '/';
});

// The two handlers that used to be inline `onclick` attributes in the HTML. They
// could not survive either change on their own: a Content Security Policy without
// 'unsafe-inline' blocks inline handlers, and a module's functions are not global
// for an attribute to reach.
el('theme-btn').onclick = () => {
  const next = toggleTheme();
  announce(next === 'dark' ? 'Dark theme' : 'Light theme');
};
el('reload-btn').onclick = () => refresh();

// Dialogs trap focus and restore it on close.
el('overlay').onclick = (event) => { if (event.target === el('overlay')) closeSheet(); };

const localePicker = el('locale-btn');
localePicker.innerHTML = Object.entries(LOCALES)
  .map(([code, name]) => `<option value="${code}">${name}</option>`).join('');
localePicker.value = locale;
localePicker.onchange = () => loadLocale(localePicker.value);

// A stored workspace is not a session -- refresh() calls /me and finds out. The
// dictionary is loaded first so the first paint is already translated.
loadLocale(locale).then(refresh);
