"""Face detection, embeddings, stable person identity, and NC tag management (M7).

Everything here is **local**: faces are detected and embedded on-box with InsightFace/ArcFace and the
embeddings never leave the server (in particular they are never sent to the vision endpoint). The
LLM only ever sees the whole image for captioning — never a face crop.

Two ingest paths:

  1. ``extract_faces(nc, image_bytes, user_id, file_id, settings)`` — per image, on upload/backfill.
     Detects faces, stores each embedding + bounding box + a small JPEG crop, then **incrementally**
     matches every new face against existing person centroids and, on a hit, tags the file straight
     away so freshly uploaded photos are grouped in real time.

  2. ``cluster_and_tag(nc, users, ...)`` — periodic full re-cluster. Runs DBSCAN over each user's
     embeddings and reconciles the resulting clusters with the persisted ``face_persons`` by centroid
     similarity, so a person keeps the same ``person_id`` (and therefore its name, merges and splits)
     run after run.

The review UI drives the person-management helpers (``set_person_name`` / ``merge_persons`` /
``split_person`` / ``set_ignored``) and the read helpers (``persons_summary`` / ``person_faces`` /
``thumb_bytes``).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import numpy as np
from nc_py_api.ex_app import LogLvl

from job_queue import _connect

if TYPE_CHECKING:
    from nc_py_api import NextcloudApp

# Lazy-loaded InsightFace model (downloads ~10 MB on first call).
_face_app = None

_THUMB_PX = 112          # face-crop thumbnail edge length shown in the review UI
_DEFAULT_MIN_SIM = 0.5   # cosine similarity to accept a face into an existing person


def _model():
    global _face_app
    if _face_app is None:
        import insightface
        from nc_py_api.ex_app import persistent_storage
        _face_app = insightface.app.FaceAnalysis(
            name="buffalo_sc",
            root=persistent_storage(),
            providers=["CPUExecutionProvider"],
            allowed_modules=["detection", "recognition"],
        )
        _face_app.prepare(ctx_id=-1, det_size=(640, 640))
    return _face_app


def _min_similarity(settings) -> float:
    return getattr(settings, "face_match_min_similarity", _DEFAULT_MIN_SIM) if settings else _DEFAULT_MIN_SIM


def _normalize(v: np.ndarray) -> np.ndarray:
    """Return the L2-normalised vector (safe for zero vectors)."""
    n = np.linalg.norm(v)
    return v / n if n > 1e-10 else v


# ---------------------------------------------------------------------------
# Embedding extraction + incremental (real-time) matching
# ---------------------------------------------------------------------------

def extract_faces(nc: "NextcloudApp", image_bytes: bytes, user_id: str, file_id: int, settings=None) -> int:
    """Detect faces in *image_bytes*, persist them, and incrementally tag known people.

    Returns the number of faces found (0 if the model is unavailable or no face is detected).
    Never raises — face grouping must never break captioning.
    """
    try:
        import cv2
        app = _model()
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return 0
        faces = app.get(img)
    except Exception:
        return 0

    now = int(time.time())
    new_faces: list[tuple[int, np.ndarray]] = []  # (face_row_id, normalized embedding)

    try:
        with _connect() as con:
            con.execute("DELETE FROM face_embeddings WHERE user_id=? AND file_id=?", (user_id, file_id))
            for i, face in enumerate(faces):
                emb = face.embedding.astype(np.float32)
                bbox = [int(x) for x in face.bbox.tolist()]
                det = float(getattr(face, "det_score", 0.0) or 0.0)
                cur = con.execute(
                    "INSERT INTO face_embeddings "
                    "(user_id, file_id, face_index, embedding, cluster_id, created_at, bbox, det_score) "
                    "VALUES (?, ?, ?, ?, -1, ?, ?, ?)",
                    (user_id, int(file_id), i, emb.tobytes(), now, json.dumps(bbox), det),
                )
                face_row_id = cur.lastrowid
                thumb = _crop_thumb(img, bbox)
                if thumb is not None:
                    con.execute(
                        "INSERT OR REPLACE INTO face_thumbs (face_id, jpeg) VALUES (?, ?)",
                        (face_row_id, thumb),
                    )
                new_faces.append((face_row_id, _normalize(emb)))
    except Exception:
        return len(faces)

    try:
        _match_new_faces(nc, user_id, file_id, new_faces, _min_similarity(settings))
    except Exception as e:
        nc.log(LogLvl.WARNING, f"recognize_llm: incremental face match failed on {file_id}: {e}")

    return len(faces)


def _crop_thumb(img, bbox: list[int]) -> bytes | None:
    """Crop *img* (cv2 BGR) to the face bbox with padding and return a small JPEG."""
    try:
        import cv2
        h, w = img.shape[:2]
        x1, y1, x2, y2 = bbox
        pad_x = int((x2 - x1) * 0.25)
        pad_y = int((y2 - y1) * 0.25)
        x1 = max(0, x1 - pad_x); y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x); y2 = min(h, y2 + pad_y)
        if x2 <= x1 or y2 <= y1:
            return None
        crop = img[y1:y2, x1:x2]
        crop = cv2.resize(crop, (_THUMB_PX, _THUMB_PX), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _match_new_faces(nc, user_id: str, file_id: int, new_faces, min_sim: float) -> None:
    """Assign each new face to the closest existing (non-ignored) person, then tag the file."""
    if not new_faces:
        return
    persons = _load_person_centroids(user_id)  # [(person_id, tag_id, centroid_np)]
    if not persons:
        return
    matrix = np.array([c for (_pid, _tid, c) in persons])  # (P, D), already normalised
    matched: dict[int, int] = {}  # person_id -> tag_id
    with _connect() as con:
        for face_row_id, emb in new_faces:
            sims = matrix @ emb
            best = int(np.argmax(sims))
            if sims[best] < min_sim:
                continue
            pid, tag_id, _c = persons[best]
            con.execute("UPDATE face_embeddings SET cluster_id=? WHERE id=?", (int(pid), face_row_id))
            matched[pid] = tag_id
    for pid, tag_id in matched.items():
        if tag_id >= 0:
            _assign_tag_id(nc, file_id, tag_id)
    if matched:
        _refresh_persons(user_id, matched.keys())


# ---------------------------------------------------------------------------
# Full re-clustering with stable person ids
# ---------------------------------------------------------------------------

def cluster_and_tag(nc: "NextcloudApp", users: list[str], min_samples: int = 3, min_similarity: float | None = None) -> dict:
    """Re-cluster every user's faces and reconcile with persisted persons (stable ids).

    Returns {user_id: {"persons": N, "tagged": M}}.
    """
    from sklearn.cluster import DBSCAN

    min_sim = _DEFAULT_MIN_SIM if min_similarity is None else min_similarity
    eps = max(0.05, 1.0 - min_sim)  # DBSCAN cosine *distance* threshold
    summary: dict[str, dict] = {}

    for user_id in users:
        nc.set_user(user_id)
        rows = _load_embeddings(user_id)
        if len(rows) < min_samples:
            summary[user_id] = {"persons": 0, "note": "too few faces"}
            continue

        embeddings = np.array([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        embeddings = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-10)

        labels = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit_predict(embeddings)

        raw: dict[int, list[int]] = {}
        for idx, label in enumerate(labels):
            if label != -1:
                raw.setdefault(int(label), []).append(idx)
        if not raw:
            summary[user_id] = {"persons": 0, "note": "no stable clusters"}
            continue

        raw_centroids = {lbl: _normalize(embeddings[members].mean(axis=0)) for lbl, members in raw.items()}
        existing = _load_persons(user_id)  # {person_id: row dict}
        assignment = _reconcile(user_id, raw_centroids, existing, min_sim)  # {raw_label: person_id}

        with _connect() as con:
            con.execute("UPDATE face_embeddings SET cluster_id=-1 WHERE user_id=?", (user_id,))
            for lbl, members in raw.items():
                pid = assignment[lbl]
                for idx in members:
                    con.execute("UPDATE face_embeddings SET cluster_id=? WHERE id=?", (int(pid), rows[idx]["id"]))

        tagged = 0
        for lbl, members in raw.items():
            pid = assignment[lbl]
            prev = existing.get(pid, {})
            name = prev.get("name", "") if prev else ""
            ignored = bool(prev.get("ignored", 0)) if prev else False
            best_idx = max(members, key=lambda i: rows[i]["det_score"])
            sample_face_id = rows[best_idx]["id"]
            file_ids = sorted({rows[i]["file_id"] for i in members})

            tag_id = -1
            if not ignored:
                tag = _ensure_person_tag(nc, user_id, pid, name, prev.get("tag_id", -1) if prev else -1)
                tag_id = tag.tag_id
                for fid in file_ids:
                    if _assign_tag_id(nc, fid, tag_id):
                        tagged += 1

            _upsert_person(
                user_id, pid, name=name, tag_id=tag_id,
                centroid=raw_centroids[lbl], sample_face_id=sample_face_id,
                face_count=len(members), ignored=1 if ignored else 0,
            )

        _prune_empty_persons(user_id, keep=set(assignment.values()))
        summary[user_id] = {"persons": len(set(assignment.values())), "tagged": tagged}
        nc.log(LogLvl.INFO, f"recognize_llm: face clustering {user_id}: {summary[user_id]}")

    return summary


def _reconcile(user_id: str, raw_centroids: dict[int, np.ndarray], existing: dict[int, dict], min_sim: float) -> dict[int, int]:
    """Map each raw DBSCAN label to a person_id, reusing existing persons by centroid similarity."""
    assignment: dict[int, int] = {}
    used: set[int] = set()

    ex_items = [
        (pid, np.frombuffer(row["centroid"], dtype=np.float32))
        for pid, row in existing.items()
        if row.get("centroid")
    ]

    pairs: list[tuple[float, int, int]] = []
    for lbl, cen in raw_centroids.items():
        for pid, ex_cen in ex_items:
            pairs.append((float(np.dot(cen, ex_cen)), lbl, pid))
    pairs.sort(reverse=True)
    for sim, lbl, pid in pairs:
        if sim < min_sim:
            break
        if lbl in assignment or pid in used:
            continue
        assignment[lbl] = pid
        used.add(pid)

    for lbl in raw_centroids:
        if lbl not in assignment:
            assignment[lbl] = _alloc_person_id(user_id)
    return assignment


# ---------------------------------------------------------------------------
# Person management (driven by the review UI)
# ---------------------------------------------------------------------------

def set_person_name(nc, user_id: str, person_id: int, name: str) -> dict:
    """Set (or clear, when *name* is empty) a person's display name and rename its NC tag in place."""
    row = _get_person(user_id, person_id)
    if not row:
        return {"error": "no such person"}
    name = name.strip()
    tag = _ensure_person_tag(nc, user_id, person_id, name, row["tag_id"])
    with _connect() as con:
        con.execute(
            "UPDATE face_persons SET name=?, tag_id=?, updated_at=? WHERE user_id=? AND person_id=?",
            (name, tag.tag_id, int(time.time()), user_id, person_id),
        )
    return {"ok": True, "person_id": person_id, "name": name}


