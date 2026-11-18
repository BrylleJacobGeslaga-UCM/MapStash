'use strict';
/* MapStash interface. Talks to Python through window.pywebview.api (see bridge.py). */

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
const COVER = (sid, kind) => `https://assets.ppy.sh/beatmaps/${sid}/covers/${kind}.jpg`;
const PREVIEW = (sid) => `https://b.ppy.sh/preview/${sid}.mp3`;
const COLLECTION_URL = (id) => `https://osucollector.com/collections/${id}`;

let S = {};      // settings (from Python)
let INFO = {};   // app info (version, categories)

// ------------------------------------------------------------------ helpers
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'data') Object.assign(n.dataset, v);
    else n.setAttribute(k, v);
  }
  for (const c of kids) if (c != null) n.append(c);
  return n;
}
function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return i < 2 ? `${Math.round(n)} ${u[i]}` : `${n.toFixed(1)} ${u[i]}`;
}
function fmtTime(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, '0')}s`;
  return `${Math.floor(sec / 3600)}h ${String(Math.floor(sec % 3600 / 60)).padStart(2, '0')}m`;
}
const num = (n) => Number(n || 0).toLocaleString('en-US');
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove('show'), 2400);
}
function parseCid(text) {
  const m = String(text || '').match(/collections\/(\d+)/);
  if (m) return Number(m[1]);
  return /^\s*\d+\s*$/.test(text || '') ? Number(text) : null;
}

// ------------------------------------------------------------------ fit to window
/* The interface is designed at 1280x800. Like a visual novel engine, scale it to fit
   the window (and Windows display scaling), keeping the width flexible. */
const DESIGN_W = 1280, DESIGN_H = 800;
function fitScreen() {
  const z = Math.max(0.6, Math.min(1.6, Math.min(window.innerWidth / DESIGN_W, window.innerHeight / DESIGN_H)));
  document.documentElement.style.zoom = z.toFixed(4);
}
window.addEventListener('resize', fitScreen);
fitScreen();

// ------------------------------------------------------------------ petals
function makePetals() {
  const box = $('petals');
  for (let i = 0; i < 16; i++) {
    const p = el('div', { class: 'petal' + (i % 3 === 0 ? ' light' : '') });
    const scale = 0.7 + Math.random() * 0.7;
    p.style.left = `${Math.random() * 100}%`;
    p.style.width = `${9 * scale}px`;
    p.style.height = `${5 * scale}px`;
    p.style.opacity = (0.35 + Math.random() * 0.4).toFixed(2);
    p.style.animationDuration = `${10 + Math.random() * 10}s`;
    p.style.animationDelay = `${-Math.random() * 20}s`;
    box.append(p);
  }
}

// ------------------------------------------------------------------ screens
let current = null;
function go(name) {
  if (name === 'exit') { api().quit(); return; }
  if (name === current) return;
  const prev = current && $(current);
  if (prev) prev.classList.remove('active', 'shown');
  const next = $(name);
  next.classList.add('active');
  requestAnimationFrame(() => requestAnimationFrame(() => next.classList.add('shown')));
  document.body.classList.toggle('on-title', name === 'title');
  current = name;
  if (name === 'browse' && !B.loaded && !B.loading) browseLoad();
  if (name === 'config') fillConfig();
  if (name === 'download') refreshOptions();
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-go]');
  if (b) go(b.dataset.go);
});

function titleMenu() {
  const items = [...document.querySelectorAll('#title-menu .vn-item')];
  const select = (i) => items.forEach((it, j) => it.classList.toggle('sel', i === j));
  items.forEach((it, i) => it.addEventListener('mouseenter', () => select(i)));
  document.addEventListener('keydown', (e) => {
    if (current !== 'title' || anyOverlayOpen()) return;
    const i = items.findIndex((it) => it.classList.contains('sel'));
    if (e.key === 'ArrowDown') { select((i + 1) % items.length); e.preventDefault(); }
    if (e.key === 'ArrowUp') { select((i - 1 + items.length) % items.length); e.preventDefault(); }
    if (e.key === 'Enter') { items[Math.max(0, i)].click(); e.preventDefault(); }
  });
}

// ------------------------------------------------------------------ message box + backlog
const SPEEDS = { 1: 45, 2: 22, 3: 8, 4: 0 };
const MSG = { timer: null, tokens: [], shown: 0, total: 0 };
const BACKLOG = [];

function addLog(text, kind) {
  if (!kind) kind = text.startsWith('✓') ? 'ok' : text.startsWith('✗') ? 'bad' : '';
  BACKLOG.push({ text, kind });
  if (BACKLOG.length > 4000) BACKLOG.splice(0, 1000);
  if ($('backlog-overlay').classList.contains('open')) renderBacklog();
}

/* say("Text with **highlights**") shows a line in the message box. */
function say(text) {
  addLog(text.replace(/\*\*/g, ''), 'say');
  const tokens = [];
  text.split('**').forEach((part, i) => { for (const ch of part) tokens.push([ch, i % 2 === 1]); });
  MSG.tokens = tokens;
  MSG.shown = 0;
  MSG.total = tokens.length;
  clearInterval(MSG.timer);
  const delay = S.typewriter === false ? 0 : (SPEEDS[S.text_speed] ?? 22);
  $('msg-next').style.visibility = 'hidden';
  if (!delay) { MSG.shown = MSG.total; drawMsg(); return; }
  MSG.timer = setInterval(() => {
    MSG.shown = Math.min(MSG.total, MSG.shown + 1);
    drawMsg();
    if (MSG.shown >= MSG.total) clearInterval(MSG.timer);
  }, delay);
}
function drawMsg() {
  const box = $('msg-text');
  box.textContent = '';
  let run = '', hl = false;
  const flush = () => { if (run) box.append(hl ? el('span', { class: 'hl', text: run }) : run); run = ''; };
  for (let i = 0; i < MSG.shown; i++) {
    const [ch, h] = MSG.tokens[i];
    if (h !== hl) { flush(); hl = h; }
    run += ch;
  }
  flush();
  if (MSG.shown >= MSG.total) $('msg-next').style.visibility = 'visible';
}
function finishTyping() {
  if (MSG.shown < MSG.total) { clearInterval(MSG.timer); MSG.shown = MSG.total; drawMsg(); }
}
function renderBacklog() {
  const list = $('bl-list');
  list.textContent = '';
  for (const l of BACKLOG.slice(-1500)) list.append(el('div', { class: `backlog-line ${l.kind}`, text: l.text }));
  list.scrollTop = list.scrollHeight;
}

// ------------------------------------------------------------------ overlays + choices
function anyOverlayOpen() { return !!document.querySelector('.overlay.open'); }
function openOverlay(id) { $(id).classList.add('open'); }
function closeOverlay(id) {
  $(id).classList.remove('open');
  if (id === 'maps-overlay') stopPreview();
}
let choiceResolve = null;
/* choose("prompt with **highlight**", [{label, value}]) -> Promise(value | null) */
function choose(prompt, options) {
  const p = $('choice-prompt');
  p.textContent = '';
  prompt.split('**').forEach((part, i) => p.append(i % 2 ? el('span', { class: 'pink', text: part }) : part));
  const list = $('choice-list');
  list.textContent = '';
  options.forEach((o, i) => list.append(el('button', {
    class: 'big-choice' + (i === 0 ? ' sel' : ''), text: o.label,
    onmouseenter: (e) => { list.querySelectorAll('.big-choice').forEach((b) => b.classList.remove('sel')); e.currentTarget.classList.add('sel'); },
    onclick: () => resolveChoice(o.value),
  })));
  openOverlay('choice-overlay');
  return new Promise((res) => { choiceResolve = res; });
}
function resolveChoice(v) {
  closeOverlay('choice-overlay');
  const r = choiceResolve;
  choiceResolve = null;
  if (r) r(v);
}
document.addEventListener('keydown', (e) => {
  if ($('choice-overlay').classList.contains('open')) {
    const btns = [...$('choice-list').children];
    const i = btns.findIndex((b) => b.classList.contains('sel'));
    const pick = (j) => btns.forEach((b, k) => b.classList.toggle('sel', j === k));
    if (e.key === 'ArrowDown') { pick((i + 1) % btns.length); e.preventDefault(); }
    if (e.key === 'ArrowUp') { pick((i - 1 + btns.length) % btns.length); e.preventDefault(); }
    if (e.key === 'Enter') { btns[Math.max(0, i)].click(); e.preventDefault(); }
    if (e.key === 'Escape') resolveChoice(null);
    return;
  }
  if (e.key === 'Escape') {
    const open = document.querySelector('.overlay.open');
    if (open) closeOverlay(open.id);
    else if (current !== 'title') go('title');
  }
});

// ------------------------------------------------------------------ browse
const B = {
  items: [], byId: {}, cursor: null, hasMore: false, loading: false, loaded: false,
  req: 0, autoPages: 0, searching: false, cat: 'All', sel: null, coverAsked: new Set(),
};

function buildCats() {
  const box = $('b-cats');
  box.textContent = '';
  for (const c of ['All', ...(INFO.categories || [])]) {
    box.append(el('button', {
      class: 'chip' + (c === B.cat ? ' on' : ''), text: c,
      onclick: () => { B.cat = c; buildCats(); filtersChanged(); },
    }));
  }
}
function starBounds() {
  const lo = parseFloat($('b-smin').value), hi = parseFloat($('b-smax').value);
  return [isNaN(lo) ? 0 : lo, isNaN(hi) ? 12 : hi];
}
const optNum = (id) => { const n = parseFloat($(id).value); return isNaN(n) ? null : n; };
function filtersActive() {
  const [lo, hi] = starBounds();
  return B.cat !== 'All' || $('b-mode').value !== '' || lo > 0 || hi < 12 ||
    optNum('b-mmin') != null || optNum('b-mmax') != null || optNum('b-favs') != null || $('b-age').value !== '';
}
function matches(c) {
  if (B.cat !== 'All' && !c.cats.includes(B.cat)) return false;
  const [lo, hi] = starBounds();
  if ((lo > 0 || hi < 12) && (c.stars == null || c.stars < lo || c.stars > hi)) return false;
  const mode = $('b-mode').value;
  if (mode && c.mode !== mode) return false;
  const mmin = optNum('b-mmin'), mmax = optNum('b-mmax'), favs = optNum('b-favs');
  if (mmin != null && c.maps < mmin) return false;
  if (mmax != null && c.maps > mmax) return false;
  if (favs != null && c.favs < favs) return false;
  const days = Number($('b-age').value);
  if (days) {
    const t = Date.parse(c.date);
    if (isNaN(t) || Date.now() - t > days * 86400000) return false;
  }
  return true;
}
function visibleItems() {
  const vis = B.items.filter(matches);
  const sort = $('b-sort').value;
  const by = {
    favs: (a, b) => b.favs - a.favs, maps: (a, b) => b.maps - a.maps,
    'stars-d': (a, b) => (b.stars ?? -1) - (a.stars ?? -1),
    'stars-a': (a, b) => (a.stars ?? 99) - (b.stars ?? 99),
    date: (a, b) => String(b.date).localeCompare(String(a.date)),
  }[sort];
  return by ? vis.sort(by) : vis;
}

async function browseLoad(more = false, search = false) {
  $('b-range').disabled = $('b-kind').value !== 'popular';
  if (!more) {
    B.searching = search;
    Object.assign(B, { items: [], byId: {}, cursor: null, hasMore: false, autoPages: 0, sel: null });
    renderDetail();
  }
  const kind = B.searching ? 'search' : $('b-kind').value;
  const req = ++B.req;
  B.loading = true;
  renderList();
  const r = await api().browse(kind, more ? B.cursor : null, $('b-range').value, $('b-search').value.trim());
  if (req !== B.req) return;
  B.loading = false;
  B.loaded = true;
  if (!r.ok) {
    renderList();
    $('b-status').textContent = B.searching
      ? "Search didn't work. Try Popular or Recent, or paste a link in Download."
      : "Couldn't reach osu!Collector. Check your internet connection.";
    addLog(`✗ Browse: ${r.error}`);
    return;
  }
  for (const c of r.collections) {
    if (B.byId[c.id]) continue;
    B.byId[c.id] = c;
    B.items.push(c);
  }
  B.cursor = r.cursor;
  B.hasMore = r.has_more;
  renderList();
  autoLoad();
  fetchCovers();
}
function autoLoad() {
  if (filtersActive() && !B.loading && B.hasMore && visibleItems().length < 30 && B.autoPages < 10) {
    B.autoPages++;
    browseLoad(true);
  }
}
const filtersChanged = debounce(() => { B.autoPages = 0; renderList(); autoLoad(); }, 250);

/* Covers are CSS backgrounds layered over a gradient, so a missing cover shows the gradient. */
function coverBg(node, sid, kind) {
  node.style.backgroundImage = sid ? `url("${COVER(sid, kind)}"), linear-gradient(135deg, #4a2a3a 0%, #2a2326 100%)` : '';
}
function thumbFor(c, cls) {
  const d = el('div', { class: cls, data: { thumb: c.id }, 'aria-hidden': 'true' });
  coverBg(d, c.cover_sid, 'list');
  return d;
}
function renderList() {
  const list = $('b-list');
  const keep = list.scrollTop;
  const vis = visibleItems();
  list.textContent = '';
  if (!vis.length) {
    list.append(el('div', { class: 'empty', text: B.loading ? 'Loading collections…' : B.items.length ? 'No collections match these filters.' : 'No collections found.' }));
  }
  vis.forEach((c, i) => {
    const sub = [c.uploader && `by ${c.uploader}`, c.cats.join(', ') || null].filter(Boolean).join(' · ');
    list.append(el('button', {
      class: 'slot' + (c.id === B.sel ? ' sel' : ''), data: { id: c.id },
      onclick: () => selectCollection(c.id), ondblclick: () => downloadCollection(c.id),
    },
    el('span', { class: 'slot-no', text: `No.${String(i + 1).padStart(2, '0')}` }),
    thumbFor(c, 'thumb'),
    el('div', { class: 'slot-main' }, el('span', { class: 'slot-name', text: c.name }), el('span', { class: 'slot-sub', text: sub || ' ' })),
    el('div', { class: 'slot-stats' },
      el('span', { class: c.id === B.sel ? 'pink' : '', text: c.stars != null ? `${c.stars.toFixed(1)}★ avg` : '-' }),
      el('span', { class: 'muted', text: `${num(c.maps)} maps · ♥ ${num(c.favs)}` }))));
  });
  list.scrollTop = keep;
  const loaded = B.items.length;
  let s = filtersActive() ? `Showing ${vis.length} of ${loaded} loaded collections` : `${loaded} collections`;
  if (B.loading) s += ' · loading…';
  else if (B.hasMore) s += ' · more available';
  $('b-status').textContent = s;
  $('b-more').disabled = !B.hasMore || B.loading;
}
async function fetchCovers() {
  const ids = B.items.filter((c) => !c.cover_sid && !B.coverAsked.has(c.id)).map((c) => c.id);
  for (let i = 0; i < ids.length; i += 10) {
    const chunk = ids.slice(i, i + 10);
    chunk.forEach((id) => B.coverAsked.add(id));
    let res = {};
    try { res = await api().cover_sids(chunk); } catch (e) { return; }
    for (const [id, sid] of Object.entries(res)) {
      const c = B.byId[id];
      if (!c || !sid) continue;
      c.cover_sid = sid;
      document.querySelectorAll(`[data-thumb="${id}"]`).forEach((ph) => coverBg(ph, sid, 'list'));
      if (B.sel === c.id) setBanner(c);
    }
  }
}
function selectCollection(id) {
  B.sel = id;
  document.querySelectorAll('#b-list .slot').forEach((s) => s.classList.toggle('sel', Number(s.dataset.id) === id));
  renderDetail();
}
function setBanner(c) {
  coverBg($('d-banner'), c && c.cover_sid, 'card');
}
function renderDetail() {
  const c = B.byId[B.sel];
  for (const id of ['d-dl', 'd-view', 'd-web']) $(id).disabled = !c;
  setBanner(c);
  if (!c) {
    $('d-name').textContent = 'Choose a collection';
    for (const id of ['d-maps', 'd-stars', 'd-type', 'd-favs']) $(id).textContent = '-';
    $('d-by').innerHTML = '&nbsp;';
    $('d-spread').textContent = '';
    $('d-desc').textContent = '';
    return;
  }
  $('d-name').textContent = c.name;
  $('d-by').textContent = [c.uploader && `by ${c.uploader}`, c.date].filter(Boolean).join(' · ');
  $('d-maps').textContent = num(c.maps);
  $('d-stars').textContent = c.stars != null ? `${c.stars.toFixed(1)}★` : '-';
  $('d-type').textContent = c.cats.join(', ') || '-';
  $('d-favs').textContent = `♥ ${num(c.favs)}`;
  $('d-spread').textContent = [c.modes, c.spread].filter(Boolean).join('\n');
  $('d-desc').textContent = c.description || '';
}
function downloadCollection(id) {
  if (D.busy) { toast('A download is already running.'); go('download'); return; }
  go('download');
  $('link').value = COLLECTION_URL(id);
  check();
}

