# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available

.PHONY: clean build run validate

# detect your host UID/GID
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

DIR := ${CURDIR}

build:
	@echo "Building with HOST_UID=$(HOST_UID) HOST_GID=$(HOST_GID)"
	docker compose build \
	  --build-arg HOST_UID=$(HOST_UID) \
	  --build-arg HOST_GID=$(HOST_GID)

run:
	docker compose run --rm mocap

# Deployment validation: loads camera config, checks hardware, disk I/O,
# runs pipeline stress test with worst-case frames, verifies all outputs.
# Reports saved to ./validation/
#   make validate              # 5-min soak (default)
#   make validate DURATION=600 # 10-minute soak
DURATION ?= 300
validate:
	docker compose run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.stress_test \
	    --config /configs/camera_config.yaml -d $(DURATION)