def merge_persons(nc, user_id: str, source_id: int, target_id: int) -> dict:
    """Move all of *source* into *target*: re-point faces, retag files, drop the source tag/person."""
    if source_id == target_id:
        return {"error": "cannot merge a person into itself"}
    src = _get_person(user_id, source_id)
    tgt = _get_person(user_id, target_id)
    if not src or not tgt:
        return {"error": "no such person"}

    src_files = _person_file_ids(user_id, source_id)
    with _connect() as con:
        con.execute(
            "UPDATE face_embeddings SET cluster_id=? WHERE user_id=? AND cluster_id=?",
            (target_id, user_id, source_id),
        )
    # Retag the moved files with the target tag; drop the source tag entirely.
    if tgt["tag_id"] >= 0:
        for fid in src_files:
            _assign_tag_id(nc, fid, tgt["tag_id"])
    if src["tag_id"] >= 0:
        _delete_tag(nc, src["tag_id"])
    with _connect() as con:
        con.execute("DELETE FROM face_persons WHERE user_id=? AND person_id=?", (user_id, source_id))
    _recompute_centroid(user_id, target_id)
    return {"ok": True, "target_id": target_id}


def split_person(nc, user_id: str, person_id: int, face_ids: list[int]) -> dict:
    """Detach *face_ids* from *person* into a brand-new person (basic manual split)."""
    row = _get_person(user_id, person_id)
    if not row:
        return {"error": "no such person"}
    face_ids = [int(f) for f in face_ids]
    if not face_ids:
        return {"error": "no faces selected"}

    new_id = _alloc_person_id(user_id)
    # Which files are affected, so we can drop the old tag from those that lose all their faces.
    affected_files = _files_of_faces(user_id, face_ids)
    with _connect() as con:
        qmarks = ",".join("?" * len(face_ids))
        con.execute(
            f"UPDATE face_embeddings SET cluster_id=? WHERE user_id=? AND id IN ({qmarks})",
            (new_id, user_id, *face_ids),
        )

    new_tag = _ensure_person_tag(nc, user_id, new_id, "", -1)
    for fid in affected_files:
        _assign_tag_id(nc, fid, new_tag.tag_id)
        # Old tag comes off a file only if no face of the old person remains on it.
        if row["tag_id"] >= 0 and not _file_has_person(user_id, fid, person_id):
            _unassign_tag_id(nc, fid, row["tag_id"])

    _upsert_person(user_id, new_id, name="", tag_id=new_tag.tag_id, centroid=None,
                   sample_face_id=face_ids[0], face_count=0, ignored=0)
    _recompute_centroid(user_id, new_id)
    _recompute_centroid(user_id, person_id)
    return {"ok": True, "new_person_id": new_id}