// ------------------------------------------------------------------ map viewer + previews
const MV = { cid: null, cursor: null, hasMore: true, loading: false, count: 0 };
const audio = () => $('preview-audio');
let playingBtn = null;

function openMaps(cid, name) {
  Object.assign(MV, { cid, cursor: null, hasMore: true, loading: false, count: 0 });
  $('mv-name').textContent = name || `Collection ${cid}`;
  $('mv-list').textContent = '';
  $('mv-status').textContent = 'Loading…';
  openOverlay('maps-overlay');
  mvLoad();
}
async function mvLoad() {
  if (MV.loading || !MV.hasMore) return;
  MV.loading = true;
  const cid = MV.cid;
  const r = await api().map_page(cid, MV.cursor);
  if (cid !== MV.cid) return;
  MV.loading = false;
  if (!r.ok) { $('mv-status').textContent = `Couldn't load maps: ${r.error}`; return; }
  MV.cursor = r.cursor;
  MV.hasMore = r.has_more;
  for (const m of r.maps) $('mv-list').append(mapRow(m));
  MV.count += r.maps.length;
  $('mv-status').textContent = `${num(MV.count)} maps loaded` + (MV.hasMore ? ' · scroll for more' : '');
  const list = $('mv-list');
  if (MV.hasMore && list.scrollHeight <= list.clientHeight + 40) mvLoad();
}
function mapRow(m) {
  const stats = [];
  if (m.stars != null) stats.push(el('span', { class: 'pink', text: `${m.stars.toFixed(2)}★` }));
  if (m.bpm) stats.push(el('span', { text: `${Math.round(m.bpm)} BPM` }));
  if (m.length_text) stats.push(el('span', { class: 'muted', text: m.length_text }));
  const sub = [`[${m.version || '?'}]`, m.creator && `mapped by ${m.creator}`, m.status].filter(Boolean).join('  ·  ');
  const play = el('button', { class: 'play', text: '▶', 'aria-label': 'Play preview' });
  play.addEventListener('click', () => togglePreview(m.sid, play));
  return el('div', { class: 'map-row' },
    (() => { const d = el('div', { class: 'map-thumb', 'aria-hidden': 'true' }); coverBg(d, m.sid, 'list'); return d; })(),
    el('div', { class: 'map-text' },
      el('div', { class: 'map-title', text: `${m.artist || '?'} - ${m.title || `Set ${m.sid}`}` }),
      el('div', { class: 'map-sub', text: sub })),
    el('div', { class: 'map-stats' }, ...stats),
    play);
}
function togglePreview(sid, btn) {
  const a = audio();
  if (playingBtn === btn) { stopPreview(); return; }
  stopPreview();
  playingBtn = btn;
  btn.classList.add('on');
  btn.textContent = '■';
  a.src = PREVIEW(sid);
  a.volume = (S.preview_volume ?? 60) / 100;
  bgmDuck(true);
  a.play().catch(() => { toast('No preview for this map.'); stopPreview(); });
}
function stopPreview() {
  const a = audio();
  a.pause();
  a.removeAttribute('src');
  if (playingBtn) { playingBtn.classList.remove('on'); playingBtn.textContent = '▶'; }
  playingBtn = null;
  bgmDuck(false);
}

