# Deploying recognize_llm to production (Podman + `podman play kube` + GHCR)

This is the runbook for a **rootless/rootful Podman** host where Nextcloud runs as a pod (via
`podman play kube`), the **container runtime is on the same host**, and the exApp image is published
to **GitHub Container Registry (ghcr.io)**.

Throughout, `occ` means running occ inside your Nextcloud container, e.g.:

```bash
NC=<your-nc-container>            # e.g. `podman ps` -> the nextcloud container name
occ() { podman exec -u www-data "$NC" php /var/www/html/occ "$@"; }
```

> **NC version note:** on a **stable** Nextcloud (30–33) app_api works out of the box. The
> `OC_Util::getChannel()` / `getL10NFactory()` patches from the dev box were only needed because that
> box runs NC 34 *dev* — you should **not** need them in production.

---

## 1. Prerequisites

```bash
# Podman API socket must be running (the deploy daemon talks to it).
systemctl --user start podman.socket    # rootless
# or:  sudo systemctl start podman.socket   # rootful

# Install AppAPI in Nextcloud
occ app:install app_api
```

---

## 2. Build & publish the image to GHCR

The image is built and pushed automatically by GitHub Actions
([.github/workflows/build.yml](.github/workflows/build.yml)) on every push to `main` and on `v*`
tags — no local build needed. After a push, the image is at
`ghcr.io/lord0gnome/recognize_llm:latest` (plus a `sha-<short>` tag, and the tag name for releases).

```bash
git push                       # main  -> :latest + :sha-xxxxxxx
git tag v0.2.0 && git push --tags   # tag   -> :v0.2.0
```

You can also trigger it manually from the repo's **Actions** tab (workflow_dispatch).

> Need a local one-off without CI? `podman build -t ghcr.io/lord0gnome/recognize_llm:latest . &&
> podman push ...` after `podman login ghcr.io`.

Then **make the package pullable by the deploy daemon**. Either:
- **Simplest:** on github.com → your `recognize_llm` package → *Package settings* → **change visibility to Public**; or
- **Private:** run `podman login ghcr.io` on the NC host too, so the daemon's Podman can pull it
  (the daemon proxies the host's Podman socket, which uses the host's registry auth).

Point the manifest at your registry — edit [appinfo/info.xml](appinfo/info.xml):

```xml
<docker-install>
    <registry>ghcr.io</registry>
    <image><your-github-user>/recognize_llm</image>
    <image-tag>latest</image-tag>
</docker-install>
```