def set_ignored(nc, user_id: str, person_id: int, ignored: bool) -> dict:
    """Mark a person as ignored (not a real person / clutter) and strip its tag from files."""
    row = _get_person(user_id, person_id)
    if not row:
        return {"error": "no such person"}
    if ignored and row["tag_id"] >= 0:
        for fid in _person_file_ids(user_id, person_id):
            _unassign_tag_id(nc, fid, row["tag_id"])
    with _connect() as con:
        con.execute(
            "UPDATE face_persons SET ignored=?, updated_at=? WHERE user_id=? AND person_id=?",
            (1 if ignored else 0, int(time.time()), user_id, person_id),
        )
    return {"ok": True, "ignored": ignored}


# ---------------------------------------------------------------------------
# Read helpers for the review UI
# ---------------------------------------------------------------------------

def persons_summary(user_id: str) -> list[dict]:
    """One row per person with live face/file counts, sorted by size (biggest first)."""
    with _connect() as con:
        persons = con.execute(
            "SELECT person_id, name, tag_id, sample_face_id, ignored FROM face_persons WHERE user_id=?",
            (user_id,),
        ).fetchall()
        counts = {
            r["cluster_id"]: (r["faces"], r["files"])
            for r in con.execute(
                "SELECT cluster_id, COUNT(*) faces, COUNT(DISTINCT file_id) files "
                "FROM face_embeddings WHERE user_id=? AND cluster_id>=0 GROUP BY cluster_id",
                (user_id,),
            ).fetchall()
        }
        sample_file = {
            r["id"]: r["file_id"]
            for r in con.execute(
                "SELECT id, file_id FROM face_embeddings WHERE user_id=? AND id IN "
                "(SELECT sample_face_id FROM face_persons WHERE user_id=?)",
                (user_id, user_id),
            ).fetchall()
        }
    out = []
    for p in persons:
        faces, files = counts.get(p["person_id"], (0, 0))
        if faces == 0 and not p["ignored"]:
            continue  # empty, non-ignored persons are noise between cluster runs
        out.append({
            "person_id": p["person_id"],
            "name": p["name"],
            "tag_id": p["tag_id"],
            "faces": faces,
            "files": files,
            "ignored": bool(p["ignored"]),
            "sample_face_id": p["sample_face_id"],
            "sample_file_id": sample_file.get(p["sample_face_id"], -1),
        })
    out.sort(key=lambda d: (d["ignored"], -d["faces"]))
    return out


