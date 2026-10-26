# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available

.PHONY: clean build run validate endurance

# Existing deployments keep the GPU profile. CPU hosts set MOCAP_PROFILE=cpu
# in .env; command-line overrides such as `make MOCAP_PROFILE=cpu build` work too.
-include .env
MOCAP_PROFILE ?= gpu
ifneq ($(MOCAP_PROFILE),gpu)
ifneq ($(MOCAP_PROFILE),cpu)
$(error MOCAP_PROFILE must be gpu or cpu)
endif
endif
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.$(MOCAP_PROFILE).yml

# detect your host UID/GID
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

DIR := ${CURDIR}

GIT_COMMIT := $(shell git rev-parse --short=10 HEAD 2>/dev/null || echo unknown)
GIT_DESCRIBE := $(shell git describe --tags --always --dirty 2>/dev/null || echo unknown)

build:
	@echo "Building $(MOCAP_PROFILE) profile with HOST_UID=$(HOST_UID) HOST_GID=$(HOST_GID) GIT_COMMIT=$(GIT_COMMIT) GIT_DESCRIBE=$(GIT_DESCRIBE)"
	$(COMPOSE) build \
	  --build-arg HOST_UID=$(HOST_UID) \
	  --build-arg HOST_GID=$(HOST_GID) \
	  --build-arg GIT_COMMIT=$(GIT_COMMIT) \
	  --build-arg GIT_DESCRIBE=$(GIT_DESCRIBE)

run:
	$(COMPOSE) run --rm mocap

# Quick validation: 5-min synthetic soak + frontend memory leak check.
#   make validate              # 5 min
#   make validate DURATION=600 # 10 min
DURATION ?= 300
validate:
	$(COMPOSE) run --rm -u root --entrypoint "" mocap \
	  bash -c '\
	    chown -R $(HOST_UID):$(HOST_GID) /Mocap/react_frontend && \
	    su appuser -c "python3 -m multi_camera.acquisition.stress_test --config /configs/camera_config.yaml -d $(DURATION) --with-frontend"'

# Endurance test: real cameras + noise-injected worst-case encoding.
# Proves pipeline survives extended operation under maximum load.
#   make endurance                             # 4-hour default
#   make endurance ENDURANCE_DURATION=86400    # 24-hour soak
#   make endurance ENDURANCE_DURATION=691200   # 8-day soak
ENDURANCE_DURATION ?= 14400
endurance:
	$(COMPOSE) run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.endurance_test \
	    --config /configs/camera_config.yaml -d $(ENDURANCE_DURATION)
