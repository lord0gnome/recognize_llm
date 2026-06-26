"""Dashboard page and its JSON API endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

import job_queue

router = APIRouter()

_NC_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")
_APP_ID = os.environ.get("APP_ID", "recognize_llm")
_PROXY_BASE = f"{_NC_URL}/index.php/apps/app_api/proxy/{_APP_ID}"


# ── JSON API ──────────────────────────────────────────────────────────────────

@router.get("/dashboard/api/status")
async def api_status() -> dict:
    return job_queue.status()


@router.get("/dashboard/api/recent")
async def api_recent() -> list:
    return job_queue.get_recent(20)


# ── Loader JS — runs inline in NC's embedded page, no iframe needed ───────────
# NC sets frame-ancestors 'none' on all proxy responses, so we can't use an
# iframe. Instead this script injects the full dashboard UI directly into #content.

_LOADER_JS = r"""
(function () {
'use strict';

var PROXY = window.location.origin + '/index.php/apps/app_api/proxy/__APP_ID__';
var API   = PROXY + '/dashboard/api';

/* ── Scoped CSS ──────────────────────────────────────────────────────────── */
var CSS = `
#rlm-dash {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #1a1b1e;
  color: #e0e0e0;
  padding: 24px;
  box-sizing: border-box;
  height: calc(100vh - var(--header-height, 50px));
  overflow-y: auto;
}
#rlm-dash *, #rlm-dash *::before, #rlm-dash *::after { box-sizing: border-box; }

#rlm-dash .hdr {
  display: flex; align-items: center; gap: 12px; margin-bottom: 28px;
}
#rlm-dash .hdr h1 {
  font-size: 1.4rem; font-weight: 600; color: #fff; margin: 0;
}
#rlm-dash .hdr h1 span { color: #4dabf7; }
#rlm-dash .dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: #51cf66; flex-shrink: 0;
  animation: rlm-pulse 2s ease-in-out infinite;
}
@keyframes rlm-pulse {
  0%,100% { transform:scale(1); opacity:1; }
  50%     { transform:scale(1.4); opacity:0.6; }
}
#rlm-dash .lup { margin-left: auto; font-size: 0.75rem; color: #666; }

#rlm-dash .stats {
  display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; margin-bottom: 24px;
}
#rlm-dash .stat {
  background: #25262b; border-radius: 12px; padding: 20px 16px;
  text-align: center; border: 1px solid #2c2d32; position: relative; overflow: hidden;
}
#rlm-dash .stat::before {
  content:''; position:absolute; inset:0 0 auto 0; height:3px; border-radius:12px 12px 0 0;
}
#rlm-dash .stat.pending::before    { background:#868e96; }
#rlm-dash .stat.processing::before { background:#4dabf7; }
#rlm-dash .stat.done::before       { background:#51cf66; }
#rlm-dash .stat.failed::before     { background:#ff6b6b; }
#rlm-dash .snum {
  font-size:2.4rem; font-weight:700; line-height:1; letter-spacing:-1px;
}
#rlm-dash .stat.pending   .snum { color:#adb5bd; }
#rlm-dash .stat.processing .snum { color:#4dabf7; }
#rlm-dash .stat.done      .snum { color:#51cf66; }
#rlm-dash .stat.failed    .snum { color:#ff6b6b; }
#rlm-dash .slbl {
  font-size:0.7rem; text-transform:uppercase; letter-spacing:1px; color:#666; margin-top:6px;
}
#rlm-dash .snum.bump { animation: rlm-bump 0.4s cubic-bezier(0.34,1.56,0.64,1); }
@keyframes rlm-bump {
  0%   { transform:scale(1); }
  40%  { transform:scale(1.25); }
  100% { transform:scale(1); }
}

#rlm-dash .pwrap {
  background:#25262b; border-radius:8px; padding:14px 16px;
  margin-bottom:28px; border:1px solid #2c2d32;
}
#rlm-dash .plbl {
  display:flex; justify-content:space-between; font-size:0.75rem; color:#888; margin-bottom:8px;
}
#rlm-dash .pbar { height:8px; background:#2c2d32; border-radius:4px; overflow:hidden; }
#rlm-dash .pfill {
  height:100%; background:linear-gradient(90deg,#4dabf7,#51cf66);
  border-radius:4px; transition:width 0.8s cubic-bezier(0.4,0,0.2,1); min-width:2px;
}

#rlm-dash .stitle {
  font-size:0.7rem; text-transform:uppercase; letter-spacing:2px; color:#555; margin-bottom:16px;
}
#rlm-dash .grid {
  display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); gap:16px;
}
#rlm-dash .card {
  background:#25262b; border-radius:14px; border:1px solid #2c2d32;
  display:flex; overflow:hidden; transform-origin:top center;
}
#rlm-dash .card.pop {
  animation: rlm-pop 0.5s cubic-bezier(0.34,1.56,0.64,1) both;
}
@keyframes rlm-pop {
  0%   { transform:scale(0.7) translateY(-12px); opacity:0; }
  60%  { transform:scale(1.03) translateY(2px); }
  100% { transform:scale(1) translateY(0); opacity:1; }
}
#rlm-dash .thumb {
  width:130px; flex-shrink:0; background:#1a1b1e; overflow:hidden;
}
#rlm-dash .thumb img {
  width:100%; height:100%; object-fit:cover; display:block; transition:transform 0.3s;
}
#rlm-dash .card:hover .thumb img { transform:scale(1.06); }
#rlm-dash .nothumb {
  width:100%; height:100%; display:flex; align-items:center; justify-content:center;
  font-size:2.5rem; color:#333;
}
#rlm-dash .cbody {
  padding:14px 16px; flex:1; min-width:0; display:flex; flex-direction:column; gap:6px;
}
#rlm-dash .cname {
  font-size:0.85rem; font-weight:600; color:#fff;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
#rlm-dash .cuser { font-size:0.7rem; color:#555; }
#rlm-dash .cdesc {
  font-size:0.78rem; color:#aaa; line-height:1.45; flex:1;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
}
#rlm-dash .ctags { display:flex; flex-wrap:wrap; gap:5px; margin-top:4px; }
#rlm-dash .tag {
  font-size:0.65rem; padding:2px 8px; border-radius:20px; font-weight:500;
  opacity:0; transform:scale(0);
}
#rlm-dash .tag.show { animation: rlm-tpop 0.35s cubic-bezier(0.34,1.56,0.64,1) forwards; }
@keyframes rlm-tpop {
  0%   { opacity:0; transform:scale(0); }
  70%  { transform:scale(1.12); }
  100% { opacity:1; transform:scale(1); }
}
#rlm-dash .ctime { font-size:0.65rem; color:#444; margin-top:2px; }
#rlm-dash .empty {
  grid-column:1/-1; text-align:center; padding:48px; color:#444; font-size:0.9rem;
}
#rlm-dash .empty-icon { font-size:3rem; margin-bottom:12px; }
`;

/* ── HTML skeleton ───────────────────────────────────────────────────────── */
var HTML = `
<div class="hdr">
  <div class="dot" id="rlm-dot"></div>
  <h1>Recognize <span>LLM</span> \xB7 Queue</h1>
  <div class="lup" id="rlm-lup">connecting…</div>
</div>
<div class="stats">
  <div class="stat pending">  <div class="snum" id="rlm-pending">—</div>  <div class="slbl">Pending</div></div>
  <div class="stat processing"><div class="snum" id="rlm-processing">—</div><div class="slbl">Processing</div></div>
  <div class="stat done">     <div class="snum" id="rlm-done">—</div>     <div class="slbl">Done</div></div>
  <div class="stat failed">   <div class="snum" id="rlm-failed">—</div>   <div class="slbl">Failed</div></div>
</div>
<div class="pwrap">
  <div class="plbl"><span id="rlm-plbl">Loading…</span><span id="rlm-ppct">—</span></div>
  <div class="pbar"><div class="pfill" id="rlm-pfill" style="width:0%"></div></div>
</div>
<div class="stitle">Recently processed</div>
<div class="grid" id="rlm-grid">
  <div class="empty"><div class="empty-icon">🔍</div>No results yet — queue is warming up.</div>
</div>
`;

/* ── Mount ───────────────────────────────────────────────────────────────── */
function mount() {
  var style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  var root = document.createElement('div');
  root.id = 'rlm-dash';
  root.innerHTML = HTML;

  var content = document.getElementById('content') || document.body;
  content.innerHTML = '';
  // NC's layout constrains #content to the viewport below the topbar (overflow:hidden).
  // height:100% doesn't resolve if #content has no explicit height (flex/position sizing).
  // Measure the real available height from #content's top edge to the viewport bottom,
  // then size #rlm-dash to exactly that so overflow-y:auto has a concrete container to scroll within.
  var availH = window.innerHeight - content.getBoundingClientRect().top;
  root.style.height = availH + 'px';
  root.style.overflowY = 'auto';
  content.appendChild(root);

  startPolling();
}

/* ── Helpers ─────────────────────────────────────────────────────────────── */
var PALETTE = [
  ['#1971c2','#d0ebff'],['#2f9e44','#d3f9d8'],['#e67700','#fff3bf'],
  ['#c92a2a','#ffe3e3'],['#6741d9','#e5dbff'],['#0c8599','#c5f6fa'],
  ['#862e9c','#f3d9fa'],['#5c940d','#e9fac8'],['#1864ab','#dbe4ff'],
  ['#a61e4d','#ffdeeb'],
];
function tagColor(tag) {
  var h = 0;
  for (var i = 0; i < tag.length; i++) h = (Math.imul(31, h) + tag.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}
function relTime(ts) {
  var d = Math.floor(Date.now()/1000) - ts;
  if (d < 5)    return 'just now';
  if (d < 60)   return d + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  if (d < 86400)return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}
function fmt(n) { return n.toLocaleString(); }

/* ── Stats update ────────────────────────────────────────────────────────── */
function updateStats(s) {
  ['pending','processing','done','failed'].forEach(function(k) {
    var el = document.getElementById('rlm-' + k);
    var val = s[k] || 0;
    if (el && el.dataset.val !== String(val)) {
      el.textContent = fmt(val);
      el.dataset.val = val;
      el.classList.remove('bump');
      void el.offsetWidth;
      el.classList.add('bump');
    }
  });
  var total = s.total || 1, done = s.done || 0;
  var pct = Math.round(done / total * 100);
  var pf = document.getElementById('rlm-pfill');
  if (pf) pf.style.width = pct + '%';
  var pp = document.getElementById('rlm-ppct');
  if (pp) pp.textContent = pct + '%';
  var pl = document.getElementById('rlm-plbl');
  if (pl) pl.textContent = fmt(done) + ' / ' + fmt(total) + ' images processed';
}

/* ── Cards ───────────────────────────────────────────────────────────────── */
var knownIds = new Set();

function makeCard(item, isNew) {
  var thumbUrl = '/index.php/core/preview?fileId=' + item.file_id + '&x=260&y=195&a=true&forceIcon=0';
  var name = item.name || ('File #' + item.file_id);
  var tags = item.tags || [];

  var card = document.createElement('div');
  card.className = 'card' + (isNew ? ' pop' : '');
  card.dataset.id = item.file_id;
  card.dataset.ts = item.processed_at;

  var tagHtml = tags.map(function(t, i) {
    var c = tagColor(t);
    return '<span class="tag" style="background:' + c[0] + ';color:' + c[1] +
           ';animation-delay:' + (0.08 + i*0.05) + 's">' + t + '</span>';
  }).join('');

  var desc = item.description
    ? item.description.replace(/</g,'&lt;').replace(/>/g,'&gt;')
    : '<em style="color:#444">no description</em>';

  card.innerHTML =
    '<div class="thumb">' +
      '<img src="' + thumbUrl + '" alt="" loading="lazy" ' +
           'onerror="this.parentNode.innerHTML=\'<div class=nothumb>🖼</div>\'">' +
    '</div>' +
    '<div class="cbody">' +
      '<div class="cname" title="' + name + '">' + name + '</div>' +
      '<div class="cuser">by ' + item.user_id + '</div>' +
      '<div class="cdesc">' + desc + '</div>' +
      '<div class="ctags">' + tagHtml + '</div>' +
      '<div class="ctime">' + relTime(item.processed_at) + '</div>' +
    '</div>';

  if (isNew) {
    setTimeout(function() {
      card.querySelectorAll('.tag').forEach(function(el) { el.classList.add('show'); });
    }, 300);
  } else {
    card.querySelectorAll('.tag').forEach(function(el) {
      el.style.opacity = '1'; el.style.transform = 'scale(1)';
    });
  }
  return card;
}

function updateRecent(items) {
  var grid = document.getElementById('rlm-grid');
  if (!grid) return;

  if (!items.length) {
    if (!knownIds.size)
      grid.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div>No results yet.</div>';
    return;
  }

  if (!knownIds.size) {
    grid.innerHTML = '';
    items.forEach(function(item) { knownIds.add(item.file_id); grid.appendChild(makeCard(item,false)); });
    return;
  }

  items.filter(function(i) { return !knownIds.has(i.file_id); }).forEach(function(item) {
    knownIds.add(item.file_id);
    grid.prepend(makeCard(item, true));
  });

  var curIds = new Set(items.map(function(i) { return i.file_id; }));
  grid.querySelectorAll('.card').forEach(function(card) {
    var item = items.find(function(i) { return String(i.file_id) === card.dataset.id; });
    if (item) card.querySelector('.ctime').textContent = relTime(item.processed_at);
    if (!curIds.has(Number(card.dataset.id))) { knownIds.delete(Number(card.dataset.id)); card.remove(); }
  });
}

/* ── Polling ─────────────────────────────────────────────────────────────── */
function startPolling() {
  function poll() {
    Promise.all([
      fetch(API + '/status', {credentials:'same-origin'}).then(function(r){return r.json();}),
      fetch(API + '/recent', {credentials:'same-origin'}).then(function(r){return r.json();}),
    ]).then(function(results) {
      updateStats(results[0]);
      updateRecent(results[1]);
      var lup = document.getElementById('rlm-lup');
      if (lup) lup.textContent = 'Updated ' + new Date().toLocaleTimeString();
      var dot = document.getElementById('rlm-dot');
      if (dot) dot.style.background = '#51cf66';
    }).catch(function() {
      var lup = document.getElementById('rlm-lup');
      if (lup) lup.textContent = 'Error — retrying…';
      var dot = document.getElementById('rlm-dot');
      if (dot) dot.style.background = '#ff6b6b';
    });
  }

  poll();
  setInterval(poll, 4000);
  setInterval(function() {
    document.querySelectorAll('#rlm-dash .card').forEach(function(card) {
      if (card.dataset.ts) card.querySelector('.ctime').textContent = relTime(Number(card.dataset.ts));
    });
  }, 30000);
}

/* ── Boot ────────────────────────────────────────────────────────────────── */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mount);
} else {
  mount();
}

})();
"""


@router.get("/js/dashboard-loader.js", response_class=PlainTextResponse)
async def dashboard_loader_js() -> PlainTextResponse:
    js = _LOADER_JS.replace("__APP_ID__", _APP_ID)
    return PlainTextResponse(js, media_type="application/javascript")


# ── Standalone HTML page (direct proxy access, optional) ─────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Queue · Recognize LLM</title>
</head>
<body>
<script>
// Redirect to the embedded NC page instead of showing raw proxy content
window.location.href = '/embedded/recognize_llm/dashboard';
</script>
</body>
</html>
"""


@router.get("/top_menu/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(_HTML)
