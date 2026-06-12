# app_api 33.0.0 events_listener patch (captured from prod cloud.morill.es)

app_api 33.0.0 declares POST /api/v1/events_listener but ships no implementation, so the exApp's
upload-event registration 996s and enable fails silently. These files restore it.

Restore — run on the NC host (web root is volume-mounted, so the files persist):
```bash
./apply.sh                 # or: ./apply.sh <nc-container>   (default: nextcloud-app)
```
`apply.sh` copies the four files in, reloads app_api so the `NodeEventListener` registers, and
disable/enables `recognize_llm` to re-register its upload-event subscription.

NB: `Application.php` is the **full patched file for app_api 33.0.0** — only restore it onto the
same app_api version. **Re-apply after every `occ app:update app_api`** (it overwrites these files).
Verify afterwards: `SELECT * FROM oc_ex_event_handlers;` shows a `recognize_llm` row, and
`podman logs nc_app_recognize_llm | grep events/node` shows hits after an upload.
