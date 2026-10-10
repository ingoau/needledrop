/* Needledrop — frontend. Vanilla JS, no build step, no external resources.
 *
 * Layout of this file:
 *   1. utilities (escaping html templates, time, storage)
 *   2. api helper, toasts, confirm dialog
 *   3. polling + keyed list reconciliation (keeps focus/expanded state)
 *   4. router + top bar status
 *   5. Download tab: search, results, artist view, tracklists, selection, jobs
 *   6. Inbox tab
 *   7. Log tab
 *   8. event wiring + init
 *
 * All API paths are relative so the app also works behind a reverse proxy
 * that mounts it under a sub-path.
 */
'use strict';

(() => {
  /* ================================================================ 1. utils */

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
    if (typeof v === 'boolean') return String(v); // for aria-expanded="${open}" etc.
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

  const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
  const norm = (s) => String(s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

  function splitKey(key) {
    const i = key.indexOf(':');
    return [key.slice(0, i), key.slice(i + 1)];
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

  /* ================================================== 2. api, toasts, dialog */

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
   * error toast unless `quiet` is set (pollers handle failures themselves).
   */
  async function api(path, { method = 'GET', body, signal, quiet = false } = {}) {
    const init = { method, headers: { Accept: 'application/json' }, signal };
    if (body !== undefined) {
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

  function toast(message, kind = 'info', timeout) {
    const key = kind + '|' + message;
    const t = Date.now();
    if ((recentToasts.get(key) || 0) > t - 2500) return;
    recentToasts.set(key, t);
    const box = $('#toasts');
    const ic = { error: 'warn', warn: 'warn', success: 'check' }[kind] || 'info';
    const el = fragment(html`
      <div class="toast t-${kind}" role="${kind === 'error' ? 'alert' : 'status'}">
        ${icon(ic, 'toast-ic')}
        <span class="toast-msg">${message}</span>
        <button type="button" class="icon-btn toast-x" aria-label="Dismiss notification">${icon('x')}</button>
      </div>`);
    let gone = false;
    const close = () => {
      if (gone) return;
      gone = true;
      el.classList.add('out');
      setTimeout(() => el.remove(), 220);
    };
    el.querySelector('.toast-x').addEventListener('click', close);
    box.append(el);
    while (box.children.length > 4) box.firstElementChild.remove();
    setTimeout(close, timeout ?? (kind === 'error' ? 9000 : 4500));
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

  const FOCUS_ATTRS = ['id', 'data-act', 'data-key', 'data-cand', 'data-input', 'data-change', 'data-submit'];

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

  /* ============================================================== state */

  const S = {
    route: { tab: null, artist: null },
    dlView: null,
    status: null,
    offline: false,
    prevDownloading: undefined,

    search: { q: '', type: '', results: null, error: null, loading: false, seq: 0, ctrl: null },
    artist: { id: null, data: null, error: null },
    artistFromApp: false,
    searchScroll: 0,
    cache: { album: new Map(), playlist: new Map(), artist: new Map() },
    items: new Map(),       // "type:id" -> normalized catalog item (for selection payloads)
    sections: new Map(),    // section id -> selectable keys (Select all)
    expanded: null,         // "album:id" | "playlist:id" whose tracklist is open
    selection: new Map(),   // "type:id" -> item
    playlistAlbums: true,

    jobs: [],
    jobsLoaded: false,
    jobArt: new Map(),      // job id -> artwork (used by inbox cards)
    jobsCollapsed: store.get('jobsCollapsed', false),

    inbox: { items: [], loaded: false, filter: store.get('inboxFilter', null) },
    log: { events: [], loaded: false, kind: store.get('logKind', ''), q: '' },

    open: new Set(),        // expanded disclosures, keyed by job id / inbox path / event id
    pending: new Map(),     // "job:id" | "inbox:path" -> ms timestamp the request finished (Infinity while in flight)
    inputs: new Map(),      // typed values inside re-rendered cards
  };

  const ACTIVE_JOB = new Set(['queued', 'downloading', 'finishing']);
  const NEEDS = new Set(['review', 'duplicate', 'error']);

  function clearPending(prefix, startedAt) {
    for (const [k, ts] of S.pending) {
      if (k.startsWith(prefix) && ts < startedAt) S.pending.delete(k);
    }
  }

  /* ========================================================= 4. router/status */

  const TABS = ['download', 'inbox', 'log'];
  const TAB_TITLE = { download: 'Download', inbox: 'Inbox', log: 'Log' };

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/');
    const tab = TABS.includes(parts[0]) ? parts[0] : 'download';
    const artist = tab === 'download' && parts[1] === 'artist' && parts[2]
      ? decodeURIComponent(parts[2]) : null;
    return { tab, artist };
  }

  function onRoute() {
    const r = parseHash();
    const tabChanged = r.tab !== S.route.tab;
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
      const view = r.artist ? `artist:${r.artist}` : 'search';
      if (view !== S.dlView) {
        const wasArtist = S.dlView && S.dlView.startsWith('artist:');
        S.dlView = view;
        if (r.artist) loadArtist(r.artist);
        else {
          renderSearch();
          if (wasArtist) requestAnimationFrame(() => window.scrollTo(0, S.searchScroll));
        }
      }
      renderSelBar();
    } else {
      renderSelBar();
    }
    if (r.tab === 'inbox') renderInbox();
    if (r.tab === 'log') renderLog();
    if (tabChanged && r.tab !== 'download') window.scrollTo(0, 0);
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
    const logSub = $('#log-sub');
    if (logSub && st && st.log_retention_days) {
      logSub.textContent = `The latest 300 events. Entries older than ${st.log_retention_days} days are pruned.`;
    }
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
    updateBadge();

    // A download started or finished somewhere: refresh jobs/inbox promptly.
    const dl = st ? st.downloading || null : undefined;
    if (dl !== S.prevDownloading) {
      if (S.prevDownloading !== undefined) {
        if (jobsPoller.active) jobsPoller.kick();
        if (inboxPoller.active) inboxPoller.kick();
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

  /* ======================================================= 5. Download tab */

  const TYPE_ORDER = ['artist', 'album', 'song', 'playlist'];
  const TYPE_LABEL = { artist: 'Artists', album: 'Albums', song: 'Songs', playlist: 'Playlists' };
  const KIND_NOUN = { song: 'song', album: 'album', playlist: 'playlist' };

  const itemKey = (it) => `${it.type}:${it.id}`;
  const selectable = (it) =>
    !!it && ['song', 'album', 'playlist'].includes(it.type) && !!it.streamable && !!it.url && !!it.id;

  function artHTML(url, cls, ph = 'disc') {
    const u = safeUrl(url);
    return u
      ? html`<img class="${cls}" src="${u}" alt="" loading="lazy" decoding="async">`
      : html`<span class="${cls} art-ph">${icon(ph)}</span>`;
  }

  const explicitTag = (it) => (it.explicit
    ? html`<span class="explicit" title="Explicit" aria-label="Explicit">E</span>` : '');

  // --- search

  const debouncedSearch = debounce(() => runSearch(), 350);

  async function runSearch() {
    debouncedSearch.cancel();
    const s = S.search;
    const q = s.q.trim();
    if (!q) return;
    if (S.route.artist) location.hash = '#download';
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
      s.error = e.message;
    } finally {
      if (seq === s.seq) {
        s.loading = false;
        renderSearchBusy();
        if (!S.route.artist && S.route.tab === 'download') renderSearch();
      }
    }
  }

  function renderSearchBusy() {
    $('#search-spin').hidden = !S.search.loading;
    $('#results').classList.toggle('is-loading', S.search.loading && !!S.search.results);
  }

  function setType(t) {
    S.search.type = t;
    $$('#type-chips [data-type]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.type === t)));
    if (S.search.q.trim()) {
      S.search.results = null;
      runSearch();
      renderSearch();
    }
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
          <li>Tick albums, songs or playlists, then <strong>Download</strong></li>
          <li>Finished rips land in the <a href="#inbox">Inbox</a> for tagging</li>
        </ul>
      </div>`;
  }

  function skeletonHTML(n = 8) {
    return html`<div class="card-grid skeleton" aria-hidden="true">${Array.from({ length: n }, () => html`
      <div class="card"><div class="card-art"><span class="art shimmer"></span></div>
      <div class="card-body"><span class="sk-line shimmer"></span><span class="sk-line short shimmer"></span></div></div>`)}</div>`;
  }

  function setBrowsing(on) {
    $('.dl-layout').classList.toggle('browsing', on);
  }

  function renderSearch() {
    const box = $('#results');
    const s = S.search;
    setBrowsing(!!s.q.trim());
    S.expanded = null;
    S.sections.clear();
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
      announce('No results');
      return;
    }
    const total = groups.reduce((n, t) => n + s.results[t].length, 0);
    setHTML(box, groups.map((t) => groupHTML(t, s.results[t])));
    announce(`${plural(total, 'result')}`);
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
    const meta = [];
    if (it.year) meta.push(it.year);
    if (it.type === 'album' && it.track_count) meta.push(plural(it.track_count, 'track'));
    if (it.type === 'playlist') meta.push('Playlist');
    const tags = [];
    if (it.is_single) tags.push('Single');
    if (it.is_compilation) tags.push('Compilation');
    return html`
      <article class="card selectable${can ? '' : ' unavailable'}" data-item="${key}">
        <div class="card-art">
          ${artHTML(it.artwork, 'art')}
          ${can ? html`<button type="button" class="sel-btn" data-act="toggle-sel" data-key="${key}" aria-pressed="false"
              aria-label="Select ${KIND_NOUN[it.type]} ${it.name || ''}${it.artist ? ` by ${it.artist}` : ''}">
              <span class="sel-check">${icon('check')}</span></button>` : ''}
        </div>
        <div class="card-body">
          ${(it.type === 'album' || it.type === 'playlist') && it.id
            ? html`<button type="button" class="card-title linkish" data-act="expand" data-key="${key}" aria-expanded="false"
                 title="Show tracks">${it.name || 'Untitled'}</button>`
            : html`<span class="card-title">${it.name || 'Untitled'}</span>`}
          <div class="card-sub" title="${it.artist || ''}">${it.artist || ''}</div>
          <div class="card-meta">${meta.join(' · ')} ${explicitTag(it)} ${tags.map((t) => html`<span class="mini-tag">${t}</span>`)}</div>
          ${it.streamable ? '' : html`<div class="note">Not available to stream</div>`}
        </div>
      </article>`;
  }

  function songRowHTML(it, { inAlbum = false, albumArtist = null, multiDisc = false } = {}) {
    const key = itemKey(it);
    S.items.set(key, it);
    const can = selectable(it);
    const sub = inAlbum
      ? (it.artist && it.artist !== albumArtist ? it.artist : '')
      : [it.artist, it.album].filter(Boolean).join(' · ');
    const num = inAlbum
      ? html`<span class="trk-num">${multiDisc && it.disc_number ? `${it.disc_number}.` : ''}${it.track_number || ''}</span>` : '';
    return html`
      <li class="row selectable${can ? '' : ' unavailable'}${inAlbum ? ' trk' : ''}" data-item="${key}">
        ${can ? html`<button type="button" class="row-sel" data-act="toggle-sel" data-key="${key}" aria-pressed="false"
            aria-label="Select song ${it.name || ''}${it.artist ? ` by ${it.artist}` : ''}"><span class="box">${icon('check')}</span></button>`
          : html`<span class="row-sel-ph" aria-hidden="true"></span>`}
        ${inAlbum ? num : artHTML(it.artwork, 'row-art')}
        <div class="row-main">
          <div class="row-title"><span class="row-name">${it.name || 'Untitled'}</span> ${explicitTag(it)}</div>
          ${sub ? html`<div class="row-sub">${sub}</div>` : ''}
          ${it.streamable ? '' : html`<div class="note">Not available to stream</div>`}
        </div>
        <div class="row-end">${!inAlbum && it.year ? html`<span>${it.year}</span>` : ''}<span class="mono">${fmtDur((it.duration_ms || 0) / 1000) || ''}</span></div>
      </li>`;
  }

  function artistCardHTML(it) {
    S.items.set(itemKey(it), it);
    const genres = (it.genres || []).filter((g) => g !== 'Music');
    return html`
      <button type="button" class="artist-card" data-act="open-artist" data-key="${it.id}">
        ${artHTML(it.artwork, 'artist-art', 'mic')}
        <span class="artist-name">${it.name || 'Unknown artist'}</span>
        ${genres.length ? html`<span class="artist-genre">${genres.slice(0, 2).join(', ')}</span>` : ''}
      </button>`;
  }

  // --- artist view

  function openArtist(id) {
    if (!id) return;
    if (!S.route.artist) S.searchScroll = window.scrollY;
    S.artistFromApp = true;
    location.hash = `#download/artist/${encodeURIComponent(id)}`;
  }

  function goBack() {
    if (S.artistFromApp) {
      S.artistFromApp = false;
      history.back();
    } else {
      location.hash = '#download';
    }
  }

  async function loadArtist(id) {
    const cached = S.cache.artist.get(id);
    S.artist = { id, data: cached || null, error: null };
    renderArtist();
    window.scrollTo(0, 0);
    if (cached) return;
    try {
      const data = await api(`api/artist/${encodeURIComponent(id)}`, { quiet: true });
      S.cache.artist.set(id, data);
      if (S.artist.id === id) S.artist.data = data;
    } catch (e) {
      if (S.artist.id === id) S.artist.error = e.message;
    }
    if (S.artist.id === id && S.route.artist === id) renderArtist();
  }

  function renderArtist() {
    const a = S.artist;
    const known = (a.data && a.data.artist) || S.items.get(`artist:${a.id}`) || {};
    const genres = (known.genres || []).filter((g) => g !== 'Music');
    const url = safeUrl(known.url);
    S.expanded = null;
    S.sections.clear();
    setBrowsing(true);
    let body;
    if (a.error) {
      body = html`<div class="empty error">${icon('warn')}<p>${a.error}</p>
        <button type="button" class="btn" data-act="artist-retry">${icon('refresh')} Try again</button></div>`;
    } else if (!a.data) {
      body = skeletonHTML(6);
    } else if (!a.data.sections || !a.data.sections.length) {
      body = html`<div class="empty">${icon('disc')}<p>No releases found for this artist.</p></div>`;
    } else {
      body = a.data.sections.map((sec) => groupHTML(
        (sec.items[0] && sec.items[0].type) || 'album', sec.items,
        { title: sec.title, sectionId: `artist-${a.id}-${sec.key}` },
      ));
    }
    setHTML($('#results'), html`
      <div class="artist-view">
        <div class="artist-head">
          <button type="button" class="btn small ghost back-btn" data-act="back">${icon('back')} Back</button>
          ${artHTML(known.artwork, 'artist-avatar', 'mic')}
          <div class="artist-titles">
            <h2 tabindex="-1" id="artist-h">${known.name || 'Artist'}</h2>
            ${genres.length ? html`<p class="muted">${genres.join(', ')}</p>` : ''}
          </div>
          ${url ? html`<a class="btn small ghost" href="${url}" target="_blank" rel="noopener noreferrer">Apple Music ${icon('external')}</a>` : ''}
        </div>
        ${body}
      </div>`);
    syncSelUI();
  }

  // --- inline tracklists for albums / playlists

  function placeExpand(card, panel) {
    const top = card.offsetTop;
    let last = card;
    for (let n = card.nextElementSibling; n; n = n.nextElementSibling) {
      if (n === panel) continue;
      if (n.offsetTop !== top) break;
      last = n;
    }
    if (last.nextElementSibling !== panel) last.after(panel);
  }

  function closeExpand(focusBack) {
    const key = S.expanded;
    S.expanded = null;
    const panel = $('#expand-panel');
    if (panel) panel.remove();
    $$('.card.expanded').forEach((c) => c.classList.remove('expanded'));
    let btn = null;
    $$('[data-act="expand"][aria-expanded="true"]').forEach((b) => {
      b.setAttribute('aria-expanded', 'false');
      b.removeAttribute('aria-controls');
      if (b.dataset.key === key) btn = b;
    });
    if (focusBack && btn) btn.focus();
  }

  async function toggleExpand(key, btn) {
    if (S.expanded === key && $('#expand-panel')) { closeExpand(true); return; }
    closeExpand(false);
    const card = btn.closest('.card');
    if (!card) return;
    const [type, id] = splitKey(key);
    S.expanded = key;
    const it = S.items.get(key) || {};
    const panel = document.createElement('div');
    panel.className = 'expand';
    panel.id = 'expand-panel';
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', `Tracks of ${it.name || type}`);
    card.classList.add('expanded');
    btn.setAttribute('aria-expanded', 'true');
    btn.setAttribute('aria-controls', 'expand-panel');
    placeExpand(card, panel);
    setHTML(panel, html`<div class="expand-msg"><span class="spinner" aria-hidden="true"></span> Loading tracks…</div>`);

    const cache = S.cache[type];
    let data = cache && cache.get(id);
    if (!data) {
      try {
        data = await api(`api/${type}/${encodeURIComponent(id)}`, { quiet: true });
        cache.set(id, data);
      } catch (e) {
        if (S.expanded === key && panel.isConnected) {
          setHTML(panel, html`<div class="expand-msg error">${icon('warn')} ${e.message}</div>`);
        }
        return;
      }
    }
    if (S.expanded !== key || !panel.isConnected) return;
    setHTML(panel, expandHTML(type, data));
    syncSelUI();
  }

  function expandHTML(type, data) {
    const head = data[type] || data.album || data.playlist || {};
    if (head.id) S.items.set(itemKey(head), head);
    const tracks = (data.tracks || []).filter((t) => t && t.type === 'song');
    const discs = new Set(tracks.map((t) => t.disc_number).filter(Boolean));
    const total = tracks.reduce((n, t) => n + (t.duration_ms || 0), 0) / 1000;
    const url = safeUrl(head.url);
    const label = head.record_label;
    return html`
      <div class="expand-head">
        <div class="expand-titles">
          <strong>${head.name || ''}</strong>
          <span class="muted">${plural(tracks.length, 'track')}${total ? ` · ${fmtDur(total)}` : ''}${label ? ` · ${label}` : ''}</span>
        </div>
        <div class="expand-tools">
          ${selectable(head) ? html`<button type="button" class="btn small" data-act="toggle-sel" data-key="${itemKey(head)}" aria-pressed="false">${icon('check')} Whole ${type}</button>` : ''}
          ${url ? html`<a class="btn small ghost" href="${url}" target="_blank" rel="noopener noreferrer">Apple Music ${icon('external')}</a>` : ''}
          <button type="button" class="icon-btn" data-act="expand-close" aria-label="Close tracklist">${icon('x')}</button>
        </div>
      </div>
      ${tracks.length
        ? html`<ol class="tracklist">${tracks.map((t) => songRowHTML(t, {
          inAlbum: type === 'album', albumArtist: head.artist, multiDisc: discs.size > 1,
        }))}</ol>
        <p class="hint">Tick single tracks to rip just those, or take the whole ${type}.</p>`
        : html`<p class="expand-msg">No tracks listed.</p>`}`;
  }

  // --- selection

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
      b.textContent = keys.length && keys.every((k) => S.selection.has(k)) ? 'Deselect all' : 'Select all';
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
    $('#sel-count').textContent = String(n);
    $('#sel-noun').textContent = 'selected';
    const kinds = {};
    items.forEach((i) => { kinds[i.type] = (kinds[i.type] || 0) + 1; });
    $('#sel-kinds').textContent = ['album', 'song', 'playlist']
      .filter((k) => kinds[k]).map((k) => plural(kinds[k], k)).join(' · ');
    setHTML($('#sel-art'), items.slice(-3).map((it) => artHTML(it.artwork, 'sel-thumb')));
    $('#pl-albums-wrap').hidden = !items.some((i) => i.type === 'playlist');
    $('#pl-albums').checked = S.playlistAlbums;
  }

  async function downloadSelection(btn) {
    const items = [...S.selection.values()].map((it) => ({
      type: it.type,
      id: String(it.id),
      url: it.url,
      name: it.name ?? null,
      artist: it.artist ?? null,
      album: it.type === 'song' ? (it.album ?? null) : it.type === 'album' ? (it.name ?? null) : null,
      artwork: it.artwork ?? null,
      track_count: Number.isInteger(it.track_count) ? it.track_count : null,
    }));
    if (!items.length) return;
    btn.disabled = true;
    try {
      const res = await api('api/jobs', {
        method: 'POST',
        body: { items, playlist_albums: S.playlistAlbums, force: false },
      });
      const n = (res && res.created && res.created.length) || items.length;
      toast(`Queued ${plural(n, 'download')}.`, 'success');
      S.selection.clear();
      syncSelUI();
      if (S.jobsCollapsed && window.matchMedia('(max-width: 959px)').matches) setJobsCollapsed(false);
      jobsPoller.kick();
      statusPoller.kick();
    } catch { /* toast already shown */ }
    btn.disabled = false;
  }

  // --- jobs

  const JOB_STATUS = {
    queued: ['Queued', 'neutral'],
    downloading: ['Downloading', 'info'],
    finishing: ['Moving to inbox', 'info'],
    done: ['Done', 'good'],
    skipped: ['Already ripped', 'muted'],
    partial: ['Incomplete', 'warn'],
    failed: ['Failed', 'bad'],
    cancelled: ['Cancelled', 'muted'],
  };

  const SUMMARY_LABELS = [
    ['flac_converted', 'FLAC'],
    ['aac_retained', 'AAC kept'],
    ['history_skipped', 'in history'],
    ['skipped', 'skipped'],
    ['failed', 'failed'],
    ['finalization_failed', 'finalize failed'],
  ];

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

  function jobHTML(j) {
    const [label, tone] = JOB_STATUS[j.status] || [j.status, 'neutral'];
    const running = j.status === 'downloading' || j.status === 'finishing';
    const p = (running && j.progress) || {};
    const done = running && p.done != null ? p.done : (j.done_tracks || 0);
    const exp = j.expected_tracks;
    const busy = S.pending.has('job:' + j.id);
    const dis = flag(busy, 'disabled');
    const outKey = 'out:' + j.id;
    const outOpen = S.open.has(outKey);
    const url = safeUrl(j.url);
    const opts = j.options || {};

    // progress
    let progress = '';
    if (running) {
      const pct = exp ? Math.min(100, (done / exp) * 100) : null;
      const bits = [exp ? `${done} / ${exp} tracks` : plural(done, 'track')];
      if (p.albums) bits.push(plural(p.albums, 'album'));
      if (p.skipped) bits.push(`${p.skipped} skipped`);
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

    // summary from the rip script's run_summary
    const sum = j.summary || {};
    const stats = running ? [] : SUMMARY_LABELS.filter(([k]) => sum[k]).map(([k, l]) => html`<span class="stat">${sum[k]} ${l}</span>`);

    const showErr = j.error && !(j.status === 'cancelled' && j.error === 'Cancelled');
    const errCls = j.status === 'skipped' || (j.status === 'done') ? 'note' : '';

    // actions
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

    return html`
      <li class="job s-${j.status}" data-job="${j.id}">
        <div class="job-art">${artHTML(j.artwork, 'art', 'disc')}${running ? html`<span class="job-live" aria-hidden="true"></span>` : ''}</div>
        <div class="job-main">
          <div class="job-top">
            <div class="job-titles">
              <div class="job-title" title="${j.title || ''}">${j.title || '(untitled)'}</div>
              ${j.subtitle ? html`<div class="job-sub" title="${j.subtitle}">${j.subtitle}</div>` : ''}
            </div>
            <span class="st t-${tone}">${running ? html`<span class="pulse-dot" aria-hidden="true"></span>` : ''}${label}</span>
          </div>
          ${progress}
          ${showErr ? html`<p class="job-err ${errCls}">${errCls ? '' : icon('warn')}${j.error}</p>` : ''}
          ${stats.length ? html`<div class="stats">${stats}</div>` : ''}
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

  async function jobAction(act, id) {
    const j = S.jobs.find((x) => x.id === id);
    const name = j ? [j.title, j.subtitle].filter(Boolean).join(' — ') : 'this job';
    if (act === 'remove') {
      const ok = await confirmDialog({
        title: 'Remove this job?',
        body: `This deletes anything already downloaded for “${name}” that hasn't reached the inbox. It can't be undone.`,
        confirm: 'Remove',
      });
      if (!ok) return;
    }
    const enc = encodeURIComponent(id);
    const calls = {
      cancel: [`api/jobs/${enc}/cancel`, 'POST', undefined, 'Cancelling…'],
      retry: [`api/jobs/${enc}/retry`, 'POST', { force: false }, 'Queued again.'],
      force: [`api/jobs/${enc}/retry`, 'POST', { force: true }, 'Queued to download again.'],
      release: [`api/jobs/${enc}/release`, 'POST', undefined, 'Sent to the inbox.'],
      remove: [`api/jobs/${enc}`, 'DELETE', undefined, 'Removed.'],
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

  /* =========================================================== 6. Inbox tab */

  const INBOX_STATUS = {
    waiting: ['Settling', 'neutral'],
    queued: ['Queued', 'neutral'],
    tagging: ['Tagging', 'info'],
    review: ['Needs review', 'accent'],
    duplicate: ['Duplicate', 'warn'],
    error: ['Error', 'bad'],
    applied: ['Imported', 'good'],
  };
  const STATUS_ORDER = ['review', 'duplicate', 'error', 'tagging', 'queued', 'waiting', 'applied'];
  const FILTERS = {
    needs: (s) => NEEDS.has(s),
    progress: (s) => s === 'waiting' || s === 'queued' || s === 'tagging',
    applied: (s) => s === 'applied',
    all: () => true,
  };
  const DUP_ACTIONS = [
    ['keep', 'Keep both'],
    ['remove', 'Replace old'],
    ['merge', 'Merge into existing album'],
    ['skip', 'Skip (leave it here)'],
  ];
  const CAND_LIMIT = 4;

  async function fetchInbox() {
    const started = Date.now();
    const data = await api('api/inbox', { quiet: true });
    S.inbox.items = (data && data.items) || [];
    S.inbox.loaded = true;
    clearPending('inbox:', started);
    renderInbox();
  }

  const findItem = (path) => S.inbox.items.find((i) => i.path === path);

  function renderInbox() {
    updateBadge();
    if (S.route.tab !== 'inbox') return;
    const items = S.inbox.items;
    const counts = {};
    items.forEach((i) => { counts[i.status] = (counts[i.status] || 0) + 1; });

    setHTML($('#inbox-counts'), STATUS_ORDER.filter((s) => counts[s]).map((s) => {
      const [label, tone] = INBOX_STATUS[s];
      return html`<span class="count-chip t-${tone}"><b>${counts[s]}</b> ${label.toLowerCase()}</span>`;
    }));

    const st = S.status;
    setHTML($('#inbox-mode'), !st ? '' : st.auto_apply
      ? html`<span class="mode on">${icon('check')} Auto-apply on</span>
          <span>Strong matches import themselves once files have been quiet for ${plural(st.buffer_seconds, 'second')}.</span>`
      : html`<span class="mode off">Auto-apply off</span>
          <span>Everything waits here for you to pick a match (after a ${st.buffer_seconds}s settle time).</span>`);

    if (!S.inbox.loaded) {
      setHTML($('#inbox-list'), '');
      const e = $('#inbox-empty');
      e.hidden = false;
      setHTML(e, html`<span class="spinner" aria-hidden="true"></span><p>Loading the inbox…</p>`);
      return;
    }

    if (!S.inbox.filter) S.inbox.filter = items.some((i) => NEEDS.has(i.status)) ? 'needs' : 'all';
    const f = S.inbox.filter;
    $$('#inbox-filters [data-filter]').forEach((b) => {
      const key = b.dataset.filter;
      b.setAttribute('aria-pressed', String(key === f));
      const n = items.filter((i) => FILTERS[key](i.status)).length;
      b.querySelector('.n').textContent = n ? String(n) : '';
    });

    const list = items.filter((i) => (FILTERS[f] || FILTERS.all)(i.status));
    reconcile($('#inbox-list'), list, (i) => i.path, inboxSig, inboxCardHTML);

    const empty = $('#inbox-empty');
    if (list.length) {
      empty.hidden = true;
    } else {
      empty.hidden = false;
      if (!items.length) {
        setHTML(empty, html`${icon('inbox')}<h2>The inbox is empty</h2>
          <p>Finished downloads land here, beets matches them against MusicBrainz and Spotify, and anything that needs a decision waits for you.</p>`);
      } else {
        const msg = { needs: 'Nothing needs your attention.', progress: 'Nothing is being tagged right now.', applied: 'Nothing imported in the last 24 hours.' }[f] || 'Nothing here.';
        setHTML(empty, html`${icon('check')}<h2>${msg}</h2>
          <button type="button" class="btn small" data-act="inbox-filter" data-filter="all">Show all ${plural(items.length, 'item')}</button>`);
      }
    }
  }

  function inboxSig(it) {
    const open = [...S.open].filter((k) => k.includes(it.path)).sort().join(',');
    const st = S.status || {};
    return [JSON.stringify(it), open, S.pending.has('inbox:' + it.path),
      S.jobArt.get(it.job_id) || '', st.buffer_seconds, st.auto_apply].join('|');
  }

  const isBusy = (it) => it.status === 'queued' || it.status === 'tagging' || S.pending.has('inbox:' + it.path);

  function allCandidates(it) {
    return (it.tasks || []).flatMap((t) => t.candidates || []);
  }

  function chosenCandidate(it) {
    for (const t of it.tasks || []) {
      if (!t.chosen) continue;
      const c = (t.candidates || []).find((x) => x.id === t.chosen);
      if (c) return c;
    }
    return null;
  }

  const pctText = (sim) => `${(Math.max(0, Math.min(1, sim || 0)) * 100).toFixed(1).replace(/\.0$/, '')}%`;
  const srcClass = (src) => (src === 'MusicBrainz' ? 'mb' : src === 'Spotify' ? 'sp' : 'other');

  function inboxCardHTML(it) {
    const [label, tone] = INBOX_STATUS[it.status] || [it.status, 'neutral'];
    const busy = isBusy(it);
    const name = it.album || (it.relpath || '').split('/').pop() || it.relpath || it.path;
    const meta = [
      it.artist,
      it.track_count != null ? plural(it.track_count, 'track') : null,
      it.singleton ? 'as single tracks' : null,
    ].filter(Boolean);

    let body = '';
    switch (it.status) {
      case 'waiting': body = waitingHTML(it); break;
      case 'queued':
      case 'tagging': body = workingHTML(it); break;
      case 'review': body = tasksHTML(it, busy, false); break;
      case 'duplicate': body = html`${dupPanelHTML(it, busy)}${tasksHTML(it, busy, true)}`; break;
      case 'error': body = errorHTML(it); break;
      case 'applied': body = appliedHTML(it); break;
      default: body = '';
    }
    const working = busy && it.status !== 'waiting';

    return html`
      <article class="icard s-${it.status}" data-path="${it.path}" aria-busy="${working}">
        <header class="icard-head">
          ${artHTML(S.jobArt.get(it.job_id), 'icard-art', 'disc')}
          <div class="icard-titles">
            <h2 class="icard-title">${name}</h2>
            ${meta.length ? html`<p class="icard-meta">${meta.join(' · ')}</p>` : ''}
            <p class="icard-path mono" title="${it.path}">${it.relpath || it.path}</p>
          </div>
          <div class="icard-state">
            <span class="st t-${tone}">${working ? html`<span class="spinner tiny" aria-hidden="true"></span>` : ''}${label}</span>
            ${timeTag(it.last_change, 'muted small')}
          </div>
        </header>
        ${body}
        ${it.status === 'applied' ? '' : inboxActionsHTML(it, busy)}
      </article>`;
  }

  function waitingHTML(it) {
    const buf = S.status ? S.status.buffer_seconds : null;
    const due = buf != null ? it.last_change + buf : null;
    return html`
      <div class="ibody waiting">
        <span class="settle" aria-hidden="true"><i></i><i></i><i></i></span>
        <p>Waiting for the files to settle${due != null
          ? html` — tagging in <strong data-countdown="${due}">${fmtCountdown(due - nowSec())}</strong>`
          : '…'}</p>
      </div>`;
  }

  function workingHTML(it) {
    return html`
      <div class="ibody working">
        <span class="spinner" aria-hidden="true"></span>
        <p>${it.status === 'tagging'
          ? 'beets is looking for matches…'
          : 'Queued for the tagger…'}</p>
      </div>`;
  }

  function tasksHTML(it, busy, dup) {
    const tasks = it.tasks || [];
    if (!tasks.length) {
      return html`<div class="ibody"><p class="muted">beets didn't report anything for this folder. Re-run the tagger or import it as-is.</p></div>`;
    }
    return html`<div class="tasks">${tasks.map((t, ti) => taskHTML(it, t, ti, busy, dup, tasks.length))}</div>`;
  }

  function taskHTML(it, t, ti, busy, dup, count) {
    const cands = t.candidates || [];
    const moreKey = `more:${it.path}|${ti}`;
    const showAll = S.open.has(moreKey);
    const visible = showAll ? cands : cands.slice(0, CAND_LIMIT);
    const cur = [t.cur_artist, t.cur_album].filter(Boolean).join(' – ');
    return html`
      <section class="task" aria-label="${t.kind === 'singleton' ? 'Track' : 'Album'}${count > 1 ? ` ${ti + 1} of ${count}` : ''}">
        <div class="task-head">
          ${count > 1 || t.kind === 'singleton'
            ? html`<span class="tag">${t.kind === 'singleton' ? 'track' : 'album'}${count > 1 ? ` ${ti + 1}/${count}` : ''}</span>` : ''}
          <span class="task-cur" title="What the files' own tags say">${icon('disc')} ${cur || 'Untagged files'}</span>
          ${t.kind === 'album' && t.items ? html`<span class="muted">${plural(t.items, 'file')}</span>` : ''}
          ${t.recommendation ? html`<span class="rec r-${t.recommendation}" title="beets' confidence in its best match">confidence: ${t.recommendation}</span>` : ''}
        </div>
        ${t.error ? html`<p class="task-warn">${icon('warn')} ${t.error}</p>` : ''}
        ${cands.length
          ? html`<ol class="cands">${visible.map((c, ci) => candHTML(it, t, ti, c, ci + 1, busy))}</ol>`
          : html`<p class="no-cands">No candidates found. Look one up by MusicBrainz or Spotify ID below, or import as-is.</p>`}
        ${cands.length > CAND_LIMIT
          ? html`<button type="button" class="disclosure more" data-act="toggle" data-key="${moreKey}" aria-expanded="${showAll}">
              ${icon('chevron', 'chev')} ${showAll ? 'Show fewer' : `Show ${cands.length - CAND_LIMIT} more`}</button>` : ''}
      </section>`;
  }

  function candHTML(it, t, ti, c, rank, busy) {
    const sim = Math.max(0, Math.min(1, c.similarity || 0));
    const tone = sim >= 0.96 ? 'good' : sim >= 0.75 ? 'warn' : 'bad';
    const pct = pctText(sim);
    const chosen = t.chosen && t.chosen === c.id;
    const src = c.data_source || 'Unknown';
    const isAlbum = c.kind === 'album';
    const media = c.media ? (c.mediums > 1 ? `${c.mediums}×${c.media}` : c.media) : null;
    const details = [media, c.label, c.country, c.albumdisambig].filter(Boolean);
    if (!details.length && c.disambig) details.push(c.disambig);
    const pens = [...(c.penalties || [])].sort((a, b) => (b.weight || 0) - (a.weight || 0));
    const url = safeUrl(c.url);
    const mapKey = `map:${it.path}|${ti}|${c.id}`;
    const mapOpen = S.open.has(mapKey);
    const mapId = hashId(mapKey);
    const warns = [];
    if (isAlbum && c.missing_tracks) warns.push(`${plural(c.missing_tracks, 'track')} missing from your files`);
    if (isAlbum && c.extra_items) warns.push(`${plural(c.extra_items, 'file')} not matched`);

    return html`
      <li class="cand${chosen ? ' chosen' : ''}">
        <div class="ring t-${tone}" style="--p:${(sim * 100).toFixed(1)}" role="img" aria-label="${pct} similar">
          <span>${pct.replace('%', '')}<small>%</small></span>
        </div>
        <div class="cand-main">
          <div class="cand-title">
            <span class="rank" aria-label="Rank ${rank}">${rank}</span>
            <span class="cand-name">${c.artist || 'Unknown'} – ${c.title || 'Untitled'}</span>
            ${c.year ? html`<span class="year">${c.year}</span>` : ''}
            <span class="src s-${srcClass(src)}">${src}</span>
            ${chosen ? html`<span class="tag t-accent">chosen</span>` : ''}
          </div>
          ${!isAlbum && c.album ? html`<div class="cand-sub">from ${c.album}</div>` : ''}
          ${details.length ? html`<div class="cand-sub" title="${c.disambig || ''}">${details.join(' · ')}</div>` : ''}
          ${isAlbum ? html`
            <div class="cand-match">
              <span class="${c.matched === c.track_count ? 'ok' : ''}">${c.matched} of ${c.track_count} tracks matched</span>
              ${warns.map((w) => html`<span class="warn-tag">${icon('warn')}${w}</span>`)}
            </div>` : ''}
          ${pens.length ? html`<ul class="pens" aria-label="Penalties">${pens.map((p) => html`<li title="penalty weight ${p.weight}">${String(p.name).replace(/_/g, ' ')}</li>`)}</ul>` : ''}
          ${src === 'Spotify' ? html`<p class="hint">${icon('info')} Spotify matches carry a source penalty, so they never auto-apply — that's expected.</p>` : ''}
          <div class="cand-actions">
            <button type="button" class="btn small${rank === 1 || chosen ? ' primary' : ''}" data-act="inbox-apply" data-path="${it.path}" data-cand="${c.id}"${flag(busy, 'disabled')}>${icon('check')} Apply</button>
            ${isAlbum && c.tracks && c.tracks.length ? html`
              <button type="button" class="btn small ghost" data-act="toggle" data-key="${mapKey}" aria-expanded="${mapOpen}" aria-controls="${mapId}">
                ${icon('chevron', 'chev')} Track mapping</button>` : ''}
            ${url ? html`<a class="btn small ghost" href="${url}" target="_blank" rel="noopener noreferrer">${src === 'Spotify' ? 'Spotify' : src === 'MusicBrainz' ? 'MusicBrainz' : 'Open'} ${icon('external')}</a>` : ''}
          </div>
          ${mapOpen ? mapTableHTML(c, mapId) : ''}
        </div>
      </li>`;
  }

  function mapTableHTML(c, id) {
    return html`
      <div class="map-wrap" id="${id}">
        <table class="map">
          <thead><tr><th scope="col" class="num">#</th><th scope="col">Your file</th><th scope="col">Matched title</th><th scope="col" class="num">Length</th></tr></thead>
          <tbody>${c.tracks.map((tr) => {
            const differs = tr.item_title && tr.title && norm(tr.item_title) !== norm(tr.title);
            return html`<tr class="${differs ? 'differs' : ''}">
              <td class="num">${tr.index ?? ''}</td>
              <td class="file">${tr.item}</td>
              <td>${tr.title}${differs ? html`<span class="was">tagged “${tr.item_title}”</span>` : ''}</td>
              <td class="num mono">${fmtDur(tr.length)}</td></tr>`;
          })}</tbody>
        </table>
        ${c.extra_items || c.missing_tracks ? html`<p class="hint">Only matched files are listed${c.missing_tracks ? `; ${plural(c.missing_tracks, 'track')} on the release have no file` : ''}${c.extra_items ? `; ${plural(c.extra_items, 'file')} matched nothing` : ''}.</p>` : ''}
      </div>`;
  }

  function dupPanelHTML(it, busy) {
    const tasks = it.tasks || [];
    const dups = tasks.flatMap((t) => t.duplicates || []);
    const chosen = chosenCandidate(it);
    const asis = !chosen && tasks.some((t) => t.decision === 'asis');
    const selId = hashId('dup:' + it.path);
    const val = S.inputs.get('dup:' + it.path) || '';
    return html`
      <div class="dup">
        <div class="dup-head">
          ${icon('warn')}
          <div>
            <strong>Already in your library</strong>
            <p>${chosen
              ? html`beets matched <b>${chosen.artist} – ${chosen.title}</b>
                  <span class="src s-${srcClass(chosen.data_source)}">${chosen.data_source}</span> ${pctText(chosen.similarity)}, but a copy exists.`
              : asis ? 'Importing as-is ran into an existing copy.' : 'beets found an existing copy.'}</p>
          </div>
        </div>
        ${dups.length ? html`<ul class="dup-list">${dups.map((d) => html`
          <li><span class="dup-sum">${d.summary}</span><span class="mono path" title="${d.path}">${d.path}</span></li>`)}</ul>` : ''}
        <div class="dup-actions">
          <label for="${selId}">What should happen to the existing copy?</label>
          <div class="dup-row">
            <select id="${selId}" data-input="dup:${it.path}"${flag(busy, 'disabled')}>
              <option value=""${flag(!val, 'selected')} disabled>Choose…</option>
              ${DUP_ACTIONS.map(([v, l]) => html`<option value="${v}"${flag(val === v, 'selected')}>${l}</option>`)}
            </select>
            <button type="button" class="btn primary" data-act="inbox-reapply" data-path="${it.path}"${flag(busy || !val, 'disabled')}>
              ${icon('refresh')} ${chosen ? 'Re-apply match' : 'Re-import as-is'}</button>
          </div>
          <p class="hint">Applying a different match below uses the same choice.</p>
        </div>
      </div>`;
  }

  function errorHTML(it) {
    const t0 = (it.tasks || [])[0];
    const msg = it.report_error || it.error || (t0 && t0.error) || 'beets failed without saying why.';
    const out = it.stderr || '';
    const k = 'err:' + it.path;
    const open = S.open.has(k);
    const id = hashId(k);
    return html`
      <div class="ibody err">
        <p class="err-msg">${icon('warn')}<span>${msg}</span></p>
        ${t0 && t0.error && t0.error !== msg ? html`<p class="err-msg sub">${t0.error}</p>` : ''}
        ${out ? html`
          <button type="button" class="disclosure" data-act="toggle" data-key="${k}" aria-expanded="${open}" aria-controls="${id}">
            ${icon('chevron', 'chev')} beets output</button>
          ${open ? html`<pre class="out" id="${id}" tabindex="0">${out}</pre>` : ''}` : ''}
      </div>`;
  }

  function appliedHTML(it) {
    const imp = it.imported || [];
    const chosen = chosenCandidate(it);
    const k = 'imp:' + it.path;
    const open = S.open.has(k);
    const shown = open ? imp : imp.slice(0, 3);
    let how;
    if (it.mode === 'asis') how = html`Imported as-is (existing tags kept).`;
    else if (chosen) {
      how = html`Matched <b>${chosen.artist} – ${chosen.title}</b>
        <span class="src s-${srcClass(chosen.data_source)}">${chosen.data_source}</span> ${pctText(chosen.similarity)}
        ${it.mode === 'auto' ? html`<span class="muted">· auto-applied</span>` : ''}`;
    } else how = html`Imported into the library.`;
    return html`
      <div class="ibody applied">
        <p class="applied-how">${icon('check')}<span>${how}</span></p>
        ${shown.length ? html`<ul class="imp-list">${shown.map((x) => {
          const paths = x.paths || [];
          return html`<li><span class="dup-sum">${x.summary}</span>${paths[0]
            ? html`<span class="mono path" title="${paths.join('\n')}">${paths[0]}${paths.length > 1 ? ` (+${paths.length - 1} more)` : ''}</span>` : ''}</li>`;
        })}</ul>` : ''}
        ${imp.length > 3 ? html`<button type="button" class="disclosure" data-act="toggle" data-key="${k}" aria-expanded="${open}">
          ${icon('chevron', 'chev')} ${open ? 'Show fewer' : `Show all ${imp.length}`}</button>` : ''}
      </div>`;
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

  function srcHint(v) {
    const s = idSource(v);
    return s ? SRC_HINT[s] : ['', ''];
  }

  function inboxActionsHTML(it, busy) {
    const id = hashId('sid:' + it.path);
    const v = S.inputs.get('sid:' + it.path) || '';
    const [hint, hintTone] = srcHint(v);
    const dis = flag(busy, 'disabled');
    return html`
      <div class="iactions">
        <form class="idsearch" data-submit="inbox-search" data-path="${it.path}">
          <label for="${id}" class="sr-only">Look up by MusicBrainz or Spotify ID or link</label>
          <div class="idsearch-field">
            ${icon('search')}
            <input id="${id}" type="text" data-input="sid:${it.path}" value="${v}"
              placeholder="MusicBrainz or Spotify ID / link" autocomplete="off" spellcheck="false" autocapitalize="off"${dis}>
            <span class="src-hint ${hintTone}" data-hint aria-live="polite">${hint}</span>
          </div>
          <button type="submit" class="btn small"${dis}>Look up</button>
        </form>
        <div class="iact-row">
          <button type="button" class="btn small ghost" data-act="inbox-retag" data-path="${it.path}"${dis}>
            ${icon('refresh')} ${it.status === 'waiting' ? 'Tag now' : 'Re-run tagger'}</button>
          <button type="button" class="btn small ghost" data-act="inbox-asis" data-path="${it.path}"${dis}
            title="Import with the files' existing tags">${icon('download')} Import as-is</button>
          <label class="switch small">
            <input type="checkbox" role="switch" data-change="inbox-singletons" data-path="${it.path}"${flag(!!it.singleton, 'checked')}${dis}>
            <span class="switch-track" aria-hidden="true"></span>
            <span>Import as single tracks</span>
          </label>
        </div>
      </div>`;
  }

  async function inboxAction(path, body, okMsg) {
    const key = 'inbox:' + path;
    S.pending.set(key, Infinity);
    renderInbox();
    let ok = false;
    try {
      await api('api/inbox/action', { method: 'POST', body: { path, ...body } });
      S.pending.set(key, Date.now());
      toast(okMsg, 'success');
      ok = true;
    } catch {
      S.pending.delete(key);
    }
    renderInbox();
    inboxPoller.kick();
    statusPoller.kick();
    return ok;
  }

  function dupChoiceOrWarn(path) {
    const v = S.inputs.get('dup:' + path) || '';
    if (!v) {
      toast('First choose what should happen to the copy already in your library.', 'warn');
      const card = $(`.icard[data-path="${CSS.escape(path)}"]`);
      const sel = card && card.querySelector('select[data-input]');
      if (sel) sel.focus();
    }
    return v;
  }

  function inboxApply(el) {
    const path = el.dataset.path;
    const cand = el.dataset.cand;
    const it = findItem(path);
    const body = { action: 'apply', candidate: cand };
    if (it && it.status === 'duplicate') {
      const d = dupChoiceOrWarn(path);
      if (!d) return;
      body.duplicate_action = d;
    }
    const c = it && allCandidates(it).find((x) => x.id === cand);
    inboxAction(path, body, `Applying ${c ? `${c.artist} – ${c.title}` : 'the match'}…`);
  }

  function inboxReapply(el) {
    const path = el.dataset.path;
    const it = findItem(path);
    if (!it) return;
    const d = dupChoiceOrWarn(path);
    if (!d) return;
    const chosen = chosenCandidate(it);
    if (chosen) inboxAction(path, { action: 'apply', candidate: chosen.id, duplicate_action: d }, 'Re-applying the match…');
    else inboxAction(path, { action: 'asis', duplicate_action: d }, 'Re-importing as-is…');
  }

  function inboxAsis(el) {
    const path = el.dataset.path;
    const it = findItem(path);
    const body = { action: 'asis' };
    if (it && it.status === 'duplicate') {
      const d = S.inputs.get('dup:' + path);
      if (d) body.duplicate_action = d;
    }
    inboxAction(path, body, 'Importing as-is…');
  }

  /* ============================================================= 7. Log tab */

  async function fetchLog() {
    const params = new URLSearchParams({ limit: '300' });
    if (S.log.kind) params.set('kind', S.log.kind);
    const kind = S.log.kind;
    const data = await api(`api/events?${params}`, { quiet: true });
    if (kind !== S.log.kind) return;
    S.log.events = (data && data.events) || [];
    S.log.loaded = true;
    renderLog();
  }

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

  function shortRef(e) {
    if (!e.ref) return '';
    if (e.ref.includes('/')) return shortPath(e.ref);
    return e.ref;
  }

  function eventHTML({ e, head }) {
    const k = 'ev:' + e.id;
    const open = S.open.has(k);
    const id = 'evd-' + e.id;
    const hasDetails = e.details != null
      && !(typeof e.details === 'object' && Object.keys(e.details).length === 0);
    const ref = shortRef(e);
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

  /* ============================================================ pollers */

  const statusPoller = new Poller(fetchStatus, () => (S.status && (S.status.downloading || S.status.tagging) ? 3000 : 5000));
  const jobsPoller = new Poller(fetchJobs, () => {
    if (S.route.tab !== 'download') return 10000;
    return S.jobs.some((j) => ACTIVE_JOB.has(j.status)) ? 2000 : 10000;
  });
  const inboxPoller = new Poller(fetchInbox, () => 3000);
  const logPoller = new Poller(fetchLog, () => 5000);
  const POLLERS = [statusPoller, jobsPoller, inboxPoller, logPoller];

  /* ============================================================ 8. wiring */

  function rerenderCurrent() {
    if (S.route.tab === 'download') renderJobs();
    else if (S.route.tab === 'inbox') renderInbox();
    else if (S.route.tab === 'log') renderLog();
  }

  const ACTIONS = {
    'type': (el) => setType(el.dataset.type),
    'toggle-sel': (el) => toggleSel(el.dataset.key),
    'expand': (el) => toggleExpand(el.dataset.key, el),
    'expand-close': () => closeExpand(true),
    'open-artist': (el) => openArtist(el.dataset.key),
    'back': () => goBack(),
    'artist-retry': () => { S.cache.artist.delete(S.artist.id); loadArtist(S.artist.id); },
    'select-all': (el) => selectAll(el.dataset.key),
    'sel-clear': () => { S.selection.clear(); syncSelUI(); },
    'sel-download': (el) => downloadSelection(el),
    'search-retry': () => runSearch(),
    'jobs-toggle': () => setJobsCollapsed(!S.jobsCollapsed),
    'jobs-clear': () => clearJobs(),
    'job-cancel': (el) => jobAction('cancel', el.dataset.key),
    'job-retry': (el) => jobAction('retry', el.dataset.key),
    'job-force': (el) => jobAction('force', el.dataset.key),
    'job-release': (el) => jobAction('release', el.dataset.key),
    'job-remove': (el) => jobAction('remove', el.dataset.key),
    'toggle': (el) => {
      const k = el.dataset.key;
      if (S.open.has(k)) S.open.delete(k); else S.open.add(k);
      rerenderCurrent();
      if (k.startsWith('out:') && S.open.has(k)) {
        const pre = document.getElementById('out-' + k.slice(4));
        if (pre) pre.scrollTop = pre.scrollHeight;
      }
    },
    'inbox-filter': (el) => {
      S.inbox.filter = el.dataset.filter;
      store.set('inboxFilter', S.inbox.filter);
      renderInbox();
    },
    'inbox-apply': (el) => inboxApply(el),
    'inbox-reapply': (el) => inboxReapply(el),
    'inbox-asis': (el) => inboxAsis(el),
    'inbox-retag': (el) => inboxAction(el.dataset.path, { action: 'retag' }, 'Re-running the tagger…'),
    'log-kind': (el) => setLogKind(el.dataset.kind),
    'status-info': () => {
      const pill = $('#account-pill');
      toast(pill.title, pill.classList.contains('t-bad') ? 'error' : 'info', 7000);
    },
  };

  const CHANGES = {
    'pl-albums': (el) => { S.playlistAlbums = el.checked; },
    'inbox-singletons': async (el) => {
      const on = el.checked;
      const ok = await inboxAction(el.dataset.path, { action: on ? 'singletons' : 'albums' },
        on ? 'Re-tagging as single tracks…' : 'Re-tagging as an album…');
      if (!ok && el.isConnected) el.checked = !on;
    },
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
    } else if (key.startsWith('dup:')) {
      const card = el.closest('.icard');
      const btn = card && card.querySelector('[data-act="inbox-reapply"]');
      if (btn) btn.disabled = !el.value || isBusy(findItem(card.dataset.path) || {});
    }
  }

  function wire() {
    document.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el || el.disabled) return;
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
        inboxAction(f.dataset.path, { action: 'search', search_id: v }, `Looking up the ${name} release…`);
      }
    });

    // Search box: debounced live search; never re-rendered, so focus stays.
    const q = $('#q');
    q.addEventListener('input', () => {
      S.search.q = q.value;
      $('#q-clear').hidden = !q.value;
      if (S.route.artist && q.value.trim()) location.hash = '#download';
      if (!q.value.trim()) {
        debouncedSearch.cancel();
        if (S.search.ctrl) S.search.ctrl.abort();
        S.search.seq++;
        S.search.results = null;
        S.search.error = null;
        S.search.loading = false;
        renderSearchBusy();
        if (!S.route.artist) renderSearch();
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
      const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        if (S.route.tab !== 'download') location.hash = '#download';
        requestAnimationFrame(() => { $('#q').focus(); $('#q').select(); });
      } else if (e.key === 'Escape' && S.expanded && !$('#confirm-dialog').open) {
        closeExpand(true);
      }
    });

    window.addEventListener('hashchange', onRoute);

    window.addEventListener('resize', debounce(() => {
      const panel = $('#expand-panel');
      const card = $('.card.expanded');
      if (panel && card) placeExpand(card, panel);
    }, 120));

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) POLLERS.forEach((p) => { if (p.active) p.kick(); });
    });

    // Keep relative times and countdowns fresh without re-rendering.
    let tick = 0;
    setInterval(() => {
      tick++;
      const t = nowSec();
      $$('[data-countdown]').forEach((el) => {
        el.textContent = fmtCountdown(Number(el.dataset.countdown) - t);
      });
      if (tick % 5 === 0) {
        $$('time[data-rel]').forEach((el) => { el.textContent = rel(Number(el.dataset.rel)); });
      }
    }, 1000);
  }

  function init() {
    // Sticky offsets (day headers, scroll padding) follow the real top bar height.
    const topbar = $('.topbar');
    const setTopbarH = () => document.documentElement.style.setProperty('--topbar-h', `${topbar.offsetHeight}px`);
    setTopbarH();
    if ('ResizeObserver' in window) new ResizeObserver(setTopbarH).observe(topbar);

    wire();
    setJobsCollapsed(S.jobsCollapsed);
    renderStatus();
    statusPoller.setActive(true);
    onRoute();
    if (S.route.tab === 'download' && window.matchMedia('(pointer: fine)').matches) {
      $('#q').focus({ preventScroll: true });
    }
  }

  init();
})();
