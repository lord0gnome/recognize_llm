# app_api 33.0.0 events_listener patch (captured from prod cloud.morill.es)

app_api 33.0.0 declares POST /api/v1/events_listener but ships no implementation, so the exApp's
upload-event registration 996s and enable fails silently. These files restore it.

Restore (run on the NC host; web root is volume-mounted so files persist):
```bash
NC=nextcloud-app   # NC container
for f in Controller/EventsListenerController.php Service/EventsListenerService.php \
         Listener/NodeEventListener.php AppInfo/Application.php; do
  podman cp "lib/$f" "$NC:/var/www/html/apps/app_api/lib/$f"
done
podman exec -u www-data $NC php /var/www/html/occ app_api:app:disable recognize_llm
podman exec -u www-data $NC php /var/www/html/occ app_api:app:enable  recognize_llm
```
NB: Application.php is the full patched file for app_api 33.0.0 — only restore it onto the same
app_api version. Re-apply after every `occ app:update app_api`.
