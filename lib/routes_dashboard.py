"""Dashboard page and its JSON API endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

import job_queue

router = APIRouter()

_NC_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")
_APP_ID = os.environ.get("APP_ID", "recognize_llm")
# AppAPI proxy route uses root='/proxy', so URL is /proxy/{appId}/... (not /apps/app_api/proxy/...)
_PROXY_BASE = f"/proxy/{_APP_ID}"


# ── JSON API ──────────────────────────────────────────────────────────────────

@router.get("/dashboard/api/status")
async def api_status() -> dict:
    return job_queue.status()


@router.get("/dashboard/api/recent")
async def api_recent() -> list:
    return job_queue.get_recent(20)


# ── Iframe loader JS (injected into NC's embedded page via registered script) ─

_LOADER_JS = """
(function () {
  var BASE_URL = window.location.origin;
  function mount() {
    var content = document.getElementById('content') || document.body;
    var iframe = document.createElement('iframe');
    iframe.src = BASE_URL + '/proxy/__APP_ID__/top_menu/dashboard';
    iframe.style.cssText = 'width:100%;height:calc(100vh - 50px);border:none;display:block;background:#1a1b1e';
    iframe.allow = 'same-origin';
    content.innerHTML = '';
    content.style.padding = '0';
    content.appendChild(iframe);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
"""


from fastapi.responses import PlainTextResponse  # noqa: E402


@router.get("/js/dashboard-loader.js", response_class=PlainTextResponse)
async def dashboard_loader_js() -> PlainTextResponse:
    js = _LOADER_JS.replace("__APP_ID__", _APP_ID)
    return PlainTextResponse(js, media_type="application/javascript")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Queue · Recognize LLM</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #1a1b1e;
  color: #e0e0e0;
  min-height: 100vh;
  padding: 24px;
}

/* ── Header ── */
.header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 28px;
}
.header h1 {
  font-size: 1.4rem;
  font-weight: 600;
  color: #fff;
}
.header h1 span { color: #4dabf7; }
.pulse-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: #51cf66;
  flex-shrink: 0;
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50%       { transform: scale(1.4); opacity: 0.6; }
}
.last-update {
  margin-left: auto;
  font-size: 0.75rem;
  color: #666;
}

/* ── Stats ── */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 24px;
}
.stat {
  background: #25262b;
  border-radius: 12px;
  padding: 20px 16px;
  text-align: center;
  border: 1px solid #2c2d32;
  position: relative;
  overflow: hidden;
}
.stat::before {
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 3px;
  border-radius: 12px 12px 0 0;
}
.stat.pending::before  { background: #868e96; }
.stat.processing::before { background: #4dabf7; }
.stat.done::before     { background: #51cf66; }
.stat.failed::before   { background: #ff6b6b; }

.stat-num {
  font-size: 2.4rem;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -1px;
  transition: color 0.3s;
}
.stat.pending   .stat-num { color: #adb5bd; }
.stat.processing .stat-num { color: #4dabf7; }
.stat.done      .stat-num { color: #51cf66; }
.stat.failed    .stat-num { color: #ff6b6b; }

.stat-label {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: #666;
  margin-top: 6px;
}

.stat-num.bump {
  animation: bump 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);
}
@keyframes bump {
  0%   { transform: scale(1); }
  40%  { transform: scale(1.25); }
  100% { transform: scale(1); }
}

/* ── Progress bar ── */
.progress-wrap {
  background: #25262b;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 28px;
  border: 1px solid #2c2d32;
}
.progress-label {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: #888;
  margin-bottom: 8px;
}
.progress-bar {
  height: 8px;
  background: #2c2d32;
  border-radius: 4px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #4dabf7, #51cf66);
  border-radius: 4px;
  transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
  min-width: 2px;
}

/* ── Recent section ── */
.section-title {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 2px;
  color: #555;
  margin-bottom: 16px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 16px;
}

/* ── Card ── */
.card {
  background: #25262b;
  border-radius: 14px;
  border: 1px solid #2c2d32;
  display: flex;
  gap: 0;
  overflow: hidden;
  transform-origin: top center;
}

.card.pop-in {
  animation: pop-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}
@keyframes pop-in {
  0%   { transform: scale(0.7) translateY(-12px); opacity: 0; }
  60%  { transform: scale(1.03) translateY(2px); }
  100% { transform: scale(1) translateY(0); opacity: 1; }
}

/* thumbnail */
.card-thumb {
  width: 130px;
  flex-shrink: 0;
  background: #1a1b1e;
  position: relative;
  overflow: hidden;
}
.card-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.3s ease;
}
.card:hover .card-thumb img { transform: scale(1.06); }
.card-thumb .no-thumb {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 2.5rem;
  color: #333;
}

/* body */
.card-body {
  padding: 14px 16px;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.card-name {
  font-size: 0.85rem;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-user {
  font-size: 0.7rem;
  color: #555;
}
.card-desc {
  font-size: 0.78rem;
  color: #aaa;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  flex: 1;
}
.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 4px;
}
.tag {
  font-size: 0.65rem;
  padding: 2px 8px;
  border-radius: 20px;
  font-weight: 500;
  opacity: 0;
  transform: scale(0);
}
.tag.tag-show {
  animation: tag-pop 0.35s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
}
@keyframes tag-pop {
  0%   { opacity: 0; transform: scale(0); }
  70%  { transform: scale(1.12); }
  100% { opacity: 1; transform: scale(1); }
}

.card-time {
  font-size: 0.65rem;
  color: #444;
  margin-top: 2px;
}

/* ── Empty state ── */
.empty {
  grid-column: 1/-1;
  text-align: center;
  padding: 48px;
  color: #444;
  font-size: 0.9rem;
}
.empty-icon { font-size: 3rem; margin-bottom: 12px; }
</style>
</head>
<body>

<div class="header">
  <div class="pulse-dot"></div>
  <h1>Recognize <span>LLM</span> · Queue</h1>
  <div class="last-update" id="last-update">connecting…</div>
</div>

<div class="stats">
  <div class="stat pending">
    <div class="stat-num" id="s-pending">—</div>
    <div class="stat-label">Pending</div>
  </div>
  <div class="stat processing">
    <div class="stat-num" id="s-processing">—</div>
    <div class="stat-label">Processing</div>
  </div>
  <div class="stat done">
    <div class="stat-num" id="s-done">—</div>
    <div class="stat-label">Done</div>
  </div>
  <div class="stat failed">
    <div class="stat-num" id="s-failed">—</div>
    <div class="stat-label">Failed</div>
  </div>
</div>

<div class="progress-wrap">
  <div class="progress-label">
    <span id="prog-label">Loading…</span>
    <span id="prog-pct">—</span>
  </div>
  <div class="progress-bar">
    <div class="progress-fill" id="prog-fill" style="width:0%"></div>
  </div>
</div>

<div class="section-title">Recently processed</div>
<div class="grid" id="grid">
  <div class="empty"><div class="empty-icon">🔍</div>No results yet — queue is warming up.</div>
</div>

<script>
const API  = '__PROXY_BASE__/dashboard/api';
const NC   = '__NC_URL__';

// Tag palette — picks colour by hashing the tag string
const PALETTE = [
  ['#1971c2','#d0ebff'], ['#2f9e44','#d3f9d8'], ['#e67700','#fff3bf'],
  ['#c92a2a','#ffe3e3'], ['#6741d9','#e5dbff'], ['#0c8599','#c5f6fa'],
  ['#862e9c','#f3d9fa'], ['#5c940d','#e9fac8'], ['#1864ab','#dbe4ff'],
  ['#a61e4d','#ffdeeb'],
];
function tagColor(tag) {
  let h = 0;
  for (let i = 0; i < tag.length; i++) h = (Math.imul(31, h) + tag.charCodeAt(i)) | 0;
  const [bg, fg] = PALETTE[Math.abs(h) % PALETTE.length];
  return [bg, fg];
}

function relTime(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 5)   return 'just now';
  if (diff < 60)  return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return `${Math.floor(diff/86400)}d ago`;
}

function fmt(n) { return n.toLocaleString(); }

// ── Stats ────────────────────────────────────────────────────────────────────
const prev = {};
function updateStats(s) {
  for (const key of ['pending','processing','done','failed']) {
    const el = document.getElementById('s-' + key);
    const val = s[key] ?? 0;
    if (el.dataset.val !== String(val)) {
      el.textContent = fmt(val);
      el.dataset.val = val;
      el.classList.remove('bump');
      void el.offsetWidth;   // reflow to restart animation
      el.classList.add('bump');
    }
  }
  const total = s.total || 1;
  const done  = s.done  || 0;
  const pct   = Math.round(done / total * 100);
  document.getElementById('prog-fill').style.width  = pct + '%';
  document.getElementById('prog-pct').textContent   = pct + '%';
  document.getElementById('prog-label').textContent =
    `${fmt(done)} / ${fmt(total)} images processed`;
}

// ── Recent cards ─────────────────────────────────────────────────────────────
let knownIds = new Set();

function makeCard(item, isNew) {
  const thumbUrl = `${NC}/index.php/core/preview?fileId=${item.file_id}&x=260&y=195&a=true&forceIcon=0`;
  const name  = item.name || ('File #' + item.file_id);
  const tags  = item.tags || [];

  const card = document.createElement('div');
  card.className = 'card' + (isNew ? ' pop-in' : '');
  card.dataset.id = item.file_id;
  card.dataset.ts = item.processed_at;

  const tagHtml = tags.map((t, i) => {
    const [bg, fg] = tagColor(t);
    return `<span class="tag" style="background:${bg};color:${fg};animation-delay:${0.08 + i*0.05}s">${t}</span>`;
  }).join('');

  card.innerHTML = `
    <div class="card-thumb">
      <img src="${thumbUrl}" alt="" loading="lazy"
           onerror="this.parentNode.innerHTML='<div class=no-thumb>🖼</div>'">
    </div>
    <div class="card-body">
      <div class="card-name" title="${name}">${name}</div>
      <div class="card-user">by ${item.user_id}</div>
      <div class="card-desc">${item.description || '<em style="color:#444">no description</em>'}</div>
      <div class="card-tags">${tagHtml}</div>
      <div class="card-time">${relTime(item.processed_at)}</div>
    </div>`;

  // trigger tag animations after card pops in
  if (isNew) {
    setTimeout(() => {
      card.querySelectorAll('.tag').forEach(el => el.classList.add('tag-show'));
    }, 300);
  } else {
    card.querySelectorAll('.tag').forEach(el => {
      el.style.opacity = '1';
      el.style.transform = 'scale(1)';
    });
  }

  return card;
}

function updateRecent(items) {
  const grid = document.getElementById('grid');

  if (!items.length) {
    if (!knownIds.size) {
      grid.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div>No results yet — queue is warming up.</div>';
    }
    return;
  }

  // First render: build all cards without pop-in
  if (!knownIds.size) {
    grid.innerHTML = '';
    items.forEach(item => {
      knownIds.add(item.file_id);
      grid.appendChild(makeCard(item, false));
    });
    return;
  }

  // Subsequent renders: prepend new items and evict excess
  const newItems = items.filter(i => !knownIds.has(i.file_id));
  newItems.forEach(item => {
    knownIds.add(item.file_id);
    grid.prepend(makeCard(item, true));
  });

  // Update relative timestamps on existing cards
  grid.querySelectorAll('.card-time').forEach(el => {
    const card = el.closest('.card');
    const item = items.find(i => String(i.file_id) === card.dataset.id);
    if (item) el.textContent = relTime(item.processed_at);
  });

  // Remove cards that fell off the bottom of the list
  const currentIds = new Set(items.map(i => i.file_id));
  grid.querySelectorAll('.card').forEach(card => {
    if (!currentIds.has(Number(card.dataset.id))) {
      knownIds.delete(Number(card.dataset.id));
      card.remove();
    }
  });
}

// ── Polling ───────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const [s, r] = await Promise.all([
      fetch(API + '/status', {credentials: 'same-origin'}).then(x => x.json()),
      fetch(API + '/recent', {credentials: 'same-origin'}).then(x => x.json()),
    ]);
    updateStats(s);
    updateRecent(r);
    const t = new Date().toLocaleTimeString();
    document.getElementById('last-update').textContent = `Updated ${t}`;
    document.querySelector('.pulse-dot').style.background = '#51cf66';
  } catch(e) {
    document.getElementById('last-update').textContent = 'Error — retrying…';
    document.querySelector('.pulse-dot').style.background = '#ff6b6b';
  }
}

poll();
setInterval(poll, 4000);

// Refresh relative timestamps every 30s without a full re-fetch
setInterval(() => {
  document.querySelectorAll('.card-time').forEach(el => {
    const card = el.closest('.card');
    // stored in dataset for micro-updates
    if (card.dataset.ts) el.textContent = relTime(Number(card.dataset.ts));
  });
}, 30000);
</script>
</body>
</html>
"""


@router.get("/top_menu/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    html = _HTML.replace("__PROXY_BASE__", _PROXY_BASE).replace("__NC_URL__", _NC_URL)
    return HTMLResponse(html)
