#!/bin/env sh

# Start React dev server in background — output to log file, not terminal.
# The frontend is served at :3000; its build warnings are not useful in the terminal.
cd /Mocap/react_frontend && BROWSER=none NODE_NO_WARNINGS=1 npm start --silent > /tmp/react.log 2>&1 &

# Start backend in foreground — only acquisition logs appear in terminal
python3 -m multi_camera.backend.fastapi
