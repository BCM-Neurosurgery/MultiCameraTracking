# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available.
#
# Per-host configuration (including MOCAP_PROFILE=gpu|cpu) lives in .env.
# Copy .env.example to .env and fill it in before running any target.

.PHONY: build run validate endurance profile-info

HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
GIT_COMMIT := $(shell git rev-parse --short=10 HEAD 2>/dev/null || echo unknown)

# .env is the single source of truth for per-host config. Make 'include'
# imports every KEY=VALUE line as a Make variable.
ifeq (,$(wildcard .env))
$(error .env not found. Copy .env.example to .env and set MOCAP_PROFILE)
endif

include .env

ifeq ($(MOCAP_PROFILE),)
$(error MOCAP_PROFILE not set in .env. Use 'gpu' or 'cpu'.)
endif

OVERLAY_gpu := docker-compose.gpu.yml
OVERLAY_cpu := docker-compose.cpu.yml
OVERLAY := $(OVERLAY_$(MOCAP_PROFILE))

ifeq ($(OVERLAY),)
$(error Unknown MOCAP_PROFILE='$(MOCAP_PROFILE)' in .env. Use 'gpu' or 'cpu'.)
endif

COMPOSE := -f docker-compose.yml -f $(OVERLAY)

DURATION ?= 300
ENDURANCE_DURATION ?= 14400

profile-info:
	@echo "MOCAP_PROFILE=$(MOCAP_PROFILE)  OVERLAY=$(OVERLAY)"
	@echo "COMPOSE=$(COMPOSE)"

build:
	@echo "Building [profile=$(MOCAP_PROFILE)] HOST_UID=$(HOST_UID) HOST_GID=$(HOST_GID) GIT_COMMIT=$(GIT_COMMIT)"
	docker compose $(COMPOSE) build \
	  --build-arg HOST_UID=$(HOST_UID) \
	  --build-arg HOST_GID=$(HOST_GID) \
	  --build-arg GIT_COMMIT=$(GIT_COMMIT)

run:
	docker compose $(COMPOSE) run --rm mocap

# Deployment validation: loads camera config, checks hardware, disk I/O,
# runs pipeline stress test with worst-case frames, verifies all outputs.
# Reports saved under <DATA_VOLUME>/stress_test/<timestamp>/.
#   make validate              # 5-min soak (default)
#   make validate DURATION=600 # 10-minute soak
validate:
	docker compose $(COMPOSE) run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.stress_test \
	    --config /configs/camera_config.yaml -d $(DURATION) --profile $(MOCAP_PROFILE)

# Endurance test: real cameras + noise-injected worst-case encoding.
# Proves pipeline survives extended operation under maximum load.
#   make endurance                             # 4-hour default
#   make endurance ENDURANCE_DURATION=86400    # 24-hour soak
#   make endurance ENDURANCE_DURATION=691200   # 8-day soak
endurance:
	docker compose $(COMPOSE) run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.endurance_test \
	    --config /configs/camera_config.yaml -d $(ENDURANCE_DURATION) --profile $(MOCAP_PROFILE)
