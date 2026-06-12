# Handoff: deploy `recognize_llm` to the production Nextcloud host

**Read this first — you (the assistant) are now on the user's PRODUCTION Nextcloud machine.**
The app is already built and verified end-to-end on a separate dev box. Your job here is **only the
production install of AppAPI + this exApp**. This is production: confirm before anything destructive
or outward-facing, and verify each step before moving on.

The user (GitHub `lord0gnome`, email lord0gnome@gmail.com) will paste this file as context, then work
with you interactively. A polished human runbook lives in `PRODUCTION.md`; this file is the
agent-oriented version with the **traps we already hit** so you don't rediscover them.

---

## What `recognize_llm` is
A Nextcloud **exApp** (external app, runs as its own container, managed by **AppAPI**). It captions
images with a **local llama.cpp** OpenAI-compatible vision endpoint and writes results back to
Nextcloud as **system tags** + a **description** (stored as a DAV dead-property and, optionally, a
visible file comment). Originals are never modified. One engine, three entry points:
- **uploads** — AppAPI pushes `NodeCreatedEvent`/`NodeWrittenEvent` → `/events/node` → queue
- **backfill** — `POST /backfill/start` crawls existing images (resumable via a per-file etag marker)
- **TaskProcessing provider** `recognize_llm:image2text` (reusable by Assistant/Memories)

Python + nc_py_api (FastAPI). Source in this repo; key files: `lib/main.py` (lifecycle + routes),
`lib/processor.py` (engine), `lib/storage.py`/`lib/dav.py` (write-back), `lib/job_queue.py` (queue),
`lib/settings.py` (config), `lib/settings_ui.py` (admin form).

## What already exists — DO NOT rebuild
- **Repo:** `github.com/lord0gnome/recognize_llm`
- **Image:** `ghcr.io/lord0gnome/recognize_llm:latest`, built+pushed by GitHub Actions
  (`.github/workflows/build.yml`) on every push to `main`. `appinfo/info.xml` already points its
  `<docker-install>` at this image.
- Verified in dev: upload auto-tags + describes; provider + admin settings form register.

## Production environment (confirmed by the user)
- Nextcloud runs via **`podman play kube`** (a pod) on **this host**; **podman is on this host**.
- A **llama.cpp** vision server is reachable on the LAN (dev used model `gemma-4-26B`, multimodal)
  and **requires an API key** (the user has it).
- Image distribution via **GHCR**.

---

## Establish these BEFORE running setup (detect, or ask the user) — do not assume

1. **Rootless or rootful podman?** Determines the socket path and `systemctl` flavor.
   - rootless: `systemctl --user start podman.socket`, socket `/run/user/$(id -u)/podman/podman.sock`
   - rootful: `sudo systemctl start podman.socket`, socket `/run/podman/podman.sock`
2. **NC container name + how to run occ.** `podman ps` → find the Nextcloud container. Then:
   `occ() { podman exec -u www-data <nc-ctr> php /var/www/html/occ "$@"; }`
   (verify the occ path and the web user — some images differ).
3. **NC version + app_api version:** `occ status` and `occ app:list | grep app_api`. **34 dev** →
   the `OC_Util`/`getL10NFactory` patches in "Gotchas". **app_api 33.0.0** (and possibly nearby) →
   the **events_listener gap** patch in "Gotchas" (CONFIRMED needed in prod). Do not assume a stable
   NC means zero patches — check both.
4. **The pod's podman network.**
   `podman inspect <nc-ctr> --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}'`
   The DSP proxy **and** the exApp container must be on the **same** network as NC so they resolve
   each other by name. If NC is on the default `podman` net or a host/slirp setup that won't allow
   that, create a dedicated network (e.g. `nextcloud`) and attach the pod to it
   (`podman play kube --network nextcloud pod.yaml`, or edit the kube manifest), then redeploy the pod.
5. **llama endpoint reachable FROM a container.** Test before configuring:
   ```bash
   podman run --rm --network <pod-network> curlimages/curl -s -m 8 http://<candidate>/v1/models
   ```
   Prefer `host.containers.internal` if llama runs on the host. The raw LAN IP often fails from the
   bridge subnet (host firewall). Get the **API key** from the user. Note `/v1/models` may be open
   while `/v1/chat/completions` returns **401** without the key — that's expected.
