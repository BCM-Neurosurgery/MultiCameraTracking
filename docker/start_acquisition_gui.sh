#!/bin/env sh

# Start backend
python3 -m multi_camera.backend.fastapi &

# Pipe through cat so React dev server sees a non-TTY stdout
# and skips its clear-screen escape codes
cd /Mocap/react_frontend && npm start 2>&1 | cat
