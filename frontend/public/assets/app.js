/**
 * The DataLens workspace: six screens over one API.
 *
 * Dependency-free on purpose. The only built asset is the <vanna-chat> web component
 * bundle. Adding a framework here would mean a second build pipeline to serve six
 * screens that are mostly lists.
 *
 * Escaping, fetch, the CSRF header and the accessibility primitives live in
 * ../shared/core.js, which the admin console imports too -- two copies of a
 * security-relevant escape function is one copy too many.
 */

import {
  announce,
  api,
  applyTheme,
  csrfToken,
  errorText,
  esc,
  relative as sharedRelative,
  roveFocus,
  setAuthFailureHandler,
  setBusy,
  setHeaderProvider,
  toggleTheme,
  trapFocus,
  until as sharedUntil,
} from './shared/core.js';
import { confirmSheet, promptSheet } from './shared/dialogs.js';
import { tileFigure } from './shared/tile-figure.js';


// ---------------------------------------------------------------- state ---

const STORAGE_KEY = 'vanna.identity';   // shared with the admin console
const THEME_KEY   = 'vanna.theme';
const RAIL_KEY    = 'vanna.rail';       // 'mini' when the rail is collapsed

let identity = null;      // { tenant, email }
let me       = null;      // response from /me
let view     = 'ask';
let chatEl   = null;
let schemaCache  = null;
let historyCache = [];
//: What the account button's sheet reports about this session. Filled by
//: `loadUsage`, which runs after the first paint, so the sheet reads them rather
//: than the rail rendering a number that is not there yet.
let planLine = '';
let planNearLimit = false;
let planExpired = false;

const $ = (id) => document.getElementById(id);

/**
 * Show or hide the conversation rail.
 *
 * Remembered per browser, like the theme: someone who works with it closed does
 * not want to close it again every morning. Applied to `#app` rather than `body`
 * so the collapsed state travels with the shell and cannot leak into the sign-in
 * screen, which has no rail to collapse.
 */
function paintRail(mini) {
  const app = $('app');
  const button = $('side-toggle');
  if (mini) app.setAttribute('data-rail', 'mini');
  else app.removeAttribute('data-rail');
  if (!button) return;
  button.setAttribute('aria-expanded', String(!mini));
  const label = t(mini ? 'nav.expand' : 'nav.collapse');
  button.setAttribute('aria-label', label);
  button.title = label;
}

function toggleRail() {
  const mini = $('app').getAttribute('data-rail') !== 'mini';
  localStorage.setItem(RAIL_KEY, mini ? 'mini' : 'full');
  paintRail(mini);
  // Say which way it went: the button stays put and only its icon state changes,
  // so a screen-reader user has nothing else to go on.
  announce(t(mini ? 'nav.collapsed' : 'nav.expanded'));
}


function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

/**
 * How long ago. For instants in the **past** only.
 *
 * The clamp below means a future instant reads "just now", so do not reach for
 * this to render an expiry -- use until(). Kept one-directional on purpose: a
 * helper that quietly handles both is how "expires just now" shipped on every
 * live session.
 */
function relative(iso) {
  return sharedRelative(iso, t, locale);
}

/** How long until. For instants in the **future** -- expiries, renewals. */
function until(iso) {
  return sharedUntil(iso, t, locale);
}

// ------------------------------------------------------------------ api ---

/** Which workspace this request is for.
 *
 *  Identity is no longer here: it is an httpOnly session cookie the page cannot
 *  read, sent automatically with `credentials: include`. Only the *workspace*
 *  travels as a header, and the server still checks membership before honouring
 *  it -- a header that selects a workspace is a preference, not a credential. */
/**
 * A personal LLM key, held in this browser and nowhere else.
 *
 * Deliberately not stored on the server. Doing that would mean a third-party
 * credential sitting in our database in plaintext -- we have no cipher in the
 * dependency set and no key-management story to add one honestly. The cost is
 * real and is stated on the Account screen: the key does not follow you to
 * another device, and any script running on this page can read it.
 */
const LLM_KEY = 'vanna.llm';
const SOURCE_KEY = 'vanna.datasource';
const EFFORT_KEY = 'vanna.reasoningEffort';
const EFFORTS = ['low', 'medium', 'high'];

/** The reasoning-effort preference ('' = server default). Not a secret. */
function reasoningEffort() {
  try {
    const v = localStorage.getItem(EFFORT_KEY) || '';
    return EFFORTS.includes(v) ? v : '';
  } catch (_) { return ''; }
}

function setReasoningEffort(value) {
  try {
    if (EFFORTS.includes(value)) localStorage.setItem(EFFORT_KEY, value);
    else localStorage.removeItem(EFFORT_KEY);
  } catch (_) { /* storage blocked: the default applies */ }
}

//: The databases this workspace can be asked about, and which one is chosen.
//: A workspace usually has exactly one, in which case the picker stays hidden --
//: a control offering a single option is noise.
let dataSources = [];
let dataSource = '';

function llmConfig() {
  try { return JSON.parse(localStorage.getItem(LLM_KEY) || '{}'); }
  catch (_) { return {}; }
}

function setLlmConfig(config) {
  if (config && config.key) localStorage.setItem(LLM_KEY, JSON.stringify(config));
  else localStorage.removeItem(LLM_KEY);
}

function authHeaders() {
  const headers = identity && identity.tenant ? { 'X-Tenant-Id': identity.tenant } : {};
  // A preference, not a credential. The server checks it against the databases
  // this workspace has registered and pins the answer to the conversation, so a
  // later message cannot move a thread to another schema.
  if (dataSource) headers['X-Data-Source-Id'] = dataSource;
  return headers;
}

/** Load the workspace's databases and settle on one. */
async function loadDataSources() {
  try {
    const { data_sources: sources } = await api('/api/vanna/v2/datasources');
    dataSources = sources || [];
  } catch {
    dataSources = [];
  }

  const remembered = localStorage.getItem(SOURCE_KEY) || '';
  const known = dataSources.some((s) => s.data_source_id === remembered);
  // A remembered choice that is no longer registered must not silently select
  // something else -- fall back to the default and say so by repainting.
  const fallback = (dataSources.find((s) => s.is_default) || dataSources[0] || {});
  dataSource = known ? remembered : (fallback.data_source_id || '');
}

/**
 * Switch the database the next question is asked against.
 *
 * Starts a new thread rather than repointing the current one. The binding is
 * per conversation on the server -- a thread's history is a record of questions
 * asked against one schema -- so continuing here would show the model earlier
 * turns about tables that are no longer in scope.
 */
function switchDataSource(next) {
  if (!next || next === dataSource) return;
  dataSource = next;
  localStorage.setItem(SOURCE_KEY, next);
  schemaCache = null;
  paintChrome();
  newThread();
  loadStarters();
  announce(t('ask.databaseSwitched', { name: labelForSource(next) }));
}

function labelForSource(id) {
  const found = dataSources.find((s) => s.data_source_id === id);
  return found ? found.label : id;
}

/**
 * Headers for the chat endpoints, which are the only ones that can use a
 * personal LLM key.
 *
 * Kept separate from authHeaders() on purpose: /usage, /schema and the rest have
 * no possible use for the key, and a secret should not be sent to endpoints that
 * cannot need it. Narrowing this costs one function and removes the key from
 * roughly a dozen request paths.
 */
function chatHeaders() {
  const headers = authHeaders();

  // The CSRF token, which the chat element cannot know about.
  //
  // Every other request goes through the shared `api()` helper, which attaches
  // this itself. <vanna-chat> owns its own transport and POSTs to /chat_sse and
  // /chat_poll with only the headers it is handed here -- so without this line the
  // middleware rejects both, and the widget shows "Connection failed. Unable to
  // reach server". Which is true, and says nothing about why.
  const token = csrfToken();
  if (token) headers['X-CSRF-Token'] = token;

  const llm = llmConfig();
  if (llm.key) {
    headers['X-LLM-Key'] = llm.key;
    headers['X-LLM-Provider'] = llm.provider || 'openai';
    if (llm.model) headers['X-LLM-Model'] = llm.model;
  }
  // Independent of the personal key: it tunes speed on the server's key too.
  const effort = reasoningEffort();
  if (effort) headers['X-LLM-Reasoning-Effort'] = effort;
  return headers;
}


// -------------------------------------------------------------- sign in ---

async function startSignIn(message) {
  $('app').classList.remove('ready');
  $('signin').classList.add('on');
  if (message) showSignInError(message);

  const select = $('si-tenant');
  select.innerHTML = `<option>${t('common.loading')}</option>`;

  try {
    const { tenants } = await api('/api/vanna/v2/tenants');
    select.innerHTML = tenants.length
      ? tenants.map((ws) => `<option value="${esc(ws.id)}">${esc(ws.name)}</option>`).join('')
      : '<option value="demo">demo</option>';
  } catch (error) {
    select.innerHTML = '<option value="demo">demo</option>';
    showSignInError(`Could not list workspaces: ${error.message}`);
  }

  const remembered = readIdentity();
  if (remembered) {
    if ([...select.options].some((o) => o.value === remembered.tenant)) {
      select.value = remembered.tenant;
    }
    $('si-email').value = remembered.email;
  }

  loadRoster(select.value);
  select.onchange = () => loadRoster(select.value);
}

/** Offer the member list as click-to-pick, when the backend exposes it.
 *  It is off by default: publishing who belongs to a workspace before anyone
 *  has authenticated is a disclosure, and only a demo wants it. */
async function loadRoster(tenantId) {
  const box = $('si-roster');
  box.innerHTML = '';
  if (!tenantId) return;
  try {
    const { users } = await api(`/api/vanna/v2/tenants/${encodeURIComponent(tenantId)}/users`);
    box.innerHTML = users.map((u) => `
      <button type="button" data-email="${esc(u.email)}">
        ${esc(u.full_name || u.email)} · ${esc(u.role)}
      </button>`).join('');
    box.querySelectorAll('button').forEach((button) => {
      button.onclick = () => { $('si-email').value = button.dataset.email; };
    });
  } catch (_) {
    // Roster disabled or unavailable -- typing an address still works.
  }
}

function showSignInError(message) {
  const node = $('si-error');
  node.textContent = message;
  node.classList.add('on');
  node.classList.remove('notice');
  announce(message, 'assertive');
}

/**
 * A neutral message on the sign-in screen.
 *
 * Distinct from the error styling because "a reset link is on its way" is not a
 * failure, and colouring it red is how a working flow gets reported as broken.
 */
function showSignInNotice(message) {
  const node = $('si-error');
  node.textContent = message;
  node.classList.add('on', 'notice');
  announce(message);
}

function readIdentity() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

async function signIn(tenant, email, password) {
  // Exchange the password for a session cookie. Nothing about the identity is
  // stored by this page -- the cookie is httpOnly and the server owns it.
  const session = await api('/api/vanna/v2/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email: email.trim().toLowerCase(), password }),
  });

  const memberships = session.memberships || [];

  // Signing in successfully and then finding every screen says "not a member" is
  // a worse experience than being told plainly. An account exists before it is
  // added to a workspace, so this is a normal state, not an error.
  if (!memberships.length) {
    throw new Error(
      'Signed in, but this account is not a member of any workspace yet. ' +
      'Ask an administrator to add you.'
    );
  }

  // The one asked for if they belong to it, else the first they do.
  const chosen = memberships.includes(tenant) ? tenant : memberships[0];

  identity = { tenant: chosen };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(identity));

  if (session.must_change_password) {
    // The session the server just issued can reach exactly one endpoint: the
    // password change. Asking for /me would 403, and used to succeed -- the flag
    // was advisory and only this page acted on it, so a temporary password was a
    // permanent credential for anything that spoke HTTP directly.
    openPasswordChangeRequired();
    return null;
  }

  me = await api('/api/vanna/v2/me');
  return me;
}

/**
 * The one screen a temporary-password session can use.
 *
 * Modal and not dismissible: there is nothing else this session is permitted to do,
 * so offering a way out would only produce a sequence of 403s.
 */
function openPasswordChangeRequired() {
  $('signin').classList.remove('on');
  $('app').classList.remove('ready');
  openSheet(`
    <h3>${t('pw.required')}</h3>
    <p class="muted small">${t('pw.requiredWhy')}</p>
    <label for="pw-current">${t('pw.current')}</label>
    <input id="pw-current" type="password" dir="ltr" autocomplete="current-password" />
    <label for="pw-new">${t('pw.new')}</label>
    <input id="pw-new" type="password" dir="ltr" autocomplete="new-password"
           aria-describedby="pw-rule" />
    <p class="muted small" id="pw-rule">${t('pw.rule')}</p>
    <div class="error" id="pw-error" role="alert"></div>
    <div class="actions">
      <button class="btn primary" id="pw-go">${t('pw.set')}</button>
    </div>`, { label: t('pw.required') });

  $('pw-go').onclick = async () => {
    const current = $('pw-current').value;
    const next = $('pw-new').value;
    const error = $('pw-error');
    error.textContent = '';
    if (next.length < 12) {
      error.textContent = t('pw.tooShort');
      announce(t('pw.tooShort'), 'assertive');
      return;
    }
    try {
      await api('/api/vanna/v2/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      // The server promotes this session to a full one on success, so there is no
      // need to sign in again with the password just set.
      closeSheet();
      announce(t('pw.done'));
      location.reload();
    } catch (failure) {
      error.textContent = failure.message;
      announce(failure.message, 'assertive');
    }
  };
}

/** Switch workspace without re-authenticating -- the session already covers it. */
async function switchWorkspace(tenant) {
  identity = { tenant };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(identity));
  location.reload();
}

