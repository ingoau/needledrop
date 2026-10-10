/* Needledrop — frontend. Vanilla JS, no build step, no external resources.
 *
 * Layout of this file:
 *   1. utilities (escaping html templates, time, storage)
 *   2. api helper, toasts, dialogs, clipboard
 *   3. polling + keyed list reconciliation (keeps focus/expanded state)
 *   4. state, router, top bar status
 *   5. context menu
 *   6. Download tab: catalog rendering, search, artist/album/playlist views,
 *      selection, history badges, jobs
 *   7. Inbox tab: keyboard-driven triage (queue + beets-style match diff,
 *      undoable applies)
 *   8. Log tab + download history panel
 *   9. event wiring + init
 *
 * All API paths are relative so the app also works behind a reverse proxy
 * that mounts it under a sub-path.
 */
'use strict';

(() => {
  /* ================================================================ 1. utils */

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const enc = encodeURIComponent;

  const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

  /** Pre-escaped HTML. Everything else interpolated into html`` is escaped. */
  class Raw {
    constructor(s) { this.s = s; }
    toString() { return this.s; }
  }
  const raw = (s) => new Raw(String(s));

  function toHTML(v) {
    if (v == null) return '';
    if (typeof v === 'boolean') return String(v); // aria-expanded="${open}" etc.
    if (v instanceof Raw) return v.s;
    if (Array.isArray(v)) return v.map(toHTML).join('');
    return esc(v);
  }

  function html(strings, ...values) {
    let out = strings[0];
    for (let i = 0; i < values.length; i++) out += toHTML(values[i]) + strings[i + 1];
    return new Raw(out);
  }

  /** Boolean attribute helper: <button${flag(x, 'disabled')}> */
  const flag = (cond, name) => (cond ? raw(` ${name}`) : '');
  const setHTML = (el, h) => { el.innerHTML = toHTML(h); };

  function fragment(h) {
    const t = document.createElement('template');
    t.innerHTML = toHTML(h).trim();
    return t.content.firstElementChild;
  }

  const icon = (name, cls = '') =>
    raw(`<svg class="ic ${cls}" aria-hidden="true" focusable="false"><use href="#i-${name}"></use></svg>`);

  function safeUrl(u) {
    if (!u) return null;
    try {
      const url = new URL(u, location.href);
      return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
    } catch {
      return null;
    }
  }

  function debounce(fn, ms) {
    let t;
    const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
    d.cancel = () => clearTimeout(t);
    return d;
  }

  const store = {
    get(k, d) {
      try {
        const v = localStorage.getItem('nd.' + k);
        return v == null ? d : JSON.parse(v);
      } catch { return d; }
    },
    set(k, v) {
      try { localStorage.setItem('nd.' + k, JSON.stringify(v)); } catch { /* private mode etc. */ }
    },
  };

  function hashId(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return 'h' + (h >>> 0).toString(36);
  }

  const plural = (n, one, many = one + 's') => `${fmtN(n)} ${n === 1 ? one : many}`;
  const fmtN = (n) => Number(n || 0).toLocaleString();
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function splitKey(key) {
    const i = key.indexOf(':');
    return [key.slice(0, i), key.slice(i + 1)];
  }

  const IS_MAC = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || '');
  const UNDO_KBD = IS_MAC ? '⌘Z' : 'Ctrl+Z';
  const UNDO_SPOKEN = IS_MAC ? 'Command Z' : 'Control Z';
  const isPhone = () => window.matchMedia('(max-width: 899px)').matches;

  function isTypingTarget(t) {
    if (!t || !t.tagName) return false;
    if (t.isContentEditable || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return true;
    if (t.tagName === 'INPUT') return !['checkbox', 'radio', 'button', 'submit', 'range', 'file'].includes(t.type);
    return false;
  }

  // --- time

  const nowSec = () => Date.now() / 1000;

  function rel(ts) {
    const d = nowSec() - ts;
    const a = Math.abs(d);
    if (a < 10) return d >= 0 ? 'just now' : 'in a moment';
    let s;
    if (a < 60) s = `${Math.round(a)}s`;
    else if (a < 3600) s = `${Math.round(a / 60)}m`;
    else if (a < 86400) s = `${Math.round(a / 3600)}h`;
    else if (a < 86400 * 30) s = `${Math.round(a / 86400)}d`;
    else return new Date(ts * 1000).toLocaleDateString();
    return d >= 0 ? `${s} ago` : `in ${s}`;
  }

  const absTime = (ts) =>
    new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
  const clock = (ts) =>
    new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const isoTime = (ts) => new Date(ts * 1000).toISOString();

  function timeTag(ts, cls = '') {
    if (!ts) return '';
    return html`<time class="${cls}" datetime="${isoTime(ts)}" data-rel="${ts}" title="${absTime(ts)}">${rel(ts)}</time>`;
  }

  function fmtDur(sec) {
    if (sec == null || !isFinite(sec)) return '';
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  }

  function fmtCountdown(sec) {
    if (sec <= 0) return 'a moment';
    sec = Math.ceil(sec);
    return sec >= 60 ? `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, '0')}s` : `${sec}s`;
  }

  const secsLeft = (until) => `${Math.max(0, Math.ceil(until - nowSec()))}s`;

  function dayLabel(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return 'Today';
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, {
      weekday: 'long', day: 'numeric', month: 'long',
      year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
    });
  }

  function announce(msg) {
    const el = $('#sr-status');
    el.textContent = '';
    requestAnimationFrame(() => { el.textContent = msg; });
  }

  /* ======================================== 2. api, toasts, dialogs, clipboard */

  class ApiError extends Error {}

  function detailText(d) {
    if (!d) return '';
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map((x) => (x && x.msg
        ? `${(x.loc || []).filter((p) => p !== 'body').join('.')}: ${x.msg}`
        : JSON.stringify(x))).join('; ');
    }
    return JSON.stringify(d);
  }

  /**
   * fetch wrapper. Throws ApiError with FastAPI's `detail` text; shows an
   * error toast unless `quiet` is set. `rawBody` sends bytes (file uploads).
   */
  async function api(path, { method = 'GET', body, rawBody, signal, quiet = false } = {}) {
    const init = { method, headers: { Accept: 'application/json' }, signal };
    if (rawBody !== undefined) {
      init.headers['Content-Type'] = 'application/octet-stream';
      init.body = rawBody;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      const err = new ApiError("Can't reach the Needledrop server.");
      if (!quiet) toast(err.message, 'error');
      throw err;
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
    if (!res.ok) {
      const msg = detailText(data && data.detail) || `${res.status} ${res.statusText}`.trim();
      const err = new ApiError(msg);
      err.status = res.status;
      if (!quiet) toast(msg, 'error');
      throw err;
    }
    return data;
  }

  const recentToasts = new Map();

  /**
   * Toast. opts.action = {label, kbd?, run} adds a button; opts.until (epoch
   * seconds) shows a live countdown and closes the toast when it runs out.
   */
  function toast(message, kind = 'info', timeout, opts = {}) {
    const key = kind + '|' + message;
    const t = Date.now();
    if (!opts.action && (recentToasts.get(key) || 0) > t - 2500) return { close() {} };
    recentToasts.set(key, t);
    const box = $('#toasts');
    const ic = { error: 'warn', warn: 'warn', success: 'check' }[kind] || 'info';
    const el = fragment(html`
      <div class="toast t-${kind}" role="${kind === 'error' ? 'alert' : 'status'}">
        ${icon(ic, 'toast-ic')}
        <span class="toast-msg">${message}</span>
        ${opts.until ? html`<span class="toast-secs" data-secs="${opts.until}">${secsLeft(opts.until)}</span>` : ''}
        ${opts.action ? html`<button type="button" class="toast-act">${opts.action.label}${opts.action.kbd ? html` <kbd>${opts.action.kbd}</kbd>` : ''}</button>` : ''}
        <button type="button" class="icon-btn toast-x" aria-label="Dismiss notification">${icon('x')}</button>
        ${opts.until ? html`<span class="toast-bar" style="--left:${Math.max(0, opts.until - nowSec()).toFixed(2)}s" aria-hidden="true"></span>` : ''}
      </div>`);
    let gone = false;
    const close = () => {
      if (gone) return;
      gone = true;
      el.classList.add('out');
      setTimeout(() => el.remove(), 220);
    };
    el.querySelector('.toast-x').addEventListener('click', close);
    if (opts.action) {
      el.querySelector('.toast-act').addEventListener('click', () => { close(); opts.action.run(); });
    }
    box.append(el);
    while (box.children.length > 5) box.firstElementChild.remove();
    const ms = opts.until ? Math.max(0, opts.until - nowSec()) * 1000 + 400
      : timeout ?? (kind === 'error' ? 9000 : 4500);
    setTimeout(close, ms);
    return { close, el };
  }

  function confirmDialog({ title, body, confirm = 'Confirm' }) {
    const dlg = $('#confirm-dialog');
    if (!dlg || typeof dlg.showModal !== 'function') {
      return Promise.resolve(window.confirm(`${title}\n\n${body}`));
    }
    $('#cd-title').textContent = title;
    $('#cd-body').textContent = body;
    $('#cd-ok').textContent = confirm;
    dlg.returnValue = '';
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.showModal();
    });
  }

  const anyDialogOpen = () => $$('dialog').some((d) => d.open);

  async function copyText(text, what = 'Link') {
    const active = document.activeElement;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // Plain-http deployments have no async clipboard API.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.className = 'clip-helper';
        document.body.append(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (active && active.focus) active.focus({ preventScroll: true });
        if (!ok) throw new Error('copy failed');
      }
      toast(`${what} copied.`, 'success', 2500);
    } catch {
      toast(`Couldn't copy automatically: ${text}`, 'warn', 9000);
    }
  }

  const go = (hash) => { location.hash = hash; };

  /* ============================================ 3. polling + reconciliation */

  class Poller {
    constructor(fn, interval) {
      this.fn = fn;
      this.interval = interval;
      this.active = false;
      this.busy = false;
      this.again = false;
      this.timer = null;
    }
    setActive(on) {
      if (on && !this.active) { this.active = true; this.kick(); }
      if (!on && this.active) { this.active = false; clearTimeout(this.timer); }
    }
    async kick() {
      clearTimeout(this.timer);
      if (this.busy) { this.again = true; return; }
      this.busy = true;
      try { await this.fn(); } catch (e) { /* the fn reports its own errors */ }
      this.busy = false;
      if (this.again) { this.again = false; this.kick(); return; }
      this.schedule();
    }
    schedule() {
      clearTimeout(this.timer);
      if (!this.active || document.hidden) return;
      this.timer = setTimeout(() => this.kick(), this.interval());
    }
  }

  const FOCUS_ATTRS = ['id', 'data-act', 'data-key', 'data-idx', 'data-dir', 'data-input', 'data-change', 'data-submit'];

  function focusSelector(el) {
    const sel = FOCUS_ATTRS
      .filter((a) => el.hasAttribute(a))
      .map((a) => `[${a}="${CSS.escape(el.getAttribute(a))}"]`)
      .join('');
    return sel ? el.tagName.toLowerCase() + sel : null;
  }

  function replaceKeepingFocus(oldNode, newNode) {
    const ae = document.activeElement;
    let sel = null;
    let caret = null;
    if (ae && ae !== document.body && oldNode.contains(ae)) {
      sel = focusSelector(ae);
      if (ae.tagName === 'INPUT' && typeof ae.selectionStart === 'number') {
        caret = [ae.selectionStart, ae.selectionEnd];
      }
    }
    oldNode.replaceWith(newNode);
    if (!sel) return;
    const target = newNode.matches(sel) ? newNode : newNode.querySelector(sel);
    if (target && !target.disabled) {
      target.focus({ preventScroll: true });
      if (caret && target.setSelectionRange) {
        try { target.setSelectionRange(caret[0], caret[1]); } catch { /* type without selection */ }
      }
    }
  }

  /**
   * Keyed list rendering. A node is only rebuilt when its signature changes,
   * so open disclosures, focused inputs and typed text survive polling.
   */
  function reconcile(container, list, keyFn, sigFn, renderFn) {
    const cache = container._rc || (container._rc = new Map());
    const seen = new Set();
    const nodes = [];
    for (const item of list) {
      const key = keyFn(item);
      const sig = sigFn(item);
      seen.add(key);
      let entry = cache.get(key);
      if (!entry || entry.sig !== sig) {
        const node = fragment(renderFn(item));
        if (entry && entry.node.isConnected) replaceKeepingFocus(entry.node, node);
        entry = { sig, node };
        cache.set(key, entry);
      }
      nodes.push(entry.node);
    }
    for (const [key, entry] of cache) {
      if (!seen.has(key)) { entry.node.remove(); cache.delete(key); }
    }
    nodes.forEach((n, i) => {
      if (container.children[i] !== n) container.insertBefore(n, container.children[i] || null);
    });
    while (container.children.length > nodes.length) container.lastElementChild.remove();
  }

  /* ========================================================= 4. state/router */

  const S = {
    route: { tab: null, detail: null, sheet: false },
    navDepth: 0,
    dlView: null,
    status: null,
    offline: false,
    prevDownloading: undefined,

    search: { q: '', type: '', results: null, error: null, loading: false, seq: 0, ctrl: null },
    detail: null,           // {type, id, data, error} for artist/album/playlist views
    searchScroll: 0,
    cache: { album: new Map(), playlist: new Map(), artist: new Map() },
    items: new Map(),       // "type:id" -> normalized catalog item
    sections: new Map(),    // section id -> selectable keys (Select all)
    selection: new Map(),   // "type:id" -> item
    playlistAlbums: true,

    jobs: [],
    jobsLoaded: false,
    jobArt: new Map(),
    jobsCollapsed: store.get('jobsCollapsed', false),

    inbox: { items: [], loaded: false },
    tri: {
      sel: null,
      selIndex: 0,
      lastKey: null,
      cand: new Map(),      // path -> shown candidate index
      task: new Map(),      // path -> shown task index
      skipped: new Set(),   // skipped this session
      overrides: new Map(), // optimistic status while a request is in flight
      undoStack: [],
      seenAuto: new Set(),
      undoSeconds: 10,
      showUnchanged: store.get('showUnchanged', true),
      appliedOpen: store.get('appliedOpen', false),
    },

    history: null,
    log: { events: [], loaded: false, kind: store.get('logKind', ''), q: '' },

    open: new Set(),
    pending: new Map(),     // "job:id" | "inbox:path" -> ms the request finished (Infinity in flight)
    inputs: new Map(),
  };

  const ACTIVE_JOB = new Set(['queued', 'downloading', 'finishing']);
  const NEEDS = new Set(['review', 'duplicate', 'error']);
  const PROGRESS = new Set(['waiting', 'queued', 'tagging']);

  function clearPending(prefix, startedAt) {
    for (const [k, ts] of S.pending) {
      if (k.startsWith(prefix) && ts < startedAt) S.pending.delete(k);
    }
  }

  const TABS = ['download', 'inbox', 'log'];
  const TAB_TITLE = { download: 'Download', inbox: 'Inbox', log: 'Log' };
  const DETAIL_TYPES = ['artist', 'album', 'playlist'];

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/');
    const tab = TABS.includes(parts[0]) ? parts[0] : 'download';
    let detail = null;
    if (tab === 'download' && DETAIL_TYPES.includes(parts[1]) && parts[2]) {
      detail = { type: parts[1], id: decodeURIComponent(parts[2]) };
    }
    return { tab, detail, sheet: tab === 'inbox' && parts[1] === 'item' };
  }

  function onRoute(initial = false) {
    if (initial !== true) S.navDepth++;
    closeMenu(false);
    const prev = S.route;
    const r = parseHash();
    S.route = r;

    for (const t of TABS) $(`#view-${t}`).hidden = t !== r.tab;
    $$('.tab').forEach((a) => {
      if (a.dataset.tab === r.tab) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    updateTitle();

    jobsPoller.setActive(r.tab === 'download' || r.tab === 'inbox');
    inboxPoller.setActive(r.tab === 'inbox');
    logPoller.setActive(r.tab === 'log');

    if (r.tab === 'download') {
      const view = r.detail ? `${r.detail.type}:${r.detail.id}` : 'search';
      if (view !== S.dlView) {
        if (S.dlView === 'search' && prev.tab === 'download') S.searchScroll = window.scrollY;
        S.dlView = view;
        if (r.detail) {
          loadDetail(r.detail.type, r.detail.id);
          window.scrollTo(0, 0);
        } else {
          renderSearch();
          requestAnimationFrame(() => window.scrollTo(0, S.searchScroll || 0));
        }
      }
    }
    renderSelBar();
    syncSheet();
    $('#tri-hint').hidden = r.tab !== 'inbox';
    document.body.classList.toggle('on-inbox', r.tab === 'inbox');
    if (r.tab === 'inbox') renderInbox();
    if (r.tab === 'log') { renderLog(); renderHistory(); fetchHistory(); }
    if (prev.tab !== r.tab && r.tab !== 'download') window.scrollTo(0, 0);
  }

  function updateTitle() {
    const n = needsCount();
    document.title = `${n ? `(${n}) ` : ''}${TAB_TITLE[S.route.tab] || ''} · Needledrop`;
  }

  function needsCount() {
    if (S.inbox.loaded) return S.inbox.items.filter((i) => NEEDS.has(i.status)).length;
    const c = (S.status && S.status.inbox_counts) || {};
    return (c.review || 0) + (c.duplicate || 0) + (c.error || 0);
  }

  function updateBadge() {
    const n = needsCount();
    const b = $('#inbox-badge');
    b.hidden = !n;
    b.textContent = n > 99 ? '99+' : String(n);
    $('#tab-inbox').setAttribute('aria-label', n ? `Inbox, ${plural(n, 'item')} need attention` : 'Inbox');
    updateTitle();
  }

  async function fetchStatus() {
    try {
      S.status = await api('api/status', { quiet: true });
      S.offline = false;
    } catch {
      S.offline = true;
    }
    renderStatus();
  }

  function renderStatus() {
    const st = S.status;
    const pill = $('#account-pill');
    let tone;
    let label;
    let title;
    if (S.offline) {
      tone = 'bad';
      label = html`Offline`;
      title = "Can't reach the Needledrop server.";
    } else if (!st) {
      tone = 'neutral';
      label = html`Connecting…`;
      title = 'Checking the Apple Music account…';
    } else if (!st.account || !st.account.ok) {
      tone = 'bad';
      label = html`<span class="pill-long">Apple Music </span>offline`;
      title = `Apple Music unavailable: ${(st.account && st.account.error) || 'unknown error'}`;
    } else {
      const sf = String(st.account.storefront || '').toUpperCase();
      const sub = !!st.account.subscription;
      tone = sub ? 'good' : 'warn';
      label = html`<span class="pill-long">Apple Music${sf ? ' · ' : ''}</span>${sf || 'OK'}${sub ? '' : html`<span class="pill-long"> · no sub</span>`}`;
      title = `Apple Music connected${sf ? ` · storefront ${sf}` : ''} · ${sub ? 'subscription active' : 'no active subscription'}`;
    }
    pill.className = `pill t-${tone}`;
    setHTML($('#account-label'), label);
    pill.title = title;
    pill.setAttribute('aria-label', `Account status: ${title}`);

    const downloading = !!(st && st.downloading);
    const tagging = !!(st && (st.tagging || st.tag_queue));
    $('#act-dl').hidden = !downloading;
    $('#act-tag').hidden = !tagging;
    if (tagging) {
      const what = st.tagging ? shortPath(st.tagging) : 'the inbox';
      $('#act-tag').title = `beets is tagging ${what}${st.tag_queue ? ` · ${st.tag_queue} queued` : ''}`;
    }
    document.body.classList.toggle('is-downloading', downloading);
    if (st && st.log_retention_days) {
      $('#log-sub').textContent = `The latest 300 events. Entries older than ${plural(st.log_retention_days, 'day')} are pruned automatically.`;
    }
    updateBadge();

    const dl = st ? st.downloading || null : undefined;
    if (dl !== S.prevDownloading) {
      if (S.prevDownloading !== undefined) {
        if (jobsPoller.active) jobsPoller.kick();
        if (inboxPoller.active) inboxPoller.kick();
        if (S.route.tab === 'log') fetchHistory();
      }
      S.prevDownloading = dl;
    }
  }

  function shortPath(p) {
    if (!p) return '';
    const root = S.status && S.status.inbox_dir ? String(S.status.inbox_dir).replace(/\/+$/, '') + '/' : null;
    if (root && p.startsWith(root)) return p.slice(root.length);
    return p.split('/').slice(-2).join('/');
  }

  /* ========================================================= 5. context menu */

  const Menu = { el: null, returnFocus: null, trigger: null, openedAt: 0, skipTrigger: null };

  function closeMenu(restore = true) {
    if (!Menu.el) return;
    Menu.el.remove();
    Menu.el = null;
    if (Menu.trigger) Menu.trigger.setAttribute('aria-expanded', 'false');
    const f = Menu.returnFocus;
    Menu.returnFocus = null;
    Menu.trigger = null;
    if (restore && f && f.isConnected) f.focus({ preventScroll: true });
  }

  /** Drop falsy entries and redundant separators. */
  function tidyEntries(entries) {
    const out = [];
    for (const e of entries.filter(Boolean)) {
      if (e.sep && (!out.length || out[out.length - 1].sep)) continue;
      out.push(e);
    }
    while (out.length && out[out.length - 1].sep) out.pop();
    return out;
  }

  function openMenu(entries, { x, y, anchor, label, returnFocus, trigger }) {
    closeMenu(false);
    entries = tidyEntries(entries);
    if (!entries.length) return;
    const el = document.createElement('div');
    el.className = 'ctxmenu';
    el.setAttribute('role', 'menu');
    el.setAttribute('aria-label', label || 'Actions');
    el.tabIndex = -1;
    for (const e of entries) {
      if (e.sep) {
        const s = document.createElement('div');
        s.className = 'ctx-sep';
        s.setAttribute('role', 'separator');
        el.append(s);
        continue;
      }
      if (e.heading) {
        const h = document.createElement('div');
        h.className = 'ctx-heading';
        h.setAttribute('role', 'presentation');
        h.textContent = e.heading;
        el.append(h);
        continue;
      }
      let node;
      if (e.href) {
        node = document.createElement('a');
        node.href = e.href;
        node.target = '_blank';
        node.rel = 'noopener noreferrer';
      } else {
        node = document.createElement('button');
        node.type = 'button';
      }
      node.className = `ctx-item${e.danger ? ' danger' : ''}`;
      node.setAttribute('role', 'menuitem');
      node.tabIndex = -1;
      if (e.disabled) node.setAttribute('aria-disabled', 'true');
      node.innerHTML = toHTML(html`${e.icon ? icon(e.icon) : raw('<span class="ic"></span>')}<span class="ctx-label">${e.label}</span>${e.hint ? html`<span class="ctx-hint">${e.hint}</span>` : ''}`);
      node.addEventListener('click', (ev) => {
        if (e.disabled) { ev.preventDefault(); return; }
        if (!e.href) ev.preventDefault();
        const rf = Menu.returnFocus;
        closeMenu(false);
        Promise.resolve(e.run ? e.run() : null).finally(() => {
          const ae = document.activeElement;
          if (rf && rf.isConnected && (!ae || ae === document.body)) rf.focus({ preventScroll: true });
        });
      });
      el.append(node);
    }
    document.body.append(el);

    const r = el.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const m = 8;
    let left;
    let top;
    if (anchor) {
      const a = anchor.getBoundingClientRect();
      left = a.right - r.width;
      top = a.bottom + 4;
      if (left < m) left = a.left;
      if (top + r.height > vh - m) top = a.top - r.height - 4;
    } else {
      left = x;
      top = y;
      if (left + r.width > vw - m) left = x - r.width;
      if (top + r.height > vh - m) top = y - r.height;
    }
    el.style.left = `${clamp(left, m, Math.max(m, vw - r.width - m))}px`;
    el.style.top = `${clamp(top, m, Math.max(m, vh - r.height - m))}px`;

    Menu.el = el;
    Menu.returnFocus = returnFocus || null;
    Menu.trigger = trigger || null;
    Menu.openedAt = Date.now();
    if (trigger) trigger.setAttribute('aria-expanded', 'true');

    el.addEventListener('keydown', (ev) => {
      const items = $$('[role="menuitem"]', el);
      const i = items.indexOf(document.activeElement);
      const focusAt = (n) => { const it = items[(n + items.length) % items.length]; if (it) it.focus(); };
      switch (ev.key) {
        case 'ArrowDown': focusAt(i + 1); break;
        case 'ArrowUp': focusAt(i < 0 ? items.length - 1 : i - 1); break;
        case 'Home': focusAt(0); break;
        case 'End': focusAt(items.length - 1); break;
        case 'Escape': closeMenu(true); break;
        case 'Tab': closeMenu(true); break;
        case ' ': if (document.activeElement && document.activeElement.tagName === 'A') document.activeElement.click(); else return; break;
        default: {
          if (ev.key.length === 1 && /\S/.test(ev.key)) {
            const ch = ev.key.toLowerCase();
            const order = [...items.slice(i + 1), ...items.slice(0, i + 1)];
            const hit = order.find((n) => n.textContent.trim().toLowerCase().startsWith(ch));
            if (hit) hit.focus();
            break;
          }
          return;
        }
      }
      ev.preventDefault();
      ev.stopPropagation();
    });
    const first = $('[role="menuitem"]:not([aria-disabled="true"])', el) || $('[role="menuitem"]', el);
    if (first) first.focus({ preventScroll: true });
  }

  function openMenuFor(host, pos, returnFocus) {
    const spec = host.dataset.menu;
    if (!spec) return;
    const [kind, rest] = splitKey(spec);
    let entries = null;
    let label = 'Actions';
    if (kind === 'item') {
      const it = S.items.get(rest);
      if (!it) return;
      entries = itemMenu(it);
      label = `${it.name || it.type} actions`;
    } else if (kind === 'job') {
      const j = S.jobs.find((x) => x.id === rest);
      if (!j) return;
      entries = jobMenu(j);
      label = `${j.title || 'Job'} actions`;
    } else if (kind === 'inbox') {
      const it = findItem(rest);
      if (!it) return;
      entries = inboxMenu(it);
      label = `${itemName(it)} actions`;
    }
    if (!entries) return;
    const moreBtn = host.querySelector('[data-act="menu"]');
    openMenu(entries, {
      ...pos,
      label,
      returnFocus: returnFocus && returnFocus !== document.body ? returnFocus : moreBtn || host,
      trigger: pos.anchor && pos.anchor.dataset && pos.anchor.dataset.act === 'menu' ? pos.anchor : null,
    });
  }

  /* ======================================================= 6. Download tab */

  const TYPE_ORDER = ['artist', 'album', 'song', 'playlist'];
  const TYPE_LABEL = { artist: 'Artists', album: 'Albums', song: 'Songs', playlist: 'Playlists' };

  const itemKey = (it) => `${it.type}:${it.id}`;
  const selectable = (it) =>
    !!it && ['song', 'album', 'playlist'].includes(it.type) && !!it.streamable && !!it.url && !!it.id;
  const viewHash = (type, id) => `#download/${type}/${enc(id)}`;

  function artHTML(url, cls, ph = 'disc') {
    const u = safeUrl(url);
    return u
      ? html`<img class="${cls}" src="${u}" alt="" loading="lazy" decoding="async">`
      : html`<span class="${cls} art-ph">${icon(ph)}</span>`;
  }

  const explicitTag = (it) => (it.explicit
    ? html`<span class="explicit" title="Explicit" aria-label="Explicit">E</span>` : '');

  function moreBtn(name, cls = '') {
    return html`<button type="button" class="more-btn ${cls}" data-act="menu" aria-haspopup="menu" aria-expanded="false"
      aria-label="More actions${name ? ` for ${name}` : ''}">${icon('more')}</button>`;
  }

  function artistLinks(it) {
    const arts = (it.artists || []).filter((a) => a && a.id && a.name);
    if (!arts.length) return it.artist || '';
    return arts.map((a, i) => html`${i ? ', ' : ''}<a class="sub-link" href="${viewHash('artist', a.id)}">${a.name}</a>`);
  }

  function isDownloaded(it) {
    if (it.type === 'song') return !!it.in_history;
    if (it.type === 'album') return (it.history_tracks || 0) >= (it.track_count || Infinity);
    return false;
  }

  function historyBadge(it) {
    if (it.type === 'song') {
      return it.in_history
        ? html`<span class="hist-badge full" title="Already downloaded — new downloads skip it">${icon('check')}Downloaded</span>` : '';
    }
    if (it.type === 'album') {
      const n = it.history_tracks || 0;
      if (!n) return '';
      if (isDownloaded(it)) {
        return html`<span class="hist-badge full" title="Every track is already downloaded">${icon('check')}Downloaded</span>`;
      }
      return html`<span class="hist-badge part" title="${n} of ${it.track_count || '?'} tracks already downloaded">${n}/${it.track_count || '?'} downloaded</span>`;
    }
    return '';
  }

  // --- search

  const debouncedSearch = debounce(() => runSearch(), 350);

  async function runSearch({ background = false } = {}) {
    debouncedSearch.cancel();
    const s = S.search;
    const q = s.q.trim();
    if (!q) return;
    if (!background && S.route.detail) go('#download');
    if (s.ctrl) s.ctrl.abort();
    const ctrl = new AbortController();
    s.ctrl = ctrl;
    const seq = ++s.seq;
    s.loading = true;
    renderSearchBusy();
    const params = new URLSearchParams({ q });
    if (s.type && !/music\.apple\.com/i.test(q)) params.set('types', s.type);
    try {
      const data = await api(`api/search?${params}`, { signal: ctrl.signal, quiet: true });
      if (seq !== s.seq) return;
      s.results = (data && data.results) || {};
      s.error = null;
    } catch (e) {
      if (e.name === 'AbortError' || seq !== s.seq) return;
      if (!background) s.error = e.message;
    } finally {
      if (seq === s.seq) {
        s.loading = false;
        renderSearchBusy();
        if (!S.route.detail && S.route.tab === 'download') renderSearch({ quiet: background });
      }
    }
  }

  function renderSearchBusy() {
    $('#search-spin').hidden = !S.search.loading;
    $('#results').classList.toggle('is-loading', S.search.loading && !!S.search.results && !S.route.detail);
  }

  function setType(t) {
    S.search.type = t;
    $$('#type-chips [data-type]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.type === t)));
    if (S.search.q.trim()) {
      S.search.results = null;
      runSearch();
      if (!S.route.detail) renderSearch();
    }
  }

  function setBrowsing(on) {
    $('.dl-layout').classList.toggle('browsing', on);
  }

  function welcomeHTML() {
    return html`
      <div class="welcome">
        <svg class="welcome-mark" viewBox="0 0 40 40" aria-hidden="true">
          <circle cx="18" cy="22" r="16" class="disc-body"/>
          <circle cx="18" cy="22" r="12.5" class="disc-groove"/>
          <circle cx="18" cy="22" r="9.5" class="disc-groove"/>
          <circle cx="18" cy="22" r="5.4" class="disc-label"/>
          <circle cx="18" cy="22" r="1.1" class="disc-hole"/>
          <path d="M36 3.5 33.5 16 25.5 25" class="tonearm"/>
          <circle cx="36" cy="3.8" r="2.6" class="tonearm-pivot"/>
        </svg>
        <h2>Drop the needle.</h2>
        <p>Search Apple Music for an artist, album or song — or paste a <span class="mono">music.apple.com</span> link to grab exactly that release.</p>
        <ul class="tips">
          <li><kbd>/</kbd> jumps to search</li>
          <li>Right-click (or long-press) anything for more actions</li>
          <li>Finished rips land in the <a href="#inbox">Inbox</a> for tagging</li>
        </ul>
      </div>`;
  }

  function skeletonHTML(n = 8) {
    return html`<div class="card-grid skeleton" aria-hidden="true">${Array.from({ length: n }, () => html`
      <div class="card"><div class="card-art"><span class="art shimmer"></span></div>
      <div class="card-body"><span class="sk-line shimmer"></span><span class="sk-line short shimmer"></span></div></div>`)}</div>`;
  }

  function renderSearch({ quiet = false } = {}) {
    const box = $('#results');
    const s = S.search;
    S.sections.clear();
    setBrowsing(!!s.q.trim());
    if (!s.q.trim()) {
      setHTML(box, welcomeHTML());
      return;
    }
    if (s.error) {
      setHTML(box, html`<div class="empty error">${icon('warn')}<p>${s.error}</p>
        <button type="button" class="btn" data-act="search-retry">${icon('refresh')} Try again</button></div>`);
      return;
    }
    if (!s.results) {
      setHTML(box, skeletonHTML());
      return;
    }
    const groups = TYPE_ORDER.filter((t) => (s.results[t] || []).length);
    if (!groups.length) {
      setHTML(box, html`<div class="empty">${icon('search')}<p>Nothing found for “${s.q.trim()}”.</p>
        <p class="muted">Try fewer words, another type filter, or paste a music.apple.com link.</p></div>`);
      if (!quiet) announce('No results');
      return;
    }
    const total = groups.reduce((n, t) => n + s.results[t].length, 0);
    setHTML(box, groups.map((t) => groupHTML(t, s.results[t])));
    if (!quiet) announce(`${plural(total, 'result')}`);
    syncSelUI();
  }

  function groupHTML(type, items, { title, sectionId } = {}) {
    let body;
    if (type === 'artist') body = html`<div class="artist-grid">${items.map(artistCardHTML)}</div>`;
    else if (type === 'song') body = html`<ol class="song-list">${items.map((it) => songRowHTML(it))}</ol>`;
    else body = html`<div class="card-grid">${items.map(cardHTML)}</div>`;
    let selectAll = '';
    if (sectionId) {
      const keys = items.filter(selectable).map(itemKey);
      if (keys.length > 1) {
        S.sections.set(sectionId, keys);
        selectAll = html`<button type="button" class="btn small ghost" data-act="select-all" data-key="${sectionId}">Select all</button>`;
      }
    }
    return html`
      <section class="group">
        <div class="group-head">
          <h2>${title || TYPE_LABEL[type] || type}<span class="count">${items.length}</span></h2>
          ${selectAll}
        </div>
        ${body}
      </section>`;
  }

  function cardHTML(it) {
    const key = itemKey(it);
    S.items.set(key, it);
    const can = selectable(it);
    const href = it.id && (it.type === 'album' || it.type === 'playlist') ? viewHash(it.type, it.id) : null;
    const meta = [];
    if (it.year) meta.push(it.year);
    if (it.type === 'album' && it.track_count) meta.push(plural(it.track_count, 'track'));
    if (it.type === 'playlist') meta.push('Playlist');
    const tags = [];
    if (it.is_single) tags.push('Single');
    if (it.is_compilation) tags.push('Compilation');
    const name = it.name || 'Untitled';
    return html`
      <article class="card selectable${can ? '' : ' unavailable'}" data-item="${key}" data-menu="item:${key}">
        <div class="card-art">
          ${href ? html`<a class="card-open" href="${href}" tabindex="-1" aria-hidden="true">${artHTML(it.artwork, 'art')}</a>` : artHTML(it.artwork, 'art')}
          ${can ? html`<button type="button" class="sel-btn" data-act="toggle-sel" data-key="${key}" aria-pressed="false"
              aria-label="Select ${it.type} ${name}${it.artist ? ` by ${it.artist}` : ''}"><span class="sel-check">${icon('check')}</span></button>` : ''}
          ${moreBtn(name, 'card-more')}
        </div>
        <div class="card-body">
          ${href ? html`<a class="card-title" href="${href}">${name}</a>` : html`<span class="card-title">${name}</span>`}
          <div class="card-sub">${artistLinks(it)}</div>
          <div class="card-meta">${meta.join(' · ')} ${explicitTag(it)} ${tags.map((t) => html`<span class="mini-tag">${t}</span>`)} ${historyBadge(it)}</div>
          ${it.streamable ? '' : html`<div class="note">Not available to stream</div>`}
        </div>
      </article>`;
  }

  function songRowHTML(it, { inAlbum = false, albumArtist = null, multiDisc = false } = {}) {
    const key = itemKey(it);
    S.items.set(key, it);
    const can = selectable(it);
    let sub;
    if (inAlbum) {
      sub = it.artist && it.artist !== albumArtist ? artistLinks(it) : '';
    } else {
      const album = it.album
        ? (it.album_id ? html`<a class="sub-link" href="${viewHash('album', it.album_id)}">${it.album}</a>` : it.album)
        : '';
      sub = html`${artistLinks(it)}${album ? html` · ${album}` : ''}`;
    }
    const num = inAlbum
      ? html`<span class="trk-num">${multiDisc && it.disc_number ? `${it.disc_number}.` : ''}${it.track_number || ''}</span>` : '';
    const name = it.name || 'Untitled';
    return html`
      <li class="row selectable${can ? '' : ' unavailable'}${inAlbum ? ' trk' : ''}${it.in_history ? ' in-history' : ''}" data-item="${key}" data-menu="item:${key}">
        ${can ? html`<button type="button" class="row-sel" data-act="toggle-sel" data-key="${key}" aria-pressed="false"
            aria-label="Select song ${name}${it.artist ? ` by ${it.artist}` : ''}${it.in_history ? ' (already downloaded)' : ''}"><span class="box">${icon('check')}</span></button>`
          : html`<span class="row-sel-ph" aria-hidden="true"></span>`}
        ${inAlbum ? num : artHTML(it.artwork, 'row-art')}
        <div class="row-main">
          <div class="row-title"><span class="row-name">${name}</span> ${explicitTag(it)} ${historyBadge(it)}</div>
          ${toHTML(sub) ? html`<div class="row-sub">${sub}</div>` : ''}
          ${it.streamable ? '' : html`<div class="note">Not available to stream</div>`}
        </div>
        <div class="row-end">${!inAlbum && it.year ? html`<span>${it.year}</span>` : ''}<span class="mono">${fmtDur((it.duration_ms || 0) / 1000) || ''}</span></div>
        ${moreBtn(name, 'row-more')}
      </li>`;
  }

  function artistCardHTML(it) {
    S.items.set(itemKey(it), it);
    const genres = (it.genres || []).filter((g) => g !== 'Music');
    return html`
      <div class="artist-card" data-menu="item:${itemKey(it)}">
        <a class="artist-link" href="${viewHash('artist', it.id)}">
          ${artHTML(it.artwork, 'artist-art', 'mic')}
          <span class="artist-name">${it.name || 'Unknown artist'}</span>
          ${genres.length ? html`<span class="artist-genre">${genres.slice(0, 2).join(', ')}</span>` : ''}
        </a>
        ${moreBtn(it.name, 'artist-more')}
      </div>`;
  }

  // --- artist / album / playlist views

  function goBack() {
    if (S.navDepth > 0) history.back();
    else go('#download');
  }

  const isCurrentDetail = (type, id) =>
    S.detail && S.detail.type === type && S.detail.id === id
    && S.route.detail && S.route.detail.type === type && S.route.detail.id === id;

  async function loadDetail(type, id, { refresh = false } = {}) {
    const cache = S.cache[type];
    if (!refresh) {
      const cached = cache.get(id);
      S.detail = { type, id, data: cached || null, error: null };
      renderDetail();
      if (cached) return;
    }
    try {
      const data = await api(`api/${type}/${enc(id)}`, { quiet: true });
      cache.set(id, data);
      if (isCurrentDetail(type, id)) { S.detail.data = data; S.detail.error = null; }
    } catch (e) {
      if (isCurrentDetail(type, id) && !refresh) S.detail.error = e.message;
    }
    if (isCurrentDetail(type, id)) renderDetail();
  }

  function renderDownloadMain() {
    if (S.route.tab !== 'download') return;
    if (S.route.detail) renderDetail();
    else renderSearch({ quiet: true });
  }

  function renderDetail() {
    const d = S.detail;
    if (!d || S.route.tab !== 'download') return;
    setBrowsing(true);
    S.sections.clear();
    let body;
    if (d.type === 'artist') body = artistViewHTML(d);
    else if (d.type === 'album') body = albumViewHTML(d);
    else body = playlistViewHTML(d);
    setHTML($('#results'), html`<div class="detail-view">
      <div class="detail-nav"><button type="button" class="btn small ghost" data-act="back">${icon('back')} Back</button></div>
      ${body}
    </div>`);
    syncSelUI();
  }

  function detailErrorHTML(d) {
    return html`<div class="empty error">${icon('warn')}<p>${d.error}</p>
      <button type="button" class="btn" data-act="detail-retry">${icon('refresh')} Try again</button></div>`;
  }

  function trackSkeleton(n = 8) {
    return html`<ol class="tracklist" aria-hidden="true">${Array.from({ length: n }, () => html`<li class="row trk sk-row"><span></span><span></span><span class="sk-line shimmer"></span></li>`)}</ol>`;
  }

  function artistViewHTML(d) {
    const known = (d.data && d.data.artist) || S.items.get(`artist:${d.id}`) || { type: 'artist', id: d.id };
    if (d.data && d.data.artist) S.items.set(itemKey(known), known);
    const genres = (known.genres || []).filter((g) => g !== 'Music');
    const url = safeUrl(known.url);
    let body;
    if (d.error) body = detailErrorHTML(d);
    else if (!d.data) body = skeletonHTML(6);
    else if (!d.data.sections || !d.data.sections.length) body = html`<div class="empty">${icon('disc')}<p>No releases found for this artist.</p></div>`;
    else {
      body = d.data.sections.map((sec) => groupHTML(
        (sec.items[0] && sec.items[0].type) || 'album', sec.items,
        { title: sec.title, sectionId: `artist-${d.id}-${sec.key}` },
      ));
    }
    return html`
      <header class="detail-head artist" data-menu="item:artist:${d.id}">
        ${artHTML(known.artwork, 'detail-art round', 'mic')}
        <div class="detail-info">
          <p class="eyebrow">Artist</p>
          <h2 class="detail-title" tabindex="-1">${known.name || 'Artist'}</h2>
          ${genres.length ? html`<p class="detail-meta">${genres.join(', ')}</p>` : ''}
          <div class="detail-actions">
            ${url ? html`<a class="btn ghost" href="${url}" target="_blank" rel="noopener noreferrer">Apple Music ${icon('external')}</a>` : ''}
            ${moreBtn(known.name, 'inline')}
          </div>
        </div>
      </header>
      ${body}`;
  }

  function tracksSection(tracks, { inAlbum, albumArtist, sectionId }) {
    const discs = new Set(tracks.map((t) => t.disc_number).filter(Boolean));
    const keys = tracks.filter(selectable).map(itemKey);
    if (keys.length) S.sections.set(sectionId, keys);
    const have = tracks.filter((t) => t.in_history).length;
    return html`
      <section class="group">
        <div class="group-head">
          <h2>Tracks<span class="count">${tracks.length}</span></h2>
          ${have ? html`<span class="hist-note">${icon('check')} ${have} of ${tracks.length} already downloaded</span>` : ''}
        </div>
        <ol class="tracklist">${tracks.map((t) => songRowHTML(t, { inAlbum, albumArtist, multiDisc: discs.size > 1 }))}</ol>
      </section>`;
  }

  function albumViewHTML(d) {
    const known = (d.data && d.data.album) || S.items.get(`album:${d.id}`) || { type: 'album', id: d.id };
    if (d.data && d.data.album) S.items.set(itemKey(known), known);
    const tracks = d.data ? (d.data.tracks || []).filter((t) => t && t.type === 'song') : [];
    const total = tracks.reduce((n, t) => n + (t.duration_ms || 0), 0) / 1000;
    const kind = known.is_single ? 'Single' : known.is_compilation ? 'Compilation' : 'Album';
    const url = safeUrl(known.url);
    const can = selectable(known);
    const sectionId = `tracks-album-${d.id}`;
    const count = tracks.length || known.track_count;
    const meta = [count ? plural(count, 'track') : null, total ? fmtDur(total) : null, known.record_label].filter(Boolean);
    const hasHist = (known.history_tracks || 0) > 0 || tracks.some((t) => t.in_history);
    return html`
      <header class="detail-head" data-menu="item:album:${d.id}">
        ${artHTML(known.artwork, 'detail-art')}
        <div class="detail-info">
          <p class="eyebrow">${kind}${known.year ? ` · ${known.year}` : ''}</p>
          <h2 class="detail-title" tabindex="-1">${known.name || 'Album'} ${explicitTag(known)}</h2>
          ${toHTML(artistLinks(known)) ? html`<p class="detail-by">${artistLinks(known)}</p>` : ''}
          ${meta.length ? html`<p class="detail-meta">${meta.join(' · ')}</p>` : ''}
          <div class="detail-badges">${historyBadge(known)}${known.streamable === false ? html`<span class="note">Not available to stream</span>` : ''}</div>
          <div class="detail-actions">
            ${can ? html`<button type="button" class="btn primary" data-act="dl-now" data-key="album:${d.id}">${icon('download')} Download album</button>
              <button type="button" class="btn" data-act="toggle-sel" data-key="album:${d.id}" aria-pressed="false">${icon('check')} Select album</button>` : ''}
            ${tracks.filter(selectable).length > 1 ? html`<button type="button" class="btn ghost" data-act="select-all" data-key="${sectionId}" data-on="Select all tracks" data-off="Deselect tracks">Select all tracks</button>` : ''}
            ${url ? html`<a class="btn ghost" href="${url}" target="_blank" rel="noopener noreferrer">Apple Music ${icon('external')}</a>` : ''}
            ${hasHist ? html`<button type="button" class="btn ghost" data-act="forget" data-key="album:${d.id}" title="Remove this album from the download history so it can be downloaded again">${icon('forget')} Forget downloads</button>` : ''}
            ${moreBtn(known.name, 'inline')}
          </div>
        </div>
      </header>
      ${d.error ? detailErrorHTML(d) : !d.data ? trackSkeleton() : tracksSection(tracks, { inAlbum: true, albumArtist: known.artist, sectionId })}`;
  }

  function playlistViewHTML(d) {
    const known = (d.data && d.data.playlist) || S.items.get(`playlist:${d.id}`) || { type: 'playlist', id: d.id };
    if (d.data && d.data.playlist) S.items.set(itemKey(known), known);
    const tracks = d.data ? (d.data.tracks || []).filter((t) => t && t.type === 'song') : [];
    const total = tracks.reduce((n, t) => n + (t.duration_ms || 0), 0) / 1000;
    const url = safeUrl(known.url);
    const can = selectable(known);
    const sectionId = `tracks-playlist-${d.id}`;
    const meta = [tracks.length ? plural(tracks.length, 'track') : null, total ? fmtDur(total) : null].filter(Boolean);
    return html`
      <header class="detail-head" data-menu="item:playlist:${d.id}">
        ${artHTML(known.artwork, 'detail-art')}
        <div class="detail-info">
          <p class="eyebrow">Playlist</p>
          <h2 class="detail-title" tabindex="-1">${known.name || 'Playlist'}</h2>
          ${known.artist ? html`<p class="detail-by">${known.artist}</p>` : ''}
          ${known.description ? html`<p class="detail-desc">${known.description}</p>` : ''}
          ${meta.length ? html`<p class="detail-meta">${meta.join(' · ')}</p>` : ''}
          <div class="detail-actions">
            ${can ? html`
              <button type="button" class="btn primary" data-act="dl-now" data-key="playlist:${d.id}" data-pl="1" title="Rip every album the playlist's songs come from">${icon('download')} Download full albums</button>
              <button type="button" class="btn" data-act="dl-now" data-key="playlist:${d.id}" data-pl="0">Download tracks only</button>
              <button type="button" class="btn ghost" data-act="toggle-sel" data-key="playlist:${d.id}" aria-pressed="false">${icon('check')} Select playlist</button>` : ''}
            ${tracks.filter(selectable).length > 1 ? html`<button type="button" class="btn ghost" data-act="select-all" data-key="${sectionId}" data-on="Select all tracks" data-off="Deselect tracks">Select all tracks</button>` : ''}
            ${url ? html`<a class="btn ghost" href="${url}" target="_blank" rel="noopener noreferrer">Apple Music ${icon('external')}</a>` : ''}
            ${moreBtn(known.name, 'inline')}
          </div>
        </div>
      </header>
      ${d.error ? detailErrorHTML(d) : !d.data ? trackSkeleton() : tracksSection(tracks, { inAlbum: false, sectionId })}`;
  }

  function refreshCatalogViews() {
    for (const m of Object.values(S.cache)) m.clear();
    if (S.search.q.trim() && S.search.results) runSearch({ background: true });
    if (S.route.detail) loadDetail(S.route.detail.type, S.route.detail.id, { refresh: true });
  }

  // --- selection + downloading

  function toggleSel(key) {
    const it = S.items.get(key);
    if (!selectable(it)) return;
    if (S.selection.has(key)) S.selection.delete(key);
    else S.selection.set(key, it);
    syncSelUI();
  }

  function selectAll(sectionId) {
    const keys = S.sections.get(sectionId) || [];
    const all = keys.every((k) => S.selection.has(k));
    for (const k of keys) {
      if (all) S.selection.delete(k);
      else if (S.items.has(k)) S.selection.set(k, S.items.get(k));
    }
    syncSelUI();
  }

  function syncSelUI() {
    $$('[data-act="toggle-sel"]').forEach((b) => {
      const on = S.selection.has(b.dataset.key);
      b.setAttribute('aria-pressed', String(on));
      const holder = b.closest('.selectable');
      if (holder) holder.classList.toggle('selected', on);
    });
    $$('[data-act="select-all"]').forEach((b) => {
      const keys = S.sections.get(b.dataset.key) || [];
      const all = keys.length && keys.every((k) => S.selection.has(k));
      b.textContent = all ? (b.dataset.off || 'Deselect all') : (b.dataset.on || 'Select all');
    });
    renderSelBar();
  }

  function renderSelBar() {
    const n = S.selection.size;
    const show = n > 0 && S.route.tab === 'download';
    $('#selbar').hidden = !show;
    document.body.classList.toggle('has-selection', show);
    if (!n) return;
    const items = [...S.selection.values()];
    $('#sel-count').textContent = fmtN(n);
    const kinds = {};
    items.forEach((i) => { kinds[i.type] = (kinds[i.type] || 0) + 1; });
    const had = items.filter(isDownloaded).length;
    $('#sel-kinds').textContent = ['album', 'song', 'playlist']
      .filter((k) => kinds[k]).map((k) => plural(kinds[k], k)).join(' · ')
      + (had ? ` · ${had} already downloaded (skipped)` : '');
    setHTML($('#sel-art'), items.slice(-3).map((it) => artHTML(it.artwork, 'sel-thumb')));
    $('#pl-albums-wrap').hidden = !items.some((i) => i.type === 'playlist');
    $('#pl-albums').checked = S.playlistAlbums;
  }

  function jobPayload(it) {
    return {
      type: it.type,
      id: String(it.id),
      url: it.url,
      name: it.name ?? null,
      artist: it.artist ?? null,
      album: it.type === 'song' ? (it.album ?? null) : it.type === 'album' ? (it.name ?? null) : null,
      album_id: it.type === 'song' && it.album_id ? String(it.album_id) : null,
      artwork: it.artwork ?? null,
      track_count: Number.isInteger(it.track_count) ? it.track_count : null,
    };
  }

  async function queueDownload(items, { playlistAlbums = S.playlistAlbums, btn = null } = {}) {
    items = items.filter(selectable);
    if (!items.length) {
      toast('Nothing downloadable here.', 'warn');
      return false;
    }
    if (btn) btn.disabled = true;
    try {
      const res = await api('api/jobs', {
        method: 'POST',
        body: { items: items.map(jobPayload), playlist_albums: playlistAlbums, force: false },
      });
      const n = (res && res.created && res.created.length) || items.length;
      const had = items.filter(isDownloaded).length;
      const what = n === 1 ? `“${items[0].name || 'item'}”` : plural(n, 'download');
      toast(`Queued ${what}.${had ? ` ${had === 1 && n === 1 ? 'It was' : `${had} were`} downloaded before and will be skipped.` : ''}`, 'success');
      if (S.jobsCollapsed && isPhone()) setJobsCollapsed(false);
      jobsPoller.kick();
      statusPoller.kick();
      return true;
    } catch {
      return false;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function downloadSelection(btn) {
    const ok = await queueDownload([...S.selection.values()], { btn });
    if (ok) { S.selection.clear(); syncSelUI(); }
  }

  async function forgetHistory(it) {
    const body = it.type === 'album' ? { album_id: String(it.id) } : { media_ids: [String(it.id)] };
    try {
      const r = await api('api/history/forget', { method: 'POST', body });
      const removed = (r && r.removed) || 0;
      toast(removed
        ? `Forgot ${plural(removed, 'downloaded track')} — “${it.name}” can be downloaded again.`
        : `“${it.name}” wasn't in the download history.`, removed ? 'success' : 'info');
      if (it.type === 'song') it.in_history = false;
      if (it.type === 'album') it.history_tracks = 0;
      renderDownloadMain();
      refreshCatalogViews();
      fetchHistory();
    } catch { /* toasted */ }
  }

  function goArtistEntries(it) {
    const arts = (it.artists || []).filter((a) => a && a.id);
    if (!arts.length) return [];
    if (arts.length === 1) {
      return [{ label: 'Go to artist', icon: 'mic', hint: arts[0].name, run: () => go(viewHash('artist', arts[0].id)) }];
    }
    return [{ heading: 'Go to artist' },
      ...arts.map((a) => ({ label: a.name || 'Artist', icon: 'mic', run: () => go(viewHash('artist', a.id)) }))];
  }

  function itemMenu(it) {
    const key = itemKey(it);
    const can = selectable(it);
    const isSel = S.selection.has(key);
    const url = safeUrl(it.url);
    const viewing = S.route.detail && S.route.detail.type === it.type && S.route.detail.id === String(it.id);
    const select = can && { label: isSel ? 'Deselect' : 'Select', icon: 'check', run: () => toggleSel(key) };
    const tail = [
      { sep: true },
      url && { label: 'Open in Apple Music', icon: 'external', href: url },
      url && { label: 'Copy link', icon: 'link', run: () => copyText(it.url, 'Link') },
    ];
    switch (it.type) {
      case 'song':
        return [
          { label: 'Download', icon: 'download', disabled: !can, hint: it.in_history ? 'already downloaded' : '', run: () => queueDownload([it]) },
          it.album_id && { label: 'Go to album', icon: 'disc', hint: it.album, run: () => go(viewHash('album', it.album_id)) },
          ...goArtistEntries(it),
          select,
          ...tail,
          it.in_history && { sep: true },
          it.in_history && { label: 'Forget download', icon: 'forget', hint: 'allow re-download', run: () => forgetHistory(it) },
        ];
      case 'album':
        return [
          !viewing && { label: 'Open album', icon: 'open', run: () => go(viewHash('album', it.id)) },
          { label: 'Download', icon: 'download', disabled: !can, hint: isDownloaded(it) ? 'already downloaded' : '', run: () => queueDownload([it]) },
          ...goArtistEntries(it),
          select,
          ...tail,
          (it.history_tracks || 0) > 0 && { sep: true },
          (it.history_tracks || 0) > 0 && { label: 'Forget downloads', icon: 'forget', hint: `${it.history_tracks} track${it.history_tracks === 1 ? '' : 's'}`, run: () => forgetHistory(it) },
        ];
      case 'artist':
        return [
          !viewing && { label: 'Open artist', icon: 'open', run: () => go(viewHash('artist', it.id)) },
          ...tail,
        ];
      case 'playlist':
        return [
          !viewing && { label: 'Open playlist', icon: 'open', run: () => go(viewHash('playlist', it.id)) },
          { label: 'Download (full albums)', icon: 'download', disabled: !can, run: () => queueDownload([it], { playlistAlbums: true }) },
          { label: 'Download (playlist tracks only)', icon: 'download', disabled: !can, run: () => queueDownload([it], { playlistAlbums: false }) },
          select,
          ...tail,
        ];
      default:
        return tail;
    }
  }

  // --- jobs

  const JOB_STATUS = {
    queued: ['Queued', 'neutral'],
    downloading: ['Downloading', 'info'],
    finishing: ['Moving to inbox', 'info'],
    done: ['Done', 'good'],
    skipped: ['Already downloaded', 'muted'],
    partial: ['Incomplete', 'warn'],
    failed: ['Failed', 'bad'],
    cancelled: ['Cancelled', 'muted'],
  };

  const SUMMARY_LABELS = [
    ['completed', 'downloaded'], ['lossy', 'lossy (AAC)'], ['unavailable', 'unavailable'],
    ['skipped', 'skipped'], ['failed', 'failed'],
  ];

  /** Which songs came down lossy, and which Apple wouldn't provide. */
  function qualityHTML(lossy, unavailable) {
    const out = [];
    if (lossy && lossy.length) {
      out.push(html`<p class="job-quality lossy">${icon('warn')}<span><strong>Lossy (AAC):</strong> ${lossy.map((t) => t.title).join(', ')}</span></p>`);
    }
    if (unavailable && unavailable.length) {
      out.push(html`<p class="job-quality unavail">${icon('info')}<span><strong>Not downloaded:</strong> ${unavailable.map((t) => `${t.title} (${t.reason})`).join(', ')}</span></p>`);
    }
    return out;
  }

  async function fetchJobs() {
    const started = Date.now();
    const data = await api('api/jobs', { quiet: true });
    S.jobs = (data && data.jobs) || [];
    S.jobsLoaded = true;
    for (const j of S.jobs) if (j.artwork) S.jobArt.set(j.id, j.artwork);
    clearPending('job:', started);
    renderJobs();
    if (S.route.tab === 'inbox') renderInbox();
  }

  function setJobsCollapsed(v) {
    S.jobsCollapsed = v;
    store.set('jobsCollapsed', v);
    $('#jobs-panel').classList.toggle('collapsed', v);
    $('#jobs-toggle').setAttribute('aria-expanded', String(!v));
  }

  function renderJobs() {
    const active = S.jobs.filter((j) => ACTIVE_JOB.has(j.status)).length;
    $('#jobs-summary').textContent = S.jobs.length
      ? (active ? `${active} active · ${S.jobs.length}` : String(S.jobs.length)) : '';
    $('#jobs-empty').hidden = !S.jobsLoaded || S.jobs.length > 0;
    $('#jobs-clear').disabled = !S.jobs.some((j) => ['done', 'skipped', 'cancelled'].includes(j.status));
    reconcile($('#jobs'), S.jobs, (j) => j.id, jobSig, jobHTML);
  }

  const jobSig = (j) => `${JSON.stringify(j)}|${S.open.has('out:' + j.id)}|${S.pending.has('job:' + j.id)}`;
  const jobTarget = (j) => (j.kind === 'album' || j.kind === 'playlist') && j.apple_id ? viewHash(j.kind, j.apple_id) : null;

  function jobHTML(j) {
    const [label, tone] = JOB_STATUS[j.status] || [j.status, 'neutral'];
    const running = j.status === 'downloading' || j.status === 'finishing';
    const p = (running && j.progress) || {};
    const done = running && p.done != null ? p.done : (j.done_tracks || 0);
    const exp = (running && p.total) || j.expected_tracks;
    const busy = S.pending.has('job:' + j.id);
    const dis = flag(busy, 'disabled');
    const outKey = 'out:' + j.id;
    const outOpen = S.open.has(outKey);
    const url = safeUrl(j.url);
    const opts = j.options || {};
    const target = jobTarget(j);

    let progress = '';
    if (running) {
      const pct = exp ? Math.min(100, (done / exp) * 100) : null;
      const bits = [exp ? `${done} / ${exp} tracks` : plural(done, 'track')];
      if (p.albums) bits.push(plural(p.albums, 'album'));
      if (p.skipped_history) bits.push(`${p.skipped_history} already downloaded`);
      const otherSkipped = (p.skipped || 0) - (p.skipped_history || 0);
      if (otherSkipped > 0) bits.push(`${otherSkipped} skipped`);
      if (p.lossy && p.lossy.length) bits.push(`${p.lossy.length} lossy`);
      if (p.unavailable && p.unavailable.length) bits.push(`${p.unavailable.length} unavailable`);
      if (p.failed) bits.push(`${p.failed} failed`);
      progress = html`
        <div class="progress${pct == null ? ' indeterminate' : ''}" role="progressbar" aria-label="Download progress"
             ${pct == null ? '' : raw(`aria-valuemin="0" aria-valuemax="${exp}" aria-valuenow="${done}"`)}>
          <span style="width:${pct == null ? 35 : pct.toFixed(1)}%"></span>
        </div>
        <div class="job-progress-text">
          <span>${bits.join(' · ')}</span>
          ${j.status === 'finishing' ? html`<span class="muted">moving files…</span>` : ''}
        </div>
        ${p.current && j.status === 'downloading' ? html`<div class="job-current" title="${p.current}">${icon('disc', 'spin-slow')} ${p.current}</div>` : ''}`;
    } else if (exp && ['done', 'partial', 'failed', 'cancelled'].includes(j.status) && done) {
      const pct = Math.min(100, (done / exp) * 100);
      progress = html`
        <div class="progress static t-${tone}" role="img" aria-label="${done} of ${exp} tracks"><span style="width:${pct.toFixed(1)}%"></span></div>
        <div class="job-progress-text"><span>${done} of ${exp} tracks</span></div>`;
    } else if (j.status === 'done' && done) {
      progress = html`<div class="job-progress-text"><span>${plural(done, 'track')} ripped</span></div>`;
    }

    const sum = j.summary || {};
    const stats = running ? [] : SUMMARY_LABELS.filter(([k]) => sum[k]).map(([k, l]) => html`<span class="stat">${sum[k]} ${l}</span>`);
    const showErr = j.error && !(j.status === 'cancelled' && j.error === 'Cancelled');
    const errCls = j.status === 'skipped' || j.status === 'done' ? 'note' : '';

    const btns = [];
    if (j.status === 'queued' || j.status === 'downloading') {
      btns.push(html`<button type="button" class="btn small ghost" data-act="job-cancel" data-key="${j.id}"${dis}>${icon('stop')} Cancel</button>`);
    }
    if (['failed', 'partial', 'cancelled'].includes(j.status)) {
      btns.push(html`<button type="button" class="btn small" data-act="job-retry" data-key="${j.id}"${dis}>${icon('refresh')} Retry</button>`);
    }
    if (['partial', 'cancelled'].includes(j.status)) {
      btns.push(html`<button type="button" class="btn small ghost" data-act="job-release" data-key="${j.id}"${dis}
        title="Move whatever was downloaded into the inbox">${icon('send')} Send to inbox anyway</button>`);
    }
    if (j.status === 'skipped') {
      btns.push(html`<button type="button" class="btn small" data-act="job-force" data-key="${j.id}"${dis}
        title="Ignore the download history and rip it again">${icon('download')} Download again</button>`);
    }
    if (['failed', 'partial', 'cancelled', 'skipped'].includes(j.status)) {
      btns.push(html`<button type="button" class="btn small ghost danger-text" data-act="job-remove" data-key="${j.id}"${dis}>${icon('trash')} Remove</button>`);
    }
    const hasOut = ['failed', 'partial', 'cancelled'].includes(j.status) && j.tail && j.tail.length;
    const outId = 'out-' + j.id;

    const tags = [html`<span class="tag">${j.kind}</span>`];
    if (opts.force) tags.push(html`<span class="tag">forced</span>`);
    if (j.kind === 'playlist' && opts.playlist_albums) tags.push(html`<span class="tag">full albums</span>`);
    if (exp && j.status === 'queued') tags.push(html`<span class="muted">${plural(exp, 'track')}</span>`);
    const title = j.title || '(untitled)';

    return html`
      <li class="job s-${j.status}" data-job="${j.id}" data-menu="job:${j.id}">
        <div class="job-art">${artHTML(j.artwork, 'art', 'disc')}${running ? html`<span class="job-live" aria-hidden="true"></span>` : ''}</div>
        <div class="job-main">
          <div class="job-top">
            <div class="job-titles">
              ${target ? html`<a class="job-title" href="${target}" title="${title}">${title}</a>` : html`<div class="job-title" title="${title}">${title}</div>`}
              ${j.subtitle ? html`<div class="job-sub" title="${j.subtitle}">${j.subtitle}</div>` : ''}
            </div>
            <span class="st t-${tone}">${running ? html`<span class="pulse-dot" aria-hidden="true"></span>` : ''}${label}</span>
            ${moreBtn(title, 'job-more')}
          </div>
          ${progress}
          ${showErr ? html`<p class="job-err ${errCls}">${errCls ? '' : icon('warn')}${j.error}</p>` : ''}
          ${stats.length ? html`<div class="stats">${stats}</div>` : ''}
          ${running ? qualityHTML(p.lossy, p.unavailable) : qualityHTML(j.lossy, j.unavailable)}
          <div class="job-meta">
            ${tags}
            <span class="sep" aria-hidden="true">·</span>
            ${timeTag(j.updated || j.created)}
            ${url ? html`<a class="meta-link" href="${url}" target="_blank" rel="noopener noreferrer" aria-label="Open in Apple Music">${icon('external')}</a>` : ''}
          </div>
          ${btns.length ? html`<div class="job-actions">${btns}</div>` : ''}
          ${hasOut ? html`
            <button type="button" class="disclosure" data-act="toggle" data-key="${outKey}" aria-expanded="${outOpen}" aria-controls="${outId}">
              ${icon('chevron', 'chev')} Output <span class="muted">(${plural(j.tail.length, 'line')})</span></button>
            ${outOpen ? html`<pre class="out" id="${outId}" tabindex="0">${j.tail.join('\n')}</pre>` : ''}` : ''}
        </div>
      </li>`;
  }

  function jobMenu(j) {
    const url = safeUrl(j.url);
    const target = jobTarget(j);
    const s = j.status;
    return [
      target && { label: `Go to ${j.kind}`, icon: 'open', run: () => go(target) },
      j.kind === 'song' && j.options && j.options.album_id &&
        { label: 'Go to album', icon: 'disc', run: () => go(viewHash('album', j.options.album_id)) },
      { sep: true },
      (s === 'queued' || s === 'downloading') && { label: 'Cancel', icon: 'stop', run: () => jobAction('cancel', j.id) },
      ['failed', 'partial', 'cancelled'].includes(s) && { label: 'Retry', icon: 'refresh', run: () => jobAction('retry', j.id) },
      s === 'skipped' && { label: 'Download again', icon: 'download', run: () => jobAction('force', j.id) },
      ['partial', 'cancelled'].includes(s) && { label: 'Send to inbox anyway', icon: 'send', run: () => jobAction('release', j.id) },
      ['failed', 'partial', 'cancelled', 'skipped'].includes(s) && { label: 'Remove', icon: 'trash', danger: true, run: () => jobAction('remove', j.id) },
      { sep: true },
      url && { label: 'Open in Apple Music', icon: 'external', href: url },
      url && { label: 'Copy link', icon: 'link', run: () => copyText(j.url, 'Link') },
    ];
  }

  async function jobAction(act, id) {
    const j = S.jobs.find((x) => x.id === id);
    const name = j ? [j.title, j.subtitle].filter(Boolean).join(' — ') : 'this job';
    if (act === 'remove') {
      const ok = await confirmDialog({
        title: 'Remove this job?',
        body: `This deletes anything already downloaded for “${name}” that hasn't reached the inbox, and forgets those tracks in the download history. It can't be undone.`,
        confirm: 'Remove',
      });
      if (!ok) return;
    }
    const e = enc(id);
    const calls = {
      cancel: [`api/jobs/${e}/cancel`, 'POST', undefined, 'Cancelling…'],
      retry: [`api/jobs/${e}/retry`, 'POST', { force: false }, 'Queued again:'],
      force: [`api/jobs/${e}/retry`, 'POST', { force: true }, 'Downloading again:'],
      release: [`api/jobs/${e}/release`, 'POST', undefined, 'Sent to the inbox:'],
      remove: [`api/jobs/${e}`, 'DELETE', undefined, 'Removed:'],
    };
    const call = calls[act];
    if (!call) return;
    const key = 'job:' + id;
    S.pending.set(key, Infinity);
    renderJobs();
    try {
      await api(call[0], { method: call[1], body: call[2] });
      S.pending.set(key, Date.now());
      toast(`${call[3]} ${name}`.trim(), act === 'remove' || act === 'cancel' ? 'info' : 'success');
    } catch {
      S.pending.delete(key);
    }
    renderJobs();
    jobsPoller.kick();
    statusPoller.kick();
  }

  async function clearJobs() {
    try {
      await api('api/jobs/clear', { method: 'POST' });
      toast('Cleared finished jobs.', 'success');
    } catch { /* toasted */ }
    jobsPoller.kick();
  }

  /* ============================================================ 7. Inbox */

  const INBOX_STATUS = {
    pending: ['Applying', 'info'],
    waiting: ['Settling', 'neutral'],
    queued: ['Queued', 'neutral'],
    tagging: ['Tagging', 'info'],
    review: ['Needs review', 'accent'],
    duplicate: ['Duplicate', 'warn'],
    error: ['Error', 'bad'],
    applied: ['Imported', 'good'],
  };
  // What a queued/tagging item is waiting for: a lookup never imports.
  const BUSY_LABELS = {
    queued: { candidates: 'Lookup queued', apply: 'Apply queued', asis: 'Import queued' },
    tagging: { candidates: 'Looking up', apply: 'Importing', asis: 'Importing' },
  };
  const statusOf = (it) => {
    const [label, tone] = INBOX_STATUS[it.status] || [it.status, 'neutral'];
    const busy = BUSY_LABELS[it.status];
    if (it.status === 'pending' && it.pending && it.pending.mode === 'archive') return ['Archiving', tone];
    return [(busy && busy[it.queued_mode]) || label, tone];
  };
  const DUP_KEYS = { keep: ['b', 'Keep both'], remove: ['x', 'Replace old'], merge: ['g', 'Merge'] };

  const findItem = (path) => S.inbox.items.find((i) => i.path === path);
  const itemName = (it) => it.album || (it.relpath || '').split('/').pop() || it.relpath || it.path;
  const itemLabel = (it) => `${it.artist ? `${it.artist} – ` : ''}${itemName(it)}`;
  const isBusy = (it) => !!it && (it.status === 'queued' || it.status === 'tagging' || it.status === 'pending'
    || S.pending.has('inbox:' + it.path));
  const simTone = (sim) => (sim == null ? 'neutral' : sim >= 0.96 ? 'good' : sim >= 0.75 ? 'warn' : 'bad');
  const pctText = (sim) => `${(clamp(sim || 0, 0, 1) * 100).toFixed(1).replace(/\.0$/, '')}%`;
  const srcClass = (src) => (src === 'MusicBrainz' ? 'mb' : src === 'Spotify' ? 'sp' : 'other');

  async function fetchInbox() {
    const started = Date.now();
    const data = await api('api/inbox', { quiet: true });
    const items = (data && data.items) || [];
    if (data && data.undo_seconds != null) S.tri.undoSeconds = data.undo_seconds;
    for (const it of items) {
      const ov = S.tri.overrides.get(it.path);
      if (!ov) continue;
      if (ov.doneAt < started) S.tri.overrides.delete(it.path);
      else Object.assign(it, ov.fields);
    }
    const first = !S.inbox.loaded;
    for (const it of items) {
      if (it.status !== 'pending' || !it.pending || !it.pending.auto) continue;
      const k = `${it.path}|${it.pending_until}`;
      if (S.tri.seenAuto.has(k)) continue;
      S.tri.seenAuto.add(k);
      if (!first && S.route.tab === 'inbox') {
        const c = candidateById(it, it.pending.candidate);
        toast(`Auto-applying ${itemLabel(it)}${c ? ` → ${c.artist} – ${c.title}` : ''}`, 'info', null, {
          until: it.pending_until,
          action: { label: 'Undo', run: () => triUndo(it.path) },
        });
      }
    }
    S.tri.undoStack = S.tri.undoStack.filter((e) => e.until > nowSec() - 2);
    S.inbox.items = items;
    S.inbox.loaded = true;
    clearPending('inbox:', started);
    renderInbox();
  }

  function triGroups() {
    const items = S.inbox.items;
    return {
      pending: items.filter((i) => i.status === 'pending'),
      needs: items.filter((i) => NEEDS.has(i.status)),
      progress: items.filter((i) => PROGRESS.has(i.status)),
      applied: items.filter((i) => i.status === 'applied'),
    };
  }

  const navList = (g) => [...g.needs, ...g.progress, ...(S.tri.appliedOpen ? g.applied : [])];

  function ensureSelection(g) {
    const nav = navList(g);
    const cur = findItem(S.tri.sel);
    if (cur && (nav.includes(cur) || cur.status === 'pending')) {
      const i = nav.indexOf(cur);
      if (i >= 0) S.tri.selIndex = i;
      return;
    }
    // The selected item left: take the one that slid into its place, but
    // only among items that still need a decision (otherwise: inbox zero).
    const pool = g.needs.length ? nav : [];
    const next = pool[Math.min(S.tri.selIndex, pool.length - 1)] || null;
    S.tri.sel = next && NEEDS.has(next.status) ? next.path : (g.needs[0] ? g.needs[0].path : null);
  }

  function renderInbox() {
    updateBadge();
    if (S.route.tab !== 'inbox') return;
    const g = triGroups();
    renderInboxHeader(g);
    if (!S.inbox.loaded) {
      $('#tri-empty').hidden = false;
      setHTML($('#tri-empty'), html`<div class="empty"><span class="spinner" aria-hidden="true"></span><p>Loading the inbox…</p></div>`);
      return;
    }
    ensureSelection(g);
    renderQueue(g);
    renderTriDetail(g);
    renderHintBar();
    syncSheet();
  }

  function renderInboxHeader(g) {
    const parts = [
      ['needs', g.needs.length, 'needs review', 'accent'],
      ['pending', g.pending.length, 'applying', 'info'],
      ['progress', g.progress.length, 'in progress', 'neutral'],
      ['applied', g.applied.length, 'imported', 'good'],
    ].filter((p) => p[1]);
    const dupCount = S.inbox.items.filter((i) => i.status === 'duplicate').length;
    setHTML($('#inbox-counts'), html`${parts.map(([, n, l, t]) => html`<span class="count-chip t-${t}"><b>${n}</b> ${l}</span>`)}${dupCount ? html`
      <button type="button" class="btn small" data-act="archive-dups" title="Move every album that's already in your library to ${(S.status && S.status.archive_dir) || 'the archive'} (undoable)">${icon('archive')} Archive ${dupCount} duplicate${dupCount === 1 ? '' : 's'}</button>` : ''}`);
    const st = S.status;
    setHTML($('#inbox-mode'), !st ? '' : st.auto_apply
      ? html`<span class="mode on">${icon('check')} Auto-apply on</span>
          <span>Strong matches apply themselves after a ${S.tri.undoSeconds}s undo window.</span>`
      : html`<span class="mode off">Auto-apply off</span>
          <span>Every album waits here for you. Applies can be undone for ${S.tri.undoSeconds}s.</span>`);
  }

  // --- queue

  const optId = (path) => 'opt-' + hashId(path);

  function miniRing(sim) {
    if (sim == null) return html`<span class="mini-ring none" role="img" aria-label="No match">–</span>`;
    return html`<span class="mini-ring t-${simTone(sim)}" style="--p:${(sim * 100).toFixed(1)}" role="img" aria-label="${pctText(sim)} match"><span>${Math.round(sim * 100)}</span></span>`;
  }

  function pendBar(it) {
    const left = Math.max(0, (it.pending_until || 0) - nowSec());
    const frac = clamp(left / (S.tri.undoSeconds || 10), 0, 1);
    return html`<span class="pend-bar" aria-hidden="true"><span style="--left:${left.toFixed(2)}s;--frac:${frac.toFixed(3)}"></span></span>`;
  }

  function pendingVerb(it) {
    const p = it.pending || {};
    if (p.mode === 'asis') return 'Importing as-is';
    if (p.mode === 'archive') return 'Archiving';
    return p.auto ? 'Auto-applying' : 'Applying';
  }

  const qSig = (it) => [JSON.stringify(it), it.path === S.tri.sel, S.tri.skipped.has(it.path),
    S.pending.has('inbox:' + it.path)].join('|');

  function qRowHTML(it) {
    const sel = it.path === S.tri.sel;
    const [label, tone] = statusOf(it);
    const pend = it.status === 'pending';
    const skipped = S.tri.skipped.has(it.path);
    const sub = [
      it.track_count != null ? plural(it.track_count, 'track') : null,
      it.singleton ? 'as tracks' : null,
      it.lossy_count ? `${it.lossy_count} lossy` : null,
      skipped ? 'skipped' : null,
    ].filter(Boolean).join(' · ');
    const working = it.status === 'queued' || it.status === 'tagging';
    return html`
      <li class="q-row s-${it.status}${sel ? ' selected' : ''}${skipped ? ' skipped' : ''}" role="option" id="${optId(it.path)}"
          aria-selected="${sel}" data-act="tri-select" data-key="${it.path}" data-menu="inbox:${it.path}">
        ${miniRing(it.best_similarity)}
        <span class="q-main">
          <span class="q-title">${it.artist ? html`<span class="q-artist">${it.artist}</span> – ` : ''}${itemName(it)}</span>
          <span class="q-sub">${pend ? html`${pendingVerb(it)} in <span data-secs="${it.pending_until}">${secsLeft(it.pending_until)}</span>` : sub}</span>
        </span>
        ${pend
          ? html`<button type="button" class="btn tiny q-undo" data-act="tri-undo" data-key="${it.path}" aria-label="Undo ${pendingVerb(it).toLowerCase()} ${itemLabel(it)}">${icon('undo')} Undo</button>`
          : html`<span class="st t-${tone}">${working ? html`<span class="spinner tiny" aria-hidden="true"></span>` : ''}${label}</span>`}
        ${pend ? pendBar(it) : ''}
      </li>`;
  }

  function renderQueue(g) {
    const groups = [['pending', g.pending], ['needs', g.needs], ['progress', g.progress], ['applied', g.applied]];
    for (const [name, list] of groups) {
      const wrap = $(`#qg-${name}`);
      wrap.hidden = !list.length;
      $('.q-count', wrap).textContent = list.length ? String(list.length) : '';
      const shown = name === 'applied' && !S.tri.appliedOpen ? [] : list;
      reconcile($(`#ql-${name}`), shown, (i) => i.path, qSig, qRowHTML);
    }
    $('#qh-applied').setAttribute('aria-expanded', String(S.tri.appliedOpen));
    const queue = $('#tri-queue');
    if (S.tri.sel) queue.setAttribute('aria-activedescendant', optId(S.tri.sel));
    else queue.removeAttribute('aria-activedescendant');

    const zero = $('#q-zero');
    const nothingToDo = !g.needs.length && !g.pending.length;
    zero.hidden = !nothingToDo;
    if (nothingToDo) {
      setHTML(zero, html`<div class="zero-mini">${icon('check')}<span><strong>Inbox zero.</strong> Nothing needs a decision.</span></div>`);
    }
  }

  // --- selection + navigation

  function selectItem(path, { openSheet = false, focusQueue = false } = {}) {
    const g = triGroups();
    const nav = navList(g);
    const it = findItem(path);
    if (!it) return;
    S.tri.sel = path;
    const i = nav.indexOf(it);
    if (i >= 0) S.tri.selIndex = i;
    renderInbox();
    const opt = document.getElementById(optId(path));
    if (opt) opt.scrollIntoView({ block: 'nearest' });
    if (focusQueue) $('#tri-queue').focus({ preventScroll: true });
    if (openSheet && isPhone() && !S.route.sheet) go('#inbox/item');
    const pane = $('#tri-pane');
    if (isPhone()) {
      pane.scrollTop = 0;
    } else {
      const top = pane.getBoundingClientRect().top;
      const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-h')) || 60;
      if (top < offset) window.scrollTo({ top: window.scrollY + top - offset - 12 });
    }
  }

  function moveSel(delta) {
    const nav = navList(triGroups());
    if (!nav.length) return;
    const cur = findItem(S.tri.sel);
    let i = cur ? nav.indexOf(cur) : -1;
    if (i < 0) i = delta > 0 ? S.tri.selIndex - 1 : S.tri.selIndex + 1;
    const next = nav[clamp(i + delta, 0, nav.length - 1)];
    if (next && next.path !== S.tri.sel) {
      selectItem(next.path);
      announce(`${itemLabel(next)}, ${statusOf(next)[0]}${next.best_similarity != null ? `, best match ${pctText(next.best_similarity)}` : ''}`);
    }
  }

  function syncSheet() {
    const open = S.route.tab === 'inbox' && S.route.sheet && isPhone() && !!S.tri.sel;
    document.body.classList.toggle('tri-sheet-open', open);
  }

  function closeSheet() {
    if (!S.route.sheet) return;
    if (S.navDepth > 0) history.back();
    else go('#inbox');
  }

  // --- current item/candidate

  function candidateById(it, id) {
    if (!id) return null;
    for (const t of it.tasks || []) {
      const c = (t.candidates || []).find((x) => x.id === id);
      if (c) return c;
    }
    return null;
  }

  function triCurrent(path = S.tri.sel) {
    const it = findItem(path);
    if (!it) return {};
    const tasks = it.tasks || [];
    const ti = clamp(S.tri.task.get(it.path) || 0, 0, Math.max(0, tasks.length - 1));
    const task = tasks[ti] || null;
    const cands = (task && task.candidates) || [];
    let ci = S.tri.cand.get(it.path);
    if (ci == null) {
      const want = (it.status === 'pending' && it.pending && it.pending.candidate) || (task && task.chosen);
      ci = Math.max(0, cands.findIndex((c) => c.id === want));
    }
    ci = clamp(ci, 0, Math.max(0, cands.length - 1));
    return { it, tasks, ti, task, cands, ci, c: cands[ci] || null };
  }

  function cycleCand(delta) {
    const { it, cands, ci } = triCurrent();
    if (!it || cands.length < 2) return;
    const n = (ci + delta + cands.length) % cands.length;
    pickCand(n);
  }

  function pickCand(n) {
    const { it, cands } = triCurrent();
    if (!it || !cands[n]) return;
    S.tri.cand.set(it.path, n);
    renderInbox();
    const c = cands[n];
    announce(`Candidate ${n + 1} of ${cands.length}: ${c.artist} – ${c.title}, ${pctText(c.similarity)}, ${c.data_source || ''}`);
  }

  // --- actions

  async function inboxAction(path, body, okMsg) {
    const key = 'inbox:' + path;
    S.pending.set(key, Infinity);
    renderInbox();
    let ok = false;
    try {
      await api('api/inbox/action', { method: 'POST', body: { path, ...body } });
      S.pending.set(key, Date.now());
      toast(okMsg, 'success');
      announce(okMsg);
      ok = true;
    } catch {
      S.pending.delete(key);
    }
    renderInbox();
    inboxPoller.kick();
    statusPoller.kick();
    return ok;
  }

  function advanceFrom(path) {
    const g = triGroups();
    const nav = navList(g);
    const it = findItem(path);
    const i = nav.indexOf(it);
    const rest = nav.filter((x) => x !== it && NEEDS.has(x.status));
    const next = rest[clamp(i < 0 ? S.tri.selIndex : i, 0, rest.length - 1)] || null;
    S.tri.sel = next ? next.path : null;
    S.tri.selIndex = Math.max(0, i);
  }

  /** Apply / as-is: optimistic, undoable for the server's undo window. */
  async function scheduleAction(it, body, label) {
    const path = it.path;
    const snapshot = { status: it.status, pending: it.pending || null, pending_until: it.pending_until || null };
    const fields = {
      status: 'pending',
      pending: { mode: body.action, candidate: body.candidate || null, duplicate_action: body.duplicate_action },
      pending_until: nowSec() + S.tri.undoSeconds,
    };
    const ov = { fields, doneAt: Infinity };
    S.tri.overrides.set(path, ov);
    if (S.tri.sel === path) advanceFrom(path);
    Object.assign(it, fields);
    S.tri.skipped.delete(path);
    renderInbox();
    const verb = { asis: 'Importing as-is', archive: 'Archiving' }[body.action] || 'Applying';
    try {
      const r = await api('api/inbox/action', { method: 'POST', body: { path, ...body } });
      const until = (r && r.pending_until) || fields.pending_until;
      fields.pending_until = until;
      it.pending_until = until;
      ov.doneAt = Date.now();
      const entry = { path, label, snapshot, until, toast: null };
      entry.toast = toast(`${verb}: ${label}`, 'success', null, {
        until,
        action: { label: 'Undo', kbd: UNDO_KBD, run: () => triUndo(path) },
      });
      S.tri.undoStack.push(entry);
      announce(`${verb} ${label}. Press ${UNDO_SPOKEN} within ${Math.round(until - nowSec())} seconds to undo.`);
      renderInbox();
      if (!S.tri.sel && S.route.sheet) closeSheet();
    } catch (e) {
      S.tri.overrides.delete(path);
      Object.assign(it, snapshot);
      S.tri.sel = path;
      renderInbox();
    }
  }

  async function triUndo(path) {
    let entry = null;
    if (!path) {
      while (S.tri.undoStack.length) {
        const e = S.tri.undoStack[S.tri.undoStack.length - 1];
        const it = findItem(e.path);
        if (it && it.status === 'pending') { entry = e; break; }
        S.tri.undoStack.pop();
      }
      if (entry) path = entry.path;
      else {
        const pend = S.inbox.items.filter((i) => i.status === 'pending')
          .sort((a, b) => (b.pending_until || 0) - (a.pending_until || 0));
        if (!pend.length) { toast('Nothing to undo.', 'info', 2500); return; }
        path = pend[0].path;
      }
    } else {
      entry = S.tri.undoStack.find((e) => e.path === path) || null;
    }
    const it = findItem(path);
    if (!it || it.status !== 'pending') {
      toast('Nothing to undo — it has already been applied.', 'warn');
      return;
    }
    const prev = { status: it.status, pending: it.pending, pending_until: it.pending_until };
    const hasDup = (it.tasks || []).some((t) => (t.duplicates || []).length && t.duplicate_action === 'skip');
    const restore = entry && entry.snapshot.status !== 'pending' ? entry.snapshot.status : hasDup ? 'duplicate' : 'review';
    const ov = { fields: { status: restore, pending: null, pending_until: null }, doneAt: Infinity };
    S.tri.overrides.set(path, ov);
    Object.assign(it, ov.fields);
    S.tri.sel = path;
    renderInbox();
    const label = entry ? entry.label : itemLabel(it);
    try {
      await api('api/inbox/undo', { method: 'POST', body: { path }, quiet: true });
      ov.doneAt = Date.now();
      S.tri.undoStack = S.tri.undoStack.filter((e) => e.path !== path);
      if (entry && entry.toast) entry.toast.close();
      toast(`Undone: ${label}`, 'info', 3000);
      announce(`Undone. ${label} is back in the queue.`);
      selectItem(path);
    } catch (e) {
      S.tri.overrides.delete(path);
      Object.assign(it, prev);
      renderInbox();
      toast(e.status === 409 ? 'Too late to undo — it has already been applied.' : e.message, e.status === 409 ? 'warn' : 'error');
    }
  }

  function triApply(dupAction) {
    const { it, task, c } = triCurrent();
    if (!it) return;
    if (isBusy(it)) { toast(it.status === 'pending' ? 'Already applying — undo it first to choose again.' : 'Busy — wait for the tagger.', 'warn', 3000); return; }
    if (!['review', 'duplicate', 'error'].includes(it.status)) { toast('Nothing to apply for this item yet.', 'info', 2500); return; }
    if (it.status === 'duplicate' && !dupAction) {
      toast('Already in your library — press B to keep both, X to replace the old copy or G to merge.', 'warn', 6000);
      return;
    }
    const asisDecision = task && task.decision === 'asis' && !task.chosen;
    if (dupAction && (asisDecision || !c)) {
      scheduleAction(it, { action: 'asis', duplicate_action: dupAction }, `${itemLabel(it)} (as-is, ${DUP_KEYS[dupAction][1].toLowerCase()})`);
      return;
    }
    if (!c) { toast('No candidate to apply — use as-is (U) or enter an ID (I).', 'warn', 4000); return; }
    const body = { action: 'apply', candidate: c.id, duplicate_action: dupAction || 'skip' };
    const target = `${c.artist} – ${c.title}`;
    const label = `${itemLabel(it)}${target !== itemLabel(it) ? ` → ${target}` : ''} (${pctText(c.similarity)}${dupAction ? `, ${DUP_KEYS[dupAction][1].toLowerCase()}` : ''})`;
    scheduleAction(it, body, label);
  }

  function triAsis(path = S.tri.sel) {
    const it = findItem(path);
    if (!it) return;
    if (isBusy(it)) { toast('Busy — wait for the tagger.', 'warn', 3000); return; }
    if (it.status === 'applied') return;
    scheduleAction(it, { action: 'asis', duplicate_action: 'skip' }, `${itemLabel(it)} (as-is)`);
  }

  function triArchive(path = S.tri.sel) {
    const it = findItem(path);
    if (!it) return;
    if (isBusy(it)) { toast('Busy — wait for the tagger.', 'warn', 3000); return; }
    if (it.status === 'applied') return;
    scheduleAction(it, { action: 'archive' }, `${itemLabel(it)} → archive`);
  }

  async function archiveDuplicates() {
    const dups = S.inbox.items.filter((i) => i.status === 'duplicate');
    if (!dups.length) return;
    try {
      const r = await api('api/inbox/archive-duplicates', { method: 'POST', body: {} });
      if (!r.count) return;
      const label = `${r.count} duplicate${r.count === 1 ? '' : 's'}`;
      // The server undoes the whole batch through any one of its paths.
      const entry = { path: r.paths[0], label, snapshot: { status: 'duplicate' }, until: r.pending_until, toast: null };
      entry.toast = toast(`Archiving ${label}`, 'success', null, {
        until: r.pending_until,
        action: { label: 'Undo', kbd: UNDO_KBD, run: () => triUndo(entry.path) },
      });
      S.tri.undoStack.push(entry);
      announce(`Archiving ${label}. Press ${UNDO_SPOKEN} to undo.`);
      inboxPoller.kick();
    } catch { /* toast shown by api() */ }
  }

  // --- the release on Apple Music (from the files' Apple Music id tags)

  const APPLE = new Map(); // path -> { loading, data, error }

  function searchFor(query) {
    const q = $('#q');
    q.value = query;
    S.search.q = query;
    $('#q-clear').hidden = !query;
    go('#download');
    runSearch();
  }

  async function triFindApple(path = S.tri.sel) {
    const it = findItem(path);
    if (!it) return;
    APPLE.set(path, { loading: true });
    renderInbox();
    try {
      const data = await api(`api/inbox/apple?path=${encodeURIComponent(path)}`);
      if (!data.album) {
        APPLE.delete(path);
        renderInbox();
        toast(data.song_ids && data.song_ids.length
          ? 'Couldn’t find that album on Apple Music — searching instead.'
          : 'These files have no Apple Music ids — searching instead.', 'info', 3500);
        searchFor(data.query || itemLabel(it));
        return;
      }
      APPLE.set(path, { data });
    } catch (e) {
      APPLE.set(path, { error: (e && e.message) || 'Lookup failed' });
    }
    renderInbox();
  }

  async function triRedownload(path, which) {
    try {
      const r = await api('api/inbox/redownload', { method: 'POST', body: { path, which } });
      toast(which === 'missing'
        ? `Downloading ${plural(r.tracks, 'missing song')} — it lands back in this folder.`
        : `Re-downloading ${plural(r.tracks, 'track')} — the new copies replace these files.`, 'success', 5000,
      { action: { label: 'Show job', run: () => go('#download') } });
      jobsPoller.kick();
    } catch { /* toast shown by api() */ }
  }

  function appleHTML(it) {
    const st = APPLE.get(it.path);
    if (!st) return '';
    if (st.loading) return html`<div class="apple-panel"><span class="spinner tiny" aria-hidden="true"></span><p>Looking it up on Apple Music…</p></div>`;
    if (st.error) return html`<div class="apple-panel warn">${icon('warn')}<p>${st.error}</p></div>`;
    const d = st.data;
    const a = d.album;
    const here = d.tracks - (d.missing ? d.missing.length : 0);
    const missing = d.missing || [];
    return html`
      <div class="apple-panel" role="region" aria-label="On Apple Music">
        ${artHTML(a.artwork, 'apple-art', 'disc')}
        <div class="apple-info">
          <p class="apple-title"><strong>${a.name}</strong> — ${a.artist}${a.year ? html` <span class="muted">${a.year}</span>` : ''}</p>
          <p class="muted small">${d.missing == null
            ? `${plural(d.files, 'file')} here, ${plural(d.tracks, 'song')} on Apple Music (some files have no song id, so missing songs can't be worked out)`
            : missing.length
              ? html`${here} of ${plural(d.tracks, 'song')} here · missing: ${missing.slice(0, 4).map((t) => t.name).join(', ')}${missing.length > 4 ? ` and ${missing.length - 4} more` : ''}`
              : `All ${plural(d.tracks, 'song')} are here`}</p>
          ${d.videos || d.unavailable ? html`<p class="muted small">${[
            d.videos ? `Also on the release: ${plural(d.videos, 'music video')}, which Needledrop doesn't download.` : '',
            d.unavailable ? `${plural(d.unavailable, 'song')} can't be streamed in your country.` : '',
          ].filter(Boolean).join(' ')}</p>` : ''}
          <div class="apple-btns">
            ${missing.length ? html`<button type="button" class="btn small primary" data-act="tri-redl" data-key="missing">${icon('download')} Download ${plural(missing.length, 'missing song')}</button>` : ''}
            <button type="button" class="btn small" data-act="tri-redl" data-key="album">${icon('refresh')} Re-download album</button>
            <a class="btn small ghost" href="${viewHash('album', a.id)}">${icon('open')} Open album</a>
          </div>
        </div>
        <button type="button" class="icon-btn" data-act="tri-apple-close" aria-label="Close">${icon('x')}</button>
      </div>`;
  }

  function triSkip() {
    const it = findItem(S.tri.sel);
    if (!it) return;
    S.tri.skipped.add(it.path);
    const nav = navList(triGroups());
    const i = nav.indexOf(it);
    const next = nav[i + 1];
    if (next) selectItem(next.path);
    else renderInbox();
    announce(`Skipped ${itemLabel(it)}`);
  }

  function triToggleTracks(path = S.tri.sel) {
    const it = findItem(path);
    if (!it || isBusy(it) || it.status === 'applied') return;
    const on = !it.singleton;
    inboxAction(it.path, { action: on ? 'singletons' : 'albums' },
      on ? `Looking up ${itemLabel(it)} as single tracks…` : `Looking up ${itemLabel(it)} as an album…`);
  }

  function triRetag(path = S.tri.sel) {
    const it = findItem(path);
    if (!it || isBusy(it) || it.status === 'applied') return;
    inboxAction(it.path, { action: 'retag' }, `Re-running the tagger on ${itemLabel(it)}…`);
  }

  function triOpenLink() {
    const { c } = triCurrent();
    const url = c && safeUrl(c.url);
    if (url) window.open(url, '_blank', 'noopener');
    else toast('This candidate has no link.', 'info', 2500);
  }

  function inboxMenu(it) {
    const busy = isBusy(it);
    const done = it.status === 'applied';
    return [
      it.status === 'pending' && { label: 'Undo', icon: 'undo', hint: UNDO_KBD, run: () => triUndo(it.path) },
      { label: it.status === 'waiting' ? 'Tag now' : 'Re-run tagger', icon: 'refresh', disabled: busy || done, run: () => triRetag(it.path) },
      { label: 'Import as-is', icon: 'download', disabled: busy || done, run: () => triAsis(it.path) },
      { label: it.singleton ? 'Look up as an album' : 'Import as single tracks', icon: 'list', disabled: busy || done, run: () => triToggleTracks(it.path) },
      { label: 'Find on Apple Music', icon: 'search', run: () => { selectItem(it.path); triFindApple(it.path); } },
      { label: 'Archive', icon: 'archive', disabled: busy || done, run: () => triArchive(it.path) },
      { sep: true },
      { label: 'Copy folder path', icon: 'copy', run: () => copyText(it.path, 'Folder path') },
    ];
  }

  // --- token diff (LCS over words, whitespace and punctuation; case-sensitive)

  const TOKEN_RE = /\s+|[\p{L}\p{N}\p{M}'’]+|[^\s\p{L}\p{N}\p{M}]/gu;
  const tokens = (s) => String(s).match(TOKEN_RE) || [];

  function diffTokens(a, b) {
    const A = tokens(a);
    const B = tokens(b);
    const n = A.length;
    const m = B.length;
    if (n * m > 60000) return null;
    const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { ops.push(['=', A[i]]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push(['-', A[i]]); i++; }
      else { ops.push(['+', B[j]]); j++; }
    }
    while (i < n) ops.push(['-', A[i++]]);
    while (j < m) ops.push(['+', B[j++]]);
    // Whitespace kept between two changes reads better as part of the change.
    for (let k = 1; k < ops.length - 1; k++) {
      if (ops[k][0] === '=' && /^\s+$/.test(ops[k][1]) && ops[k - 1][0] !== '=' && ops[k + 1][0] !== '=') {
        ops.splice(k, 1, ['-', ops[k][1]], ['+', ops[k][1]]);
        k++;
      }
    }
    return ops;
  }

  /** [leftHTML, rightHTML]: removed runs struck in red, added runs underlined in green. */
  function diffPair(a, b) {
    const ops = diffTokens(a, b);
    if (!ops) return [html`<del>${a}</del>`, html`<ins>${b}</ins>`];
    const left = [];
    const right = [];
    let k = 0;
    while (k < ops.length) {
      const t = ops[k][0];
      let text = '';
      while (k < ops.length && ops[k][0] === t) text += ops[k++][1];
      if (t === '=') { left.push(text); right.push(text); }
      else if (t === '-') left.push(html`<del>${text}</del>`);
      else right.push(html`<ins>${text}</ins>`);
    }
    return [left, right];
  }

  const blank = (v) => v == null || v === '' || v === 0;

  function fieldRow(label, a, b) {
    const A = blank(a) ? null : String(a);
    const B = blank(b) ? null : String(b);
    if (A === B) {
      return html`<div class="drow same"><span class="dmark" aria-hidden="true"></span><span class="dlabel">${label}</span><span class="dval">${A ?? '—'}</span></div>`;
    }
    let left;
    let right;
    if (A != null && B != null) [left, right] = diffPair(A, B);
    else {
      left = A != null ? html`<del>${A}</del>` : html`<span class="none">—</span>`;
      right = B != null ? html`<ins>${B}</ins>` : html`<span class="none">—</span>`;
    }
    return html`<div class="drow"><span class="dmark" aria-hidden="true">≠</span><span class="dlabel">${label}</span>
      <span class="dleft"><span class="sr-only">changes from </span>${left}</span><span class="darrow" aria-hidden="true">→</span>
      <span class="dright"><span class="sr-only"> to </span>${right}</span></div>`;
  }

  function trackRow(tr, c, cur) {
    const multi = (c.mediums || 1) > 1;
    const lnum = tr.item_track != null && tr.item_track !== 0
      ? `#${multi && tr.item_disc ? `${tr.item_disc}-` : ''}${tr.item_track}` : '#?';
    const ridx = tr.medium_index ?? tr.index;
    const rnum = `#${multi && tr.medium ? `${tr.medium}-` : ''}${ridx ?? '?'}`;
    const numChanged = lnum !== rnum;
    const oldTitle = tr.item_title || '';
    const newTitle = tr.title || '';
    const titleChanged = oldTitle !== newTitle;
    const dl = tr.item_length != null && tr.length != null ? tr.length - tr.item_length : null;
    const lenFlag = dl != null && Math.abs(dl) > 10;
    const showArtist = (tr.artist && tr.artist !== c.artist) || (tr.item_artist && cur.artist && tr.item_artist !== cur.artist);
    const artistChanged = !!showArtist && (tr.item_artist || '') !== (tr.artist || '');
    const changed = numChanged || titleChanged || lenFlag || artistChanged;
    const [lt, rt] = titleChanged ? diffPair(oldTitle || '—', newTitle || '—') : [oldTitle, newTitle];
    const [la, ra] = artistChanged ? diffPair(tr.item_artist || '—', tr.artist || '—') : [tr.item_artist, tr.artist];
    const lnumH = numChanged ? html`<del>${lnum}</del>` : lnum;
    const rnumH = numChanged ? html`<ins>${rnum}</ins>` : rnum;
    const delta = lenFlag ? html` <span class="lendelta" title="Length differs by more than 10 seconds">${dl > 0 ? '+' : '−'}${fmtDur(Math.abs(dl))}</span>` : '';
    return {
      changed,
      html: html`
        <li class="trow${changed ? '' : ' same'}${lenFlag ? ' lenflag' : ''}">
          <span class="dmark" aria-hidden="true">${changed ? '≠' : ''}</span>
          <span class="tleft" title="${tr.item || ''}"><span class="tnum">(${lnumH})</span> <span class="ttitle">${oldTitle ? lt : html`<em class="tfile">${tr.item || 'untitled file'}</em>`}</span>${showArtist && tr.item_artist ? html`<span class="tart"> · ${la}</span>` : ''} <span class="tlen">(${fmtDur(tr.item_length) || '?:??'})</span></span>
          <span class="darrow" aria-hidden="true">→</span>
          <span class="tright"><span class="sr-only">becomes </span><span class="tnum">(${rnumH})</span> <span class="ttitle">${rt}</span>${showArtist && tr.artist ? html`<span class="tart"> · ${ra}</span>` : ''} <span class="tlen${lenFlag ? ' flag' : ''}">(${fmtDur(tr.length) || '?:??'})${delta}</span></span>
        </li>`,
    };
  }

  /** Point at the best candidate that would take every file, if any. */
  function coversAll(task, c) {
    const cands = (task && task.candidates) || [];
    const i = cands.findIndex((x) => x !== c && x.kind === 'album' && !x.extra_items);
    if (i < 0) return '';
    const x = cands[i];
    return html`<p class="diff-hint">${icon('info')}<span>Candidate ${i + 1}, <strong>${x.artist} – ${x.title}</strong>${x.disambig ? html` <span class="muted">(${x.disambig.split(', ').filter((p) => p && p !== 'None').slice(1, 4).join(', ')})</span>` : ''} at ${pctText(x.similarity)}, includes every file${x.missing_tracks ? ` (${plural(x.missing_tracks, 'track')} you don't have)` : ''}. ${i < 9 ? html`Press <kbd>${i + 1}</kbd> to see it.` : ''}</span></p>`;
  }

  function diffHTML(it, task, c) {
    const cur = task.current || {};
    const isAlbum = c.kind === 'album';
    const fields = [fieldRow('Artist', cur.artist, c.artist)];
    if (isAlbum) fields.push(fieldRow('Album', cur.album, c.title));
    else {
      fields.push(fieldRow('Title', cur.title, c.title));
      if (c.album) fields.push(html`<div class="drow same"><span class="dmark"></span><span class="dlabel">Album</span><span class="dval">${c.album}</span></div>`);
    }
    if (!blank(cur.year) && !blank(c.year)) fields.push(fieldRow('Year', cur.year, c.year));
    else if (!blank(c.year)) fields.push(html`<div class="drow same"><span class="dmark"></span><span class="dlabel">Year</span><span class="dval">${c.year}</span></div>`);

    const tracks = c.tracks || [];
    const rows = tracks.map((tr) => trackRow(tr, c, cur));
    const changed = rows.filter((r) => r.changed).length;
    const same = rows.length - changed;
    const show = S.tri.showUnchanged;
    const extra = c.extra_items_list || [];
    const missing = c.missing_tracks_list || [];
    return html`
      <div class="diff">
        <div class="diff-fields">${fields}</div>
        ${rows.length ? html`
          <div class="diff-sub">
            <h4>${isAlbum ? 'Tracks' : 'Track'}</h4>
            <span class="muted">${isAlbum ? `${c.matched} of ${c.track_count} matched · ` : ''}${changed ? `${changed} changed` : 'no changes'}</span>
            ${same && changed ? html`<button type="button" class="disclosure" data-act="tri-unchanged" aria-pressed="${show}">${show ? `Hide ${same} unchanged` : `Show ${same} unchanged`}</button>` : ''}
          </div>
          <ol class="dtracks">${rows.filter((r) => r.changed || show || !changed).map((r) => r.html)}</ol>` : ''}
        ${extra.length ? html`
          <div class="diff-sub warnish"><h4>${icon('warn')} Not on this release</h4><span class="muted">${plural(extra.length, 'file')} — applying imports the rest; ${extra.length === 1 ? 'this one stays' : 'these stay'} in the inbox as a separate item</span></div>
          <ul class="dlist">${extra.map((x) => html`<li><span class="tnum">(#${x.track || '?'})</span> ${x.title || html`<em>untitled</em>`} <span class="tlen">(${fmtDur(x.length) || '?:??'})</span> <span class="tfile mono">${x.file}</span></li>`)}</ul>
          ${coversAll(task, c)}` : ''}
        ${missing.length ? html`
          <div class="diff-sub warnish"><h4>${icon('warn')} Missing from your files</h4><span class="muted">${plural(missing.length, 'track')} on this release</span></div>
          <ul class="dlist">${missing.map((x) => html`<li><span class="tnum">(#${x.index ?? '?'})</span> ${x.title} <span class="tlen">(${fmtDur(x.length) || '?:??'})</span></li>`)}</ul>` : ''}
      </div>`;
  }

  // --- detail

  function triSig(it) {
    const cur = triCurrent(it.path);
    const open = [...S.open].filter((k) => k.includes(it.path)).sort().join(',');
    const st = S.status || {};
    return [JSON.stringify(it), cur.ti, cur.ci, S.tri.showUnchanged, open, S.pending.has('inbox:' + it.path),
      S.tri.skipped.has(it.path), S.jobArt.get(it.job_id) || '', st.buffer_seconds, isPhone(),
      JSON.stringify(APPLE.get(it.path) || null)].join('|');
  }

  function renderTriDetail(g) {
    const host = $('#tri-detail');
    const it = findItem(S.tri.sel);
    const empty = $('#tri-empty');
    if (!it) {
      reconcile(host, [], () => '', () => '', () => '');
      empty.hidden = false;
      const zero = !g.needs.length;
      setHTML(empty, zero
        ? html`<div class="inbox-zero">
            <svg class="zero-mark" viewBox="0 0 40 40" aria-hidden="true">
              <circle cx="18" cy="22" r="16" class="disc-body"/><circle cx="18" cy="22" r="12.5" class="disc-groove"/>
              <circle cx="18" cy="22" r="9.5" class="disc-groove"/><circle cx="18" cy="22" r="5.4" class="disc-label"/>
              <circle cx="18" cy="22" r="1.1" class="disc-hole"/><path d="M36 3.5 33.5 16 25.5 25" class="tonearm"/>
              <circle cx="36" cy="3.8" r="2.6" class="tonearm-pivot"/></svg>
            <h2>Inbox zero</h2>
            <p>Every rip has been filed.${g.pending.length ? ` ${plural(g.pending.length, 'item')} still applying.` : ''}${g.progress.length ? ` ${plural(g.progress.length, 'item')} being tagged.` : ''}${g.applied.length ? ` ${plural(g.applied.length, 'album')} imported today.` : ''}</p>
            <a class="btn" href="#download">${icon('download')} Find more music</a>
          </div>`
        : html`<div class="empty"><p>Select an item from the queue.</p></div>`);
      return;
    }
    empty.hidden = true;
    const enter = S.tri.lastKey !== it.path;
    S.tri.lastKey = it.path;
    reconcile(host, [it], (i) => i.path, triSig, triDetailHTML);
    if (enter && host.firstElementChild) {
      host.firstElementChild.classList.add('enter');
    }
  }

  function triDetailHTML(it) {
    const { tasks, ti, task, cands, ci, c } = triCurrent(it.path);
    const busy = isBusy(it);
    const [label, tone] = statusOf(it);
    const meta = [it.artist, it.track_count != null ? plural(it.track_count, 'track') : null, it.singleton ? 'as single tracks' : null,
      it.lossy_count ? plural(it.lossy_count, 'lossy file') : null].filter(Boolean);
    const showMatch = task && ['review', 'duplicate', 'pending', 'error'].includes(it.status);
    return html`
      <article class="tri-card s-${it.status}" data-path="${it.path}" data-menu="inbox:${it.path}" aria-labelledby="tri-title">
        <header class="tri-head">
          <button type="button" class="icon-btn tri-close" data-act="tri-close" aria-label="Back to the queue">${icon('back')}</button>
          ${artHTML(S.jobArt.get(it.job_id), 'tri-art', 'disc')}
          <div class="tri-titles">
            <h2 id="tri-title" tabindex="-1">${itemName(it)}</h2>
            ${meta.length ? html`<p class="tri-meta">${meta.join(' · ')}</p>` : ''}
            <p class="tri-path mono" title="${it.path}">${it.relpath || it.path}</p>
          </div>
          <div class="tri-state">
            <span class="st t-${tone}">${it.status === 'queued' || it.status === 'tagging' ? html`<span class="spinner tiny" aria-hidden="true"></span>` : ''}${label}</span>
            ${timeTag(it.last_change, 'muted small')}
            ${moreBtn(itemName(it), 'inline')}
          </div>
        </header>
        ${pendingBanner(it)}
        ${appleHTML(it)}
        ${statusBody(it)}
        ${it.auto_undone && it.status !== 'pending' ? html`<p class="tri-note">${icon('info')} Auto-apply was undone for this item, so it waits for you even though the match is strong.</p>` : ''}
        ${tasks.length > 1 ? html`<div class="task-tabs" role="group" aria-label="Tasks">${tasks.map((t, i) => html`
          <button type="button" class="chip" data-act="tri-task" data-idx="${i}" aria-pressed="${i === ti}">${t.kind === 'singleton' ? 'Track' : 'Album'} ${i + 1}${t.current && (t.current.title || t.current.album) ? html`<span class="muted"> · ${t.current.title || t.current.album}</span>` : ''}</button>`)}</div>` : ''}
        ${showMatch && it.status === 'duplicate' ? dupPanelHTML(it, task, busy) : ''}
        ${showMatch ? (c ? matchHTML(it, task, c, ci, cands) : html`<div class="tri-msg">${icon('search')}<p>No candidates found. Enter a MusicBrainz or Spotify ID (<kbd>I</kbd>), look it up as single tracks (<kbd>T</kbd>), or use the files as-is (<kbd>U</kbd>).</p></div>`) : ''}
        ${task && task.error && it.status !== 'error' ? html`<p class="tri-note warn">${icon('warn')} ${task.error}</p>` : ''}
        ${it.status === 'applied' ? '' : actionBarHTML(it, c, busy)}
      </article>`;
  }

  function pendingBanner(it) {
    if (it.status !== 'pending') return '';
    const c = it.pending && it.pending.candidate ? candidateById(it, it.pending.candidate) : null;
    const dup = it.pending && it.pending.duplicate_action && it.pending.duplicate_action !== 'skip' && DUP_KEYS[it.pending.duplicate_action];
    return html`
      <div class="pend-banner" role="status">
        <span class="spinner tiny" aria-hidden="true"></span>
        <p><strong>${pendingVerb(it)}</strong>${c ? html` ${c.artist} – ${c.title} (${pctText(c.similarity)})` : ''}${dup ? ` · ${dup[1].toLowerCase()}` : ''} in <span class="secs" data-secs="${it.pending_until}">${secsLeft(it.pending_until)}</span></p>
        <button type="button" class="btn small" data-act="tri-undo" data-key="${it.path}">${icon('undo')} Undo <kbd>${UNDO_KBD}</kbd></button>
        ${pendBar(it)}
      </div>`;
  }

  function statusBody(it) {
    switch (it.status) {
      case 'waiting': {
        const buf = S.status ? S.status.buffer_seconds : null;
        const due = buf != null ? it.last_change + buf : null;
        return html`<div class="tri-msg"><span class="settle" aria-hidden="true"><i></i><i></i><i></i></span>
          <p>Waiting for the files to settle${due != null ? html` — tagging in <strong data-countdown="${due}">${fmtCountdown(due - nowSec())}</strong>` : '…'}. Press <kbd>R</kbd> to tag now.</p></div>`;
      }
      case 'queued':
      case 'tagging':
        return html`<div class="tri-msg"><span class="spinner" aria-hidden="true"></span>
          <p>${it.status === 'tagging' ? 'beets is looking for matches…' : 'Queued for the tagger…'}</p></div>`;
      case 'error': {
        const t0 = (it.tasks || [])[0];
        const msg = it.error || it.report_error || (t0 && t0.error) || 'beets failed without saying why.';
        const k = 'err:' + it.path;
        const open = S.open.has(k);
        const id = hashId(k);
        return html`<div class="tri-err">
          <p class="err-msg">${icon('warn')}<span>${msg}</span></p>
          ${it.stderr ? html`<button type="button" class="disclosure" data-act="toggle" data-key="${k}" aria-expanded="${open}" aria-controls="${id}">${icon('chevron', 'chev')} beets output</button>
            ${open ? html`<pre class="out" id="${id}" tabindex="0">${it.stderr}</pre>` : ''}` : ''}
          <p class="muted small">Press <kbd>R</kbd> to run the tagger again.</p>
        </div>`;
      }
      case 'applied': {
        const imp = it.imported || [];
        const chosen = (it.tasks || []).map((t) => t.chosen && (t.candidates || []).find((x) => x.id === t.chosen)).find(Boolean);
        return html`<div class="tri-applied">
          <p class="applied-how">${icon('check')}<span>${it.mode === 'asis' ? 'Imported as-is (existing tags kept).'
            : chosen ? html`Matched <b>${chosen.artist} – ${chosen.title}</b> <span class="src s-${srcClass(chosen.data_source)}">${chosen.data_source}</span> ${pctText(chosen.similarity)}${it.mode === 'auto' ? ' · auto-applied' : ''}`
              : 'Imported into the library.'}</span></p>
          ${imp.length ? html`<ul class="imp-list">${imp.slice(0, 6).map((x) => {
            const paths = x.paths || [];
            return html`<li><span class="dup-sum">${x.summary}</span>${paths[0] ? html`<span class="mono path" title="${paths.join('\n')}">${paths[0]}${paths.length > 1 ? ` (+${paths.length - 1} more)` : ''}</span>` : ''}</li>`;
          })}</ul>` : ''}
        </div>`;
      }
      default:
        return '';
    }
  }

  function dupPanelHTML(it, task, busy) {
    const dups = (it.tasks || []).flatMap((t) => t.duplicates || []);
    return html`
      <div class="dup">
        <div class="dup-head">${icon('warn')}<div><strong>Already in your library</strong>
          <p>Applying would create a duplicate. Choose what happens to the copy you already have — still undoable for ${S.tri.undoSeconds}s.</p></div></div>
        ${dups.length ? html`<ul class="dup-list">${dups.map((d) => html`<li><span class="dup-sum">${d.summary}</span><span class="mono path" title="${d.path}">${d.path}</span></li>`)}</ul>` : ''}
        <div class="dup-row">
          ${Object.entries(DUP_KEYS).map(([action, [key, label]]) => html`
            <button type="button" class="btn${action === 'keep' ? ' primary' : ''}" data-act="tri-dup" data-key="${action}"${flag(busy, 'disabled')}>${label} <kbd>${key.toUpperCase()}</kbd></button>`)}
          <button type="button" class="btn" data-act="tri-archive"${flag(busy, 'disabled')}>${icon('archive')} Archive this copy <kbd>D</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-skip">Leave it <kbd>S</kbd></button>
        </div>
      </div>`;
  }

  function matchHTML(it, task, c, ci, cands) {
    const sim = clamp(c.similarity || 0, 0, 1);
    const tone = simTone(sim);
    const src = c.data_source || 'Unknown';
    const url = safeUrl(c.url);
    const pens = [...(c.penalties || [])].sort((a, b) => (b.weight || 0) - (a.weight || 0));
    const chosen = task.chosen && task.chosen === c.id;
    return html`
      <section class="match" aria-label="Candidate ${ci + 1} of ${cands.length}">
        ${cands.length > 1 ? html`
          <div class="cand-nav">
            <button type="button" class="icon-btn" data-act="tri-cand" data-dir="-1" aria-label="Previous candidate">${icon('left')}</button>
            <ol class="cand-pills" aria-label="Candidates">${cands.map((x, i) => html`
              <li><button type="button" class="cand-pill t-${simTone(x.similarity)}${i === ci ? ' on' : ''}" data-act="tri-pick" data-idx="${i}" aria-pressed="${i === ci}"
                title="${x.artist} – ${x.title} (${x.data_source || '?'})"><span class="n">${i + 1}</span>${pctText(x.similarity)}</button></li>`)}</ol>
            <button type="button" class="icon-btn" data-act="tri-cand" data-dir="1" aria-label="Next candidate">${icon('right')}</button>
          </div>` : ''}
        <div class="match-head">
          <div class="ring big t-${tone}" style="--p:${(sim * 100).toFixed(1)}" role="img" aria-label="${pctText(sim)} similar">
            <span>${pctText(sim).replace('%', '')}<small>%</small></span>
          </div>
          <div class="match-titles">
            <p class="match-kicker">Match ${ci + 1}/${cands.length}
              <span class="src s-${srcClass(src)}">${src}</span>
              ${task.recommendation ? html`<span class="rec r-${task.recommendation}" title="beets' confidence in its best match">${task.recommendation}</span>` : ''}
              ${chosen ? html`<span class="tag t-accent">chosen</span>` : ''}</p>
            <h3 class="match-name">${c.artist} – ${c.title}${c.year ? html` <span class="year">${c.year}</span>` : ''}</h3>
            ${pens.length ? html`<p class="match-pens"><span class="neq" aria-label="Differences:">≠</span> ${pens.map((p, i) => html`${i ? ', ' : ''}<span title="penalty weight ${p.weight}">${String(p.name).replace(/_/g, ' ')}</span>`)}</p>`
              : html`<p class="match-pens ok">${icon('check')} no penalties</p>`}
            ${c.disambig ? html`<p class="match-disambig">${c.disambig}</p>` : ''}
            ${url ? html`<a class="match-link mono" href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>` : ''}
          </div>
        </div>
        ${src === 'Spotify' ? html`<p class="tri-note">${icon('info')} Spotify matches carry a source penalty, so they never auto-apply — that's expected.</p>` : ''}
        ${diffHTML(it, task, c)}
      </section>`;
  }

  // --- MusicBrainz / Spotify ID hint

  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function idSource(v) {
    v = String(v || '').trim();
    if (!v) return null;
    if (/musicbrainz\.org\/(release|recording)\/[0-9a-f-]{36}/i.test(v) || UUID_RE.test(v)) return 'mb';
    if (/musicbrainz\.org/i.test(v)) return 'mb-bad';
    if (/open\.spotify\.com\/(intl-[a-z]+\/)?(album|track)\/[A-Za-z0-9]+/i.test(v)
      || /^spotify:(album|track):[A-Za-z0-9]+$/i.test(v) || /^[A-Za-z0-9]{22}$/.test(v)) return 'sp';
    if (/spotify/i.test(v)) return 'sp-bad';
    return 'unknown';
  }

  const SRC_HINT = {
    mb: ['MusicBrainz', 'ok'],
    sp: ['Spotify', 'ok'],
    'mb-bad': ['Needs a release link', 'warn'],
    'sp-bad': ['Needs an album/track link', 'warn'],
    unknown: ['Unrecognised', 'warn'],
  };
  const srcHint = (v) => { const s = idSource(v); return s ? SRC_HINT[s] : ['', '']; };

  function actionBarHTML(it, c, busy) {
    const dis = flag(busy, 'disabled');
    const decidable = ['review', 'duplicate', 'error'].includes(it.status);
    const v = S.inputs.get('sid:' + it.path) || '';
    const [hint, hintTone] = srcHint(v);
    const url = c && safeUrl(c.url);
    return html`
      <div class="tri-actions" role="toolbar" aria-label="Decide">
        <div class="tri-btns">
          <button type="button" class="btn primary" data-act="tri-apply"${flag(busy || !c || !decidable || it.status === 'duplicate', 'disabled')}>${icon('check')} Apply <kbd>A</kbd></button>
          <button type="button" class="btn" data-act="tri-asis"${dis}>Use as-is <kbd>U</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-skip">${icon('skip')} Skip <kbd>S</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-archive"${dis}>${icon('archive')} Archive <kbd>D</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-tracks"${dis}>${it.singleton ? 'As album' : 'As tracks'} <kbd>T</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-apple">${icon('search')} On Apple Music <kbd>F</kbd></button>
          <button type="button" class="btn ghost" data-act="tri-retag"${dis}>${icon('refresh')} ${it.status === 'waiting' ? 'Tag now' : 'Re-run'} <kbd>R</kbd></button>
          ${url ? html`<a class="btn ghost" href="${url}" target="_blank" rel="noopener noreferrer">${icon('external')} Open <kbd>O</kbd></a>` : ''}
        </div>
        <form class="idsearch" data-submit="inbox-search" data-key="${it.path}">
          <label for="tri-id" class="sr-only">Look up by MusicBrainz or Spotify ID or link</label>
          <div class="idsearch-field">
            ${icon('search')}
            <input id="tri-id" type="text" data-input="sid:${it.path}" value="${v}"
              placeholder="Enter MusicBrainz / Spotify ID or link" autocomplete="off" spellcheck="false" autocapitalize="off"${dis}>
            <span class="src-hint ${hintTone}" data-hint>${hint}</span>
            <kbd class="id-kbd" aria-hidden="true">I</kbd>
          </div>
          <button type="submit" class="btn small"${dis}>Look up</button>
        </form>
      </div>`;
  }

  function renderHintBar() {
    const it = findItem(S.tri.sel);
    const dup = it && it.status === 'duplicate';
    const k = (key, before, after = '') => html`<span class="hk">${before}<b>${key}</b>${after}</span>`;
    const parts = dup
      ? [k('D', 'Archive (', ')'), k('b', 'Keep ', 'oth'), k('X', 'Replace old (', ')'), k('g', 'Mer', 'e'), k('M', '', 'ore candidates'), k('S', '', 'kip'), k('U', '', 'se as-is'), k('I', 'Enter ', 'd')]
      : [k('A', '', 'pply'), k('D', 'Archive (', ')'), k('M', '', 'ore candidates'), k('S', '', 'kip'), k('U', '', 'se as-is'), k('T', 'as ', 'racks'), k('I', 'Enter ', 'd'), k('R', '', 'e-run'), k('O', '', 'pen')];
    setHTML($('#tri-hint-keys'), html`${parts}<span class="hint-sep">·</span><span><kbd>j</kbd><kbd>k</kbd> move</span><span><kbd>${UNDO_KBD}</kbd> undo</span>`);
  }

  function toggleHelp() {
    const d = $('#keys-dialog');
    if (d.open) d.close();
    else {
      $('#keys-undo').textContent = UNDO_KBD;
      d.showModal();
    }
  }

  /**
   * Inbox shortcuts. Every key means one thing in every state: nothing here
   * changes meaning depending on the selected item. Keys handled outside
   * this table (global search, undo, context menu) are listed in
   * GLOBAL_KEYS so the clash check below covers them too.
   */
  const GLOBAL_KEYS = ['/'];
  const dupOnly = (action) => () => {
    const it = findItem(S.tri.sel);
    if (it && it.status === 'duplicate') triApply(action);
    else toast(`${DUP_KEYS[action][1]} only applies to albums already in your library.`, 'info', 2500);
  };
  const TRI_KEYS = [
    { keys: ['j', 'ArrowDown'], run: () => moveSel(1) },
    { keys: ['k', 'ArrowUp'], run: () => moveSel(-1) },
    { keys: ['Home'], run: () => { const nav = navList(triGroups()); if (nav[0]) selectItem(nav[0].path); } },
    { keys: ['End'], run: () => { const nav = navList(triGroups()); if (nav.length) selectItem(nav[nav.length - 1].path); } },
    { keys: ['ArrowRight', ']', 'm'], run: () => cycleCand(1) },
    { keys: ['ArrowLeft', '['], run: () => cycleCand(-1) },
    { keys: ['1', '2', '3', '4', '5', '6', '7', '8', '9'], run: (ctx) => pickCand(Number(ctx.key) - 1) },
    { keys: ['a'], run: () => triApply() },
    { keys: ['Enter'], run: (ctx) => (ctx.onControl ? false : triApply()) },
    { keys: ['u'], run: () => triAsis() },
    { keys: ['s'], run: () => triSkip() },
    { keys: ['t'], run: () => triToggleTracks() },
    { keys: ['r'], run: () => triRetag() },
    { keys: ['o'], run: () => triOpenLink() },
    { keys: ['d'], run: () => triArchive() },
    { keys: ['f'], run: () => triFindApple() },
    { keys: ['i'], run: () => { const inp = $('#tri-id'); if (inp && !inp.disabled) { inp.focus(); inp.select(); } } },
    { keys: [DUP_KEYS.keep[0]], run: dupOnly('keep') },
    { keys: [DUP_KEYS.remove[0]], run: dupOnly('remove') },
    { keys: [DUP_KEYS.merge[0]], run: dupOnly('merge') },
    { keys: ['?'], run: () => toggleHelp() },
    { keys: ['Escape'], run: () => (document.body.classList.contains('tri-sheet-open') ? closeSheet() : false) },
  ];
  const TRI_KEYMAP = new Map();
  for (const binding of TRI_KEYS) {
    for (const key of binding.keys) {
      if (TRI_KEYMAP.has(key) || GLOBAL_KEYS.includes(key)) {
        console.error(`Needledrop: shortcut "${key}" is bound twice`);
        continue;
      }
      TRI_KEYMAP.set(key, binding);
    }
  }

  /** Returns true when the key was handled. */
  function triKey(e) {
    if (S.route.tab !== 'inbox' || e.altKey || e.ctrlKey || e.metaKey) return false;
    if (isTypingTarget(e.target) || Menu.el || anyDialogOpen()) return false;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const binding = TRI_KEYMAP.get(key);
    if (!binding) return false;
    const onControl = !!(e.target && e.target.closest && e.target.closest('button, a, summary, input, select'));
    return binding.run({ key, onControl }) !== false;
  }

  /* ===================================================== 8. Log + history */

  async function fetchLog() {
    const params = new URLSearchParams({ limit: '300' });
    if (S.log.kind) params.set('kind', S.log.kind);
    const kind = S.log.kind;
    const data = await api(`api/events?${params}`, { quiet: true });
    if (kind !== S.log.kind) return;
    S.log.events = (data && data.events) || [];
    S.log.loaded = true;
    renderLog();
    if (++S.log.ticks % 6 === 0) fetchHistory();
  }
  S.log.ticks = 0;

  function setLogKind(kind) {
    S.log.kind = kind;
    store.set('logKind', kind);
    S.log.events = [];
    S.log.loaded = false;
    renderLog();
    logPoller.kick();
  }

  function renderLog() {
    $$('#log-filters [data-kind]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.kind === S.log.kind)));
    if (S.route.tab !== 'log') return;
    const q = S.log.q.trim().toLowerCase();
    const evs = q
      ? S.log.events.filter((e) => `${e.message} ${e.ref || ''} ${e.kind} ${e.level}`.toLowerCase().includes(q))
      : S.log.events;
    let last = null;
    const rows = evs.map((e) => {
      const d = dayLabel(e.ts);
      const head = d !== last ? d : null;
      last = d;
      return { e, head };
    });
    reconcile($('#log-list'), rows, (r) => r.e.id,
      (r) => `${r.e.id}|${r.head}|${S.open.has('ev:' + r.e.id)}`, eventHTML);
    const empty = $('#log-empty');
    if (rows.length) {
      empty.hidden = true;
    } else {
      empty.hidden = false;
      setHTML(empty, !S.log.loaded
        ? html`<span class="spinner" aria-hidden="true"></span><p>Loading events…</p>`
        : html`${icon('log')}<p>${q ? 'No events match that filter.' : 'No events yet.'}</p>`);
    }
  }

  function eventHTML({ e, head }) {
    const k = 'ev:' + e.id;
    const open = S.open.has(k);
    const id = 'evd-' + e.id;
    const hasDetails = e.details != null
      && !(typeof e.details === 'object' && Object.keys(e.details).length === 0);
    const ref = e.ref ? (e.ref.includes('/') ? shortPath(e.ref) : e.ref) : '';
    return html`
      <li class="ev l-${e.level}">
        ${head ? html`<div class="ev-day">${head}</div>` : ''}
        <div class="ev-row">
          <div class="ev-when">
            <time datetime="${isoTime(e.ts)}" data-rel="${e.ts}" title="${absTime(e.ts)}">${rel(e.ts)}</time>
            <span class="ev-clock mono">${clock(e.ts)}</span>
          </div>
          <span class="ev-dot" aria-hidden="true"></span>
          <div class="ev-body">
            <p class="ev-msg"><span class="sr-only">${e.level}: </span>${e.message}</p>
            <div class="ev-meta">
              <span class="tag k-${e.kind}">${e.kind}</span>
              ${e.level !== 'info' ? html`<span class="lvl">${e.level}</span>` : ''}
              ${ref ? html`<span class="mono ev-ref" title="${e.ref}">${ref}</span>` : ''}
              ${hasDetails ? html`<button type="button" class="disclosure" data-act="toggle" data-key="${k}" aria-expanded="${open}" aria-controls="${id}">
                ${icon('chevron', 'chev')} details</button>` : ''}
            </div>
            ${open ? html`<pre class="json" id="${id}" tabindex="0">${JSON.stringify(e.details, null, 2)}</pre>` : ''}
          </div>
        </div>
      </li>`;
  }

  async function fetchHistory() {
    try {
      S.history = await api('api/history', { quiet: true });
    } catch { /* keep the old numbers */ }
    renderHistory();
  }

  const SOURCE_LABEL = (s) => (s === 'needledrop' ? 'ripped by Needledrop'
    : String(s || 'unknown').startsWith('import:') ? `imported from ${String(s).slice(7)}` : String(s || 'unknown'));

  function renderHistory() {
    const box = $('#hist-stats');
    const h = S.history;
    if (!h) {
      setHTML(box, html`<span class="spinner" aria-hidden="true"></span>`);
      return;
    }
    if (!h.tracks) {
      setHTML(box, html`<p class="muted">Nothing downloaded yet. Import an existing history to skip what you already have.</p>`);
      return;
    }
    setHTML(box, html`
      <dl class="stat-tiles">
        <div><dt>Tracks</dt><dd>${fmtN(h.tracks)}</dd></div>
        <div><dt>Albums</dt><dd>${fmtN(h.albums)}</dd></div>
        <div><dt>Latest</dt><dd>${h.latest ? timeTag(h.latest) : '—'}</dd></div>
      </dl>
      ${(h.sources || []).length ? html`<ul class="src-list" aria-label="By source">${h.sources.map((s) => html`<li><b>${fmtN(s.n)}</b> ${SOURCE_LABEL(s.source)}</li>`)}</ul>` : ''}`);
  }

  async function importHistory(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const btn = $('#hist-pick');
    btn.disabled = true;
    const old = btn.innerHTML;
    setHTML(btn, html`<span class="spinner tiny" aria-hidden="true"></span> Importing ${file.name}…`);
    try {
      const r = await api('api/history/import', { method: 'POST', rawBody: file });
      toast(`Imported ${plural(r.added || 0, 'track')} from ${r.format}, ${fmtN(r.already_known || 0)} already known.`, 'success', 8000);
      fetchHistory();
      refreshCatalogViews();
      logPoller.kick();
    } catch { /* toasted with the server's detail */ }
    btn.disabled = false;
    btn.innerHTML = old;
    input.value = '';
  }

  /* ============================================================ pollers */

  const statusPoller = new Poller(fetchStatus, () => (S.status && (S.status.downloading || S.status.tagging) ? 3000 : 5000));
  const jobsPoller = new Poller(fetchJobs, () => {
    if (S.route.tab !== 'download') return 10000;
    return S.jobs.some((j) => ACTIVE_JOB.has(j.status)) ? 2000 : 10000;
  });
  const inboxPoller = new Poller(fetchInbox, () => (S.inbox.items.some((i) => i.status === 'pending') ? 1500 : 3000));
  const logPoller = new Poller(fetchLog, () => 5000);
  const POLLERS = [statusPoller, jobsPoller, inboxPoller, logPoller];

  /* ============================================================ 9. wiring */

  function rerenderCurrent() {
    if (S.route.tab === 'download') renderJobs();
    else if (S.route.tab === 'inbox') renderInbox();
    else if (S.route.tab === 'log') renderLog();
  }

  const ACTIONS = {
    'type': (el) => setType(el.dataset.type),
    'toggle-sel': (el) => toggleSel(el.dataset.key),
    'back': () => goBack(),
    'detail-retry': () => { if (S.detail) loadDetail(S.detail.type, S.detail.id); },
    'select-all': (el) => selectAll(el.dataset.key),
    'sel-clear': () => { S.selection.clear(); syncSelUI(); },
    'sel-download': (el) => downloadSelection(el),
    'dl-now': (el) => {
      const it = S.items.get(el.dataset.key);
      if (it) queueDownload([it], { btn: el, playlistAlbums: el.dataset.pl ? el.dataset.pl === '1' : S.playlistAlbums });
    },
    'forget': (el) => { const it = S.items.get(el.dataset.key); if (it) forgetHistory(it); },
    'search-retry': () => runSearch(),
    'jobs-toggle': () => setJobsCollapsed(!S.jobsCollapsed),
    'jobs-clear': () => clearJobs(),
    'job-cancel': (el) => jobAction('cancel', el.dataset.key),
    'job-retry': (el) => jobAction('retry', el.dataset.key),
    'job-force': (el) => jobAction('force', el.dataset.key),
    'job-release': (el) => jobAction('release', el.dataset.key),
    'job-remove': (el) => jobAction('remove', el.dataset.key),
    'menu': (el) => {
      if (Menu.skipTrigger === el) { Menu.skipTrigger = null; return; }
      const host = el.closest('[data-menu]');
      if (host) openMenuFor(host, { anchor: el }, el);
    },
    'toggle': (el) => {
      const k = el.dataset.key;
      if (S.open.has(k)) S.open.delete(k); else S.open.add(k);
      rerenderCurrent();
      if (k.startsWith('out:') && S.open.has(k)) {
        const pre = document.getElementById('out-' + k.slice(4));
        if (pre) pre.scrollTop = pre.scrollHeight;
      }
    },
    'tri-select': (el) => selectItem(el.dataset.key, { openSheet: true }),
    'tri-undo': (el) => triUndo(el.dataset.key),
    'tri-apply': () => triApply(),
    'tri-asis': () => triAsis(),
    'tri-skip': () => triSkip(),
    'tri-archive': () => triArchive(),
    'tri-apple': () => triFindApple(),
    'tri-apple-close': () => { APPLE.delete(S.tri.sel); renderInbox(); },
    'tri-redl': (el) => triRedownload(S.tri.sel, el.dataset.key),
    'archive-dups': () => archiveDuplicates(),
    'tri-tracks': () => triToggleTracks(),
    'tri-retag': () => triRetag(),
    'tri-dup': (el) => triApply(el.dataset.key),
    'tri-cand': (el) => cycleCand(Number(el.dataset.dir)),
    'tri-pick': (el) => pickCand(Number(el.dataset.idx)),
    'tri-task': (el) => { S.tri.task.set(S.tri.sel, Number(el.dataset.idx)); S.tri.cand.delete(S.tri.sel); renderInbox(); },
    'tri-unchanged': () => { S.tri.showUnchanged = !S.tri.showUnchanged; store.set('showUnchanged', S.tri.showUnchanged); renderInbox(); },
    'tri-applied': () => { S.tri.appliedOpen = !S.tri.appliedOpen; store.set('appliedOpen', S.tri.appliedOpen); renderInbox(); },
    'tri-close': () => closeSheet(),
    'tri-help': () => toggleHelp(),
    'log-kind': (el) => setLogKind(el.dataset.kind),
    'hist-pick': () => $('#hist-file').click(),
    'status-info': () => {
      const pill = $('#account-pill');
      toast(pill.title, pill.classList.contains('t-bad') ? 'error' : 'info', 7000);
    },
  };

  const CHANGES = {
    'pl-albums': (el) => { S.playlistAlbums = el.checked; },
    'hist-import': (el) => importHistory(el),
  };

  function onFieldInput(el) {
    const key = el.dataset.input;
    if (!key) return;
    S.inputs.set(key, el.value);
    if (key.startsWith('sid:')) {
      const hint = el.parentElement.querySelector('[data-hint]');
      if (hint) {
        const [text, tone] = srcHint(el.value);
        hint.textContent = text;
        hint.className = `src-hint ${tone}`;
      }
    }
  }

  function wireMenus() {
    document.addEventListener('contextmenu', (e) => {
      if (Menu.el && Date.now() - Menu.openedAt < 700) { e.preventDefault(); return; }
      const host = e.target.closest && e.target.closest('[data-menu]');
      if (!host) return;
      if (e.target.closest('input, textarea, select, pre, .ctxmenu')) return;
      if (e.shiftKey && e.button === 2) return; // Shift+right-click: the browser's own menu
      e.preventDefault();
      const kbd = e.clientX === 0 && e.clientY === 0;
      const ae = document.activeElement;
      openMenuFor(host, kbd ? { anchor: ae && host.contains(ae) ? ae : host } : { x: e.clientX, y: e.clientY }, ae);
    });

    document.addEventListener('pointerdown', (e) => {
      if (!Menu.el || Menu.el.contains(e.target)) return;
      const trig = e.target.closest && e.target.closest('[data-act="menu"]');
      if (trig && trig === Menu.trigger) Menu.skipTrigger = trig;
      closeMenu(false);
    }, true);

    window.addEventListener('scroll', (e) => {
      if (Menu.el && e.target !== Menu.el && !(Menu.el.contains && e.target instanceof Node && Menu.el.contains(e.target))) closeMenu(false);
    }, { capture: true, passive: true });
    window.addEventListener('resize', () => closeMenu(false));
    window.addEventListener('blur', () => closeMenu(false));

    // Long-press on touch screens opens the same menu.
    let lp = null;
    let suppressClickUntil = 0;
    const cancelLp = () => { if (lp) { clearTimeout(lp.timer); lp = null; } };
    document.addEventListener('pointerdown', (e) => {
      if (e.pointerType !== 'touch') return;
      const host = e.target.closest && e.target.closest('[data-menu]');
      if (!host || e.target.closest('input, textarea, select')) return;
      const x = e.clientX;
      const y = e.clientY;
      lp = {
        x, y,
        timer: setTimeout(() => {
          lp = null;
          if (Menu.el && Date.now() - Menu.openedAt < 700) return;
          suppressClickUntil = Date.now() + 800;
          if (navigator.vibrate) navigator.vibrate(8);
          openMenuFor(host, { x, y }, null);
        }, 520),
      };
    }, { passive: true });
    document.addEventListener('pointermove', (e) => {
      if (lp && Math.hypot(e.clientX - lp.x, e.clientY - lp.y) > 10) cancelLp();
    }, { passive: true });
    document.addEventListener('pointerup', cancelLp, { passive: true });
    document.addEventListener('pointercancel', cancelLp, { passive: true });
    document.addEventListener('click', (e) => {
      if (Date.now() < suppressClickUntil && !(Menu.el && Menu.el.contains(e.target))) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
  }

  function wire() {
    document.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
      const fn = ACTIONS[el.dataset.act];
      if (!fn) return;
      if (el.tagName === 'A') e.preventDefault();
      fn(el, e);
    });

    document.addEventListener('change', (e) => {
      const el = e.target;
      if (el.dataset && el.dataset.change && CHANGES[el.dataset.change]) CHANGES[el.dataset.change](el, e);
      if (el.dataset && el.dataset.input) onFieldInput(el);
    });

    document.addEventListener('input', (e) => {
      const el = e.target;
      if (el.dataset && el.dataset.input) onFieldInput(el);
    });

    document.addEventListener('submit', (e) => {
      const f = e.target;
      if (f.id === 'search-form') {
        e.preventDefault();
        runSearch();
        return;
      }
      if (f.dataset.submit === 'inbox-search') {
        e.preventDefault();
        const input = f.querySelector('input');
        const v = input.value.trim();
        if (!v) {
          toast('Paste a MusicBrainz release ID or a Spotify album ID / link first.', 'warn');
          input.focus();
          return;
        }
        const src = idSource(v);
        const name = src === 'mb' ? 'MusicBrainz' : src === 'sp' ? 'Spotify' : 'ID';
        input.blur();
        inboxAction(f.dataset.key, { action: 'search', search_id: v }, `Looking up the ${name} release…`);
      }
    });

    const q = $('#q');
    q.addEventListener('input', () => {
      S.search.q = q.value;
      $('#q-clear').hidden = !q.value;
      if (S.route.detail && q.value.trim()) go('#download');
      if (!q.value.trim()) {
        debouncedSearch.cancel();
        if (S.search.ctrl) S.search.ctrl.abort();
        S.search.seq++;
        S.search.results = null;
        S.search.error = null;
        S.search.loading = false;
        renderSearchBusy();
        if (!S.route.detail) renderSearch();
        return;
      }
      debouncedSearch();
    });
    $('#q-clear').addEventListener('click', () => {
      q.value = '';
      q.dispatchEvent(new Event('input'));
      q.focus();
    });

    $('#log-q').addEventListener('input', debounce((e) => {
      S.log.q = e.target.value;
      renderLog();
    }, 150));

    document.addEventListener('keydown', (e) => {
      const t = e.target;
      const typing = isTypingTarget(t);

      // Undo: Ctrl/⌘+Z anywhere except while typing.
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'z' && !typing && !anyDialogOpen()) {
        e.preventDefault();
        triUndo();
        return;
      }
      // Context menu from the keyboard.
      if ((e.key === 'F10' && e.shiftKey) || e.key === 'ContextMenu') {
        const ae = document.activeElement;
        let host = ae && ae.closest && ae.closest('[data-menu]');
        if (!host && ae && ae.id === 'tri-queue' && S.tri.sel) host = document.getElementById(optId(S.tri.sel));
        if (host && !Menu.el) {
          e.preventDefault();
          openMenuFor(host, { anchor: ae === document.body || !host.contains(ae) ? host : ae }, ae);
        }
        return;
      }
      // The ID box: Escape hands focus back to the triage view.
      if (typing && e.key === 'Escape' && t.id === 'tri-id') {
        e.preventDefault();
        t.blur();
        $('#tri-queue').focus({ preventScroll: true });
        return;
      }
      if (triKey(e)) { e.preventDefault(); return; }
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey && !anyDialogOpen()) {
        e.preventDefault();
        if (S.route.tab !== 'download' || S.route.detail) go('#download');
        requestAnimationFrame(() => { $('#q').focus(); $('#q').select(); });
      }
    });

    window.addEventListener('hashchange', () => onRoute());
    window.addEventListener('resize', debounce(() => syncSheet(), 150));

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) POLLERS.forEach((p) => { if (p.active) p.kick(); });
    });

    // Keep relative times and countdowns fresh without re-rendering.
    let tick = 0;
    setInterval(() => {
      tick++;
      const t = nowSec();
      $$('[data-countdown]').forEach((el) => { el.textContent = fmtCountdown(Number(el.dataset.countdown) - t); });
      $$('[data-secs]').forEach((el) => { el.textContent = secsLeft(Number(el.dataset.secs)); });
      if (tick % 5 === 0) $$('time[data-rel]').forEach((el) => { el.textContent = rel(Number(el.dataset.rel)); });
    }, 1000);

    wireMenus();
  }

  function init() {
    const topbar = $('.topbar');
    const setTopbarH = () => document.documentElement.style.setProperty('--topbar-h', `${topbar.offsetHeight}px`);
    setTopbarH();
    if ('ResizeObserver' in window) new ResizeObserver(setTopbarH).observe(topbar);

    wire();
    setJobsCollapsed(S.jobsCollapsed);
    renderStatus();
    statusPoller.setActive(true);
    onRoute(true);
    if (S.route.tab === 'download' && !S.route.detail && window.matchMedia('(pointer: fine)').matches) {
      $('#q').focus({ preventScroll: true });
    }
  }

  init();
})();
