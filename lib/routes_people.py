"""People (face grouping) review UI — JSON API + the injected front-end (M7).

All endpoints are user-scoped through ``nc.user`` (the logged-in Nextcloud user reaching us via the
AppAPI proxy), so a user only ever sees and edits their own people. Face crops are served from the
local SQLite store and never leave the server.
"""

from __future__ import annotations

import os
from typing import Annotated

import face_pipeline
import settings as settings_mod
from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import nc_app
from pydantic import BaseModel

router = APIRouter()

_APP_ID = os.environ.get("APP_ID", "recognize_llm")


def _require_user(nc: NextcloudApp) -> str | None:
    return nc.user or None


# ── JSON API ──────────────────────────────────────────────────────────────────

@router.get("/people/api/list")
async def api_people_list(nc: Annotated[NextcloudApp, Depends(nc_app)]) -> list:
    user = _require_user(nc)
    if not user:
        return []
    return face_pipeline.persons_summary(user)


@router.get("/people/api/faces/{person_id}")
async def api_person_faces(person_id: int, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> list:
    user = _require_user(nc)
    if not user:
        return []
    return face_pipeline.person_faces(user, person_id)


@router.get("/people/api/photos/{person_id}")
async def api_person_photos(person_id: int, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> dict:
    user = _require_user(nc)
    if not user:
        return {"total": 0, "photos": []}
    return face_pipeline.person_photos(user, person_id)


@router.get("/people/api/thumb/{face_id}")
async def api_face_thumb(face_id: int, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> Response:
    user = _require_user(nc)
    if not user:
        return Response(status_code=404)
    jpeg = face_pipeline.thumb_bytes(user, face_id)
    if not jpeg:
        return Response(status_code=404)
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


class NameReq(BaseModel):
    person_id: int
    name: str = ""


@router.post("/people/api/name")
async def api_name(req: NameReq, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> JSONResponse:
    user = _require_user(nc)
    if not user:
        return JSONResponse({"error": "no user"}, status_code=403)
    nc.set_user(user)
    return JSONResponse(face_pipeline.set_person_name(nc, user, req.person_id, req.name))


class MergeReq(BaseModel):
    source_id: int
    target_id: int


@router.post("/people/api/merge")
async def api_merge(req: MergeReq, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> JSONResponse:
    user = _require_user(nc)
    if not user:
        return JSONResponse({"error": "no user"}, status_code=403)
    nc.set_user(user)
    return JSONResponse(face_pipeline.merge_persons(nc, user, req.source_id, req.target_id))


class SplitReq(BaseModel):
    person_id: int
    face_ids: list[int]


@router.post("/people/api/split")
async def api_split(req: SplitReq, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> JSONResponse:
    user = _require_user(nc)
    if not user:
        return JSONResponse({"error": "no user"}, status_code=403)
    nc.set_user(user)
    return JSONResponse(face_pipeline.split_person(nc, user, req.person_id, req.face_ids))


class IgnoreReq(BaseModel):
    person_id: int
    ignored: bool = True


@router.post("/people/api/ignore")
async def api_ignore(req: IgnoreReq, nc: Annotated[NextcloudApp, Depends(nc_app)]) -> JSONResponse:
    user = _require_user(nc)
    if not user:
        return JSONResponse({"error": "no user"}, status_code=403)
    nc.set_user(user)
    return JSONResponse(face_pipeline.set_ignored(nc, user, req.person_id, req.ignored))


@router.post("/people/api/recluster")
async def api_recluster(
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    user = _require_user(nc)
    if not user:
        return JSONResponse({"error": "no user"}, status_code=403)
    cfg = settings_mod.load(nc)
    background_tasks.add_task(
        face_pipeline.cluster_and_tag, nc, [user], cfg.face_min_samples, cfg.face_match_min_similarity
    )
    return JSONResponse({"status": "started"})


# ── Loader JS (injected into NC's embedded page, mirrors the queue dashboard) ──

_LOADER_JS = r"""
(function () {
'use strict';

var PROXY = window.location.origin + '/index.php/apps/app_api/proxy/__APP_ID__';
var API   = PROXY + '/people/api';
var mergeSource = null;

var CSS = `
#rlm-people {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background:#1a1b1e; color:#e0e0e0; padding:24px; box-sizing:border-box; overflow-y:auto;
}
#rlm-people *, #rlm-people *::before, #rlm-people *::after { box-sizing:border-box; }
#rlm-people .hdr { display:flex; align-items:center; gap:12px; margin-bottom:24px; }
#rlm-people .hdr h1 { font-size:1.4rem; font-weight:600; color:#fff; margin:0; }
#rlm-people .hdr h1 span { color:#4dabf7; }
#rlm-people .sub { font-size:0.75rem; color:#666; margin-left:auto; }
#rlm-people .btn {
  font-size:0.72rem; padding:6px 14px; border-radius:20px; cursor:pointer;
  background:#25262b; color:#adb5bd; border:1px solid #34353b; transition:all .15s;
}
#rlm-people .btn:hover { background:#2c2d32; color:#fff; }
#rlm-people .btn.primary { background:rgba(77,171,247,.15); color:#4dabf7; border-color:rgba(77,171,247,.4); }
#rlm-people .stitle { font-size:0.7rem; text-transform:uppercase; letter-spacing:2px; color:#555; margin:24px 0 14px; }
#rlm-people .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:16px; }
#rlm-people .pcard {
  background:#25262b; border:1px solid #2c2d32; border-radius:14px; overflow:hidden;
  display:flex; flex-direction:column; transition:border-color .15s, transform .15s;
}
#rlm-people .pcard.merge-src { border-color:#4dabf7; box-shadow:0 0 0 2px rgba(77,171,247,.4); }
#rlm-people .pcard.dim { opacity:.5; }
#rlm-people .face { width:100%; aspect-ratio:1; background:#1a1b1e; overflow:hidden; position:relative; }
#rlm-people .face img { width:100%; height:100%; object-fit:cover; display:block; }
#rlm-people .noface { width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-size:3rem; color:#333; }
#rlm-people .cnt {
  position:absolute; bottom:6px; right:6px; background:rgba(0,0,0,.65); color:#fff;
  font-size:0.65rem; padding:2px 8px; border-radius:20px;
}
#rlm-people .pbody { padding:10px 12px; display:flex; flex-direction:column; gap:8px; }
#rlm-people .nameinput {
  width:100%; background:#1a1b1e; border:1px solid #34353b; border-radius:8px;
  color:#fff; font-size:0.82rem; padding:6px 8px; outline:none;
}
#rlm-people .nameinput:focus { border-color:#4dabf7; }
#rlm-people .nameinput::placeholder { color:#555; }
#rlm-people .acts { display:flex; gap:6px; flex-wrap:wrap; }
#rlm-people .mini {
  font-size:0.63rem; padding:3px 9px; border-radius:14px; cursor:pointer; border:1px solid #34353b;
  background:#2c2d32; color:#999; transition:all .12s;
}
#rlm-people .mini:hover { color:#fff; background:#34353b; }
#rlm-people .mini.warn:hover { background:rgba(255,107,107,.25); color:#ff6b6b; border-color:rgba(255,107,107,.4); }
#rlm-people .mini.go { color:#4dabf7; border-color:rgba(77,171,247,.4); }
#rlm-people .empty { grid-column:1/-1; text-align:center; padding:56px; color:#444; }
#rlm-people .empty-icon { font-size:3rem; margin-bottom:12px; }
/* modal */
#rlm-modal {
  position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:9999;
  display:flex; align-items:center; justify-content:center; padding:24px;
}
#rlm-modal .box { background:#25262b; border:1px solid #34353b; border-radius:16px; max-width:720px; width:100%; max-height:80vh; overflow-y:auto; padding:22px; }
#rlm-modal h2 { margin:0 0 6px; font-size:1.05rem; color:#fff; }
#rlm-modal p { margin:0 0 16px; font-size:0.78rem; color:#888; }
#rlm-modal .fgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(84px,1fr)); gap:10px; }
#rlm-modal .fitem { position:relative; aspect-ratio:1; border-radius:10px; overflow:hidden; cursor:pointer; border:2px solid transparent; }
#rlm-modal .fitem img { width:100%; height:100%; object-fit:cover; display:block; }
#rlm-modal .fitem.sel { border-color:#4dabf7; }
#rlm-modal .fitem.sel::after { content:'✓'; position:absolute; top:2px; right:5px; color:#4dabf7; font-weight:700; }
#rlm-modal .foot { display:flex; gap:10px; justify-content:flex-end; margin-top:18px; }
#rlm-modal .pgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; }
#rlm-modal .pitem { display:block; aspect-ratio:1; border-radius:10px; overflow:hidden; background:#1a1b1e; border:1px solid #2c2d32; }
#rlm-modal .pitem img { width:100%; height:100%; object-fit:cover; display:block; transition:transform .15s; }
#rlm-modal .pitem:hover img { transform:scale(1.06); }
`;

function el(tag, cls, html) { var e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e; }
function faceThumb(faceId, fileId) {
  var img = new Image();
  img.loading = 'lazy';
  img.src = API + '/thumb/' + faceId;
  img.onerror = function () {
    if (fileId && fileId >= 0) { img.onerror=null; img.src='/index.php/core/preview?fileId='+fileId+'&x=240&y=240&a=1'; }
    else { var d=el('div','noface','👤'); if(img.parentNode) img.parentNode.replaceChild(d,img); }
  };
  return img;
}

function mount() {
  var style=el('style'); style.textContent=CSS; document.head.appendChild(style);
  var root=el('div'); root.id='rlm-people';
  var content=document.getElementById('content')||document.body;
  content.innerHTML='';
  root.style.height=(window.innerHeight-content.getBoundingClientRect().top)+'px';
  content.appendChild(root);
  root.innerHTML =
    '<div class="hdr"><h1>Recognize <span>LLM</span> · People</h1>' +
    '<div class="sub" id="rlm-psub">loading…</div>' +
    '<button class="btn primary" id="rlm-recluster">Recluster now</button></div>' +
    '<div id="rlm-named"></div><div id="rlm-review"></div><div id="rlm-ignored"></div>';
  document.getElementById('rlm-recluster').addEventListener('click', recluster);
  load();
}

function recluster() {
  var b=document.getElementById('rlm-recluster');
  b.disabled=true; b.textContent='Clustering…';
  fetch(API+'/recluster',{method:'POST',credentials:'same-origin'})
    .then(function(){ setTimeout(function(){ b.disabled=false; b.textContent='Recluster now'; load(); }, 2500); })
    .catch(function(){ b.disabled=false; b.textContent='Recluster now'; });
}

function post(path, body) {
  return fetch(API+path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json();});
}

function load() {
  fetch(API+'/list',{credentials:'same-origin'}).then(function(r){return r.json();}).then(render)
    .catch(function(){ document.getElementById('rlm-psub').textContent='error loading'; });
}

function render(people) {
  var named=[], review=[], ignored=[];
  people.forEach(function(p){ if(p.ignored) ignored.push(p); else if(p.name) named.push(p); else review.push(p); });
  document.getElementById('rlm-psub').textContent = people.length+' people · '+
    people.reduce(function(a,p){return a+p.faces;},0)+' faces';

  section('rlm-named', named.length?'Named':'', named);
  section('rlm-review', 'To review'+(mergeSource?' — pick a card to merge into':''), review);
  section('rlm-ignored', ignored.length?'Ignored':'', ignored);
  if(!people.length)
    document.getElementById('rlm-review').innerHTML =
      '<div class="grid"><div class="empty"><div class="empty-icon">🧑‍🤝‍🧑</div>No people yet. Process some photos, then hit “Recluster now”.</div></div>';
}

function section(id, title, list) {
  var host=document.getElementById(id); host.innerHTML='';
  if(!list.length){ return; }
  if(title) host.appendChild(el('div','stitle',title));
  var grid=el('div','grid');
  list.forEach(function(p){ grid.appendChild(card(p)); });
  host.appendChild(grid);
}

function card(p) {
  var c=el('div','pcard'+(p.ignored?' dim':'')+(mergeSource===p.person_id?' merge-src':''));
  var face=el('div','face');
  if(p.sample_face_id>=0) face.appendChild(faceThumb(p.sample_face_id,p.sample_file_id));
  else face.appendChild(el('div','noface','👤'));
  face.appendChild(el('div','cnt',p.faces+' · '+p.files+' 🖼'));
  face.style.cursor='pointer'; face.title='View all photos of this person';
  face.addEventListener('click',function(){ openPhotos(p); });
  c.appendChild(face);

  var body=el('div','pbody');
  var input=el('input','nameinput'); input.type='text'; input.value=p.name||''; input.placeholder='Name this person…';
  input.addEventListener('keydown',function(e){ if(e.key==='Enter'){ input.blur(); } });
  input.addEventListener('blur',function(){
    if((input.value||'').trim()!==(p.name||'')) post('/name',{person_id:p.person_id,name:input.value.trim()}).then(load);
  });
  body.appendChild(input);

  var acts=el('div','acts');
  if(mergeSource && mergeSource!==p.person_id){
    var into=el('button','mini go','＋ merge here');
    into.addEventListener('click',function(){ post('/merge',{source_id:mergeSource,target_id:p.person_id}).then(function(){ mergeSource=null; load(); }); });
    acts.appendChild(into);
  } else if(mergeSource===p.person_id){
    var cancel=el('button','mini','cancel merge');
    cancel.addEventListener('click',function(){ mergeSource=null; render_reload(); });
    acts.appendChild(cancel);
  } else {
    var vw=el('button','mini go','📷 photos'); vw.addEventListener('click',function(){ openPhotos(p); }); acts.appendChild(vw);
    var mg=el('button','mini','merge'); mg.addEventListener('click',function(){ mergeSource=p.person_id; render_reload(); }); acts.appendChild(mg);
    var sp=el('button','mini','split'); sp.addEventListener('click',function(){ openSplit(p); }); acts.appendChild(sp);
    if(p.ignored){ var rs=el('button','mini','restore'); rs.addEventListener('click',function(){ post('/ignore',{person_id:p.person_id,ignored:false}).then(load); }); acts.appendChild(rs); }
    else { var ig=el('button','mini warn','not a person'); ig.addEventListener('click',function(){ post('/ignore',{person_id:p.person_id,ignored:true}).then(load); }); acts.appendChild(ig); }
  }
  body.appendChild(acts);
  c.appendChild(body);
  return c;
}

function render_reload() { load(); }

function openPhotos(p) {
  fetch(API+'/photos/'+p.person_id,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(data){
    var photos=data.photos||[];
    var modal=el('div'); modal.id='rlm-modal';
    var box=el('div','box'); box.style.maxWidth='920px';
    var title=(p.name||('Person '+p.person_id))+' — '+data.total+' photo'+(data.total===1?'':'s');
    box.appendChild(el('h2', title));
    box.appendChild(el('p','Every photo matched to this person. Click any photo to open it in Files.'
      + (data.total>photos.length ? ' Showing the '+photos.length+' clearest.' : '')));
    var pg=el('div','pgrid');
    photos.forEach(function(ph){
      var a=el('a','pitem'); a.href='/index.php/f/'+ph.file_id; a.target='_blank'; a.rel='noopener';
      var img=new Image(); img.loading='lazy';
      img.src='/index.php/core/preview?fileId='+ph.file_id+'&x=256&y=256&a=1';
      img.onerror=function(){ img.onerror=null; img.src=API+'/thumb/'+ph.face_id; };
      a.appendChild(img); pg.appendChild(a);
    });
    box.appendChild(pg);
    var foot=el('div','foot');
    var close=el('button','btn','Close'); close.addEventListener('click',function(){ modal.remove(); });
    foot.appendChild(close); box.appendChild(foot);
    modal.appendChild(box);
    modal.addEventListener('click',function(e){ if(e.target===modal) modal.remove(); });
    document.body.appendChild(modal);
  });
}

function openSplit(p) {
  fetch(API+'/faces/'+p.person_id,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(faces){
    var sel={};
    var modal=el('div'); modal.id='rlm-modal';
    var box=el('div','box');
    box.appendChild(el('h2','Split “'+(p.name||('person '+p.person_id))+'”'));
    box.appendChild(el('p','Select the faces that are NOT this person — they’ll be moved into a new person.'));
    var fg=el('div','fgrid');
    faces.forEach(function(f){
      var it=el('div','fitem'); it.appendChild(faceThumb(f.face_id,f.file_id));
      it.addEventListener('click',function(){ if(sel[f.face_id]){delete sel[f.face_id]; it.classList.remove('sel');} else {sel[f.face_id]=1; it.classList.add('sel');} });
      fg.appendChild(it);
    });
    box.appendChild(fg);
    var foot=el('div','foot');
    var cancel=el('button','btn','Cancel'); cancel.addEventListener('click',function(){ modal.remove(); });
    var ok=el('button','btn primary','Split selected'); ok.addEventListener('click',function(){
      var ids=Object.keys(sel).map(Number); if(!ids.length){ modal.remove(); return; }
      post('/split',{person_id:p.person_id,face_ids:ids}).then(function(){ modal.remove(); load(); });
    });
    foot.appendChild(cancel); foot.appendChild(ok); box.appendChild(foot);
    modal.appendChild(box);
    modal.addEventListener('click',function(e){ if(e.target===modal) modal.remove(); });
    document.body.appendChild(modal);
  });
}

if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',mount); else mount();
})();
"""


@router.get("/js/people-loader", response_class=PlainTextResponse)
@router.get("/js/people-loader.js", response_class=PlainTextResponse)
async def people_loader_js() -> PlainTextResponse:
    js = _LOADER_JS.replace("__APP_ID__", _APP_ID)
    return PlainTextResponse(js, media_type="application/javascript")


_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>People · Recognize LLM</title></head>
<body><script>window.location.href='/embedded/recognize_llm/people';</script></body></html>"""


@router.get("/top_menu/people", response_class=HTMLResponse)
async def people_page() -> HTMLResponse:
    return HTMLResponse(_HTML)