async function signOut() {
  try {
    await api('/api/vanna/v2/auth/logout', { method: 'POST' });
  } catch (_) { /* signing out locally matters more than the round trip */ }
  localStorage.removeItem(STORAGE_KEY);
  // The personal API key belongs to the person signing out, not to the browser.
  // Leaving it would mean the next account to sign in here silently spends
  // someone else's key -- and the theme survives a sign-out precisely because
  // it is the kind of thing that should.
  setLlmConfig(null);
  location.reload();
}

// ------------------------------------------------------------ language ---
//
// Dictionary lookup rather than a framework. The rest of this page has no
// dependencies and adding one to translate a hundred strings is not a trade
// worth making -- t() plus an attribute sweep is the whole of it.
//
// Two ways in: static markup carries data-i18n (and data-i18n-attr for
// placeholders and titles), while text built in JS calls t() directly. Both are
// needed because most of this UI is rendered from template literals.

const LOCALE_KEY = 'vanna.locale';
const LOCALES = { en: 'English', ar: 'العربية' };

/** Locales written right-to-left. Only the interface flips; see applyLocale. */
const RTL = new Set(['ar']);

let locale = localStorage.getItem(LOCALE_KEY) || 'en';
let strings = {};

/**
 * Translate a key, interpolating {placeholders}.
 *
 * Falls back to the key itself rather than to empty text: a missing string
 * should look like a missing string, not like a blank label nobody notices.
 */
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
    const response = await fetch(`/locales/${locale}.json`, { cache: 'no-cache' });
    // A 404 is not an exception. Without this check the JSON parse of an HTML
    // error page is what fails, several lines later, with a message about
    // an unexpected token -- which says nothing about the missing file.
    if (!response.ok) throw new Error(`locale ${locale}: ${response.status}`);
    strings = await response.json();
  } catch (error) {
    // An unreachable dictionary must not blank the interface: t() falls back to
    // the key, and for English the keys are close enough to read.
    //
    // But say so. The console's dictionary moved directory and its fetch was not
    // updated; every label on that screen quietly became its own lookup key --
    // `console.title`, `tab.billing` -- and read like a deliberate naming scheme
    // rather than a 404. A degradation nobody can see is one nobody fixes.
    console.error('Could not load translations; falling back to raw keys.', error);
    strings = {};
  }
  applyLocale();
}

function applyLocale() {
  const rtl = RTL.has(locale);
  document.documentElement.setAttribute('lang', locale);
  document.documentElement.setAttribute('dir', rtl ? 'rtl' : 'ltr');

  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  // "placeholder" and "title" carry real instructions, so they need translating
  // too: data-i18n-attr="placeholder:signin.email".
  document.querySelectorAll('[data-i18n-attr]').forEach((node) => {
    node.dataset.i18nAttr.split(',').forEach((pair) => {
      const [attr, key] = pair.split(':');
      if (attr && key) node.setAttribute(attr.trim(), t(key.trim()));
    });
  });

  // The chat is a separate component with its own bundled strings; it is told,
  // not re-created -- destroying it would throw away the conversation on screen.
  if (chatEl) chatEl.setAttribute('locale', locale);

  // Re-render whatever view is open; its text came from t() at render time.
  if (typeof view !== 'undefined' && view && view !== 'ask' && !$('view-other').hidden) {
    switchView(view);
  }
  if (typeof me !== 'undefined' && me) paintChrome();
  // The rail toggle's label depends on which way it is pointing, and the loop
  // above has just overwritten it with the expanded wording from `data-i18n-attr`.
  if ($('side-toggle')) paintRail($('app').getAttribute('data-rail') === 'mini');
}

// ------------------------------------------------------------- chrome ----

/**
 * Apply a theme and tell the chat element about it.
 *
 * The document half lives in the shared module; the <vanna-chat> element is this
 * page's own concern and takes its theme as an attribute, so it has to be told
 * separately. Named differently from the imported `applyTheme` on purpose -- a
 * local function with the same name as an import is a redeclaration, and an ES
 * module that fails to parse does not half-run: nothing on the page works at all.
 */
function setTheme(next) {
  const applied = applyTheme(next);
  if (chatEl) chatEl.setAttribute('theme', applied);
  return applied;
}

function paintChrome() {
  // Who you are, which workspace, and which database all live behind the account
  // button at the foot of the rail. They were three separate pills across the top
  // of every screen, saying things that do not change between one question and the
  // next -- and the same facts were already in the sheet, so the header was
  // spending a third of its width restating it.
  $('ask-tools').hidden = !me.is_admin;
  paintStartersLabel();
  // Only shown when the workspace has a semantic layer -- an empty Metrics tab
  // teaches people the feature does not work.
  api('/api/vanna/v2/cubes')
    .then(({ cubes }) => {
      if (cubes && cubes.length) $('nav-cubes').hidden = false;
    })
    .catch(() => {});
  paintDataSources();
  paintRailAccount();
}

/** Which database this workspace is answering from, in words. */
function sourceLine() {
  if (!me.control_plane) return t('account.noControlPlane');
  if (dataSource) return t('ask.queryingDatabase', { name: labelForSource(dataSource) });
  return me.tenant.data_source
    ? t('ask.queryingDatabase', { name: me.tenant.data_source })
    : '';
}

/** The foot of the rail: who you are, and the role you hold here. */
function paintRailAccount() {
  const button = $('rail-account');
  if (!button || !me) return;
  const email = me.user.email || me.user.id || '';
  const role = me.is_platform_admin ? t('account.platformAdmin') : (me.user.role || '');
  $('rail-avatar').textContent = email.slice(0, 2) || '--';
  $('rail-email').textContent = email;
  $('rail-role').textContent = role;
  // Collapsed there is no text at all, so the name has to carry both parts.
  const label = t('account.openMenuFor', { email, role });
  button.setAttribute('aria-label', label);
  button.title = label;
}

/**
 * Everything about this session, behind the one control at the foot of the rail.
 *
 * Built from the same facts the rail used to print as three unclickable notes --
 * plan, database, admin console -- plus the identity that was in the opposite
 * corner of the header. Somebody asking "who am I, where am I, and what am I
 * querying?" now has one place to look.
 */
function openAccountSheet() {
  const others = (me.memberships || []).filter((id) => id !== me.tenant.id);
  const source = sourceLine();
  openSheet(`
    <h3>${esc(me.user.email || '')}</h3>
    <p class="muted small">
      <span class="chip ${me.is_admin ? 'admin' : ''}">${esc(
        me.is_platform_admin ? t('account.platformAdmin') : (me.user.role || '')
      )}</span>
      ${t('ws.youAreIn')} <strong>${esc(me.tenant.name || me.tenant.id)}</strong>.
    </p>
    ${source ? `<p class="muted small" dir="auto">${esc(source)}</p>` : ''}
    ${dataSources.length > 1 ? `
      <label for="db-pick">${t('ask.database')}</label>
      <select id="db-pick" data-i18n-attr="title:ask.databaseTitle"></select>` : ''}
    ${planLine ? `<p class="small" style="${planNearLimit ? 'color:var(--warn)' : ''}">
      ${esc(planLine)}${planExpired ? ` — ${t('plan.expiredNote')}` : ''}
    </p>` : ''}
    <div class="roster" style="margin-top:10px">
      <button id="acct-account">${t('nav.account')}</button>
      ${me.is_admin ? `<button id="acct-console">${t('nav.console')}</button>` : ''}
    </div>
    ${others.length ? `
      <label style="margin-top:12px">${t('ws.switchTo')}</label>
      <div class="roster">
        ${others.map((id) => `<button data-tenant="${esc(id)}">${esc(id)}</button>`).join('')}
      </div>` : ''}
    <div class="actions">
      <button class="btn danger" id="ws-signout">${t('header.signOut')}</button>
      <button class="btn" data-close>${t('common.close')}</button>
    </div>`, { label: t('account.openMenu') });

  paintDataSources();
  const picker = $('db-pick');
  if (picker) {
    picker.onchange = (event) => {
      switchDataSource(event.target.value);
      closeSheet();
    };
  }
  $('acct-account').onclick = () => { closeSheet(); switchView('account'); };
  const console_ = $('acct-console');
  if (console_) console_.onclick = () => { window.location.href = '/admin/'; };
  $('sheet').querySelectorAll('[data-tenant]').forEach((button) => {
    button.onclick = () => switchWorkspace(button.dataset.tenant);
  });
  $('ws-signout').onclick = signOut;
}

/** Fill the database picker, or hide it when there is nothing to choose. */
function paintDataSources() {
  const pick = $('db-pick');
  // Absent unless the account sheet is open. Everything else about a data source
  // -- the header it travels in, the runtime it resolves to -- is unchanged; this
  // is only where the choice is made.
  if (!pick) return;

  if (dataSources.length < 2) {
    pick.hidden = true;
    pick.innerHTML = '';
    return;
  }

  pick.hidden = false;
  pick.innerHTML = dataSources
    .map(
      (source) =>
        `<option value="${esc(source.data_source_id)}"${
          source.data_source_id === dataSource ? ' selected' : ''
        }>${esc(source.label)}</option>`
    )
    .join('');
}

function switchView(next) {
  view = next;
  document.querySelectorAll('nav.side button[data-view]').forEach((button) => {
    const selected = button.dataset.view === next;
    // aria-selected is what a tablist reports; aria-current is kept for the
    // "you are here" reading assistive technology gives a navigation landmark.
    button.setAttribute('aria-selected', String(selected));
    button.setAttribute('aria-current', String(selected));
    button.setAttribute('tabindex', selected ? '0' : '-1');
  });

  const ask   = $('view-ask');
  const other = $('view-other');
  const main  = $('main');

  if (next === 'ask') {
    ask.classList.add('on');
    other.hidden = true;
    main.classList.add('flush');
    return;
  }

  ask.classList.remove('on');
  other.hidden = false;
  main.classList.remove('flush');
  other.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  setBusy(other, true);
  announce(`${t(`nav.${next === 'cubes' ? 'metrics' : next}`)} — ${t('common.loading')}`);

  const renderers = {
    schema: renderSchema,
    history: renderHistory,
    saved: renderSaved,
    dashboards: renderDashboards,
    cubes: renderCubes,
    account: renderAccount,
  };
  const render = renderers[next];
  if (!render) {
    setBusy(other, false);
    return;
  }
  // aria-busy has to be cleared whether the render succeeded or not: a panel left
  // marked busy tells a screen reader the page is still loading, forever.
  Promise.resolve(render()).finally(() => {
    setBusy(other, false);
    announce(t(`nav.${next === 'cubes' ? 'metrics' : next}`));
  });
}

// ----------------------------------------------------------------- ask ---

function mountChat() {
  const chat = document.createElement('vanna-chat');
  chat.setAttribute('title', 'Ask your data');
  chat.setAttribute('sse-endpoint', '/api/vanna/v2/chat_sse');
  chat.setAttribute('theme', document.documentElement.getAttribute('data-theme') || 'light');
  chat.setAttribute('locale', locale);

  // Attach the listener before the element is connected: the component fires
  // vanna-ready in its first render, and its very first backend call follows
  // immediately after. Setting headers any later means that call goes out
  // unauthenticated.
  chat.addEventListener('vanna-ready', () => chat.setCustomHeaders(chatHeaders()));
  chat.addEventListener('vanna-feedback', () => { historyCache = []; });
  // A new thread only gets a title once its first question is stored, so the
  // list is refreshed when an answer finishes rather than when it starts.
  chat.addEventListener('message-sent', () => {
    historyCache = [];
    setTimeout(() => { loadThreads(); loadUsage(); }, 1500);
  });

  $('chat-wrap').appendChild(chat);
  chatEl = chat;
  wireEffortPicker();
}

function ask(question) {
  switchView('ask');
  if (chatEl && typeof chatEl.sendMessage === 'function') {
    chatEl.sendMessage(question);
  }
}

/** Show usage before the wall is hit, rather than at it. */
async function loadUsage() {
  try {
    const usage = await api('/api/vanna/v2/usage');
    if (!usage.enabled) return;
    // Held rather than painted: the rail shows who you are, and the plan lives
    // one click away with the rest of the session's facts.
    const label = usage.plan_label || '';
    planLine = label
      ? `${label} · ${usage.used} / ${usage.limit} ${t('plan.questionsToday')}`
      : `${usage.used} / ${usage.limit} ${t('plan.questionsToday')}`;
    planNearLimit = usage.used >= usage.limit * 0.8;
    planExpired = !!usage.expired;
    paintRailAccount();
  } catch (_) { /* usage is informational; never block the app on it */ }
}

/** The aside always holds the effort picker; its "Try asking" heading only needs to
 *  show when there are starters under it. */
function paintStartersLabel() {
  $('starters-label').hidden = !$('starters').children.length;
}

