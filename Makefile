.PHONY: build test unregister help

APP_ID = recognize_llm
IMAGE = recognize_llm:latest
OCC = ../nextcloud-docker-dev/scripts/occ.sh
NC_CONTAINER ?= nextcloud

# Local dev helpers only. The production image is built + pushed to GHCR by
# .github/workflows/build.yml (on push to main / tags). See PRODUCTION.md.

help:
	@echo "make build                       - build the dev image locally (recognize_llm:latest)"
	@echo "make test                        - run unit tests"
	@echo "make unregister NC_CONTAINER=<c> - unregister the exApp (dev)"
	@echo "Image publishing is handled by GitHub Actions (.github/workflows/build.yml)."
	@echo "Production deploy: see PRODUCTION.md"

build:
	podman build --no-cache -t $(IMAGE) .

unregister:
	$(OCC) $(NC_CONTAINER) app_api:app:unregister $(APP_ID)

test:
	python3 -m pytest tests/ -q
