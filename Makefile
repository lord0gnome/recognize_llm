.PHONY: build push test unregister help

APP_ID = recognize_llm
VERSION ?= 0.1.0
IMAGE = recognize_llm:latest
OCC = ../nextcloud-docker-dev/scripts/occ.sh
NC_CONTAINER ?= nextcloud

# For production publishing to GitHub Container Registry.
# Override GHCR_OWNER (your GitHub user/org), e.g. `make push GHCR_OWNER=lord0gnome`.
GHCR_OWNER ?= CHANGE_ME
GHCR_IMAGE = ghcr.io/$(GHCR_OWNER)/$(APP_ID)

help:
	@echo "make build                      - build the dev image (recognize_llm:latest)"
	@echo "make test                       - run unit tests"
	@echo "make push GHCR_OWNER=<you>      - build + push :$(VERSION) and :latest to ghcr.io/<you>/$(APP_ID)"
	@echo "make unregister NC_CONTAINER=<c> - unregister the exApp (dev)"
	@echo "Production deploy: see PRODUCTION.md"

build:
	podman build --no-cache -t $(IMAGE) .

push:
	@test "$(GHCR_OWNER)" != "CHANGE_ME" || { echo "Set GHCR_OWNER=<your github user/org>"; exit 1; }
	podman build -t $(GHCR_IMAGE):$(VERSION) -t $(GHCR_IMAGE):latest .
	podman push $(GHCR_IMAGE):$(VERSION)
	podman push $(GHCR_IMAGE):latest
	@echo "Pushed $(GHCR_IMAGE):$(VERSION) and :latest"

unregister:
	$(OCC) $(NC_CONTAINER) app_api:app:unregister $(APP_ID)

test:
	python3 -m pytest tests/ -q
