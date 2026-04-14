# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available

.PHONY: clean build run validate endurance build-cpu run-cpu validate-cpu endurance-cpu profile-info

# detect your host UID/GID
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

DIR := ${CURDIR}

GIT_COMMIT := $(shell git rev-parse --short=10 HEAD 2>/dev/null || echo unknown)

# Deployment profile. Default is `gpu`; operators on CPU-only hosts must
# pass PROFILE=cpu (or use the build-cpu/run-cpu/validate-cpu targets).
PROFILE ?= gpu

COMPOSE_gpu := docker-compose.yml
COMPOSE_cpu := docker-compose.cpu.yml
COMPOSE := $(COMPOSE_$(PROFILE))

ifeq ($(COMPOSE),)
$(error Unknown PROFILE='$(PROFILE)'. Use PROFILE=gpu or PROFILE=cpu.)
endif

profile-info:
	@echo "PROFILE=$(PROFILE)  COMPOSE=$(COMPOSE)"

build:
	@echo "Building [profile=$(PROFILE)] with HOST_UID=$(HOST_UID) HOST_GID=$(HOST_GID) GIT_COMMIT=$(GIT_COMMIT)"
	docker compose -f $(COMPOSE) build \
	  --build-arg HOST_UID=$(HOST_UID) \
	  --build-arg HOST_GID=$(HOST_GID) \
	  --build-arg GIT_COMMIT=$(GIT_COMMIT)

run:
	docker compose -f $(COMPOSE) run --rm mocap

# Deployment validation: loads camera config, checks hardware, disk I/O,
# runs pipeline stress test with worst-case frames, verifies all outputs.
# Reports saved to ./validation/
#   make validate              # 5-min soak (default), profile auto-detected
#   make validate DURATION=600 # 10-minute soak
#   make validate PROFILE=cpu  # force CPU encode path
DURATION ?= 300
validate:
	docker compose -f $(COMPOSE) run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.stress_test \
	    --config /configs/camera_config.yaml -d $(DURATION) --profile $(PROFILE)

# Endurance test: real cameras + noise-injected worst-case encoding.
# Proves pipeline survives extended operation under maximum load.
#   make endurance                        # 4-hour default, profile auto-detected
#   make endurance ENDURANCE_DURATION=86400   # 24-hour soak
#   make endurance ENDURANCE_DURATION=691200  # 8-day soak
#   make endurance PROFILE=cpu            # force CPU encode path
ENDURANCE_DURATION ?= 14400
endurance:
	docker compose -f $(COMPOSE) run --rm --entrypoint "" mocap \
	  python3 -m multi_camera.acquisition.endurance_test \
	    --config /configs/camera_config.yaml -d $(ENDURANCE_DURATION) --profile $(PROFILE)

# Explicit CPU convenience targets (equivalent to <target> PROFILE=cpu).
build-cpu:
	@$(MAKE) build PROFILE=cpu
run-cpu:
	@$(MAKE) run PROFILE=cpu
validate-cpu:
	@$(MAKE) validate PROFILE=cpu
endurance-cpu:
	@$(MAKE) endurance PROFILE=cpu