6. **NC public base URL** (for the daemon's `nextcloud_url`) and **GHCR package visibility**
   (public, or you must `podman login ghcr.io` on this host so the daemon can pull a private image).

---

## Setup steps

```bash
# 0) Podman API socket (pick rootless or rootful per above)
systemctl --user start podman.socket

# 1) Install AppAPI
occ app:install app_api

# 2) Shared network (skip if the pod already has a usable bridge network with DNS)
podman network create nextcloud           # then ensure the NC pod is attached to it

# 3) GHCR pull access: make the package Public on GitHub, OR:
podman login ghcr.io -u lord0gnome        # (PAT with read:packages)

# 4) Deploy the Docker-Socket-Proxy on the shared network
podman run -d --name nc-dsp --network nextcloud \
  -v /run/user/$(id -u)/podman/podman.sock:/var/run/docker.sock:z \
  -e NC_HAPROXY_PASSWORD='<STRONG_PW>' -e EX_APPS_NET=nextcloud \
  ghcr.io/nextcloud/nextcloud-appapi-dsp:release
# rootful: socket = /run/podman/podman.sock, drop $(id -u)

# 5) Register the deploy daemon  (NOTE the :2375 and --net — both bit us in dev)
occ app_api:daemon:register dsp_http "DSP HTTP" docker-install http \
  "nc-dsp:2375" "https://<NC_DOMAIN>" \
  --net=nextcloud --haproxy_password='<STRONG_PW>' --set-default

# 6) Register + deploy the exApp (info.xml already points at ghcr.io/lord0gnome/recognize_llm)
podman cp appinfo/info.xml <nc-ctr>:/tmp/recognize_llm.xml
occ app_api:app:register recognize_llm dsp_http --info-xml /tmp/recognize_llm.xml --wait-finish

# 7) Configure (URL must be reachable from the exApp container)
occ app_api:app:config:set recognize_llm llama_url --value "http://host.containers.internal:11434/v1"
occ app_api:app:config:set recognize_llm api_key  --value "<LLAMA_API_KEY>"
```

Enabling auto-registers the TaskProcessing provider, the upload-event listener, the
"Describe with AI" file action, and the admin settings form (Administration → Additional →
Recognize LLM). Other settings (prompt, max tags, mimetypes, concurrency) are editable there.

---

## Gotchas we already hit (these WILL recur)

- **app_api may ship a broken/missing `events_listener` (CONFIRMED on app_api 33.0.0).** The route
  `POST /api/v1/events_listener` is declared but the controller/service/NC-side file-event dispatch
  are absent, so the exApp's `enabled_handler` gets a **996** registering upload events and enable
  "fails silently" (`Failed to enable ExApp recognize_llm`), leaving the app disabled. Backfill and
  the TaskProcessing provider still work — only **new-upload** events break. This is NOT covered by
  "stable NC = no patches". Fix on the host (in `apps/app_api/lib/`, persisted via the web-root
  volume): add `Controller/EventsListenerController.php`, `Service/EventsListenerService.php`,
  `Listener/NodeEventListener.php`, and register `NodeEventListener` for `NodeCreatedEvent` +
  `NodeWrittenEvent` in `AppInfo/Application.php`; then disable/enable `recognize_llm`. **`occ
  app:update app_api` overwrites these four files** — re-apply after any app_api update. Verify:
  `SELECT * FROM oc_ex_event_handlers;` shows a recognize_llm row, and
  `podman logs nc_app_recognize_llm | grep events/node` shows hits after an upload. **The captured
  patch + a one-command restore live in the repo: `appapi-patches/app_api-33.0.0/` (`./apply.sh`).**
- **Image pull always happens.** AppAPI always issues a pull through the DSP; it does not use a bare
  local image. A private GHCR package needs `podman login ghcr.io` on the host (the daemon proxies
  the host podman socket, which uses the host's registry auth), or make the package public.
  Symptom: `Failed to pull image … 403/500`.
- **Daemon `--net` is mandatory.** Without it the exApp lands on the wrong network and NC tries to
  reach it at `localhost:23000` → heartbeat fails, app won't enable. The exApp is a **separate
  container**, not part of the NC pod — they must share a network.
- **DSP host needs the port** `:2375` in the daemon host (`nc-dsp:2375`), else connection refused.
- **Podman socket / DSP staleness:** if you (re)start `podman.socket` or change `registries.conf`
  after the DSP is up, the DSP's bind-mounted socket can go stale → `503`. Fix:
  `podman restart nc-dsp`. After editing registries.conf, restart the socket service so the API
  reloads it.
- **llama reachability:** use `host.containers.internal`, not the LAN IP (host firewall, e.g.
  firewalld on Fedora, blocks the podman bridge → LAN). `/v1/models` open but completions `401`
  ⇒ set `api_key`. The app handles a `llama_url` that already ends in `/v1` (won't double-append).
- **NC 34 dev only:** app_api 3.2.3 calls server methods NC 34 removed, breaking the **ExApps admin
  page** (not the exApp itself). If `occ`/the page throws `Call to undefined method
  OC_Util::getChannel()` or `OC\Server::getL10NFactory()`, patch in
  `apps-writable/app_api/lib/`: `\OC_Util::getChannel()` →
  `\OCP\Server::get(\OCP\ServerVersion::class)->getChannel()`, and
  `\OC::$server->getL10NFactory()` → `\OCP\Server::get(\OCP\L10N\IFactory::class)`.
  **On a stable NC release you should NOT need this.**

---

## Verify (end-to-end)

```bash
# app enabled + provider + form
occ app_api:app:list | grep recognize          # -> [enabled]
curl -s -u <admin>:<pw> -H "OCS-APIRequest: true" -H "Accept: application/json" \
  "https://<NC_DOMAIN>/ocs/v2.php/taskprocessing/tasktypes" | grep -o recognize_llm:image2text

# upload a photo as a user via WebDAV, then check tags appear in the Files sidebar + a
# description comment on the file. Worker logs go to the Nextcloud log via nc.log:
podman exec <nc-ctr> sh -c 'grep -i "recognize_llm:" /var/www/html/data/nextcloud.log | tail'
```

**Backfill** the existing library (resumable; re-runs skip processed/unchanged files):
```bash
podman exec nc_app_recognize_llm sh -c \
  'curl -s -XPOST localhost:23000/backfill/start -H "Content-Type: application/json" -d "{\"users\":[],\"path\":\"\"}"'
podman exec nc_app_recognize_llm sh -c 'curl -s localhost:23000/backfill/status'
```

## Updating later
`git push` (CI rebuilds `:latest`) → on the host `occ app_api:app:update recognize_llm`
(or unregister/register). See `PRODUCTION.md` §8.
```