// ------------------------------------------------------------------ download
const D = { plan: null, busy: false, checking: false, pendingDownload: false, needsCheck: false };

/* "3:30" -> 210 seconds; a plain number means minutes ("3" -> 180). */
function parseLength(text) {
  const t = String(text || '').trim();
  if (!t) return null;
  const m = t.match(/^(\d+):(\d{1,2})$/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  const n = parseFloat(t);
  return isNaN(n) ? null : Math.round(n * 60);
}
function readNum(input, k) {
  if (input.disabled) return null;
  if (k === 'length') return parseLength(input.value);
  const n = parseFloat(input.value);
  return isNaN(n) ? null : n;
}
function chipValues(id) {
  return [...document.querySelectorAll(`#${id} .chip.on:not(:disabled)`)].map((b) => b.dataset.v);
}
function currentFilter() {
  if (!$('f-on').checked) return null;
  const f = {};
  document.querySelectorAll('#f-panel .frange').forEach((row) => {
    const [lo, hi] = row.querySelectorAll('input');
    const a = readNum(lo, row.dataset.k), b = readNum(hi, row.dataset.k);
    if (a != null || b != null) f[row.dataset.k] = [a, b];
  });
  const modes = chipValues('f-modes'), statuses = chipValues('f-status');
  if (modes.length) f.modes = modes;
  if (statuses.length) f.statuses = statuses;
  return f;
}
const filterActive = () => { const f = currentFilter(); return !!f && Object.keys(f).length > 0; };
function setFilterFields(fields) {
  // Grey out filters this collection has no data for.
  document.querySelectorAll('#f-panel .frange').forEach((row) => {
    const ok = !fields || fields[row.dataset.k] !== false;
    row.querySelectorAll('input').forEach((i) => { i.disabled = !ok; });
    row.title = ok ? (row.dataset.k === 'length' ? 'Minutes, or minutes:seconds (3:30)' : '') : "This collection doesn't include this information";
  });
  for (const [id, key] of [['f-modes', 'modes'], ['f-status', 'statuses']]) {
    document.querySelectorAll(`#${id} .chip`).forEach((b) => {
      b.disabled = !!fields && !(fields[key] || []).includes(b.dataset.v);
    });
  }
}
function setBusy(busy, status) {
  D.busy = busy;
  $('start').disabled = busy;
  $('check').disabled = busy;
  $('stop').disabled = !busy;
  if (status) $('status').textContent = status;
}
function setBar(frac, busy) {
  $('bar').classList.toggle('busy', !!busy);
  $('bar').firstElementChild.style.width = `${Math.round((frac || 0) * 1000) / 10}%`;
}
function clearInfo(text) {
  $('i-name').textContent = text;
  $('i-name').className = 'muted';
  for (const id of ['i-maps', 'i-have', 'i-size', 'i-free']) { $(id).textContent = '…'; $(id).className = ''; }
  $('i-note').innerHTML = '&nbsp;';
}

async function check(thenDownload = false) {
  if (D.busy) return;
  const link = $('link').value.trim();
  const r = await api().check(link);
  if (!r.ok) { say(r.error); toast(r.error); return; }
  D.checking = true;
  D.pendingDownload = thenDownload;
  D.needsCheck = false;
  clearInfo('Checking…');
  setBusy(true, 'Checking the collection and download size…');
  setBar(0, true);
  say(thenDownload ? 'Let me check this collection first…' : 'Let me look at this collection…');
}

async function onPlan(plan) {
  D.checking = false;
  setBar(0, false);
  const r = await api().set_filter(currentFilter());
  D.plan = (r && r.plan) || plan;
  showPlan(D.plan);
  setBusy(false, D.plan.todo ? 'Ready to download.' : 'Nothing to download.');
  $('open-maps').disabled = false;
  const p = D.plan;
  say(`"${p.name}" has ${num(p.total_maps)} maps in ${num(p.total_sets)} sets.\n` +
      (p.todo ? `Downloading ${p.filtered ? 'the matching ones ' : ''}takes about **${fmtBytes(p.est_bytes)}**${p.est_rough ? ' (rough guess)' : ''}.`
              : (p.sets ? 'You already have all of them.' : 'None of them match your filter.')));
  if (D.pendingDownload) { D.pendingDownload = false; startDownload(); }
}

function showPlan(p) {
  $('i-name').textContent = p.name;
  $('i-name').className = '';
  $('i-maps').textContent = `${num(p.maps)} maps · ${num(p.sets)} sets${p.filtered ? ' (filtered)' : ''}`;
  $('i-have').textContent = `${num(p.have)} sets (${num(p.todo)} to go)`;
  $('i-size').textContent = p.todo ? `~${fmtBytes(p.est_bytes)}${p.est_rough ? ' (rough)' : ''}` : 'Nothing to download';
  showFree(p.free, p.todo ? p.est_bytes : 0);
  setFilterFields(p.fields);
  $('f-panel').classList.toggle('off', !$('f-on').checked);
  const anyData = p.fields && Object.entries(p.fields).some(([k, v]) => (Array.isArray(v) ? v.length : v));
  if (!anyData) {
    $('f-note').textContent = "osu!Collector didn't send map details for this collection, so filters aren't available and every map will be downloaded.";
  } else if (p.filtered) {
    $('f-note').textContent = `Matching: ${num(p.maps)} of ${num(p.total_maps)} maps in ${num(p.sets)} sets (${p.filter_label}). Whole sets are downloaded, but the collection only gets the matching difficulties.`;
  } else if ($('f-on').checked) {
    $('f-note').textContent = 'Set a range or pick a mode or status to narrow it down.';
  } else {
    $('f-note').textContent = `All ${num(p.total_maps)} maps will be downloaded.`;
  }
}
function showFree(free, needed) {
  const f = $('i-free'), note = $('i-note');
  if (free == null) { f.textContent = 'unknown'; f.className = ''; note.innerHTML = '&nbsp;'; return; }
  const enough = free >= needed;
  f.textContent = fmtBytes(free);
  f.className = enough ? 'good' : 'bad';
  if (!needed) note.innerHTML = '&nbsp;';
  else if (enough) { note.textContent = `✓ Enough space. About ${fmtBytes(free - needed)} left afterwards.`; note.className = 'space-note muted'; }
  else { note.textContent = `⚠ Not enough space. You need about ${fmtBytes(needed - free)} more.`; note.className = 'space-note bad'; }
}
const filterChanged = debounce(async () => {
  if (!D.plan || D.busy) return;
  const r = await api().set_filter(currentFilter());
  if (r.ok) { D.plan = r.plan; showPlan(D.plan); }
}, 300);

async function startDownload() {
  if (D.busy) return;
  const link = $('link').value.trim();
  if (!D.plan || D.needsCheck || parseCid(link) !== D.plan.cid) { check(true); return; }
  const fr = await api().set_filter(currentFilter());
  if (fr.ok) { D.plan = fr.plan; showPlan(D.plan); }
  const p = D.plan;
  if (!p.todo) { say(p.sets ? 'You already have every matching map.' : 'No maps match your filter.'); return; }
  if (p.free != null && p.est_bytes > p.free) {
    const c = await choose(
      `This needs about **${fmtBytes(p.est_bytes)}**, but the drive only has **${fmtBytes(p.free)}** free.\nIf I keep going, it will fill up partway through.`,
      [{ label: 'Download as much as fits', value: 'go' },
       { label: 'Choose another folder', value: 'folder' },
       ...(S.no_video ? [] : [{ label: 'Skip videos and check again', value: 'novideo' }]),
       { label: 'Go back', value: null }]);
    if (c === 'folder') { go('config'); return; }
    if (c === 'novideo') {
      const r = await api().save_settings({ no_video: true });
      if (r.ok) S = r.settings;
      refreshOptions();
      check(true);
      return;
    }
    if (c !== 'go') return;
  }
  const r = await api().download(link, currentFilter());
  if (!r.ok) {
    if (r.error === 'recheck') { check(true); return; }
    say(r.error);
    return;
  }
  D.plan = r.plan;
  setBusy(true, 'Starting…');
  setBar(0, false);
  $('open-col').disabled = true;
  say(`Downloading **${num(D.plan.todo)}** sets from "${D.plan.name}".\nYou can stop anytime. Next time I'll pick up where we left off.`);
}

function onProgress(p) {
  setBar(p.total ? p.done / p.total : 0, false);
  if (!p.total) { $('status').textContent = 'Nothing to download.'; return; }
  let s = `${num(p.done)} / ${num(p.total)} maps  ·  ${fmtBytes(p.bytes)} of ~${fmtBytes(p.est_total)}`;
  if (p.speed > 0 && p.done) s += `  ·  ${fmtBytes(p.speed)}/s  ·  ${fmtTime(Math.max(p.est_total - p.bytes, 0) / p.speed)} left`;
  $('status').textContent = s;
  $('i-size').textContent = `~${fmtBytes(p.est_total)}`;
  if (p.free != null) showFree(p.free, Math.max(p.est_total - p.bytes, 0));
}

function onFinished(r) {
  setBusy(false);
  D.needsCheck = true;
  $('open-maps').disabled = false;
  $('open-col').disabled = !r.col_dir;
  if (r.stopped) {
    $('status').textContent = `Stopped. ${fmtBytes(r.bytes)} downloaded.`;
    say('Stopped. Press Download again to continue where we left off.');
    return;
  }
  setBar(1, false);
  $('status').textContent = `Done. ${num(r.ok)} downloaded (${fmtBytes(r.bytes)}), ${num(r.failed)} failed.`;
  const failed = r.failed ? `\n${num(r.failed)} didn't download. Press Download again to retry them.` : '';
  if (r.lazer) {
    say('All done! To finish in osu!lazer: click **Open folder**, select every .osz file and drag them onto the lazer window.' +
        (r.col_dir ? '\nThen in lazer: Settings → Maintenance → import from stable, pick the **Collection folder**, and tick only Collections.' : '') + failed);
  } else {
    say('All done! Open osu! and it will import the new maps by itself.' + failed);
  }
}
function onError(text) {
  setBusy(false, 'Something went wrong.');
  setBar(0, false);
  D.checking = false;
  D.pendingDownload = false;
  if ($('i-name').textContent === 'Checking…') clearInfo("Couldn't load that collection");
  addLog(`✗ ${text}`);
  say(`Something went wrong: ${text}`);
}

function refreshOptions() {
  $('o-game').textContent = S.target === 'stable' ? 'osu!stable' : 'osu!lazer';
  $('o-folder').textContent = S.target === 'stable' ? `osu! folder: ${S.stable_dir || '(not set)'}` : `Saving to: ${S.lazer_out}`;
  $('o-novideo').checked = !!S.no_video;
  $('o-collection').checked = !!S.make_collection;
}
async function saveQuick(changes, recheck) {
  const r = await api().save_settings(changes);
  if (!r.ok) { toast(`Couldn't save: ${r.error}`); return; }
  S = r.settings;
  refreshOptions();
  if (recheck && D.plan && !D.busy) check();
}

// ------------------------------------------------------------------ background music
const BGM = { ctx: null, master: null, engine: null, mode: null, ducked: false, list: [], idx: 0, base: '', errors: 0, fadeTimer: null };
const bgmAudio = () => $('bgm-audio');
const bgmLevel = () => (BGM.ducked ? 0 : (S.bgm_volume ?? 35) / 100);
function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}
function bgmLabel(text) {
  document.body.classList.toggle('bgm-off', !S.bgm);
  $('now-playing').textContent = S.bgm ? `♪ ${text}` : '♪ music off';
}
function fadeEl(a, target, ms, then) {
  clearInterval(BGM.fadeTimer);
  const start = a.volume, steps = Math.max(1, Math.round(ms / 50));
  let i = 0;
  BGM.fadeTimer = setInterval(() => {
    i++;
    a.volume = Math.min(1, Math.max(0, start + (target - start) * (i / steps)));
    if (i >= steps) { clearInterval(BGM.fadeTimer); if (then) then(); }
  }, 50);
}
function pianoFade(level, secs) {
  const g = BGM.master.gain, t = BGM.ctx.currentTime;
  g.cancelScheduledValues(t);
  g.setValueAtTime(g.value, t);
  g.linearRampToValueAtTime(level, t + secs);
}
function pianoStart() {
  if (!BGM.ctx) {
    BGM.ctx = new (window.AudioContext || window.webkitAudioContext)();
    BGM.master = BGM.ctx.createGain();
    BGM.master.gain.value = 0;
    BGM.master.connect(BGM.ctx.destination);
    BGM.engine = new Piano.Engine(BGM.ctx, BGM.master);
  }
  BGM.ctx.resume().catch(() => {});
  BGM.engine.start();
  pianoFade(bgmLevel(), 2.5);
  BGM.mode = 'piano';
  bgmLabel('moonlit piano');
}
function pianoStop() {
  if (!BGM.engine || !BGM.engine.timer) return;
  pianoFade(0, 1.2);
  BGM.engine.stop();
}
function folderPlay() {
  const a = bgmAudio();
  if (BGM.errors >= BGM.list.length) {
    toast("Couldn't play your music files. Playing the built-in piano instead.");
    BGM.mode = null;
    pianoStart();
    return;
  }
  const name = BGM.list[BGM.idx];
  a.src = BGM.base + encodeURIComponent(name);
  a.volume = 0;
  BGM.mode = 'folder';
  bgmLabel(name.replace(/\.[^.]+$/, ''));
  a.play().then(() => { BGM.errors = 0; fadeEl(a, bgmLevel(), 1500); }).catch((e) => {
    if (e && e.name === 'NotAllowedError') return; // waits for the first click (see unlock)
    BGM.errors++;
    folderNext();
  });
}
function folderNext() {
  BGM.idx++;
  if (BGM.idx >= BGM.list.length) { BGM.idx = 0; shuffle(BGM.list); }
  folderPlay();
}
function folderStop(now) {
  const a = bgmAudio();
  if (now || a.paused) { clearInterval(BGM.fadeTimer); a.pause(); a.removeAttribute('src'); return; }
  fadeEl(a, 0, 800, () => { a.pause(); a.removeAttribute('src'); });
}
async function bgmStart() {
  bgmStop(true);
  if (!S.bgm) { bgmLabel(''); return; }
  if (S.bgm_source === 'folder') {
    const r = await api().music_files(S.bgm_folder);
    if (r.ok) {
      BGM.base = r.base;
      BGM.list = shuffle(r.files);
      BGM.idx = 0;
      BGM.errors = 0;
      folderPlay();
      return;
    }
    toast(`${r.error} Playing the built-in piano instead.`);
  }
  pianoStart();
}
function bgmStop(now) {
  if (BGM.mode === 'piano') pianoStop();
  if (BGM.mode === 'folder') folderStop(now);
  BGM.mode = null;
  bgmLabel('');
}
function bgmApplyVolume() {
  if (BGM.mode === 'piano') pianoFade(bgmLevel(), 0.5);
  if (BGM.mode === 'folder' && !bgmAudio().paused) fadeEl(bgmAudio(), bgmLevel(), 500);
}
/* Silence the music while a song preview plays, then bring it back. */
function bgmDuck(on) {
  if (BGM.ducked === on) return;
  BGM.ducked = on;
  if (BGM.mode === 'piano') pianoFade(bgmLevel(), on ? 0.4 : 1.5);
  if (BGM.mode === 'folder') {
    const a = bgmAudio();
    if (on) {
      if (!a.paused) fadeEl(a, 0, 400, () => { if (BGM.ducked) a.pause(); });
    } else if (a.src) {
      a.play().then(() => fadeEl(a, bgmLevel(), 1500)).catch(() => {});
    }
  }
}
async function bgmToggle() {
  const r = await api().save_settings({ bgm: !S.bgm });
  if (r.ok) S = r.settings;
  if (S.bgm) { bgmStart(); toast('Music on'); } else { bgmStop(); toast('Music off'); }
}
function bgmUnlock() {
  // Browsers only allow sound after the first click or key press.
  if (BGM.ctx && BGM.ctx.state === 'suspended') BGM.ctx.resume().catch(() => {});
  const a = bgmAudio();
  if (BGM.mode === 'folder' && a.paused && a.src) a.play().then(() => fadeEl(a, bgmLevel(), 1500)).catch(() => {});
}