def person_faces(user_id: str, person_id: int, limit: int = 60) -> list[dict]:
    """Member faces of a person (for the split/review grid), sharpest first."""
    with _connect() as con:
        rows = con.execute(
            "SELECT id, file_id, det_score FROM face_embeddings "
            "WHERE user_id=? AND cluster_id=? ORDER BY det_score DESC LIMIT ?",
            (user_id, person_id, limit),
        ).fetchall()
    return [{"face_id": r["id"], "file_id": r["file_id"]} for r in rows]


def thumb_bytes(user_id: str, face_id: int) -> bytes | None:
    """Stored JPEG face crop for *face_id*, or None. Scoped to *user_id* to prevent cross-user reads."""
    with _connect() as con:
        row = con.execute(
            "SELECT t.jpeg FROM face_thumbs t JOIN face_embeddings e ON e.id=t.face_id "
            "WHERE t.face_id=? AND e.user_id=?",
            (int(face_id), user_id),
        ).fetchone()
    return row["jpeg"] if row else None


# ---------------------------------------------------------------------------
# Low-level DB + tag helpers
# ---------------------------------------------------------------------------

def _load_embeddings(user_id: str) -> list:
    with _connect() as con:
        return con.execute(
            "SELECT id, file_id, face_index, embedding, cluster_id, det_score "
            "FROM face_embeddings WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()


def _load_persons(user_id: str) -> dict[int, dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT person_id, name, tag_id, centroid, sample_face_id, face_count, ignored "
            "FROM face_persons WHERE user_id=?",
            (user_id,),
        ).fetchall()
    return {r["person_id"]: dict(r) for r in rows}


def _load_person_centroids(user_id: str) -> list[tuple[int, int, np.ndarray]]:
    """(person_id, tag_id, centroid) for every non-ignored person that has a centroid."""
    with _connect() as con:
        rows = con.execute(
            "SELECT person_id, tag_id, centroid FROM face_persons "
            "WHERE user_id=? AND ignored=0 AND centroid IS NOT NULL",
            (user_id,),
        ).fetchall()
    return [(r["person_id"], r["tag_id"], np.frombuffer(r["centroid"], dtype=np.float32)) for r in rows]


def _get_person(user_id: str, person_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT person_id, name, tag_id, sample_face_id, ignored FROM face_persons "
            "WHERE user_id=? AND person_id=?",
            (user_id, person_id),
        ).fetchone()
    return dict(row) if row else None


def _alloc_person_id(user_id: str) -> int:
    """Return a fresh, never-reused person_id for *user_id*."""
    with _connect() as con:
        con.execute(
            "INSERT INTO face_meta (user_id, next_person_id) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        row = con.execute("SELECT next_person_id FROM face_meta WHERE user_id=?", (user_id,)).fetchone()
        pid = int(row["next_person_id"])
        con.execute("UPDATE face_meta SET next_person_id=? WHERE user_id=?", (pid + 1, user_id))
    return pid


def _upsert_person(user_id, person_id, *, name, tag_id, centroid, sample_face_id, face_count, ignored) -> None:
    blob = centroid.astype(np.float32).tobytes() if centroid is not None else None
    with _connect() as con:
        con.execute(
            """
            INSERT INTO face_persons
                (user_id, person_id, name, tag_id, centroid, sample_face_id, face_count, ignored, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, person_id) DO UPDATE SET
                name=excluded.name, tag_id=excluded.tag_id, centroid=excluded.centroid,
                sample_face_id=excluded.sample_face_id, face_count=excluded.face_count,
                ignored=excluded.ignored, updated_at=excluded.updated_at
            """,
            (user_id, person_id, name, tag_id, blob, sample_face_id, face_count, ignored, int(time.time())),
        )


def _refresh_persons(user_id: str, person_ids) -> None:
    """Cheap face_count refresh after incremental assignment (no centroid recompute)."""
    with _connect() as con:
        for pid in set(person_ids):
            n = con.execute(
                "SELECT COUNT(*) c FROM face_embeddings WHERE user_id=? AND cluster_id=?",
                (user_id, pid),
            ).fetchone()["c"]
            con.execute(
                "UPDATE face_persons SET face_count=?, updated_at=? WHERE user_id=? AND person_id=?",
                (n, int(time.time()), user_id, pid),
            )


def _recompute_centroid(user_id: str, person_id: int) -> None:
    """Recompute a person's centroid, face_count and representative face from its current members."""
    with _connect() as con:
        rows = con.execute(
            "SELECT id, embedding, det_score FROM face_embeddings WHERE user_id=? AND cluster_id=?",
            (user_id, person_id),
        ).fetchall()
        if not rows:
            con.execute(
                "UPDATE face_persons SET face_count=0, centroid=NULL WHERE user_id=? AND person_id=?",
                (user_id, person_id),
            )
            return
        embs = np.array([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        embs = embs / np.maximum(np.linalg.norm(embs, axis=1, keepdims=True), 1e-10)
        centroid = _normalize(embs.mean(axis=0)).astype(np.float32)
        best = max(rows, key=lambda r: r["det_score"])
        con.execute(
            "UPDATE face_persons SET centroid=?, face_count=?, sample_face_id=?, updated_at=? "
            "WHERE user_id=? AND person_id=?",
            (centroid.tobytes(), len(rows), best["id"], int(time.time()), user_id, person_id),
        )


def _prune_empty_persons(user_id: str, keep: set[int]) -> None:
    """Delete persons that ended a cluster run with no members (except ignored ones)."""
    with _connect() as con:
        rows = con.execute(
            "SELECT person_id FROM face_persons WHERE user_id=? AND ignored=0", (user_id,)
        ).fetchall()
        for r in rows:
            pid = r["person_id"]
            if pid in keep:
                continue
            has = con.execute(
                "SELECT 1 FROM face_embeddings WHERE user_id=? AND cluster_id=? LIMIT 1",
                (user_id, pid),
            ).fetchone()
            if not has:
                con.execute("DELETE FROM face_persons WHERE user_id=? AND person_id=?", (user_id, pid))


def _person_file_ids(user_id: str, person_id: int) -> list[int]:
    with _connect() as con:
        rows = con.execute(
            "SELECT DISTINCT file_id FROM face_embeddings WHERE user_id=? AND cluster_id=?",
            (user_id, person_id),
        ).fetchall()
    return [r["file_id"] for r in rows]


def _files_of_faces(user_id: str, face_ids: list[int]) -> list[int]:
    with _connect() as con:
        qmarks = ",".join("?" * len(face_ids))
        rows = con.execute(
            f"SELECT DISTINCT file_id FROM face_embeddings WHERE user_id=? AND id IN ({qmarks})",
            (user_id, *face_ids),
        ).fetchall()
    return [r["file_id"] for r in rows]


def _file_has_person(user_id: str, file_id: int, person_id: int) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM face_embeddings WHERE user_id=? AND file_id=? AND cluster_id=? LIMIT 1",
            (user_id, file_id, person_id),
        ).fetchone()
    return row is not None


# ── NC system-tag helpers ─────────────────────────────────────────────────────

def _person_tag_name(user_id: str, person_id: int, name: str) -> str:
    """The NC system-tag label for a person: ``person:<name>`` when named, else ``person:<user>:<id>``."""
    return f"person:{name}" if name else f"person:{user_id}:{person_id}"


def _ensure_person_tag(nc, user_id: str, person_id: int, name: str, tag_id: int):
    """Return the SystemTag for a person, renaming an existing tag in place when possible."""
    from nc_py_api._exceptions import NextcloudException
    desired = _person_tag_name(user_id, person_id, name)

    # Rename the existing tag in place (preserves all file assignments).
    if tag_id is not None and tag_id >= 0:
        try:
            existing = next((t for t in nc.files.list_tags() if t.tag_id == tag_id), None)
        except Exception:
            existing = None
        if existing is not None:
            if existing.display_name == desired:
                return existing
            try:
                nc.files.update_tag(tag_id, name=desired)
                return _tag_by_name(nc, desired) or existing
            except NextcloudException as e:
                if e.status_code != 409:  # 409 => a tag named `desired` already exists; fall through
                    raise
                found = _tag_by_name(nc, desired)
                if found:
                    return found
    return _create_or_get_tag(nc, desired)


def _create_or_get_tag(nc, name: str):
    from nc_py_api._exceptions import NextcloudException
    found = _tag_by_name(nc, name)
    if found:
        return found
    try:
        nc.files.create_tag(name, user_visible=True, user_assignable=True)
    except NextcloudException as e:
        if e.status_code != 409:
            raise
    return _tag_by_name(nc, name)


def _tag_by_name(nc, name: str):
    return next((t for t in nc.files.list_tags() if t.display_name == name), None)


def _assign_tag_id(nc, file_id: int, tag_id: int) -> bool:
    """Assign a system tag to a file by id. Returns True on a fresh assignment, False if noop/failed."""
    from nc_py_api._exceptions import NextcloudException
    try:
        nc.files.assign_tag(int(file_id), int(tag_id))
        return True
    except NextcloudException as e:
        if e.status_code not in (404, 409):
            nc.log(LogLvl.WARNING, f"recognize_llm: tag assign failed on {file_id}: {e}")
        return False
    except Exception:
        return False


def _unassign_tag_id(nc, file_id: int, tag_id: int) -> None:
    from nc_py_api._exceptions import NextcloudException
    try:
        nc.files.unassign_tag(int(file_id), int(tag_id))
    except NextcloudException as e:
        if e.status_code not in (404, 409):
            nc.log(LogLvl.WARNING, f"recognize_llm: tag unassign failed on {file_id}: {e}")
    except Exception:
        pass


def _delete_tag(nc, tag_id: int) -> None:
    try:
        nc.files.delete_tag(int(tag_id))
    except Exception:
        pass
