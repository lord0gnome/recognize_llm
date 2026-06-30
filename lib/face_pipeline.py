"""Face detection, embedding extraction, DBSCAN clustering, and NC tag management.

Pipeline:
  1. ``extract_faces(image_bytes, user_id, file_id)`` — detect faces and persist
     ArcFace embeddings in the face_embeddings SQLite table. Called per image.
  2. ``cluster_and_tag(nc, users, min_samples)`` — load all stored embeddings for
     each user, run DBSCAN, create/update NC system tags, and assign them to photos.
     Tags are named ``person:<user_id>:<N>``; the user can rename them in NC.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
from nc_py_api.ex_app import LogLvl

import job_queue
from job_queue import _connect

if TYPE_CHECKING:
    from nc_py_api import NextcloudApp

# Lazy-loaded InsightFace model (downloads ~10 MB on first call).
_face_app = None


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


# ---------------------------------------------------------------------------
# Embedding extraction
# ---------------------------------------------------------------------------

def extract_faces(image_bytes: bytes, user_id: str, file_id: int) -> int:
    """Detect faces in *image_bytes* and upsert their embeddings into the DB.

    Returns the number of faces found (0 if the model is unavailable or the
    image has no detectable faces).
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
    with _connect() as con:
        con.execute(
            "DELETE FROM face_embeddings WHERE user_id=? AND file_id=?",
            (user_id, file_id),
        )
        for i, face in enumerate(faces):
            emb: np.ndarray = face.embedding.astype(np.float32)
            con.execute(
                "INSERT INTO face_embeddings "
                "(user_id, file_id, face_index, embedding, cluster_id, created_at) "
                "VALUES (?, ?, ?, ?, -1, ?)",
                (user_id, int(file_id), i, emb.tobytes(), now),
            )
    return len(faces)


# ---------------------------------------------------------------------------
# Clustering and NC tag assignment
# ---------------------------------------------------------------------------

def cluster_and_tag(nc: "NextcloudApp", users: list[str], min_samples: int = 3) -> dict:
    """Run per-user DBSCAN clustering and assign NC system tags.

    For each user:
      - Fetches all stored face embeddings.
      - Runs DBSCAN (cosine metric, eps=0.5).
      - Tries to reuse existing person-tag names by matching new clusters to old
        ones via the largest file-set overlap.
      - Creates missing NC system tags, assigns them to the right photos.
    Returns a summary dict {user_id: {"clusters": N, "tagged": M}}.
    """
    from sklearn.cluster import DBSCAN
    from nc_py_api._exceptions import NextcloudException

    summary: dict[str, dict] = {}

    for user_id in users:
        nc.set_user(user_id)
        rows = _load_embeddings(user_id)
        if len(rows) < min_samples:
            summary[user_id] = {"clusters": 0, "note": "too few faces"}
            continue

        embeddings = np.array(
            [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        )
        # L2-normalise for cosine distance
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.maximum(norms, 1e-10)

        labels = DBSCAN(eps=0.5, min_samples=min_samples, metric="cosine").fit_predict(embeddings)

        # Build new cluster → file_id sets
        new_clusters: dict[int, set[int]] = {}
        for row, label in zip(rows, labels):
            if label == -1:
                continue
            new_clusters.setdefault(label, set()).add(row["file_id"])

        if not new_clusters:
            summary[user_id] = {"clusters": 0, "note": "no stable clusters"}
            continue

        # Load previous mapping to preserve any user-assigned names
        old_map = _load_cluster_map(user_id)  # {old_cluster_id: (tag_name, file_id_set)}
        name_map = _match_clusters(new_clusters, old_map, user_id)  # {new_cluster_id: tag_name}

        # Persist new cluster assignments
        with _connect() as con:
            for row, label in zip(rows, labels):
                con.execute(
                    "UPDATE face_embeddings SET cluster_id=? WHERE id=?",
                    (int(label), row["id"]),
                )
            con.execute("DELETE FROM face_clusters WHERE user_id=?", (user_id,))
            for cid, name in name_map.items():
                file_ids_json = str(sorted(new_clusters[cid]))
                con.execute(
                    "INSERT INTO face_clusters (user_id, cluster_id, tag_name, file_ids_json) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, int(cid), name, file_ids_json),
                )

        tagged = 0
        for cid, file_ids in new_clusters.items():
            tag_name = name_map[cid]
            tag = _ensure_tag(nc, tag_name)
            for file_id in file_ids:
                node = nc.files.by_id(file_id)
                if node is None:
                    continue
                try:
                    nc.files.assign_tag(node, tag)
                    tagged += 1
                except NextcloudException as e:
                    if e.status_code != 409:
                        nc.log(LogLvl.WARNING, f"recognize_llm: tag assign failed: {e}")

        summary[user_id] = {"clusters": len(new_clusters), "tagged": tagged}
        nc.log(LogLvl.INFO, f"recognize_llm: face clustering {user_id}: {summary[user_id]}")

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_embeddings(user_id: str) -> list:
    with _connect() as con:
        return con.execute(
            "SELECT id, file_id, face_index, embedding, cluster_id "
            "FROM face_embeddings WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()


def _load_cluster_map(user_id: str) -> dict[int, tuple[str, set[int]]]:
    """Return {cluster_id: (tag_name, {file_ids})} from the last clustering run."""
    import json
    with _connect() as con:
        rows = con.execute(
            "SELECT cluster_id, tag_name, file_ids_json FROM face_clusters WHERE user_id=?",
            (user_id,),
        ).fetchall()
    result: dict[int, tuple[str, set[int]]] = {}
    for r in rows:
        try:
            fids = set(json.loads(r["file_ids_json"]))
        except Exception:
            fids = set()
        result[r["cluster_id"]] = (r["tag_name"], fids)
    return result


def _match_clusters(
    new_clusters: dict[int, set[int]],
    old_map: dict[int, tuple[str, set[int]]],
    user_id: str,
) -> dict[int, str]:
    """Assign tag names to new clusters, reusing old names where there is overlap."""
    used_names: set[str] = set()
    name_map: dict[int, str] = {}

    # Greedily match each new cluster to the old cluster with the most overlap.
    old_items = list(old_map.items())  # [(old_cid, (name, file_ids)), ...]
    for new_cid, new_files in sorted(new_clusters.items()):
        best_name = None
        best_overlap = 0
        for _, (old_name, old_files) in old_items:
            if old_name in used_names:
                continue
            overlap = len(new_files & old_files)
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = old_name
        if best_name and best_overlap > 0:
            name_map[new_cid] = best_name
            used_names.add(best_name)
        else:
            # Need a new name; find the lowest unused N.
            n = 1
            while True:
                candidate = f"person:{user_id}:{n}"
                if candidate not in used_names and not _name_in_old_map(candidate, old_map):
                    name_map[new_cid] = candidate
                    used_names.add(candidate)
                    break
                n += 1

    return name_map


def _name_in_old_map(name: str, old_map: dict) -> bool:
    return any(v[0] == name for v in old_map.values())


def _ensure_tag(nc, name: str):
    from nc_py_api._exceptions import NextcloudException
    tags = nc.files.list_tags()
    existing = next((t for t in tags if t.display_name.lower() == name.lower()), None)
    if existing:
        return existing
    try:
        nc.files.create_tag(name, user_visible=True, user_assignable=True)
    except NextcloudException as e:
        if e.status_code != 409:
            raise
    return nc.files.tag_by_name(name)