// ------------------------------------------------------------------ config
function seg(id, value) {
  document.querySelectorAll(`#${id} .chip`).forEach((b) => b.classList.toggle('on', String(b.dataset.v) === String(value)));
}
function segValue(id) {
  const b = document.querySelector(`#${id} .chip.on`);
  return b ? b.dataset.v : null;
}
function fillConfig() {
  seg('c-target', S.target);
  $('c-lazer').value = S.lazer_out || '';
  $('c-stable').value = S.stable_dir || '';
  $('c-workers').value = S.workers;
  $('c-workers-v').textContent = S.workers;
  $('c-novideo').checked = !!S.no_video;
  $('c-collection').checked = !!S.make_collection;
  $('c-petals').checked = S.petals !== false;
  $('c-typewriter').checked = S.typewriter !== false;
  seg('c-speed', S.text_speed);
  $('c-volume').value = S.preview_volume;
  $('c-volume-v').textContent = `${S.preview_volume}%`;
  $('c-bgm').checked = !!S.bgm;
  seg('c-bgm-source', S.bgm_source);
  $('c-bgm-folder').value = S.bgm_folder || '';
  $('c-bgm-volume').value = S.bgm_volume;
  $('c-bgm-volume-v').textContent = `${S.bgm_volume}%`;
  $('c-status').textContent = '';
}
async function saveConfig() {
  const before = { ...S };
  const r = await api().save_settings({
    target: segValue('c-target') || 'lazer', lazer_out: $('c-lazer').value.trim(), stable_dir: $('c-stable').value.trim(),
    workers: Number($('c-workers').value), no_video: $('c-novideo').checked, make_collection: $('c-collection').checked,
    petals: $('c-petals').checked, typewriter: $('c-typewriter').checked,
    text_speed: Number(segValue('c-speed') || 2), preview_volume: Number($('c-volume').value),
    bgm: $('c-bgm').checked, bgm_source: segValue('c-bgm-source') || 'piano',
    bgm_folder: $('c-bgm-folder').value.trim(), bgm_volume: Number($('c-bgm-volume').value),
  });
  if (!r.ok) { $('c-status').textContent = `Couldn't save: ${r.error}`; return; }
  S = r.settings;
  applyUi();
  refreshOptions();
  $('c-status').textContent = 'Saved.';
  toast('Settings saved');
  if (['bgm', 'bgm_source', 'bgm_folder'].some((k) => before[k] !== S[k])) bgmStart();
  else bgmApplyVolume();
  const affects = ['target', 'lazer_out', 'stable_dir', 'no_video'].some((k) => before[k] !== S[k]);
  if (affects && D.plan) D.needsCheck = true;
}
function applyUi() {
  document.body.classList.toggle('no-petals', S.petals === false);
  audio().volume = (S.preview_volume ?? 60) / 100;
}