(The repo's `info.xml` currently uses the dev `localhost:5000` registry — change it for prod.)

---

## 3. Networking — the one part that bites with `podman play kube`

The exApp is deployed as a **separate container** (`nc_app_recognize_llm`), not inside your NC pod.
Nextcloud and the exApp must share a Podman network so they can reach each other by name. The
deploy daemon's `--net` controls which network the exApp joins.

**Recommended:** put the NC pod, the deploy-daemon proxy, and the exApp on one named network.

```bash
podman network create nextcloud        # if you don't already have a dedicated one
```

Make sure your NC pod is attached to it — in your `pod.yaml`/run, add the pod to `nextcloud`
(`podman play kube --network nextcloud pod.yaml`, or add the network in the manifest). Confirm:

```bash
podman inspect <your-nc-container> --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

You'll use this network name as `--net=nextcloud` in the next step.

---

## 4. Deploy the Docker-Socket-Proxy (DSP) and register the daemon

The DSP is AppAPI's gatekeeper to the container runtime.

```bash
podman run -d --name nc-dsp --network nextcloud \
  -v /run/user/$(id -u)/podman/podman.sock:/var/run/docker.sock:z \
  -e NC_HAPROXY_PASSWORD='<choose-a-strong-password>' \
  -e EX_APPS_NET=nextcloud \
  ghcr.io/nextcloud/nextcloud-appapi-dsp:release
# (rootful: socket is /run/podman/podman.sock and drop the $(id -u))

occ app_api:daemon:register dsp_http "DSP HTTP" docker-install http \
  "nc-dsp:2375" "https://<your-nextcloud-domain>" \
  --net=nextcloud --haproxy_password='<same-password>' --set-default
```

> If you'd rather run the DSP on a **separate** Docker host, use the **HTTPS DSP** with certificates
> (`--net=host`, `protocol https`) — see the AppAPI docs. Same-host HTTP as above is simplest.

---

## 5. Register and enable the exApp

`--info-xml` accepts a local path **inside the NC container** or a URL. Easiest is to copy it in:

```bash
podman cp recognize_llm/appinfo/info.xml "$NC":/tmp/recognize_llm.xml
occ app_api:app:register recognize_llm dsp_http --info-xml /tmp/recognize_llm.xml --wait-finish
```

AppAPI pulls `ghcr.io/<you>/recognize_llm:latest`, starts the container on `nextcloud`, and the
enable step auto-registers the TaskProcessing provider, the upload-event listener, the
"Describe with AI" file action, and the admin settings form.

---

## 6. Configure the vision endpoint

```bash
# URL must be reachable FROM the exApp container.
#  - llama.cpp on the host:        http://host.containers.internal:11434/v1
#  - llama.cpp in another container on `nextcloud`:  http://<that-container>:11434/v1
#  - llama.cpp elsewhere on the LAN: http://<ip>:11434/v1  (ensure host firewall allows the
#    Podman subnet; on Fedora, firewalld often blocks bridge->LAN, so host.containers.internal is safest)
occ app_api:app:config:set recognize_llm llama_url --value "http://host.containers.internal:11434/v1"
occ app_api:app:config:set recognize_llm api_key  --value "<your-llama-api-key>"
```

Everything else (prompt, max tags, mimetypes, concurrency, write-comment) is editable in the UI under
**Administration settings → Additional → Recognize LLM**.

---

## 7. Verify & run a backfill

- **New uploads** are tagged automatically. Upload a photo and check the Files sidebar for tags + the
  description in the Comments tab.
- **Backfill** your existing library — the backfill routes are ADMIN-level and reached through the
  AppAPI proxy:

```bash
# Trigger via the exApp container directly (simplest), or proxy through Nextcloud.
podman exec nc_app_recognize_llm sh -c \
  'curl -s -XPOST localhost:23000/backfill/start -H "Content-Type: application/json" -d "{\"users\":[],\"path\":\"\"}"'
# Watch progress:
podman exec nc_app_recognize_llm sh -c 'curl -s localhost:23000/backfill/status'
```

`users:[]` scans all users (falls back to the admin if user-listing scope is unavailable); pass a
list to target specific users, and `path` to limit to a folder. It's resumable — re-running skips
already-processed, unchanged files.

---

## 8. Updating the exApp later

```bash
cd recognize_llm
make push GHCR_OWNER=<you> VERSION=0.2.0
occ app_api:app:unregister recognize_llm                 # keeps data volume by default
occ app_api:app:register recognize_llm dsp_http --info-xml /tmp/recognize_llm.xml --wait-finish
```

(Or `occ app_api:app:update recognize_llm` if you only bumped the image tag.)

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Failed to enable ExApp` / silent enable failure / 996 on `POST /api/v1/events_listener` | **app_api ships no events_listener implementation (CONFIRMED on app_api 33.0.0).** Upload events can't register. The captured fix + one-command restore are in the repo: **`appapi-patches/app_api-33.0.0/` → `./apply.sh [nc-container]`**. **Re-apply after every `occ app:update app_api`** (it overwrites the files). Backfill + provider work without this; only new-upload events break. |
| `Failed to pull image … 403/500` | Image not pullable: make the GHCR package public, or `podman login ghcr.io` on the host. Confirm `<registry>/<image>:<tag>` in info.xml matches what you pushed. |
| Heartbeat fails / NC hits `localhost:23000` | Daemon missing `--net`; exApp landed on the wrong network. Re-register the daemon with `--net=<your network>` and redeploy. |
| exApp can't reach llama (`Connection refused`) | Use `host.containers.internal`; if llama is on the LAN, open the host firewall for the Podman subnet. `/v1/models` open but completions `401` → set `api_key`. |
| ExApps admin page 500 `undefined method` | Only on NC **34 dev** with old app_api — not expected on stable. See the dev notes if you ever hit it. |
| Provider/task-type missing | Check `occ` taskprocessing task types; re-enable the app to re-run registration. |
