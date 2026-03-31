# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available

.PHONY: clean build run validate endurance

# detect your host UID/GID
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

DIR := ${CURDIR}

GIT_COMMIT := $(shell git rev-parse --short=10 HEAD 2>/dev/null || echo unknown)

build:
	@echo "Building with HOST_UID=$(HOST_UID) HOST_GID=$(HOST_GID) GIT_COMMIT=$(GIT_COMMIT)"
	docker compose build \
	  --build-arg HOST_UID=$(HOST_UID) \
	  --build-arg HOST_GID=$(HOST_GID) \
	  --build-arg GIT_COMMIT=$(GIT_COMMIT)

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

# Endurance test: real cameras + noise-injected worst-case encoding.
# Proves pipeline survives extended operation under maximum load.
#   make endurance                        # 4-hour default
#   make endurance ENDURANCE_DURATION=86400   # 24-hour soak
#   make endurance ENDURANCE_DURATION=691200  # 8-day soak
ENDURANCE_DURATION ?= 14400
endurance:
	docker compose run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.endurance_test \
	    --config /configs/camera_config.yaml -d $(ENDURANCE_DURATION)
