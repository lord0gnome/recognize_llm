#!/usr/bin/env bash
# Re-apply the app_api 33.0.0 events_listener patch onto a running Nextcloud container.
# Needed because app_api 33.0.0 declares POST /api/v1/events_listener but ships no implementation,
# and `occ app:update app_api` overwrites these files. See README.md for the why.
#
# Usage:  ./apply.sh [NC_CONTAINER]      (default: nextcloud-app)
set -euo pipefail

NC="${1:-nextcloud-app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/lib"
APP_API="/var/www/html/apps/app_api/lib"

occ() { podman exec -u www-data "$NC" php /var/www/html/occ "$@"; }

echo ">> Copying patch files into $NC:$APP_API"
for f in Controller/EventsListenerController.php \
         Service/EventsListenerService.php \
         Listener/NodeEventListener.php \
         AppInfo/Application.php; do
  podman cp "$LIB/$f" "$NC:$APP_API/$f"
done
# Best-effort: ensure the web user can read them (ignore if the image disallows it).
podman exec -u root "$NC" chown www-data:www-data \
  "$APP_API/Controller/EventsListenerController.php" \
  "$APP_API/Service/EventsListenerService.php" \
  "$APP_API/Listener/NodeEventListener.php" \
  "$APP_API/AppInfo/Application.php" 2>/dev/null || true

echo ">> Reloading app_api so the NodeEventListener registers"
occ app:disable app_api
occ app:enable  app_api

echo ">> Re-registering the exApp's upload-event subscription"
occ app_api:app:disable recognize_llm
occ app_api:app:enable  recognize_llm

echo ">> Status:"
occ app_api:app:list | grep -i recognize || true
echo ">> Done. After an upload, confirm with:"
echo "   podman logs nc_app_recognize_llm | grep events/node"