/** The reasoning-effort picker above the chat. */
function wireEffortPicker() {
  const select = $('llm-effort');
  if (!select) return;
  select.value = reasoningEffort();
  select.onchange = () => {
    setReasoningEffort(select.value);
    // The chat element caches the headers it was given at startup.
    if (chatEl && typeof chatEl.setCustomHeaders === 'function') {
      chatEl.setCustomHeaders(chatHeaders());
    }
    toast(t('ask.effortSaved'));
  };
}

async function loadStarters() {
  const box = $('starters');
  box.innerHTML = '';
  // The panel holds the starters *and* the admin tools, so it stays if either
  // has something to show.
  const show = () => {
    paintStartersLabel();
  };
  if (!me.control_plane) return show();

  try {
    const { starters } = await api('/api/vanna/v2/starters');
    box.innerHTML = starters.map((s) => (
      `<button class="starter" data-q="${esc(s.question)}">${esc(s.question)}</button>`
    )).join('');
    box.querySelectorAll('.starter').forEach((button) => {
      button.onclick = () => ask(button.dataset.q);
    });
  } catch (_) {
    // Starters are a convenience; their absence is not worth an error banner.
  }
  show();
}

// -------------------------------------------------------------- schema ---

let schemaLayer = 'active';   // 'active' | 'physical'

/**
 * Describing a table or a column, from the screen that shows them.
 *
 * Both descriptions already reach the model's prompt, and `value_labels` is the
 * single highest-value piece of column metadata there is: without it the model
 * guesses the literal, and `status = 'cancelled'` against a column holding `'C'`
 * returns zero rows rather than an error anybody can act on. Until now the only
 * way to write any of it was raw SQL.
 *
 * Admin only, and only over the physical layer -- an annotation is keyed on a
 * catalog table, and a semantic model is not one. The buttons are simply absent
 * otherwise rather than present and refused.
 */
function catalogUrl(suffix) {
  return `/api/vanna/v2/admin/tenants/${encodeURIComponent(me.tenant.id)}/catalog${suffix}`;
}

/** The key the catalog and the grant tables both use: lower-cased, dots kept. */
function tableKeyOf(table) {
  const qualified = table.schema ? `${table.schema}.${table.name}` : table.name;
  // `name` is already qualified when the scan qualified it; do not double it up.
  return (table.name.includes('.') ? table.name : qualified).toLowerCase();
}

function describeTableSheet(table, onSaved) {
  openSheet(`
    <h3>${t('schema.describeTable', { name: esc(table.name) })}</h3>
    <p class="muted small">${t('schema.describeHelp')}</p>
    <label for="ann-desc">${t('schema.description')}</label>
    <textarea id="ann-desc" rows="4"
              placeholder="${esc(t('schema.tablePlaceholder'))}">${esc(table.description || '')}</textarea>
    <div class="actions">
      <button class="btn" data-close>${t('common.cancel')}</button>
      <button class="btn primary" id="ann-save">${t('common.save')}</button>
    </div>`, { label: t('schema.describeTable', { name: table.name }) });

  $('ann-save').onclick = async () => {
    const description = $('ann-desc').value.trim();
    try {
      await api(catalogUrl(`/tables/${encodeURIComponent(tableKeyOf(table))}`), {
        method: 'PATCH', body: JSON.stringify({ description }),
      });
    } catch (error) { return toast(error.message); }
    closeSheet();
    toast(t('common.saved'));
    onSaved();
  };
}

function labelRow(code = '', meaning = '') {
  return `
    <div class="row label-row" style="gap:8px;margin-bottom:6px">
      <input class="label-code" value="${esc(code)}" style="max-width:120px"
             placeholder="${esc(t('schema.code'))}" aria-label="${esc(t('schema.code'))}" />
      <input class="label-text" value="${esc(meaning)}"
             placeholder="${esc(t('schema.codeMeans'))}" aria-label="${esc(t('schema.codeMeans'))}" />
      <button class="btn" type="button" data-label-remove
              aria-label="${esc(t('schema.removeCode'))}">&times;</button>
    </div>`;
}

async function describeColumnSheet(table, column, onSaved) {
  const key = tableKeyOf(table);

  // The description this screen renders has the code book folded into it -- that
  // is the form that reaches the prompt -- so it cannot be edited as-is. Read the
  // stored annotation to get the two fields back apart.
  let mine = { description: '', value_labels: {} };
  try {
    const body = await api(catalogUrl(`/tables/${encodeURIComponent(key)}`));
    mine = (body.columns || {})[column.name.toLowerCase()] || mine;
  } catch (error) { return toast(error.message); }

  const labels = Object.entries(mine.value_labels || {});
  openSheet(`
    <h3>${t('schema.describeColumn', { name: esc(column.name) })}</h3>
    <p class="muted small">${t('schema.describeColumnHelp')}</p>
    <label for="ann-col-desc">${t('schema.description')}</label>
    <textarea id="ann-col-desc" rows="3"
              placeholder="${esc(t('schema.columnPlaceholder'))}">${esc(mine.description || '')}</textarea>
    <h4 style="margin:14px 0 4px;font-size:.875rem">${t('schema.codeBook')}</h4>
    <p class="muted small" style="margin:0 0 8px">${t('schema.codeBookHelp')}</p>
    <div id="ann-labels">
      ${(labels.length ? labels : [['', '']]).map(([c, m]) => labelRow(c, m)).join('')}
    </div>
    <button class="btn" type="button" id="ann-add-label">${t('schema.addCode')}</button>
    ${(column.categories || []).length ? `
      <p class="muted small" style="margin:10px 0 0">
        ${t('schema.seenValues')}
        ${column.categories.slice(0, 12).map((v) => `<span class="chip">${esc(v)}</span>`).join(' ')}
      </p>` : ''}
    <div class="actions">
      <button class="btn" data-close>${t('common.cancel')}</button>
      <button class="btn primary" id="ann-col-save">${t('common.save')}</button>
    </div>`, { label: t('schema.describeColumn', { name: column.name }) });

  const wireLabels = () => {
    $('ann-labels').querySelectorAll('[data-label-remove]').forEach((button) => {
      button.onclick = () => {
        const rows = $('ann-labels').querySelectorAll('.label-row');
        if (rows.length === 1) {
          rows[0].querySelectorAll('input').forEach((input) => { input.value = ''; });
          return;
        }
        button.closest('.label-row').remove();
      };
    });
  };
  wireLabels();
  $('ann-add-label').onclick = () => {
    $('ann-labels').insertAdjacentHTML('beforeend', labelRow());
    wireLabels();
  };

  $('ann-col-save').onclick = async () => {
    const value_labels = {};
    $('ann-labels').querySelectorAll('.label-row').forEach((row) => {
      const code = row.querySelector('.label-code').value.trim();
      const meaning = row.querySelector('.label-text').value.trim();
      if (code && meaning) value_labels[code] = meaning;
    });
    try {
      await api(
        catalogUrl(`/columns/${encodeURIComponent(key)}/${encodeURIComponent(column.name)}`),
        {
          method: 'PATCH',
          body: JSON.stringify({
            description: $('ann-col-desc').value.trim(),
            // Sent whole: a code book is edited as a set, so an omitted code is a
            // removed code rather than one left behind.
            value_labels,
          }),
        }
      );
    } catch (error) { return toast(error.message); }
    closeSheet();
    toast(t('common.saved'));
    onSaved();
  };
}

