# Operations: the real production topology (read me first)

*Last verified 2026-08-21. This documents the **live** deployment as it actually is — it supersedes
any assumption in [PRODUCTION.md](PRODUCTION.md) (a generic runbook) about where the app runs.*

## TL;DR

- **Production recognize_llm runs on `apoc` (192.168.0.143)**, not on the Nextcloud host.
- The kube stack on the Nextcloud host (`~/infra/`, `recognize-llm-stack.service`) is an
  **unregistered duplicate**. Never run both at once (see [The two-instance incident](#the-two-instance-incident)).
- The LLM endpoint is **llama-swap** on apoc (port 11434), one config entry per model.
  Captioning uses **`qwen3.8-26b-vision`**.
- If the queue freezes with `[400] Bad Request <find: …>` on every job:
  `ssh apoc podman restart nc-app-recognize-llm-app`. Root cause below.

## Topology

```
Nextcloud host (192.168.0.105)                 apoc (192.168.0.143, RTX 4090)
┌─────────────────────────────┐                ┌──────────────────────────────────────┐
│ nextcloud-app / -db / -redis │   AppAPI      │ nc-app-recognize-llm-app  (rootless) │
│                              │──proxy───────▶│   :23000  ← UI, API, occ, webhooks   │
│ oc_webhook_listeners ────────│──uploads─────▶│   own SQLite queue + face DB         │
│                              │               │                                      │
│ (local kube stack: STOPPED,  │               │ llama-stack pod (rootful)            │
│  decommission-pending)       │      :11434   │   token-logger ──▶ llama-swap :8080  │
│                              │──captions────▶│     └─ spawns llama-server per model │
└─────────────────────────────┘                └──────────────────────────────────────┘
```

| What | Where | Details |
|---|---|---|
| ExApp registration | NC AppAPI | daemon `manual_install`, host `192.168.0.143`, port `23000` |
| App container | apoc, rootless podman | pod `nc-app-recognize-llm`, container `nc-app-recognize-llm-app` |
| App boot unit | apoc, systemd **user** | `recognize-llm.service` → runs deploy script below |
| App deploy script | apoc | `/home/gnome/infra/recognize-llm/deploy.sh` (kube play `--replace`; secret from `secret.env`) |
| Queue / face DB | apoc | volume-mounted `/nc_app_recognize_llm_data/recognize_llm_queue.db` — **authoritative** (named persons live here) |
| Upload webhooks | NC → apoc | `oc_webhook_listeners` rows POST to `http://192.168.0.143:23000/events/webhook` |
| LLM endpoint | apoc, rootful podman | quadlet `/opt/infra/systemd-containers/llama-cpp/llama-server.container` (service `llama-server.service`) |
| App settings | NC DB (`oc_appconfig_ex`) | shared by any running instance; edit via `occ app_api:app:config:set recognize_llm …` |

## Deploying new app code

CI builds `ghcr.io/lord0gnome/recognize_llm:latest` on every push to `main`. The apoc deploy script
does **not** pull, so:

```bash
git push                                   # wait for the GitHub Action to finish
ssh apoc
podman pull ghcr.io/lord0gnome/recognize_llm:latest
/home/gnome/infra/recognize-llm/deploy.sh   # kube play --replace; queue DB volume survives
```

## The LLM endpoint: llama-swap

Since 2026-08-21 apoc runs **llama-swap** (`ghcr.io/mostlygeek/llama-swap:cuda`) instead of
llama.cpp's router mode. Reason: the router applies one global `-c 131072`, and any model whose
weights + KV cache + mmproj exceed the 4090's 24 564 MiB can never load (the qwen vision mmproj
OOM'd; `qwen-small` needed ~12.8 GiB of KV alone). llama-swap gives **each model its own command
line**; per-model context sizes encode each VRAM budget.

- Config: `/opt/infra/systemd-containers/llama-cpp/llama-swap.yaml` on apoc (12 models; keys match
  the old router directory names so clients never noticed the switch).
- Apply changes / add a model: edit the yaml, then `sudo systemctl restart llama-server.service`
  (root — needs a human; polkit denies non-interactive sudo).
- Request chain unchanged: `client → apoc:11434 → token-logger → llama-swap:8080 → llama-server:580x`.
- Status/logs/manual load-unload web UI: <http://192.168.0.143:11434/ui>.
- The old router quadlet is preserved as `llama-server.container.bak-20260821`.

Captioning model: **`qwen3.8-26b-vision`** — Q4_K_M + `mmproj-BF16` (hardlinked files, zero extra
disk) at `-c 32768`, preloaded at service start via llama-swap's `hooks.on_startup` so the queue
never waits on a cold load. Alternatives already configured: `qwen3.8-27b-vision` (Q3_K_XL,
`-c 65536`, roomier context) and `gemma-4-26b` (the proven previous captioner). Switch with:

```bash
occ app_api:app:config:set recognize_llm llama_model --value "<model-key>" --update-only
```

Workers reload settings per job — no restart needed.

## The queue-freeze root cause (400 storms)

Symptom, seen repeatedly since July: every job fails with `[400] Bad Request <find: user, [], >`
(the `[]` is cosmetic — nc_py_api consumes the criteria list before formatting the error), the
dashboard queue freezes, and *"a restart of the container always fixes it"*. Diagnosed 2026-08-21:

1. **Transport race, not app logic.** ExApp→NC traffic goes through the reverse proxy at
   `cloud.morill.es`. Reusing a kept-alive connection that idled ≈5 s hits the server-side keepalive
   close (measured on one session: `by_id` OK at +0 s, **502 at exactly +5 s**, OK at +15 s/+45 s —
   Apache's `KeepAliveTimeout` is 5 s).
2. **Self-sustaining trap.** Healthy jobs idle 60–90 s inside the llama call, so stale connections
   get evicted and each job starts on a fresh socket. Once one job fails *fast*, the retry loop
   tightens to 2–10 s gaps — inside the danger window — and every subsequent request dies. That is
   why restarts fix it (the first post-restart job re-enters the slow cadence) and why the July
   session-refresh patch in `job_queue.py` cannot rescue it (fresh sessions fail identically at that
   cadence).
3. The `Session token is invalid … Logging out` noise in `nextcloud.log` for ExApp requests is a red
   herring (AppAPI ephemeral sessions + reused cookies), not the cause.

**Relief (should no longer be needed):** `ssh apoc podman restart nc-app-recognize-llm-app`.

**Durable fix (shipped 2026-08-31):** three layers, so the queue retries forever instead of freezing:

1. **Transport immunity** — [`lib/nc_transport.py`](lib/nc_transport.py) patches
   `nc_py_api._session.Session` with an HTTP/1.1-only subclass that sends `Connection: close` on
   every request (per-request injection, because `NcSessionApp` replaces `session.headers`
   wholesale; the header only has meaning over HTTP/1.1). No reuse, no race. Imported at the top of
   `lib/main.py` before anything constructs a `NextcloudApp`.
2. **Transient-error policy** — `job_queue._is_transient_infra_error` classifies infrastructure
   failures (HTTP 400/401/408/425/429/5xx and status-less network errors) and requeues those jobs
   **without burning attempts** (they rotate to the back of the queue), while the worker backs off
   exponentially (5 s → 300 s cap) and rebuilds its session. Worst case the queue retries every
   5 minutes forever — it can no longer freeze. Job-level errors (403/404/422, parse failures)
   still burn `attempts` and can park a single job in `failed`.
3. **Read-only-share skip** — jobs for files the user cannot write (shared-in read-only, e.g.
   marie's view of guillaume's camera roll) are skipped before captioning (`processor.py`), and the
   poll no longer enqueues them (`file_events.py`); the owner's own job does the real work. This
   removes the caption-then-403 churn that kept triggering the trap.
   *Gotcha:* a file NO user can write (e.g. a fully read-only group folder) is now skipped by
   everyone instead of being captioned without a marker — after making such a share writable, run a
   backfill to pick its files up.

## Tag consolidation

Since 2026-09-01 the app can condense its own tag vocabulary (14.6k tags had drifted into plural
pairs and near-synonyms). Flow: dashboard admin card "Tag cleanup" (or
`occ recognize_llm:consolidate-tags`) → **analyze** feeds the vocabulary (names + usage counts, no
images) to the LLM in ~500-tag chunks → proposed merges appear in the dashboard for **review**
(click a chip to veto) → **approve** persists `tag_aliases` in the queue DB (future captions are
canonicalized immediately at the processor choke point; unknown new tags always pass through) →
**apply** is ADDITIVE (user decision 2026-09-01): every file carrying a source tag also gets the
canonical tag — nothing is renamed or deleted, both names stay searchable (per-user re-tag,
resumable, transient-error policy applies; the canonical is created if missing). Future captions
likewise keep the model's tag and gain the canonical (capped model tags first, expansion exempt
from max_tags). Excluded always: `person:*` tags, the recognize-v3 marker, anything with `:`.
Re-running analyze folds newly appeared tags into the established canonicals.
Prompt note: the naive "condense" phrasing makes the model CATEGORIZE (sunset→time) — the shipped
prompt is dedup-framed with explicit wrong-examples; keep it that way.
*Gotcha:* apply is additive, so no tag ever disappears; but a share upgraded to writable later
still needs a backfill to get captions at all (see read-only skip).

## The two-instance incident

The local kube stack was built in July as a migration that never re-pointed the AppAPI daemon, and
later sessions — believing it was production — revived it. From ~Aug 18 to Aug 21 **both** instances
ran: double-processing every upload, and both eventually wedged in the 400 trap above. The local
container was stopped on 2026-08-21. Rules going forward:

- **Never run both instances.** The registered one (apoc) is production.
- The local stack's boot service must stay off:
  `systemctl --user disable --now recognize-llm-stack.service` on the Nextcloud host
  (as of writing, the container is stopped but the service may still be enabled).
- To consolidate for real: either delete the local stack, or migrate properly (re-register the
  daemon to the new host **and** move the queue-DB volume **and** re-point the webhook rows) — never
  half of it.

**Known data divergence:** faces scanned between ~Jul 23 and Aug 21 exist only in the *local*
instance's DB (59 named persons there vs 71 on apoc, which is otherwise authoritative). Apoc's
People UI is missing that window until its non-destructive face scan is re-run over the affected
files; person names may then need a manual once-over.