// ------------------------------------------------------------------ events from Python
function handleEvent(ev) {
  switch (ev.type) {
    case 'log':
      addLog(ev.text);
      if (D.checking && /^(Loading map details|Checking download size|Fetching collection)/.test(ev.text)) $('status').textContent = ev.text;
      break;
    case 'plan': onPlan(ev.plan); break;
    case 'progress': onProgress(ev); break;
    case 'finished': onFinished(ev.result); break;
    case 'error': onError(ev.text); break;
  }
}
async function pollLoop() {
  try {
    const evs = await api().poll();
    for (const ev of evs) handleEvent(ev);
  } catch (e) { /* window closing */ }
  setTimeout(pollLoop, 150);
}

// ------------------------------------------------------------------ wiring
function wire() {
  // browse
  $('b-kind').addEventListener('change', () => browseLoad());
  $('b-range').addEventListener('change', () => browseLoad());
  $('b-sort').addEventListener('change', () => renderList());
  $('b-search-go').addEventListener('click', () => browseLoad(false, !!$('b-search').value.trim()));
  $('b-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') browseLoad(false, !!$('b-search').value.trim()); });
  for (const id of ['b-smin', 'b-smax']) $(id).addEventListener('input', filtersChanged);
  $('b-mode').addEventListener('change', filtersChanged);
  for (const id of ['b-mmin', 'b-mmax', 'b-favs']) $(id).addEventListener('input', filtersChanged);
  $('b-age').addEventListener('change', filtersChanged);
  $('b-reset').addEventListener('click', () => {
    B.cat = 'All'; $('b-smin').value = 0; $('b-smax').value = 12; $('b-mode').value = '';
    for (const id of ['b-mmin', 'b-mmax', 'b-favs']) $(id).value = '';
    $('b-age').value = '';
    buildCats(); filtersChanged();
  });
  $('b-more').addEventListener('click', () => browseLoad(true));
  $('b-list').addEventListener('scroll', (e) => {
    const l = e.target;
    if (B.hasMore && !B.loading && l.scrollTop + l.clientHeight > l.scrollHeight - 80) browseLoad(true);
  });
  $('d-dl').addEventListener('click', () => B.sel && downloadCollection(B.sel));
  $('d-view').addEventListener('click', () => B.sel && openMaps(B.sel, B.byId[B.sel].name));
  $('d-web').addEventListener('click', () => B.sel && api().open_url(COLLECTION_URL(B.sel)));

  // map viewer
  $('mv-close').addEventListener('click', () => closeOverlay('maps-overlay'));
  $('mv-web').addEventListener('click', () => api().open_url(COLLECTION_URL(MV.cid)));
  $('mv-dl').addEventListener('click', () => { const id = MV.cid; closeOverlay('maps-overlay'); downloadCollection(id); });
  $('mv-list').addEventListener('scroll', (e) => {
    const l = e.target;
    if (l.scrollTop + l.clientHeight > l.scrollHeight - 120) mvLoad();
  });
  audio().addEventListener('ended', stopPreview);

  // download
  $('link').addEventListener('keydown', (e) => { if (e.key === 'Enter') check(); });
  $('paste').addEventListener('click', async () => {
    try {
      $('link').value = (await navigator.clipboard.readText()).trim();
      if (parseCid($('link').value)) check();
    } catch (e) { $('link').focus(); toast('Press Ctrl+V to paste'); }
  });
  $('check').addEventListener('click', () => check());
  $('view-maps').addEventListener('click', () => {
    const cid = parseCid($('link').value);
    if (!cid) { toast('Paste a collection link first'); return; }
    openMaps(cid, D.plan && D.plan.cid === cid ? D.plan.name : null);
  });
  $('start').addEventListener('click', startDownload);
  $('stop').addEventListener('click', () => { api().stop(); $('stop').disabled = true; $('status').textContent = 'Stopping after the current downloads…'; D.pendingDownload = false; });
  $('open-maps').addEventListener('click', async () => { if (!await api().open_folder('maps')) toast('Nothing downloaded yet'); });
  $('open-col').addEventListener('click', () => api().open_folder('collection'));
  const filterTouched = () => {
    if (!$('f-on').checked) { $('f-on').checked = true; }
    $('f-panel').classList.remove('off');
    filterChanged();
  };
  $('f-on').addEventListener('change', () => { $('f-panel').classList.toggle('off', !$('f-on').checked); filterChanged(); });
  document.querySelectorAll('#f-panel .frange input').forEach((i) => i.addEventListener('input', filterTouched));
  for (const id of ['f-modes', 'f-status']) {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('.chip');
      if (!b || b.disabled) return;
      b.classList.toggle('on');
      filterTouched();
    });
  }
  $('f-more').addEventListener('click', () => {
    const open = $('f-panel').classList.toggle('show-more');
    $('f-more').textContent = open ? 'Fewer filters ▴' : 'More filters ▾';
    $('f-more').setAttribute('aria-expanded', String(open));
  });
  $('f-clear').addEventListener('click', () => {
    document.querySelectorAll('#f-panel .frange input').forEach((i) => { i.value = ''; });
    document.querySelectorAll('#f-panel .seg .chip').forEach((b) => b.classList.remove('on'));
    $('f-on').checked = false;
    $('f-panel').classList.add('off');
    filterChanged();
  });
  $('f-panel').classList.add('off');
  $('o-novideo').addEventListener('change', (e) => saveQuick({ no_video: e.target.checked }, true));
  $('o-collection').addEventListener('change', (e) => saveQuick({ make_collection: e.target.checked }, false));
  $('msgbox').addEventListener('click', (e) => { if (!e.target.closest('.msg-tools')) finishTyping(); });
  $('backlog-btn').addEventListener('click', () => { renderBacklog(); openOverlay('backlog-overlay'); });
  $('bl-close').addEventListener('click', () => closeOverlay('backlog-overlay'));

  // config
  for (const id of ['c-target', 'c-speed', 'c-bgm-source']) {
    $(id).addEventListener('click', (e) => { const b = e.target.closest('.chip'); if (b) seg(id, b.dataset.v); });
  }
  $('c-workers').addEventListener('input', (e) => { $('c-workers-v').textContent = e.target.value; });
  $('c-volume').addEventListener('input', (e) => { $('c-volume-v').textContent = `${e.target.value}%`; audio().volume = e.target.value / 100; });
  document.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', async () => {
    const input = $(b.dataset.pick);
    const path = await api().pick_folder(input.value);
    if (path) input.value = path;
  }));
  $('c-bgm-volume').addEventListener('input', (e) => {
    $('c-bgm-volume-v').textContent = `${e.target.value}%`;
    S.bgm_volume = Number(e.target.value); // live preview; saved with Save
    bgmApplyVolume();
  });
  $('c-save').addEventListener('click', saveConfig);
  document.querySelectorAll('.bgm-toggle, #now-playing').forEach((b) => b.addEventListener('click', bgmToggle));
  bgmAudio().addEventListener('ended', () => { if (BGM.mode === 'folder') folderNext(); });
  document.addEventListener('pointerdown', bgmUnlock);
  document.addEventListener('keydown', bgmUnlock);

  // close overlays by clicking the dim background
  document.querySelectorAll('.overlay').forEach((o) => o.addEventListener('click', (e) => {
    if (e.target !== o) return;
    if (o.id === 'choice-overlay') resolveChoice(null);
    else closeOverlay(o.id);
  }));
}

let started = false;
async function init() {
  if (started) return;
  started = true;
  S = await api().get_settings();
  INFO = await api().app_info();
  $('version-line').textContent = `v${INFO.version} · unofficial · not affiliated with osu!Collector or ppy`;
  makePetals();
  titleMenu();
  buildCats();
  wire();
  applyUi();
  refreshOptions();
  MSG.tokens = [...'Paste an osu!Collector link above, or pick a collection in Browse.'].map((c) => [c, false]);
  MSG.shown = MSG.total = MSG.tokens.length;
  drawMsg();
  go('title');
  bgmStart();
  pollLoop();
}
window.addEventListener('pywebviewready', init);
if (window.pywebview && window.pywebview.api) init();
