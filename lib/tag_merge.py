"""Tag consolidation: persistence, analyze/apply background runners, and the runtime alias map.

Pipeline (see tag_consolidation for the pure logic):
  analyze  — LLM-condense the tag vocabulary in chunks -> proposed pairs in the queue DB
  review   — admin vetoes individual pairs (dashboard), then approves or discards the run
  approve  — folds the approved mapping into tag_aliases (future captions canonical immediately)
  apply    — resumable background rewrite of existing Nextcloud tags (renames + collision merges)
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET

import job_queue
import settings as settings_mod
import tag_consolidation as tc
import vision_client
from nc_py_api import NextcloudApp, NextcloudException
from nc_py_api.ex_app import LogLvl

class _Stopped(Exception):
    """Raised inside runners when the admin requested stop (or a newer run superseded this one)."""


_lock = threading.Lock()
_state = {
    "running": False,
    "gen": 0,          # generation token: a stale runner must never clobber a newer run's state
    "mode": "",            # "analyze" | "apply"
    "run_id": 0,
    "chunks_done": 0,
    "chunks_total": 0,
    "pairs_done": 0,
    "pairs_total": 0,
    "files_retagged": 0,
    "current": "",
    "error": "",
}


def state() -> dict:
    with _lock:
        return dict(_state)


def request_stop() -> None:
    _state["running"] = False


def _log(nc, lvl, msg: str) -> None:
    try:
        nc.log(lvl, f"recognize_llm: {msg}")
    except Exception:
        pass


def _resolve_users(nc: NextcloudApp) -> list[str]:
    try:
        users = nc.users.get_list()
        if users:
            return users
    except Exception:
        pass
    return [nc.user] if nc.user else []


def _resolve_users_strict(nc_ref: dict, gen: int) -> list[str]:
    """User set for APPLY. delete_tag cascades assignments for ALL users server-side, so a
    partial user list means silent cross-user data loss — refuse to guess."""
    listed: set[str] = set()
    try:
        listed = set(_nc_retry(nc_ref, gen, lambda nc: nc.users.get_list() or []))
    except _Stopped:
        raise
    except Exception:
        pass  # provisioning scope may be unavailable; the jobs table is the backstop
    known: set[str] = set()
    try:
        known = set(getattr(job_queue, "known_user_ids", lambda: [])())
    except Exception:
        pass
    if not listed and not known:
        raise RuntimeError(
            "cannot resolve the user list (no provisioning access and empty jobs table) — "
            "refusing to apply merges that would delete tags for unseen users")
    caller = nc_ref["nc"].user
    return sorted(listed | known | ({caller} if caller else set()))


# ── store ────────────────────────────────────────────────────────────────────

def latest_run() -> dict | None:
    with job_queue._connect() as con:
        row = con.execute("SELECT * FROM tag_merge_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def _set_run(run_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    with job_queue._connect() as con:
        con.execute(f"UPDATE tag_merge_runs SET {sets} WHERE id=?", (*fields.values(), run_id))


def get_proposal(run_id: int | None = None) -> dict:
    run = latest_run() if run_id is None else None
    if run_id is None:
        if not run:
            return {"run_id": 0, "phase": "none", "groups": []}
        run_id = run["id"]
    with job_queue._connect() as con:
        if run is None:
            r = con.execute("SELECT * FROM tag_merge_runs WHERE id=?", (run_id,)).fetchone()
            run = dict(r) if r else {"phase": "none"}
        rows = [dict(r) for r in con.execute(
            "SELECT source, canonical, source_count, canonical_count, reason, status, error "
            "FROM tag_merge_pairs WHERE run_id=?", (run_id,))]
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["canonical"], {
            "canonical": r["canonical"], "canonical_count": r["canonical_count"],
            "impact": 0, "sources": [],
        })
        g["impact"] += r["source_count"]
        g["sources"].append({k: r[k] for k in ("source", "source_count", "reason", "status", "error")})
    ordered = sorted(groups.values(), key=lambda g: -g["impact"])
    for g in ordered:
        g["sources"].sort(key=lambda s: -s["source_count"])
    return {"run_id": run_id, "phase": run.get("phase", "none"), "groups": ordered}


def set_pair_rejected(source: str, rejected: bool) -> bool:
    run = latest_run()
    if not run or run["phase"] != "proposed":
        return False  # after approval the mapping is frozen; discard the run to back out
    new_status = "rejected" if rejected else "proposed"
    with job_queue._connect() as con:
        cur = con.execute(
            "UPDATE tag_merge_pairs SET status=?, updated_at=? "
            "WHERE run_id=? AND source=? AND status IN ('proposed', 'rejected')",
            (new_status, int(time.time()), run["id"], source))
    return cur.rowcount > 0


def _rebuild_aliases() -> dict[str, str]:
    """Recompute tag_aliases from every effective (approved/applying/applied) run in order.
    Single source of truth: approve adds a run, discard removes one — both just rebuild."""
    with job_queue._connect() as con:
        run_ids = [r["id"] for r in con.execute(
            "SELECT id FROM tag_merge_runs WHERE phase IN ('approved','applying','applied') "
            "ORDER BY id")]
        folded: dict[str, str] = {}
        for rid in run_ids:
            m = {r["source"]: r["canonical"] for r in con.execute(
                "SELECT source, canonical FROM tag_merge_pairs WHERE run_id=? "
                "AND status IN ('approved','applied')", (rid,))}
            folded = tc.compose_aliases(folded, m)
        now = int(time.time())
        con.execute("DELETE FROM tag_aliases")
        con.executemany(
            "INSERT INTO tag_aliases (source, canonical, run_id, created_at) VALUES (?,?,0,?)",
            [(s, c, now) for s, c in folded.items()])
    invalidate_alias_cache()
    return folded


def approve_run() -> int:
    """Promote proposed pairs to approved and fold them into the persistent alias map."""
    run = latest_run()
    if not run or run["phase"] != "proposed":
        return 0
    now = int(time.time())
    with job_queue._connect() as con:
        con.execute(
            "UPDATE tag_merge_pairs SET status='approved', updated_at=? "
            "WHERE run_id=? AND status='proposed'", (now, run["id"]))
        approved = con.execute(
            "SELECT COUNT(*) FROM tag_merge_pairs WHERE run_id=? AND status='approved'",
            (run["id"],)).fetchone()[0]
        con.execute("UPDATE tag_merge_runs SET phase='approved' WHERE id=?", (run["id"],))
    _rebuild_aliases()
    return approved


def discard_run() -> bool:
    run = latest_run()
    if not run or run["phase"] not in ("proposed", "failed", "approved"):
        return False
    _set_run(run["id"], phase="discarded", finished_at=int(time.time()))
    _rebuild_aliases()  # discarding an approved run must also retire its aliases
    return True


def load_aliases() -> dict[str, str]:
    with job_queue._connect() as con:
        return {r["source"]: r["canonical"]
                for r in con.execute("SELECT source, canonical FROM tag_aliases")}


_alias_cache: dict = {"at": 0.0, "map": {}}
_ALIAS_TTL = 60.0


def get_alias_map() -> dict[str, str]:
    """Cheap, never-failing alias map for the caption choke point."""
    now = time.time()
    if now - _alias_cache["at"] > _ALIAS_TTL:
        try:
            _alias_cache["map"] = load_aliases()
        except Exception:
            pass  # keep last known map
        _alias_cache["at"] = now
    return _alias_cache["map"]


def invalidate_alias_cache() -> None:
    _alias_cache["at"] = 0.0


# ── tag usage counts (one PROPFIND per user) ─────────────────────────────────

_COUNTS_BODY = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
    'xmlns:nc="http://nextcloud.org/ns"><d:prop><oc:id/><nc:files-assigned/></d:prop></d:propfind>'
)
_NS = {"d": "DAV:", "oc": "http://owncloud.org/ns", "nc": "http://nextcloud.org/ns"}


def fetch_tag_counts(nc: NextcloudApp, users: list[str]) -> dict[int, int]:
    """Per-tag file counts summed across users via /systemtags-assigned (advisory only —
    failures leave counts at 0 and analysis proceeds)."""
    counts: dict[int, int] = {}
    for uid in users:
        try:
            nc.set_user(uid)
            resp = nc._session.adapter_dav.request(
                "PROPFIND", "/systemtags-assigned", data=_COUNTS_BODY,
                headers={"Depth": "1", "Content-Type": "application/xml"})
            if resp.status_code >= 400:
                continue
            root = ET.fromstring(resp.text)
            for response in root.findall("d:response", _NS):
                tid = response.findtext(".//oc:id", default="", namespaces=_NS)
                num = response.findtext(".//nc:files-assigned", default="", namespaces=_NS)
                if tid and num:
                    counts[int(tid)] = counts.get(int(tid), 0) + int(num)
        except Exception:
            continue
    return counts


# ── analyze ──────────────────────────────────────────────────────────────────

def _active(gen: int) -> bool:
    return _state["running"] and _state["gen"] == gen


def _chat_with_retry(cfg, prompt: str, gen: int) -> dict:
    """LLM call with never-stuck semantics: backend-down waits forever (stop-aware);
    malformed output retries 3x then raises."""
    last: Exception | None = None
    for attempt in range(3):
        delay = 15.0
        while _active(gen):
            try:
                return vision_client.chat_json(cfg, prompt, max_tokens=6000)
            except vision_client.VisionUnavailable:
                time.sleep(min(delay, 300.0))
                delay *= 2
            except vision_client.VisionError as e:
                last = e
                time.sleep(5.0)
                break  # next attempt
        if not _active(gen):
            raise _Stopped()
    raise last if last else RuntimeError("chat failed")


def _analyze(nc: NextcloudApp, include_uppercase: bool) -> None:
    with _lock:
        if _state["running"]:
            return
        _state["gen"] += 1
        gen = _state["gen"]
        _state.update(running=True, mode="analyze", chunks_done=0, chunks_total=0,
                      pairs_done=0, pairs_total=0, files_retagged=0, current="", error="")
    run_id = 0
    try:
        cfg = settings_mod.load(nc)
        raw = [(t.tag_id, t.display_name, t.user_visible, t.user_assignable)
               for t in nc.files.list_tags()]
        vocab = tc.filter_vocabulary(raw, include_uppercase)
        id_by_name = {name: tid for tid, name in vocab}
        users = _resolve_users(nc)
        counts_by_id = fetch_tag_counts(nc, users)
        counts = {name: counts_by_id.get(tid, 0) for tid, name in vocab}
        chunks = tc.chunk_vocabulary([(name, counts[name]) for _, name in vocab])

        now = int(time.time())
        with job_queue._connect() as con:
            cur = con.execute(
                "INSERT INTO tag_merge_runs (started_at, phase, include_uppercase, tags_total, "
                "chunks_total) VALUES (?, 'analyzing', ?, ?, ?)",
                (now, int(include_uppercase), len(vocab), len(chunks)))
            run_id = cur.lastrowid
        _state.update(run_id=run_id, chunks_total=len(chunks))
        _log(nc, LogLvl.INFO, f"tag analyze started: {len(vocab)} tags in {len(chunks)} chunks")

        canonical_seen = set(load_aliases().values())
        canonicals = sorted(canonical_seen)
        vocab_names = set(id_by_name)
        all_pairs: list[tuple[str, str, str]] = []
        for i, chunk in enumerate(chunks):
            if not _active(gen):
                raise _Stopped()
            _state["current"] = f"chunk {i + 1}/{len(chunks)}"
            data = _chat_with_retry(cfg, tc.build_chunk_prompt(chunk, canonicals), gen)
            pairs = tc.sanitize_pairs(tc.parse_merge_response(data), vocab_names,
                                      include_uppercase)
            all_pairs.extend(pairs)
            for _, canonical, _r in pairs:
                if canonical not in canonical_seen:
                    canonical_seen.add(canonical)
                    canonicals.append(canonical)
            _state["chunks_done"] = i + 1
            _set_run(run_id, chunks_done=i + 1)

        resolved = tc.resolve_mapping(all_pairs, counts)
        reasons = {s: r for s, _c, r in all_pairs}
        now = int(time.time())
        with job_queue._connect() as con:
            con.executemany(
                "INSERT OR REPLACE INTO tag_merge_pairs (run_id, source, canonical, "
                "source_tag_id, canonical_tag_id, source_count, canonical_count, reason, "
                "status, updated_at) VALUES (?,?,?,?,?,?,?,?,'proposed',?)",
                [(run_id, s, c, id_by_name.get(s, -1), id_by_name.get(c, -1),
                  counts.get(s, 0), counts.get(c, 0), reasons.get(s, ""), now)
                 for s, c in resolved.items()])
        _set_run(run_id, phase="proposed")
        _log(nc, LogLvl.INFO, f"tag analyze proposed {len(resolved)} merges (run {run_id})")
    except _Stopped:
        if run_id:
            _set_run(run_id, phase="failed", error="stopped by admin",
                     finished_at=int(time.time()))
    except Exception as e:
        if run_id:
            _set_run(run_id, phase="failed", error=str(e)[:500], finished_at=int(time.time()))
        if _state["gen"] == gen:
            _state["error"] = str(e)
        _log(nc, LogLvl.ERROR, f"tag analyze failed: {e}")
    finally:
        with _lock:
            if _state["gen"] == gen:
                _state.update(running=False, current="")


# ── apply ────────────────────────────────────────────────────────────────────

def _nc_retry(nc_ref: dict, gen: int, fn):
    """Run fn(nc) with the queue's transient-error policy: infra failures back off (stop-aware)
    with a fresh session and retry forever; job-level errors raise to the caller. A requested
    stop raises _Stopped (never marks the pair failed — it stays resumable)."""
    delay = job_queue.TRANSIENT_BACKOFF_BASE
    while True:
        try:
            return fn(nc_ref["nc"])
        except Exception as e:
            if not job_queue._is_transient_infra_error(e):
                raise
            if not _active(gen):
                raise _Stopped() from e
            time.sleep(min(delay, job_queue.TRANSIENT_BACKOFF_CAP))
            delay *= 2
            try:
                nc_ref["nc"] = NextcloudApp()
            except Exception:
                pass


def _apply_pair(nc_ref: dict, pair: dict, users: list[str], admin_uid: str, gen: int) -> str:
    """Apply one approved merge; returns final status ('applied')."""
    source, canonical = pair["source"], pair["canonical"]
    if tc._excluded_name(source) or tc._excluded_name(canonical):
        raise ValueError("refusing excluded tag name")

    def _as_admin(fn):
        # Tag admin ops (list/rename/delete) must run in the initiating admin's context — the
        # per-user collision loop below changes the session user, and a _nc_retry rebuild
        # resets it to none at all.
        def run(nc):
            if admin_uid:
                nc.set_user(admin_uid)
            return fn(nc)
        return run

    tags = _nc_retry(nc_ref, gen, _as_admin(lambda nc: nc.files.list_tags()))
    by_exact = {t.display_name: t for t in tags}
    src = by_exact.get(source)
    if src is None:
        return "applied"  # already gone/merged; alias still guards the future
    dst = by_exact.get(canonical)
    if dst is None:
        # Case-insensitive fallback MUST exclude the source itself: for a case-only merge
        # (Bicycle -> bicycle) matching src here would send us down the collision path, whose
        # final delete_tag would destroy the only copy of the tag. Rename handles that case.
        dst = next((t for t in tags if t.display_name.casefold() == canonical.casefold()
                    and t.tag_id != src.tag_id), None)

    if dst is None:
        # Pure rename (including case-only changes): preserves every assignment in one call.
        try:
            _nc_retry(nc_ref, gen,
                      _as_admin(lambda nc: nc.files.update_tag(src.tag_id, name=canonical)))
            return "applied"
        except NextcloudException as e:
            if e.status_code != 409:
                raise
            tags = _nc_retry(nc_ref, gen, _as_admin(lambda nc: nc.files.list_tags()))
            dst = next((t for t in tags if t.display_name == canonical
                        and t.tag_id != src.tag_id), None)
            if dst is None:
                raise

    # Collision merge: re-tag every reachable file, then drop the source tag (cascades mappings).
    for uid in users:
        if not _active(gen):
            raise _Stopped()
        _state["current"] = f"{source} -> {canonical} ({uid})"

        def _files(nc, uid=uid):
            nc.set_user(uid)
            return nc.files.list_by_criteria(tags=[src.tag_id])

        files = _nc_retry(nc_ref, gen, _files)
        for f in files:
            fid = int(f.info.fileid)

            def _assign(nc, fid=fid, uid=uid):
                nc.set_user(uid)
                try:
                    nc.files.assign_tag(fid, dst.tag_id)
                except NextcloudException as e:
                    if e.status_code not in (403, 404, 409):
                        raise  # 409 already tagged; 404 gone; 403 read-only share (owner covers)

            _nc_retry(nc_ref, gen, _assign)
            _state["files_retagged"] += 1
    _nc_retry(nc_ref, gen, _as_admin(lambda nc: nc.files.delete_tag(src.tag_id)))
    return "applied"


def _apply(nc: NextcloudApp) -> None:
    with _lock:
        if _state["running"]:
            return
        _state["gen"] += 1
        gen = _state["gen"]
        _state.update(running=True, mode="apply", pairs_done=0, files_retagged=0,
                      current="", error="")
    nc_ref = {"nc": nc}
    admin_uid = nc.user or ""
    run = latest_run()
    try:
        if not run or run["phase"] not in ("approved", "applying"):
            _state["error"] = "no approved run to apply"
            return
        run_id = run["id"]
        _state["run_id"] = run_id
        _set_run(run_id, phase="applying")
        with job_queue._connect() as con:
            # 'failed' pairs are retried on every re-trigger (never-stuck: nothing is
            # permanently parked by a transient hiccup — genuinely bad pairs fail again fast).
            pairs = [dict(r) for r in con.execute(
                "SELECT * FROM tag_merge_pairs WHERE run_id=? AND status IN ('approved','failed') "
                "ORDER BY (canonical_tag_id != -1), source_count DESC", (run_id,))]
        users = _resolve_users_strict(nc_ref, gen)
        _state["pairs_total"] = len(pairs)
        _log(nc, LogLvl.INFO,
             f"tag apply started: {len(pairs)} merges, {len(users)} users (run {run_id})")

        for pair in pairs:
            if not _active(gen):
                raise _Stopped()
            try:
                status = _apply_pair(nc_ref, pair, users, admin_uid, gen)
                err = ""
            except _Stopped:
                raise  # stopped mid-pair; assigns are idempotent, safe to resume later
            except Exception as e:
                status, err = "failed", str(e)[:500]
            with job_queue._connect() as con:
                con.execute(
                    "UPDATE tag_merge_pairs SET status=?, error=?, updated_at=? "
                    "WHERE run_id=? AND source=?",
                    (status, err, int(time.time()), run_id, pair["source"]))
            _state["pairs_done"] += 1

        with job_queue._connect() as con:
            left = con.execute(
                "SELECT COUNT(*) FROM tag_merge_pairs WHERE run_id=? AND status='approved'",
                (run_id,)).fetchone()[0]
        if left == 0:
            _set_run(run_id, phase="applied", finished_at=int(time.time()))
        _log(nc_ref["nc"], LogLvl.INFO,
             f"tag apply finished: {_state['pairs_done']} pairs, "
             f"{_state['files_retagged']} files re-tagged, {left} left approved")
    except _Stopped:
        pass  # resumable: re-trigger continues with remaining approved/failed rows
    except Exception as e:
        if _state["gen"] == gen:
            _state["error"] = str(e)
        _log(nc_ref["nc"], LogLvl.ERROR, f"tag apply failed: {e}")
    finally:
        with _lock:
            if _state["gen"] == gen:
                _state.update(running=False, current="")


def pair_counts() -> dict:
    run = latest_run()
    if not run:
        return {}
    with job_queue._connect() as con:
        return {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM tag_merge_pairs WHERE run_id=? GROUP BY status",
            (run["id"],))}
