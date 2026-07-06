# Recognize LLM

A Nextcloud **exApp** that generates descriptive metadata for images using a locally-hosted
[llama.cpp](https://github.com/ggml-org/llama.cpp) vision model (any OpenAI-compatible
`/v1/chat/completions` endpoint with multimodal support).

For every new upload — and, on demand, for an existing library via **backfill** — it sends the image
to your local vision endpoint and writes the result back into Nextcloud as:

- **System tags** (collaborative tags) — searchable & filterable in the Files app.
- **A free-text description** — stored as a dead DAV property and (optionally) a visible comment.

Original image files are **never modified**. The app also registers a **TaskProcessing provider**
(`recognize_llm:image2text`) so other Nextcloud AI features can reuse the same engine.

## Architecture

One vision engine ([`lib/processor.py`](lib/processor.py)), three entry points:

1. **Uploads** — AppAPI pushes `NodeCreatedEvent`/`NodeWrittenEvent` to `/events/node`
   ([`lib/routes_events.py`](lib/routes_events.py)) → enqueue.
2. **Backfill** — `POST /backfill/start` crawls each user's images and enqueues them
   ([`lib/routes_backfill.py`](lib/routes_backfill.py)); resumable via a per-file etag marker.
3. **TaskProcessing** — background loop pulls tasks and captions them
   ([`lib/task_provider.py`](lib/task_provider.py)).

A persistent SQLite queue + worker pool ([`lib/job_queue.py`](lib/job_queue.py)) drains jobs at a
controlled concurrency so the local model is never flooded, and survives restarts.

## Prerequisites

- A running llama.cpp vision server, e.g.:
  ```
  llama-server -m model.gguf --mmproj mmproj.gguf --host 0.0.0.0 --port 8080
  ```
- Nextcloud with **AppAPI** installed and a deploy daemon registered. In `nextcloud-docker-dev` the
  `appapi-dsp` proxy is already in `docker-compose.yml`.

> The vision endpoint must be reachable **from the exApp container**. Use the container-network host
> (e.g. `http://host.docker.internal:8080` or the host gateway IP), not `localhost`.

## Configuration

Settings live in Nextcloud app-config and can be seeded via environment variables at registration:

| Key | Env var | Default |
|-----|---------|---------|
| llama_url | `LLAMA_URL` | `http://host.docker.internal:8080` |
| llama_model | `LLAMA_MODEL` | _(empty = server default)_ |
| api_key | `LLAMA_API_KEY` | _(empty)_ |
| max_tokens | `MAX_TOKENS` | `1024` |
| prompt | `LLAMA_PROMPT` | built-in JSON-tagging prompt |
| max_tags | `MAX_TAGS` | `8` |
| mimetypes | `MIMETYPES` | `image/jpeg,image/png,image/webp,image/heic,…` |
| concurrency | `CONCURRENCY` | `1` |
| request_timeout | `REQUEST_TIMEOUT` | `180` |
| write_comment | `WRITE_COMMENT` | `yes` |

## Build & register (dev)

```bash
make build      # podman build -t recognize_llm:latest .
```

Register against your deploy daemon (name from `occ app_api:daemon:list`), e.g.:

```bash
../nextcloud-docker-dev/scripts/occ.sh nextcloud app_api:app:register \
  recognize_llm <DAEMON> --info-xml /path/to/appinfo/info.xml \
  --env LLAMA_URL=http://host.docker.internal:8080 --wait-finish
```

Enabling the app auto-registers the TaskProcessing provider, the file-event listener, and the
"Describe with AI" files action.

## Usage

- **New uploads** are processed automatically.
- **Backfill** an existing library:
  ```bash
  curl -X POST http://<exapp>/backfill/start -H 'Content-Type: application/json' -d '{"users": [], "path": ""}'
  curl http://<exapp>/backfill/status
  ```
  (These routes are ADMIN access-level; call them through AppAPI.)
- **On demand**: right-click an image in Files → **Describe with AI**.

## Tests

```bash
make test       # python -m pytest tests/
```

## Status / roadmap

- [x] M0 skeleton, vision client, metadata write-back
- [x] M1 processor + storage
- [x] M2 upload events
- [x] M3 resumable backfill
- [x] M4 OCC command (`occ recognize_llm:backfill`) — no more manual curl
- [x] M5 admin settings UI, `max_tokens` config, improved default prompt
- [x] M6 TaskProcessing input-file fetch — verified `tasks_provider/{taskId}/file/{fileId}` against
      the running server (NC 34 / app_api 34.0.0). Found and fixed a real bug along the way: the
      custom task type's shape entries must use the key `shape_type`, not `type`
      (`TaskProcessingService::getAnonymousTaskType()` throws on `type`, which silently drops the
      whole task type from `getAvailableTaskTypes()` — no task types show up at all in that case,
      not just ours). See `lib/task_provider.py`.
- [x] M7 **Face detection and person grouping**
  - [x] Detect faces in images and extract embeddings — InsightFace/ArcFace `buffalo_sc`, fully local
        ([lib/face_pipeline.py](lib/face_pipeline.py))
  - [x] Cluster embeddings across the library (DBSCAN, cosine) to identify unique individuals
  - [x] Stable `person:N` system tag per cluster — person ids survive re-clustering by matching
        cluster centroids, so names/merges/splits persist (unnamed → `person:<user>:<id>`)
  - [x] Review UI to name, merge, split, and ignore people — the **People** top-menu entry
        ([lib/routes_people.py](lib/routes_people.py)); named persons become `person:<name>`
  - [x] Privacy: embeddings and face crops stay on-box; only the whole image is ever sent to the
        vision endpoint, never a face crop
  - [x] Incremental: new uploads are matched against existing person centroids in real time and
        tagged immediately; a periodic/`occ`/UI recluster seeds and refreshes the groups
  - Run a batch cluster: `occ recognize_llm:cluster-faces [--users a,b] [--min-photos 3]`, the
    People page's **Recluster now** button, or automatically once a backfill finishes.
