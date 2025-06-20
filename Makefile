# This is the build file for the docker. Note this should be run from the
# parent directory for the necessary files to be available

.PHONY: clean build run

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