async function renderSchema() {
  const root = $('view-other');
  try {
    schemaCache = schemaCache ||
      await api(`/api/vanna/v2/schema?layer=${encodeURIComponent(schemaLayer)}`);
  } catch (error) {
    root.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  const { tables, relationships, dialect, data_source, semantic, layer } = schemaCache;

  // With a semantic layer the agent sees models, not tables — and the physical
  // tables they are built on are deliberately hidden from it. The toggle lets a
  // human check what a model actually points at without changing what the agent
  // is shown.
  const showingPhysical = layer === 'physical';
  // An annotation is keyed on a catalog table; a semantic model is not one, so the
  // editor is absent over that layer rather than present and refused.
  const canDescribe = me.is_admin && (!semantic || showingPhysical);
  const noun = showingPhysical ? 'table' : (semantic ? 'model' : 'table');

  root.innerHTML = `
    <h2 class="view-title">${t('schema.title')}</h2>
    <p class="view-sub">
      ${tables.length} ${noun}${tables.length === 1 ? '' : 's'} in
      <span class="mono">${esc(data_source || dialect)}</span>.
      ${semantic && !showingPhysical
        ? t('schema.subSemantic')
        : t('schema.subPhysical')}
    </p>
    <div class="toolbar">
      <input type="search" id="schema-search" placeholder="${t('schema.filter')}" />
      ${semantic ? `
        <button class="btn" id="layer-toggle">
          ${showingPhysical ? t('schema.showSemantic') : t('schema.showPhysical')}
        </button>` : ''}
      ${me.is_admin ? `<button class="btn" id="rescan">${t('schema.rescan')}</button>` : ''}
    </div>
    <div class="schema-grid">
      <div class="card table-list" id="table-list"></div>
      <div id="table-detail"></div>
    </div>`;

  if (!tables.length) {
    $('table-detail').innerHTML = `
      <div class="card empty">
        ${t('schema.nothingScanned')}
        ${me.is_admin ? t('schema.scanToBuild') : t('schema.askAnAdmin')}
      </div>`;
  }

  let selected = tables.length ? tables[0].name : null;

  function paintList(filter) {
    const needle = (filter || '').toLowerCase();
    const shown = tables.filter((tbl) =>
      !needle ||
      tbl.name.toLowerCase().includes(needle) ||
      tbl.columns.some((c) => c.name.toLowerCase().includes(needle))
    );

    $('table-list').innerHTML = shown.length
      ? shown.map((tbl) => `
          <button data-table="${esc(tbl.name)}" aria-current="${tbl.name === selected}">
            ${esc(tbl.name)}<span class="count">${tbl.columns.length}</span>
          </button>`).join('')
      : `<div class="empty small">${t('schema.noMatch')}</div>`;

    $('table-list').querySelectorAll('button').forEach((button) => {
      button.onclick = () => { selected = button.dataset.table; paintList(filter); paintDetail(); };
    });
  }

  function paintDetail() {
    const table = tables.find((tbl) => tbl.name === selected);
    if (!table) return;

    const related = relationships.filter(
      (r) => r.from_table === table.name || r.to_table === table.name
    );

    $('table-detail').innerHTML = `
      <div class="card">
        <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
          <h3 style="margin:0;font-size:1rem">${esc(table.name)}</h3>
          ${table.schema ? `<span class="chip">${esc(table.schema)}</span>` : ''}
          ${table.row_count_estimate != null
            ? `<span class="muted small">${t('schema.rows', { n: Number(table.row_count_estimate).toLocaleString() })}</span>`
            : ''}
          <span class="grow"></span>
          <button class="btn" data-ask="Describe the ${esc(table.name)} table and show me 10 rows.">
            ${t('schema.askAbout')}
          </button>
          ${canDescribe
            ? `<button class="btn" id="describe-table">${t('schema.describe')}</button>`
            : ''}
        </div>
        ${table.description
          ? `<p class="muted small" style="margin:9px 0 0">${esc(table.description)}</p>`
          : canDescribe
            ? `<p class="muted small" style="margin:9px 0 0">${t('schema.noDescription')}</p>`
            : ''}
      </div>

      <div class="card scroll-x">
        <table class="data">
          <thead><tr>
            <th>${t('schema.colColumn')}</th><th>${t('schema.colType')}</th><th>${t('schema.colKey')}</th><th>${t('schema.colValues')}</th><th>${t('schema.colNotes')}</th>${canDescribe ? '<th></th>' : ''}
          </tr></thead>
          <tbody>
            ${table.columns.map((c) => `
              <tr>
                <td class="mono">${esc(c.name)}${c.nullable ? '' : ' <span class="muted">*</span>'}</td>
                <td class="muted">${esc(c.data_type)}</td>
                <td>
                  ${c.is_primary_key ? '<span class="chip">pk</span>' : ''}
                  ${c.foreign_key
                    ? `<span class="chip" title="references ${esc(c.foreign_key.references_table)}.${esc(c.foreign_key.references_column)}">fk</span>`
                    : ''}
                </td>
                <td class="small">${
                  (c.categories && c.categories.length)
                    ? c.categories.slice(0, 8).map((v) => `<span class="chip">${esc(v)}</span>`).join(' ')
                    : (c.sample_values || []).slice(0, 3).map(esc).join(', ')
                }</td>
                <td class="muted small">${esc(c.description || '')}</td>
                ${canDescribe ? `
                <td>
                  <button class="btn" data-describe-column="${esc(c.name)}"
                          aria-label="${esc(t('schema.describeColumn', { name: c.name }))}"
                          >${t('schema.describe')}</button>
                </td>` : ''}
              </tr>`).join('')}
          </tbody>
        </table>
      </div>

      ${related.length ? `
        <div class="card">
          <h4 style="margin:0 0 9px;font-size:.875rem">${t('schema.joins')}</h4>
          ${related.map((r) => `
            <div class="small mono" style="padding:3px 0">
              ${esc(r.from_table)}.${esc(r.from_column)} → ${esc(r.to_table)}.${esc(r.to_column)}
            </div>`).join('')}
        </div>` : ''}`;

    $('table-detail').querySelectorAll('[data-ask]').forEach((button) => {
      button.onclick = () => ask(button.dataset.ask);
    });

    // A saved description has to be re-read, not patched into the cached copy: the
    // column form the prompt sees folds the code book into the description, and the
    // server is the only thing that renders that.
    const reload = () => { schemaCache = null; renderSchema(); };
    if ($('describe-table')) {
      $('describe-table').onclick = () => describeTableSheet(table, reload);
    }
    $('table-detail').querySelectorAll('[data-describe-column]').forEach((button) => {
      const column = table.columns.find((c) => c.name === button.dataset.describeColumn);
      button.onclick = () => describeColumnSheet(table, column, reload);
    });
  }

  paintList('');
  if (selected) paintDetail();

  $('schema-search').oninput = (event) => paintList(event.target.value);

  if ($('layer-toggle')) {
    $('layer-toggle').onclick = () => {
      schemaLayer = showingPhysical ? 'active' : 'physical';
      schemaCache = null;
      renderSchema();
    };
  }

  if ($('rescan')) {
    $('rescan').onclick = async () => {
      const button = $('rescan');
      button.disabled = true;
      button.textContent = t('schema.scanning');
      try {
        const report = await api('/api/vanna/v2/schema/rescan', { method: 'POST' });
        toast(t('schema.scanned', {
          tables: report.tables_scanned, columns: report.columns_profiled,
        }));
        schemaCache = null;
        renderSchema();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
        button.textContent = t('schema.rescan');
      }
    };
  }
}

// ------------------------------------------------------------- history ---

function statusChip(status) {
  const kind = status === 'valid' ? 'ok'
    : status === 'empty' ? 'warn'
    : 'err';
  return `<span class="chip ${kind}">${esc(status)}</span>`;
}

/**
 * Past questions: read, filter, forget.
 *
 * Every filter goes to the server. The page used to fetch a hard-coded hundred
 * rows and narrow them here, which cannot answer "the failures from last week" on
 * a workspace with more history than that -- and the store has had the indexes for
 * it since migration 0005.
 *
 * `state` is module-level rather than threaded through `options` because the view
 * repaints on every filter change and on "load more", and a partly-remembered set
 * of filters is worse than none: the list would stop matching the controls above
 * it.
 */
const historyState = {
  search: '',
  mine: false,
  status: '',
  since: '',
  until: '',
  rows: [],
  more: false,
};

const HISTORY_PAGE = 50;

const HISTORY_STATUSES = [
  'valid', 'invalid', 'empty', 'rejected_by_policy', 'timeout', 'error',
];

function historyQuery(offset) {
  const params = new URLSearchParams({
    limit: String(HISTORY_PAGE),
    offset: String(offset),
  });
  if (historyState.search) params.set('search', historyState.search);
  if (historyState.mine) params.set('mine', 'true');
  if (historyState.status) params.set('status', historyState.status);
  if (historyState.since) params.set('since', historyState.since);
  // Exclusive upper bound, so a day picked in both boxes covers that whole day.
  if (historyState.until) params.set('until', `${historyState.until}T23:59:59`);
  return params;
}

async function renderHistory(options = {}) {
  const root = $('view-other');
  if (!options.keepFilters) {
    historyState.search = options.search ?? '';
    historyState.mine = options.mine ?? false;
    historyState.status = '';
    historyState.since = '';
    historyState.until = '';
  }

  root.innerHTML = `
    <h2 class="view-title">${t('history.title')}</h2>
    <p class="view-sub">${t('history.sub')}</p>
    <div class="toolbar">
      <input type="search" id="h-search"
             aria-label="${esc(t('history.search'))}"
             placeholder="${esc(t('history.search'))}"
             value="${esc(historyState.search)}" />
      <label class="inline">
        <input type="checkbox" id="h-mine" ${historyState.mine ? 'checked' : ''} />
        ${t('history.onlyMine')}
      </label>
      <label class="inline">
        <span class="sr-only">${t('history.status')}</span>
        <select id="h-status" aria-label="${esc(t('history.status'))}">
          <option value="">${t('history.anyStatus')}</option>
          ${HISTORY_STATUSES.map((value) => `
            <option value="${value}" ${historyState.status === value ? 'selected' : ''}>
              ${esc(t(`status.${value}`))}
            </option>`).join('')}
        </select>
      </label>
      <label class="inline">
        <span class="sr-only">${t('history.from')}</span>
        <input type="date" id="h-since" aria-label="${esc(t('history.from'))}"
               value="${esc(historyState.since)}" />
      </label>
      <label class="inline">
        <span class="sr-only">${t('history.to')}</span>
        <input type="date" id="h-until" aria-label="${esc(t('history.to'))}"
               value="${esc(historyState.until)}" />
      </label>
      <button class="btn" id="h-reload">${t('history.reload')}</button>
      <button class="btn" id="h-export">${t('history.export')}</button>
      <button class="btn danger" id="h-clear">${t('history.clearMine')}</button>
    </div>
    <div id="h-body"><div class="empty">${t('common.loading')}</div></div>
    <div class="row" style="justify-content:center;margin-top:12px">
      <button class="btn" id="h-more" hidden>${t('thread.loadMore')}</button>
    </div>`;

  const reload = () => {
    historyState.search = $('h-search').value;
    historyState.mine = $('h-mine').checked;
    historyState.status = $('h-status').value;
    historyState.since = $('h-since').value;
    historyState.until = $('h-until').value;
    return loadHistory({ append: false });
  };
  $('h-reload').onclick = reload;
  $('h-mine').onchange = reload;
  $('h-status').onchange = reload;
  $('h-since').onchange = reload;
  $('h-until').onchange = reload;
  let timer = null;
  $('h-search').oninput = () => { clearTimeout(timer); timer = setTimeout(reload, 300); };
  $('h-more').onclick = () => loadHistory({ append: true });
  $('h-export').onclick = exportHistory;
  $('h-clear').onclick = clearMyHistory;

  await loadHistory({ append: false });
}

async function loadHistory({ append }) {
  const offset = append ? historyState.rows.length : 0;
  let payload;
  try {
    payload = await api(`/api/vanna/v2/history?${historyQuery(offset)}`);
  } catch (error) {
    $('h-body').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }
  if (!payload.control_plane) {
    $('h-body').innerHTML = `<div class="empty">${t('history.needsDb')}</div>`;
    $('h-more').hidden = true;
    return;
  }
  const page = payload.history || [];
  historyState.rows = append ? historyState.rows.concat(page) : page;
  historyState.more = page.length === HISTORY_PAGE;
  historyCache = historyState.rows;
  paintHistory();
}

/** True when the row is the caller's own, which is what may be rated or deleted. */
function isMyRow(row) {
  return !!me && !!row.user_id && row.user_id === me.user.id;
}

function paintHistory() {
  const rows = historyState.rows;
  $('h-more').hidden = !historyState.more;

  if (!rows.length) {
    $('h-body').innerHTML = `<div class="empty">${t('history.empty')}</div>`;
    return;
  }

  $('h-body').innerHTML = rows.map((row, index) => {
    const mine = isMyRow(row);
    return `
    <div class="card">
      <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
        <strong style="flex:1;min-width:200px">${esc(row.question || t('history.noQuestion'))}</strong>
        ${statusChip(row.status)}
        ${row.feedback === 'positive' ? `<span class="chip ok">${t('history.liked')}</span>` : ''}
        ${row.feedback === 'negative' ? `<span class="chip err">${t('history.disliked')}</span>` : ''}
      </div>
      <div class="muted small" style="margin:5px 0 9px">
        ${esc(row.user_id)} &middot; ${esc(relative(row.created_at))}
        ${row.row_count != null ? ` &middot; ${t('history.rows', { count: row.row_count })}` : ''}
        ${row.execution_ms != null ? ` &middot; ${Math.round(row.execution_ms)} ms` : ''}
      </div>
      ${row.sql ? `<pre class="sql">${esc(row.sql)}</pre>` : ''}
      ${row.error ? `<div class="small" style="color:var(--bad);margin-top:8px">${esc(row.error)}</div>` : ''}
      <div class="toolbar" style="margin:12px 0 0">
        <button class="btn" data-act="ask"  data-i="${index}">${t('history.askAgain')}</button>
        <button class="btn" data-act="run"  data-i="${index}">${t('history.runSql')}</button>
        <button class="btn" data-act="save" data-i="${index}">${t('common.save')}</button>
        <button class="btn" data-act="copy" data-i="${index}">${t('result.copySql')}</button>
        ${mine ? `
        <button class="btn" data-act="up" data-i="${index}"
                aria-label="${esc(t('history.rateUp'))}"
                aria-pressed="${row.feedback === 'positive'}">&#128077;</button>
        <button class="btn" data-act="down" data-i="${index}"
                aria-label="${esc(t('history.rateDown'))}"
                aria-pressed="${row.feedback === 'negative'}">&#128078;</button>
        <button class="btn danger" data-act="del" data-i="${index}"
                aria-label="${esc(t('history.deleteOne'))}">${t('common.delete')}</button>` : ''}
      </div>
    </div>`;
  }).join('');

  $('h-body').querySelectorAll('[data-act]').forEach((button) => {
    const row = rows[Number(button.dataset.i)];
    button.onclick = () => {
      const act = button.dataset.act;
      if (act === 'ask')  return ask(row.question);
      if (act === 'run')  return runSql(row.sql, { question: row.question, title: t('history.runSql') });
      if (act === 'save') return openSaveSheet(row.question, row.sql);
      if (act === 'up')   return rateHistoryRow(row, 'positive');
      if (act === 'down') return rateHistoryRow(row, 'negative');
      if (act === 'del')  return deleteHistoryRow(row);
      navigator.clipboard.writeText(row.sql || '').then(() => toast(t('common.copied')));
    };
  });
}

/**
 * Rate one of your own turns from here.
 *
 * Only your own: a positive rating makes the turn a candidate example, which then
 * steers the model for the whole workspace, so the server scopes the update to the
 * owner. Hiding the buttons on other people's rows matches that instead of
 * offering an action that is refused.
 */
async function rateHistoryRow(row, rating) {
  if (!row.request_id) return toast(t('history.cannotRate'));
  try {
    await api('/api/vanna/v2/feedback', {
      method: 'POST',
      body: JSON.stringify({
        request_id: row.request_id,
        conversation_id: row.conversation_id || '',
        rating,
        question: row.question || '',
        sql: row.sql || '',
      }),
    });
  } catch (error) { return toast(error.message); }
  row.feedback = rating;
  paintHistory();
  toast(t('history.rated'));
}

async function deleteHistoryRow(row) {
  const ok = await confirmSheet(t, {
    title: t('history.deleteTitle'),
    body: row.question || t('history.noQuestion'),
    confirmLabel: t('common.delete'),
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/vanna/v2/history/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
  } catch (error) { return toast(error.message); }
  historyState.rows = historyState.rows.filter((item) => item.id !== row.id);
  historyCache = historyState.rows;
  paintHistory();
  toast(t('common.deleted'));
}

async function clearMyHistory() {
  const ok = await confirmSheet(t, {
    title: t('history.clearTitle'),
    body: t('history.clearBody'),
    confirmLabel: t('history.clearMine'),
    danger: true,
  });
  if (!ok) return;
  let removed;
  try {
    ({ deleted: removed } = await api('/api/vanna/v2/history', { method: 'DELETE' }));
  } catch (error) { return toast(error.message); }
  toast(t('history.cleared', { count: removed }));
  await loadHistory({ append: false });
}

/**
 * Export what is on screen, not what is on the server.
 *
 * Exporting the whole filtered set would mean paging it all down first, and the
 * honest thing is for the file to match the list the user is looking at.
 */
function exportHistory() {
  if (!historyState.rows.length) return toast(t('history.empty'));
  downloadCsv({
    columns: ['created_at', 'user_id', 'status', 'feedback', 'question', 'sql',
              'row_count', 'execution_ms', 'error'],
    rows: historyState.rows.map((row) => [
      row.created_at, row.user_id, row.status, row.feedback || '',
      row.question || '', row.sql || '', row.row_count, row.execution_ms,
      row.error || '',
    ]),
  });
}

async function renderSaved() {
  const root = $('view-other');
  let saved;
  try {
    ({ saved } = await api('/api/vanna/v2/saved-queries'));
  } catch (error) {
    root.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  root.innerHTML = `
    <h2 class="view-title">${t('saved.title')}</h2>
    <p class="view-sub">${t('saved.sub')}</p>
    <div class="toolbar">
      <button class="btn primary" id="new-saved">${t('saved.new')}</button>
    </div>
    <div id="saved-body">
      ${saved.length ? saved.map((item, index) => `
        <div class="card">
          <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
            <strong style="flex:1;min-width:180px">${esc(item.title)}</strong>
            <span class="muted small">${esc(item.created_by)} · ${esc(relative(item.created_at))}</span>
          </div>
          ${item.question ? `<p class="muted small" style="margin:6px 0 9px">${esc(item.question)}</p>` : ''}
          <pre class="sql">${esc(item.sql)}</pre>
          <div class="toolbar" style="margin:12px 0 0">
            <button class="btn primary" data-act="run" data-i="${index}">${t('common.run')}</button>
            <button class="btn" data-act="ask"  data-i="${index}">${t('saved.askAbout')}</button>
            <button class="btn" data-act="copy" data-i="${index}">${t('common.copy')}</button>
            <button class="btn danger" data-act="del" data-i="${index}">${t('common.delete')}</button>
          </div>
        </div>`).join('')
      : `<div class="empty">${t('saved.empty')}</div>`}
    </div>`;

  $('new-saved').onclick = () => openSaveSheet('', '');

  $('saved-body').querySelectorAll('[data-act]').forEach((button) => {
    const item = saved[Number(button.dataset.i)];
    button.onclick = async () => {
      const action = button.dataset.act;
      if (action === 'run') return runSql(item.sql, { question: item.question, title: item.title });
      if (action === 'ask') return ask(item.question || `Explain what this query returns: ${item.sql}`);
      if (action === 'copy') {
        navigator.clipboard.writeText(item.sql).then(() => toast(t('common.copied')));
        return;
      }
      const go = await confirmSheet(t, {
        title: t('saved.deleteTitle'),
        body: t('saved.deleteConfirm', { title: item.title }),
        confirmLabel: t('common.delete'),
        danger: true,
      });
      if (!go) return;
      try {
        await api(`/api/vanna/v2/saved-queries/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        toast(t('common.deleted'));
        renderSaved();
      } catch (error) { toast(error.message); }
    };
  });
}

// ------------------------------------------------------------- account ---

/**
 * Where a limit came from, in the words a capped user would use.
 *
 * This is the provenance the Billing tab shows admins, said to the person it
 * actually constrains -- "why am I capped at 200?" has a real answer and it
 * should not require reading code or asking an admin.
 */
function limitSourceText(usage) {
  const key = `plan.source.${usage.limit_source}`;
  const text = t(key, { plan: usage.plan_label || usage.plan || '' });
  return text === key ? '' : text;   // unknown source: say nothing rather than a key
}

/** A browser's user-agent, shortened to the part a person recognises. */
function deviceName(userAgent) {
  if (!userAgent) return t('device.unknown');
  const browser = /Edg\//.test(userAgent) ? 'Edge'
    : /OPR\//.test(userAgent) ? 'Opera'
    : /Chrome\//.test(userAgent) ? 'Chrome'
    : /Safari\//.test(userAgent) ? 'Safari'
    : /Firefox\//.test(userAgent) ? 'Firefox'
    : /curl|python|node/i.test(userAgent) ? t('device.script') : t('device.browser');
  const os = /Windows/.test(userAgent) ? 'Windows'
    : /Mac OS X|Macintosh/.test(userAgent) ? 'macOS'
    : /Android/.test(userAgent) ? 'Android'
    : /iPhone|iPad/.test(userAgent) ? 'iOS'
    : /Linux/.test(userAgent) ? 'Linux' : '';
  // Browser and OS names are proper nouns and stay as they are; only the
  // joining word is translated.
  return os ? t('device.on', { browser, os }) : browser;
}

async function renderAccount() {
  const root = $('view-other');

  // Each panel is independent: a deployment without a control plane has no
  // sessions or tokens, and that should cost the password form nothing.
  // Unwrapped here, not at the use site. Both of these are `{tokens: [...]}` and
  // `{sessions: [...]}`, and the templates below asked the *envelope* for its
  // `.length` -- always undefined, so the first paint of this screen has always
  // said "no tokens" and "no other sessions" however many there were. The token
  // list recovered on the next `loadTokenList()`; the session list had nothing to
  // recover it, which is why "sign out everywhere else" was never offered either.
  const [usage, tokenBody, sessionBody] = await Promise.all([
    api('/api/vanna/v2/usage').catch(() => ({ enabled: false })),
    api('/api/vanna/v2/auth/tokens').catch(() => ({ tokens: [] })),
    api('/api/vanna/v2/auth/sessions').catch(() => ({ sessions: [] })),
  ]);
  const tokens = tokenBody.tokens || [];
  const sessions = sessionBody.sessions || [];

  const plan = usage.enabled ? `
    <div class="card">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <strong style="font-size:1.05rem">${esc((usage.plan_label || usage.plan || 'Free'))}</strong>
        ${usage.expired ? `<span class="chip warn">${t('plan.expired')}</span>` : ''}
        <span class="grow"></span>
        <span class="${usage.used >= usage.limit * 0.8 ? 'warn' : 'muted'}">
          ${usage.used.toLocaleString()} / ${usage.limit.toLocaleString()} ${t('plan.questionsToday')}
        </span>
      </div>
      <p class="muted small" style="margin:10px 0 0">
        ${t('plan.rowsPerQuery', { rows: (usage.max_rows || 0).toLocaleString() })}
        ${t('plan.limitIs', { source: limitSourceText(usage) })}
      </p>
      ${usage.expired ? `<p class="muted small" style="margin:8px 0 0">
        ${t('plan.expiredNote')}
      </p>` : ''}
      <p class="muted small" style="margin:8px 0 0">
        ${me && me.is_admin
          ? `<a href="/admin/">${t('plan.adminChange')}</a>`
          : t('plan.askAdmin')}
      </p>
    </div>` : '';

  // The workspace can forbid personal keys outright. When it does, the form is
  // not rendered at all -- but the header is also ignored server-side, so this
  // is presentation, not enforcement.
  const llm = llmConfig();
  const byoCard = me.tenant.allow_byo_key === false ? `
    <div class="card">
      <h3 style="margin:0 0 4px">${t('account.ownKey')}</h3>
      <p class="muted small" style="margin:0">
        ${t('account.ownKeyBlocked')}
      </p>
    </div>` : `
    <div class="card">
      <h3 style="margin:0 0 4px">${t('account.ownKey')}</h3>
      <p class="muted small" style="margin:0 0 12px">${t('account.ownKeyHelp')}</p>
      <div class="form-grid">
        <label>${t('account.provider')}
          <select id="llm-provider">
            <option value="openai"${llm.provider !== 'anthropic' ? ' selected' : ''}>OpenAI</option>
            <option value="anthropic"${llm.provider === 'anthropic' ? ' selected' : ''}>Anthropic</option>
          </select>
        </label>
        <label>${t('account.apiKey')}
          <input type="password" id="llm-key" dir="ltr" autocomplete="off"
                 placeholder="${llm.key ? '•••••••• (saved)' : 'sk-…'}" />
        </label>
        <label>${t('account.model')} <span class="muted small">${t('account.optional')}</span>
          <input id="llm-model" dir="ltr" value="${esc(llm.model || '')}" placeholder="${t('account.modelDefault')}" />
        </label>
      </div>
      <div class="toolbar" style="margin:12px 0 0">
        <button class="btn primary" id="llm-save">${llm.key ? t('account.replaceKey') : t('account.saveKey')}</button>
        ${llm.key ? `<button class="btn danger" id="llm-clear">${t('account.removeKey')}</button>` : ''}
        <span class="chip ${llm.key ? 'ok' : ''}">${llm.key ? t('account.keyInUse') : t('account.keyNotSet')}</span>
      </div>
      <p class="muted small" style="margin:12px 0 0">${t('account.keyWarning')}</p>
    </div>`;

  root.innerHTML = `
    <h2 class="view-title">${t('account.title')}</h2>
    <p class="view-sub">${esc(me.user.email)} &middot; ${esc(me.user.role)} in
      ${esc(me.tenant.name)}</p>

    ${plan}

    <div class="card">
      <h3 style="margin:0 0 4px">${t('account.password')}</h3>
      <p class="muted small" style="margin:0 0 12px">
        ${t('account.passwordHelp')}
      </p>
      <div class="form-grid">
        <label>${t('account.currentPassword')}<input type="password" id="pw-current" dir="ltr" autocomplete="current-password" /></label>
        <label>${t('account.newPassword')}<input type="password" id="pw-new" dir="ltr" autocomplete="new-password" /></label>
        <label>${t('account.repeatPassword')}<input type="password" id="pw-again" dir="ltr" autocomplete="new-password" /></label>
      </div>
      <div class="toolbar" style="margin:12px 0 0">
        <button class="btn primary" id="pw-save">${t('account.changePassword')}</button>
        <span class="muted small">${t('account.passwordLength')}</span>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 4px">${t('account.tokens')}</h3>
      <p class="muted small" style="margin:0 0 12px">
        ${t('account.tokensHelp')}
      </p>
      <div id="token-list">
        ${tokens.length ? tokens.map((token, i) => `
          <div class="row-line">
            <div>
              <strong>${esc(token.name || t('account.unnamedToken'))}</strong>
              <div class="muted small">
                ${t('account.created')} ${esc(relative(token.created_at))} &middot;
                ${token.last_used_at
                    ? `${t('account.lastUsed')} ${esc(relative(token.last_used_at))}`
                    : t('account.neverUsed')}
                ${token.expires_at ? ` &middot; ${t('account.expires')} ${esc(until(token.expires_at))}` : ''}
              </div>
            </div>
            <button class="btn danger" data-revoke="${i}">${t('account.revoke')}</button>
          </div>`).join('')
        : `<div class="empty">${t('account.noTokens')}</div>`}
      </div>
      <div class="toolbar" style="margin:12px 0 0">
        <input id="token-name" placeholder="${t('account.tokenName')}" style="max-width:260px"
               aria-label="${esc(t('account.tokenName'))}" />
        <label class="inline">
          <span class="sr-only">${t('account.tokenExpiry')}</span>
          <select id="token-ttl" aria-label="${esc(t('account.tokenExpiry'))}">
            <option value="30">${t('account.ttlDays', { n: 30 })}</option>
            <option value="90" selected>${t('account.ttlDays', { n: 90 })}</option>
            <option value="365">${t('account.ttlDays', { n: 365 })}</option>
            <option value="">${t('account.ttlNever')}</option>
          </select>
        </label>
        <button class="btn primary" id="token-new">${t('account.createToken')}</button>
      </div>
      <div id="token-result"></div>
    </div>

    ${byoCard}

    <div class="card">
      <h3 style="margin:0 0 4px">${t('account.sessions')}</h3>
      <p class="muted small" style="margin:0 0 12px">
        ${t('account.sessionsHelp')}
      </p>
      <div>
        ${sessions.length ? sessions.map((s) => `
          <div class="row-line">
            <div>
              <strong>${esc(deviceName(s.user_agent))}</strong>
              ${s.is_current ? `<span class="chip">${t('account.thisBrowser')}</span>` : ''}
              <div class="muted small">
                ${esc(s.ip || t('account.unknownAddress'))} &middot;
                ${t('account.signedIn')} ${esc(relative(s.created_at))} &middot;
                ${t('account.expires')} ${esc(until(s.expires_at))}
              </div>
            </div>
            ${s.is_current ? '' : `
              <button class="btn danger" data-session="${esc(s.id)}"
                      aria-label="${esc(t('account.signOutThisOne', {
                        device: deviceName(s.user_agent),
                      }))}">${t('account.signOutOne')}</button>`}
          </div>`).join('')
        : `<div class="empty">${t('account.noSessions')}</div>`}
      </div>
      ${sessions.length > 1 ? `
        <div class="toolbar" style="margin:12px 0 0">
          <button class="btn danger" id="sess-purge">${t('account.signOutOthers')}</button>
        </div>` : ''}
    </div>`;

  // -- password
  $('pw-save').onclick = async () => {
    const current = $('pw-current').value;
    const next    = $('pw-new').value;
    if (next !== $('pw-again').value) return toast(t('account.passwordMismatch'));
    if (next.length < 10) return toast(t('account.passwordTooShort'));
    try {
      await api('/api/vanna/v2/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      toast(t('account.passwordChanged'));
      renderAccount();
    } catch (error) { toast(error.message); }
  };

  // -- tokens
  $('token-new').onclick = async () => {
    const name = $('token-name').value.trim();
    if (!name) return toast(t('account.tokenNeedsName'));
    try {
      const ttl = $('token-ttl').value;
      const result = await api('/api/vanna/v2/auth/tokens', {
        method: 'POST',
        body: JSON.stringify(ttl ? { name, ttl_days: Number(ttl) } : { name }),
      });
      // Rendered outside the list, and not re-fetchable: only the hash is stored,
      // so this is the only moment the value exists anywhere but the server's reply.
      $('token-result').innerHTML = `
        <div class="card" style="border-color:var(--good);margin:12px 0 0">
          <strong>${t('account.newToken')}</strong>
          <pre class="sql" style="margin:8px 0" dir="ltr">${esc(result.token)}</pre>
          <p class="muted small" style="margin:0">${t('account.tokenOnce')}</p>
        </div>`;
      $('token-name').value = '';
      loadTokenList();
    } catch (error) { toast(error.message); }
  };

  async function loadTokenList() {
    // Refresh the list without wiping the just-issued token off the screen.
    const { tokens: fresh } = await api('/api/vanna/v2/auth/tokens').catch(() => ({ tokens: [] }));
    $('token-list').innerHTML = fresh.length ? fresh.map((token, i) => `
      <div class="row-line">
        <div>
          <strong>${esc(token.name || t('account.unnamedToken'))}</strong>
          <div class="muted small">${t('account.created')} ${esc(relative(token.created_at))} &middot;
            ${token.last_used_at
                ? `${t('account.lastUsed')} ${esc(relative(token.last_used_at))}`
                : t('account.neverUsed')}</div>
        </div>
        <button class="btn danger" data-revoke="${i}">${t('account.revoke')}</button>
      </div>`).join('') : `<div class="empty">${t('account.noTokens')}</div>`;
    wireRevoke(fresh);
  }

  function wireRevoke(list) {
    $('token-list').querySelectorAll('[data-revoke]').forEach((button) => {
      const token = list[Number(button.dataset.revoke)];
      button.onclick = async () => {
        const go = await confirmSheet(t, {
          title: t('account.revokeTitle'),
          body: t('account.revokeConfirm', { name: token.name || t('account.thisToken') }),
          confirmLabel: t('common.revoke'),
          danger: true,
        });
        if (!go) return;
        try {
          await api(`/api/vanna/v2/auth/tokens/${encodeURIComponent(token.id)}`, { method: 'DELETE' });
          toast(t('common.revoked'));
          loadTokenList();
        } catch (error) { toast(error.message); }
      };
    });
  }
  wireRevoke(tokens);

  // -- personal key
  if ($('llm-save')) $('llm-save').onclick = () => {
    const key = $('llm-key').value.trim();
    if (!key) return toast(t('account.keyNeeded'));
    setLlmConfig({
      key,
      provider: $('llm-provider').value,
      model: $('llm-model').value.trim(),
    });
    // Headers are read fresh per request, but the chat component caches the set
    // it was given at startup -- so it has to be told again.
    if (chatEl && typeof chatEl.setCustomHeaders === 'function') {
      chatEl.setCustomHeaders(chatHeaders());
    }
    toast(t('account.keySaved'));
    renderAccount();
  };

  if ($('llm-clear')) $('llm-clear').onclick = () => {
    setLlmConfig(null);
    if (chatEl && typeof chatEl.setCustomHeaders === 'function') {
      chatEl.setCustomHeaders(chatHeaders());
    }
    toast(t('account.keyRemoved'));
    renderAccount();
  };

  // -- sessions
  if ($('sess-purge')) $('sess-purge').onclick = async () => {
    const go = await confirmSheet(t, {
      title: t('account.signOutOthers'),
      body: t('account.signOutOthersConfirm'),
      confirmLabel: t('account.signOutOthers'),
      danger: true,
    });
    if (!go) return;
    try {
      const { ended } = await api('/api/vanna/v2/auth/sessions', { method: 'DELETE' });
      toast(t(ended === 1 ? 'account.endedOne' : 'account.endedMany', { count: ended }));
      renderAccount();
    } catch (error) { toast(error.message); }
  };

  // One device, not all of them. "An unfamiliar session in the list" is the
  // common case, and there is rarely a reason to also sign out the ones that
  // are fine.
  root.querySelectorAll('[data-session]').forEach((button) => {
    button.onclick = async () => {
      const go = await confirmSheet(t, {
        title: t('account.signOutOne'),
        body: t('account.signOutOneConfirm'),
        confirmLabel: t('account.signOutOne'),
        danger: true,
      });
      if (!go) return;
      try {
        await api(
          `/api/vanna/v2/auth/sessions/${encodeURIComponent(button.dataset.session)}`,
          { method: 'DELETE' }
        );
        toast(t('account.endedOne'));
        renderAccount();
      } catch (error) { toast(error.message); }
    };
  });
}

function openSaveSheet(question, sql) {
  openSheet(`
    <h3>${t('saved.sheetTitle')}</h3>
    <label for="sv-title">${t('saved.fieldTitle')}</label>
    <input id="sv-title" placeholder="Monthly revenue by region" />
    <label for="sv-question">${t('saved.fieldQuestion')}</label>
    <input id="sv-question" value="${esc(question || '')}" />
    <label for="sv-sql">SQL</label>
    <textarea id="sv-sql">${esc(sql || '')}</textarea>
    <div class="actions">
      <button class="btn" data-close>${t('common.cancel')}</button>
      <button class="btn primary" id="sv-save">${t('common.save')}</button>
    </div>`);

  const titleField = $('sv-title');
  titleField.value = (question || '').slice(0, 60);
  titleField.focus();

  $('sv-save').onclick = async () => {
    const title = $('sv-title').value.trim();
    const body = {
      title,
      question: $('sv-question').value.trim(),
      sql: $('sv-sql').value.trim(),
    };
    if (!title || !body.sql) return toast(t('saved.needTitleAndSql'));
    try {
      await api('/api/vanna/v2/saved-queries', { method: 'POST', body: JSON.stringify(body) });
      closeSheet();
      toast(t('saved.saved'));
      if (view === 'saved') renderSaved();
    } catch (error) { toast(error.message); }
  };
}

// ------------------------------------------------------------- preview ---

/** Show what the model is actually told, and what the budget cut.
 *
 *  The reason this is worth a button: when an answer is wrong, the usual cause
 *  is that the right example or rule never reached the prompt, and there is
 *  otherwise no way to see that from the outside. */
async function previewPrompt() {
  const question = await promptSheet(t, {
    title: t('preview.askTitle'),
    label: t('preview.askLabel'),
    confirmLabel: t('preview.run'),
  });
  if (!question) return;

  openSheet(`<h3>${t('preview.assembling')}</h3><div class="empty">${t('preview.running')}</div>`);

  let payload;
  try {
    payload = await api(
      `/api/vanna/v2/prompt-preview?question=${encodeURIComponent(question.trim())}`
    );
  } catch (error) {
    openSheet(`<h3>${t('preview.failed')}</h3>
      <div class="small" style="color:var(--bad)">${esc(error.message)}</div>
      <div class="actions"><button class="btn" data-close>${t('common.close')}</button></div>`);
    return;
  }

  const over = payload.budget && payload.tokens_used > payload.budget * 0.9;
  openSheet(`
    <h3>Context for &ldquo;${esc(payload.question)}&rdquo;</h3>
    <div class="small ${over ? '' : 'muted'}" style="${over ? 'color:var(--warn)' : ''}">
      ${payload.tokens_used} / ${payload.budget} tokens
    </div>
    ${payload.sections.length ? `
      <div class="card scroll-x" style="margin-top:12px">
        <table class="data">
          <thead><tr><th>${t('preview.section')}</th><th>${t('preview.tokens')}</th><th>${t('preview.dropped')}</th></tr></thead>
          <tbody>${payload.sections.map((s) => `
            <tr>
              <td>${esc(s.name)}${s.truncated ? ' <span class="chip warn">truncated</span>' : ''}</td>
              <td>${s.tokens}</td>
              <td>${s.dropped_items
                ? `<span style="color:var(--warn)">${s.dropped_items}</span>` : '0'}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>` : `<div class="empty">${esc(payload.note || 'Nothing retrieved.')}</div>`}
    ${payload.text ? `
      <div class="muted small" style="margin:14px 0 6px">${t('preview.title')}</div>
      <pre class="sql" style="max-height:38vh;overflow:auto">${esc(payload.text)}</pre>` : ''}
    <div class="actions"><button class="btn" data-close>${t('common.close')}</button></div>`);
}

// --------------------------------------------------------------- cubes ---
//
// Pick measures and dimensions rather than writing an aggregate. The grain was
// decided when the cube was defined, so a selection here cannot double-count
// across a one-to-many join the way a hand-written SUM can.

let cubeCache = null;

async function renderCubes() {
  const root = $('view-other');
  try {
    cubeCache = cubeCache || (await api('/api/vanna/v2/cubes')).cubes;
  } catch (error) {
    root.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  if (!cubeCache.length) {
    root.innerHTML = `<div class="empty">${t('cubes.emptyBefore')}
      <span class="mono" dir="ltr">cubes/</span> ${t('cubes.emptyAfter')}</div>`;
    return;
  }

  let selected = cubeCache[0];

  root.innerHTML = `
    <h2 class="view-title">${t('cubes.title')}</h2>
    <p class="view-sub">${t('cubes.sub')}</p>
    <div class="toolbar">
      <select id="cube-pick" style="max-width:280px">
        ${cubeCache.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('')}
      </select>
      <button class="btn primary" id="cube-run">${t('common.run')}</button>
    </div>
    <div class="card" id="cube-controls"></div>
    <div id="cube-result"></div>`;

  function paintControls() {
    $('cube-controls').innerHTML = `
      <label class="inline" style="display:block;margin-bottom:6px">${t('cubes.measures')}</label>
      <div class="roster" id="cube-measures">
        ${selected.measures.map((m, i) => `
          <label class="inline" title="${esc(m.expression)}">
            <input type="checkbox" data-measure="${esc(m.name)}" ${i === 0 ? 'checked' : ''} />
            ${esc(m.name)}
          </label>`).join('')}
      </div>
      ${selected.dimensions.length ? `
        <label class="inline" style="display:block;margin:12px 0 6px">${t('cubes.breakdown')}</label>
        <div class="roster">
          ${selected.dimensions.map((d) => `
            <label class="inline">
              <input type="checkbox" data-dimension="${esc(d)}" /> ${esc(d)}
            </label>`).join('')}
        </div>` : ''}
      ${selected.time_dimensions.length ? `
        <label class="inline" style="display:block;margin:12px 0 6px">${t('cubes.overTime')}</label>
        <select id="cube-time" style="max-width:200px;display:inline-block">
          <option value="">(none)</option>
          ${selected.time_dimensions.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('')}
        </select>
        <select id="cube-grain" style="max-width:160px;display:inline-block;margin-inline-start:8px">
          ${['day','week','month','quarter','year'].map((g) =>
            `<option value="${g}" ${g === 'month' ? 'selected' : ''}>${g}</option>`).join('')}
        </select>` : ''}`;
  }

  paintControls();

  $('cube-pick').onchange = () => {
    selected = cubeCache.find((c) => c.name === $('cube-pick').value);
    paintControls();
    $('cube-result').innerHTML = '';
  };

  $('cube-run').onclick = async () => {
    const measures = [...document.querySelectorAll('[data-measure]:checked')]
      .map((el) => el.dataset.measure);
    const dimensions = [...document.querySelectorAll('[data-dimension]:checked')]
      .map((el) => el.dataset.dimension);
    const time = $('cube-time') ? $('cube-time').value : '';

    if (!measures.length) return toast(t('cubes.pickMeasure'));

    $('cube-result').innerHTML = `<div class="empty">${t('cubes.running')}</div>`;
    let payload;
    try {
      payload = await api(`/api/vanna/v2/cubes/${encodeURIComponent(selected.name)}/query`, {
        method: 'POST',
        body: JSON.stringify({
          measures, dimensions,
          time_dimension: time || null,
          granularity: time ? $('cube-grain').value : null,
        }),
      });
    } catch (error) {
      $('cube-result').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
      return;
    }

    $('cube-result').innerHTML = `
      ${(payload.warnings || []).map((w) =>
        `<div class="card small" style="color:var(--warn)">${esc(w)}</div>`).join('')}
      <div class="card"><div id="cube-chart" style="height:300px"></div></div>
      <div class="card scroll-x">
        <table class="data">
          <thead><tr>${payload.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
          <tbody>${payload.rows.map((row) =>
            `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}
          </tbody>
        </table>
        <div class="muted small" style="margin-top:8px">${payload.row_count} rows</div>
      </div>`;

    // The first column is whatever we grouped by; the rest are the measures.
    const grouped = dimensions.length || time;
    mountChart($('cube-chart'),
      { chart: { type: time ? 'line' : 'bar',
                 x: grouped ? payload.columns[0] : null,
                 y: payload.columns.slice(grouped ? 1 : 0) } },
      payload);
  };
}

// ------------------------------------------------------------- threads ---
//
// A conversation is one person's working notes. The backend scopes them by
// (tenant, user) and the agent already persists every turn, so this only has to
// list, switch and rename.

let threads = [];
let currentThread = null;
//: Client-side filter over the loaded page. The route has no search parameter, so
//: this narrows what has been fetched rather than pretending to search the server.
let threadFilter = '';
//: Whether the server said there is another page. `summaries` returns at most
//: `limit`, so "we got a full page" is the signal.
let threadsHaveMore = false;
const THREAD_PAGE = 30;

async function loadThreads({ append = false } = {}) {
  const offset = append ? threads.length : 0;
  let payload;
  try {
    payload = await api(
      `/api/vanna/v2/conversations?limit=${THREAD_PAGE}&offset=${offset}`
    );
  } catch (_) {
    // A rail that cannot load is worse than no rail: it shows a stale list that
    // no longer matches the server.
    $('threads').style.display = 'none';
    return;
  }
  if (!payload.persisted) {
    // Without a control plane, threads vanish on restart. Say so, rather than
    // hiding the rail and letting it read as a broken page. New chat stays --
    // chatting still works, it just is not remembered -- so only the list and
    // its controls are replaced.
    threads = [];
    threadsHaveMore = false;
    $('thread-search').hidden = true;
    $('thread-heading').hidden = true;
    $('thread-list').innerHTML =
      `<p class="muted small" style="padding:6px 8px">${esc(t('thread.notPersisted'))}</p>`;
    $('thread-more').hidden = true;
    return;
  }
  const page = payload.conversations || [];
  threads = append ? threads.concat(page) : page;
  threadsHaveMore = page.length === THREAD_PAGE;
  paintThreads();
}

function paintThreads() {
  const term = (threadFilter || '').trim().toLowerCase();
  const shown = term
    ? threads.filter((thread) => (thread.title || '').toLowerCase().includes(term))
    : threads;

  if (!shown.length) {
    $('thread-list').innerHTML =
      `<p class="muted small" style="padding:6px 8px">${
        term ? t('thread.noMatches') : t('thread.none')
      }</p>`;
    paintThreadTools({ listed: 0 });
    return;
  }

  // A `role=list` of bare divs with onclick is unreachable by keyboard. Rows are
  // buttons inside listitems, and `roveFocus` gives the whole rail one tab stop
  // with arrows to move inside it -- the same treatment the view tablist gets.
  $('thread-list').innerHTML = shown.map((thread) => {
    const title = thread.title || t('thread.untitled');
    const badge = threadBadge(thread);
    return `
    <div class="thread" role="listitem" data-id="${esc(thread.id)}"
         aria-current="${thread.id === currentThread}">
      <button class="label" data-open="${esc(thread.id)}" title="${esc(title)}">
        <span>${esc(title)}</span>${badge}
      </button>
      <button class="menu" data-rename="${esc(thread.id)}"
              aria-label="${esc(t('thread.renameOne', { title }))}"
              title="${esc(t('thread.rename'))}">&#9998;</button>
      <button class="menu" data-del="${esc(thread.id)}"
              aria-label="${esc(t('thread.deleteOne', { title }))}"
              title="${esc(t('common.delete'))}">&times;</button>
    </div>`;
  }).join('');

  $('thread-list').querySelectorAll('[data-open]').forEach((button) => {
    button.onclick = () => openThread(button.dataset.open);
  });
  $('thread-list').querySelectorAll('[data-rename]').forEach((button) => {
    button.onclick = () => renameThread(button.dataset.rename);
  });
  $('thread-list').querySelectorAll('[data-del]').forEach((button) => {
    button.onclick = () => deleteThread(button.dataset.del);
  });
  paintThreadTools({ listed: shown.length });
}

/**
 * Which database a thread is about.
 *
 * Only when the workspace has more than one -- otherwise it is a badge saying the
 * only thing it could say. The binding is per conversation server-side, so this is
 * the one place a user can see it without opening the thread.
 */
function threadBadge(thread) {
  if (dataSources.length < 2 || !thread.data_source_id) return '';
  return `<span class="chip">${esc(labelForSource(thread.data_source_id))}</span>`;
}

/** The search box and "load more", repainted with the list. */
function paintThreadTools({ listed = 0 } = {}) {
  const search = $('thread-search');
  if (search && search.value !== threadFilter) search.value = threadFilter;
  const more = $('thread-more');
  if (more) more.hidden = !threadsHaveMore;
  // A heading over "No conversations yet" is furniture with nothing under it.
  const heading = $('thread-heading');
  if (heading) heading.hidden = listed === 0;
}

async function openThread(id) {
  let payload;
  try {
    payload = await api(`/api/vanna/v2/conversations/${encodeURIComponent(id)}`);
  } catch (error) { return toast(error.message); }

  currentThread = id;
  switchView('ask');
  // Without this the widget keeps posting to the previous conversation and the
  // two transcripts silently merge server-side.
  chatEl.loadConversation(id, payload.messages);
  paintThreads();
}

function newThread() {
  currentThread = chatEl.newConversation();
  switchView('ask');
  paintThreads();
}

async function renameThread(id) {
  const thread = threads.find((item) => item.id === id);
  const title = await promptSheet(t, {
    title: t('thread.rename'),
    label: t('thread.title'),
    value: thread ? thread.title : '',
  });
  if (!title) return;
  try {
    await api(`/api/vanna/v2/conversations/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: JSON.stringify({ title }),
    });
    await loadThreads();
  } catch (error) { toast(error.message); }
}

async function deleteThread(id) {
  const thread = threads.find((item) => item.id === id);
  const ok = await confirmSheet(t, {
    title: t('thread.deleteTitle'),
    body: t('thread.deleteOne', { title: (thread && thread.title) || t('thread.untitled') }),
    confirmLabel: t('common.delete'),
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/vanna/v2/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (error) { return toast(error.message); }
  if (currentThread === id) newThread();
  await loadThreads();
}

// ---------------------------------------------------------- dashboards ---

async function renderDashboards() {
  const root = $('view-other');
  let dashboards;
  try {
    ({ dashboards } = await api('/api/vanna/v2/dashboards'));
  } catch (error) {
    root.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  root.innerHTML = `
    <h2 class="view-title">${t('dash.title')}</h2>
    <p class="view-sub">${t('dash.sub')}</p>
    <div id="dash-body">
      ${dashboards.length ? dashboards.map((d, index) => `
        <div class="card">
          <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
            <strong style="flex:1;min-width:180px">${esc(d.title)}</strong>
            <span class="muted small">${esc(d.created_by)} · ${esc(relative(d.updated_at))}</span>
          </div>
          <div class="toolbar" style="margin:12px 0 0">
            <button class="btn primary" data-open="${index}">${t('dash.open')}</button>
            <button class="btn danger" data-del="${index}">${t('common.delete')}</button>
          </div>
        </div>`).join('')
      : `<div class="empty">${t('dash.empty')}</div>`}
    </div>`;

  root.querySelectorAll('[data-open]').forEach((button) => {
    button.onclick = () => openDashboard(dashboards[Number(button.dataset.open)]);
  });
  root.querySelectorAll('[data-del]').forEach((button) => {
    const dashboard = dashboards[Number(button.dataset.del)];
    button.onclick = async () => {
      const go = await confirmSheet(t, {
        title: t('dash.deleteTitle'),
        body: t('dash.deleteConfirm', { title: dashboard.title }),
        confirmLabel: t('common.delete'),
        danger: true,
      });
      if (!go) return;
      try {
        await api(`/api/vanna/v2/dashboards/${encodeURIComponent(dashboard.id)}`,
                  { method: 'DELETE' });
        renderDashboards();
      } catch (error) { toast(error.message); }
    };
  });
}

async function openDashboard(summary) {
  openSheet(`<h3>${esc(summary.title)}</h3><div class="empty">${t('dash.runningTiles')}</div>`);

  let payload;
  try {
    payload = await api(`/api/vanna/v2/dashboards/${encodeURIComponent(summary.id)}/data`);
  } catch (error) {
    openSheet(`<h3>${esc(summary.title)}</h3>
      <div class="small" style="color:var(--bad)">${esc(error.message)}</div>
      <div class="actions"><button class="btn" data-close>${t('common.close')}</button></div>`);
    return;
  }

  const tiles = (summary.document?.tiles) || [];
  const byId = Object.fromEntries(payload.results.map((r) => [r.tile_id, r]));

  // Laid out on the 12-column grid each tile has always carried and nothing has
  // ever read: `grid: {x, y, width, height}` was stored, exported, and then
  // rendered as one column of stacked cards, so a dashboard built as four
  // metrics across and two charts side by side arrived as eight full-width
  // blocks in a modal. Ordering by (y, x) keeps rows intact when a tile's span
  // does not divide evenly.
  const laidOut = tiles.slice().sort((left, right) => {
    const a = left.grid || {}, b = right.grid || {};
    return (a.y || 0) - (b.y || 0) || (a.x || 0) - (b.x || 0);
  });

  const body = laidOut.map((tile) => {
    const result = byId[tile.id] || {};
    // The span goes on the card as an inline custom property rather than a class:
    // a width is a number from the document, and twelve classes to express
    // twelve numbers is worse than the number.
    const span = `style="--span:${Math.min(Math.max(tile.grid?.width || 6, 1), 12)}"`;

    if (tile.kind === 'text') {
      return `<div class="card tile" ${span}><strong>${esc(tile.title || '')}</strong>
                ${headings(tile.text || '')}</div>`;
    }
    if (result.error) {
      // Per tile, so one broken query leaves the rest of the page readable.
      return `<div class="card tile" ${span}><strong>${esc(tile.title || 'Untitled')}</strong>
                <div class="small" style="color:var(--bad);margin-top:6px">
                  ${esc(result.error)}</div></div>`;
    }
    const columns = result.columns || [];
    const rows = result.rows || [];
    const caveats = (result.warnings || []).map((w) =>
      `<div class="small" style="color:var(--warn);margin-top:6px">${esc(w)}</div>`
    ).join('');

    // Every tile that has data is a Plotly figure -- a chart, an `indicator` for
    // a metric, a `table` trace for rows. One renderer rather than three, and the
    // same one the export uses, so a tile looks the same wherever it is read.
    //
    // Mounted after insertion, because <plotly-chart> takes its data as element
    // properties and those cannot be expressed in an HTML string.
    const rowsHigh = tile.grid?.height || (tile.kind === 'metric' ? 3 : 5);
    const height = Math.max(tile.kind === 'metric' ? 120 : 180, rowsHigh * 52);
    const heading = tile.kind === 'metric'
      ? ''  // the indicator carries its own title, and two would be a repetition
      : `<strong>${esc(tile.title || 'Untitled')}</strong>`;
    return `
      <div class="card tile" ${span}>
        ${heading}
        ${caveats}
        <div class="tile-chart" data-tile="${esc(tile.id)}"
             style="height:${height}px;margin-top:${heading ? 10 : 0}px"></div>
        <div class="tile-note muted small" data-note="${esc(tile.id)}"></div>
      </div>`;
  }).join('');

  openSheet(`<h3>${esc(summary.title)}</h3>
    <div class="tile-grid">${body || `<div class="empty">${t('dash.noTiles')}</div>`}</div>
    <div class="actions">
      <button class="btn" id="dash-export">${t('dash.export')}</button>
      <button class="btn" data-close>${t('common.close')}</button>
    </div>
    <p class="muted small">${t('dash.exportHint')}</p>`,
    { label: summary.title, wide: true });

  // The server re-runs the tiles as this user and streams back a file; the
  // browser never assembles the document, so what is downloaded is exactly what
  // the access rules allowed.
  const exportBtn = $('dash-export');
  if (exportBtn) exportBtn.onclick = async () => {
    exportBtn.disabled = true;
    const original = exportBtn.textContent;
    exportBtn.textContent = t('dash.exporting');
    try {
      const response = await fetch(
        `/api/vanna/v2/dashboards/${encodeURIComponent(summary.id)}/export`,
        { credentials: 'include', headers: authHeaders() }
      );
      if (!response.ok) throw new Error(errorText((await response.json()).detail, 'Export failed'));

      // Filename comes from Content-Disposition so the server names the file.
      const disposition = response.headers.get('content-disposition') || '';
      const match = /filename="([^"]+)"/.exec(disposition);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = match ? match[1] : 'dashboard.html';
      link.click();
      URL.revokeObjectURL(url);
      toast(t('dash.exported'));
    } catch (error) {
      toast(error.message);
    } finally {
      exportBtn.disabled = false;
      exportBtn.textContent = original;
    }
  };

  // Mounted after the sheet exists, because <plotly-chart> receives its series as
  // element properties rather than attributes. Text tiles are prose and metric,
  // table and chart tiles are all figures.
  laidOut.filter((tile) => tile.kind !== 'text').forEach((tile) => {
    const sheet = $('sheet');
    const mount = sheet.querySelector(`.tile-chart[data-tile="${CSS.escape(tile.id)}"]`);
    if (mount) {
      const note = mountChart(mount, tile, byId[tile.id] || {});
      const line = sheet.querySelector(`.tile-note[data-note="${CSS.escape(tile.id)}"]`);
      if (line && note) line.textContent = note;
    }
  });
}

/**
 * The little of markdown a text tile actually uses: headings and paragraphs.
 *
 * `TileKind.TEXT` is documented as markdown and was rendered with `esc()` alone,
 * so a section heading arrived on screen as a literal `## Catalogue performance`
 * -- the syntax, not the effect. Deliberately not a markdown parser: every line
 * is escaped first and only the leading hashes are interpreted, so there is no
 * path from a stored tile to markup.
 */
function headings(text) {
  return text.split(/\n{2,}|\n(?=#)/).map((block) => {
    const line = block.trim();
    if (!line) return '';
    const hashes = /^(#{1,6})\s+(.*)$/s.exec(line);
    if (!hashes) return `<p class="muted small">${esc(line)}</p>`;
    const level = Math.min(hashes[1].length + 2, 6);
    return `<h${level} style="margin:6px 0 2px;font-size:.95rem">${esc(hashes[2])}</h${level}>`;
  }).join('');
}

/**
 * Draw one tile into `mount`, and return the note to print under it (if any).
 *
 * The figure itself comes from `shared/tile-figure.js`, which the exported HTML
 * inlines and calls too. That sharing is the point: the export used to draw its
 * own SVG approximations with their own axis-picking rules, so the file somebody
 * circulated showed a different chart than the screen it came from.
 */
function mountChart(mount, tile, result) {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  // `<plotly-chart>` sizes to `layout.height` and falls back to 400px, with a
  // `min-height: 400px` under it -- so a chart in a tile shorter than that grew
  // out of its card and drew over the tile beneath. The tile owns the height.
  const boxed = Math.round(mount.getBoundingClientRect().height) || 300;

  let figure = null;
  try {
    figure = tileFigure(tile, result,
                        { height: boxed, dark, otherLabel: t('dash.otherSlice') });
  } catch (error) {
    // A tile whose data the builder cannot make sense of says so, in its own
    // card, rather than taking the rest of the dashboard down with it.
    mount.innerHTML = `<div class="small" style="color:var(--bad)">${esc(error.message)}</div>`;
    return '';
  }

  if (!figure) {
    mount.innerHTML = `<div class="empty small">${t('dash.noData')}</div>`;
    return '';
  }

  const element = document.createElement('plotly-chart');
  // `<plotly-chart>` defaults its `theme` property to 'dark' and nothing here was
  // setting it, so every tile on a light page got the dark-theme modebar: a near
  // opaque charcoal band across the top of the plot, over the highest gridline and
  // its tick label. The traces were right and the numbers were right, which is why
  // it survived -- it reads as a styling quirk rather than the wrong theme.
  element.theme = dark ? 'dark' : 'light';
  element.data = figure.traces;
  element.layout = figure.layout;
  element.config = figure.config;
  element.style.height = '100%';
  mount.innerHTML = '';
  mount.appendChild(element);
  return figure.note || '';
}

// ----------------------------------------------------------- run result ---

async function runSql(sql, options = {}) {
  if (!sql) return toast(t('result.noSql'));

  // The statement stays editable while the result is on screen. Adjusting a
  // LIMIT or a filter and re-running is the single most common thing anyone
  // does with generated SQL, and closing the sheet to do it loses the result
  // you were comparing against.
  const state = { sql, result: null, error: null, running: false };

  function render() {
    const isWrite = !/^\s*(select|with)/i.test(state.sql);
    openSheet(`
      <h3>${esc(options.title || 'Query')}</h3>
      <textarea id="sql-edit" class="sql-editor" spellcheck="false">${esc(state.sql)}</textarea>
      ${isWrite ? `
        <div class="small" style="color:var(--warn);margin-top:8px">
          This is not a SELECT. It will be rejected unless writes are enabled
          for this workspace and you are an admin.
        </div>` : ''}
      <div class="toolbar" style="margin:10px 0 0">
        <button class="btn primary" id="sql-run" ${state.running ? 'disabled' : ''}>
          ${state.running ? 'Running…' : 'Run'}
        </button>
        <button class="btn" id="sql-save">${t('result.saveAsQuery')}</button>
        <span class="muted small">${t('result.ctrlEnter')}</span>
      </div>
      ${state.error ? `
        <div class="card small" style="color:var(--bad);margin-top:12px">${esc(state.error)}</div>` : ''}
      ${state.result ? `
        <div class="muted small" style="margin:12px 0 6px">
          ${state.result.row_count} row${state.result.row_count === 1 ? '' : 's'}${
            state.result.truncated ? ' (capped)' : ''}
        </div>
        <div class="scroll-x card" style="max-height:44vh;overflow:auto">
          <table class="data">
            <thead><tr>${state.result.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
            <tbody>${state.result.rows.map((row) =>
              `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}
            </tbody>
          </table>
        </div>` : ''}
      <div class="actions">
        ${state.result ? `<button class="btn" id="dl-csv">${t('result.downloadCsv')}</button>` : ''}
        <button class="btn" data-close>${t('common.close')}</button>
      </div>`);

    const editor = $('sql-edit');
    editor.oninput = () => { state.sql = editor.value; };
    editor.onkeydown = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        execute();
      }
    };

    $('sql-run').onclick = execute;
    $('sql-save').onclick = () => openSaveSheet(options.question || '', state.sql);
    if ($('dl-csv')) $('dl-csv').onclick = () => downloadCsv(state.result);
  }

  async function execute() {
    if (!state.sql.trim()) return toast(t('result.nothingToRun'));
    state.running = true;
    state.error = null;
    render();
    try {
      // Through the same endpoint, so the policy and the semantic compiler
      // apply. A statement someone typed by hand is not more trusted than one
      // the model wrote.
      state.result = await api('/api/vanna/v2/run-sql', {
        method: 'POST',
        body: JSON.stringify({ sql: state.sql, limit: 200 }),
      });
    } catch (error) {
      state.result = null;
      state.error = error.message;
    }
    state.running = false;
    render();
  }

  render();
  execute();
}

function downloadCsv(result) {
  const quote = (value) => {
    const text = value == null ? '' : String(value);
    // Prefix anything a spreadsheet would evaluate. A cell that starts with
    // '=' or '+' is a formula in Excel, and this data came from a database
    // someone else may control.
    const guarded = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
    return `"${guarded.replace(/"/g, '""')}"`;
  };

  const csv = [result.columns.map(quote).join(',')]
    .concat(result.rows.map((row) => row.map(quote).join(',')))
    .join('\n');

  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `datalens-${Date.now()}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

// -------------------------------------------------------------- sheets ---

/** The active dialog's focus trap, so closing can release it. */
let releaseTrap = null;

/**
 * Open the modal sheet.
 *
 * The dialog was previously a div that appeared: keyboard users tabbed straight
 * through it into the page behind, and on close were dropped at the top of the
 * document. `trapFocus` fixes both halves -- it keeps Tab inside and restores focus
 * to whatever opened it.
 */
function openSheet(html, { label = '', wide = false } = {}) {
  const sheet = $('sheet');
  sheet.innerHTML = html;
  // A dashboard is a grid of tiles; at the default reading width its twelve
  // columns are a few pixels each and every chart is a smear.
  sheet.classList.toggle('wide', wide);
  $('overlay').classList.add('on');
  $('overlay').removeAttribute('aria-hidden');
  if (label) sheet.setAttribute('aria-label', label);

  sheet.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = closeSheet;
  });

  if (releaseTrap) releaseTrap();
  releaseTrap = trapFocus(sheet, { onEscape: closeSheet });
}

function closeSheet() {
  if (!$('overlay').classList.contains('on')) return;
  $('overlay').classList.remove('on');
  $('overlay').setAttribute('aria-hidden', 'true');
  $('sheet').innerHTML = '';
  $('sheet').classList.remove('wide');
  if (releaseTrap) {
    releaseTrap();
    releaseTrap = null;
  }
}

// ---------------------------------------------------- workspace switch ---


/**
 * Show only the sign-in options this deployment actually offers.
 *
 * A "forgotten your password" link that leads nowhere because no mail server is
 * configured is worse than no link, and an SSO button on a password-only deployment
 * is a dead end somebody will report as a bug.
 */
async function paintAuthMethods() {
  try {
    const methods = await api('/api/vanna/v2/auth/methods');
    $('si-sso').hidden = !methods.oidc;
    if (methods.oidc && methods.oidc_label) $('si-sso').textContent = methods.oidc_label;
    $('si-forgot').hidden = !methods.can_reset;
    $('si-form').querySelector('#si-password').closest('form')
      .classList.toggle('password-off', !methods.password);
  } catch (_) {
    // An older server, or one that is still starting. Password sign-in is the
    // safe assumption and is what the markup already shows.
  }

  $('si-forgot').onclick = async () => {
    const email = $('si-email').value.trim();
    if (!email) return showSignInError(t('signin.needEmail'));
    try {
      const result = await api('/api/vanna/v2/auth/forgot', {
        method: 'POST',
        body: JSON.stringify({ email }),
      });
      // The server answers identically whether or not the address exists, and so
      // does this: anything else would turn the screen into an account oracle.
      showSignInNotice(result.message || t('signin.resetSent'));
    } catch (error) {
      showSignInError(error.message);
    }
  };

  // A reset link lands back here with ?reset=<token>.
  const token = new URLSearchParams(location.search).get('reset');
  if (token) openPasswordReset(token);
}

/** Choose a new password from a reset link. */
function openPasswordReset(token) {
  openSheet(`
    <h3>${t('pw.chooseNew')}</h3>
    <label for="rs-new">${t('pw.new')}</label>
    <input id="rs-new" type="password" dir="ltr" autocomplete="new-password"
           aria-describedby="rs-rule" />
    <p class="muted small" id="rs-rule">${t('pw.rule')}</p>
    <div class="error" id="rs-error" role="alert"></div>
    <div class="actions">
      <button class="btn" data-close>${t('common.cancel')}</button>
      <button class="btn primary" id="rs-go">${t('pw.set')}</button>
    </div>`, { label: t('pw.chooseNew') });

  $('rs-go').onclick = async () => {
    const next = $('rs-new').value;
    const error = $('rs-error');
    if (next.length < 12) {
      error.textContent = t('pw.tooShort');
      return;
    }
    try {
      await api('/api/vanna/v2/auth/reset', {
        method: 'POST',
        body: JSON.stringify({ token, new_password: next }),
      });
      closeSheet();
      // Drop the token from the address bar: a reset link in browser history is a
      // reset link in whatever syncs that history.
      history.replaceState({}, '', location.pathname);
      showSignInNotice(t('pw.doneSignIn'));
      announce(t('pw.doneSignIn'));
    } catch (failure) {
      error.textContent = failure.message;
    }
  };
}

// ----------------------------------------------------------------- boot ---

async function boot() {
  setTheme();

  // The shared fetch helper needs to know which workspace a request is for, and
  // what to do when the server stops accepting the session. Registered once, here,
  // rather than threaded through every call site.
  setHeaderProvider(authHeaders);
  setAuthFailureHandler((status, detail) => {
    const code = detail && detail.code;
    if (code === 'password_change_required') {
      openPasswordChangeRequired();
      return;
    }
    // A 403 on a single admin action is not a signed-out session; only treat the
    // loss of identity itself that way.
    if (status === 401) {
      identity = null;
      startSignIn(t('signin.expired'));
    }
  });

  // Before anything renders, so the sign-in screen is already translated -- the
  // first screen is exactly the one that must not be in a language you cannot read.
  const options = Object.entries(LOCALES)
    .map(([code, name]) => `<option value="${code}">${name}</option>`).join('');
  ['locale-btn', 'si-locale'].forEach((id) => {
    const picker = $(id);
    if (!picker) return;
    picker.innerHTML = options;
    picker.value = locale;
    picker.onchange = async () => {
      await loadLocale(picker.value);
      // Both pickers show the same setting; keep the other in step.
      ['locale-btn', 'si-locale'].forEach((other) => {
        if ($(other)) $(other).value = locale;
      });
    };
  });
  await loadLocale(locale);

  $('side-toggle').onclick = toggleRail;
  paintRail(localStorage.getItem(RAIL_KEY) === 'mini');
  $('theme-btn').onclick = () => {
    const next = toggleTheme();
    if (chatEl) chatEl.setAttribute('theme', next);
    announce(next === 'dark' ? 'Dark theme' : 'Light theme');
  };
  $('new-chat').onclick = newThread;
  // Debounced like the history search: repainting on every keystroke of a
  // thirty-row list is fine, but the two behave the same way for a reason.
  let threadSearchTimer = null;
  $('thread-search').oninput = (event) => {
    threadFilter = event.target.value;
    window.clearTimeout(threadSearchTimer);
    threadSearchTimer = window.setTimeout(paintThreads, 150);
  };
  $('thread-more').onclick = () => loadThreads({ append: true });
  // Registered once, outside paintThreads: re-registering on every repaint stacks
  // a listener per paint, which console.js documents the hard way.
  roveFocus($('thread-list'), '.thread [data-open]');
  $('preview-prompt').onclick = previewPrompt;
  $('rail-account').onclick = openAccountSheet;
  // Escape is handled by the dialog's own focus trap; a second global listener
  // would fire for every keypress on the page whether a dialog is open or not.
  $('overlay').onclick = (event) => { if (event.target === $('overlay')) closeSheet(); };

  // The view switcher is a tablist: one stop in the tab order, arrows to move
  // within it, which is what a screen reader announces a set of tabs as.
  const nav = document.querySelector('nav.side');
  nav.querySelectorAll('button[data-view]').forEach((button) => {
    button.onclick = () => switchView(button.dataset.view);
  });
  roveFocus(nav, 'button[data-view]');

  // A real <form>, so Enter submits from any field and the browser's own
  // required-field handling applies -- two per-field keydown listeners and a
  // hand-rolled validity check were doing that job less well.
  $('si-form').onsubmit = async (event) => {
    event.preventDefault();
    const tenant   = $('si-tenant').value;
    const email    = $('si-email').value.trim();
    const password = $('si-password').value;
    if (!email) return showSignInError(t('signin.needEmail'));
    if (!password) return showSignInError(t('signin.needPassword'));
    try {
      const session = await signIn(tenant, email, password);
      if (session) enterApp();
    } catch (error) {
      identity = null;
      showSignInError(error.message);
    }
  };

  await paintAuthMethods();

  // The stored value is only the last workspace. Whether there is a session at
  // all is the server's answer, not something this page can know.
  identity = readIdentity() || {};
  try {
    me = await api('/api/vanna/v2/me');
    if (me) enterApp();
  } catch (error) {
    identity = null;
    // A 401/403 on boot is the normal signed-out case, not an error worth
    // shouting about; anything else is worth showing.
    startSignIn(/sign in|not valid|disabled/i.test(error.message) ? '' : error.message);
  }
}

async function enterApp() {
  $('signin').classList.remove('on');
  $('app').classList.add('ready');
  // Before paintChrome: it renders the picker and the "querying X" note, and
  // before mountChat, because the chat element takes its headers once at
  // connect time and one of them names the database.
  await loadDataSources();
  paintChrome();
  if (!chatEl) mountChat();
  loadStarters();
  loadThreads();
  loadUsage();
  switchView('ask');
}

boot();
