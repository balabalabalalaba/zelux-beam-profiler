#!/bin/bash
# Starts the beam profiler on a Mac with a simulated beam (no camera needed).
# You can also open saved images from the page. Double-click this file in Finder.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating the Python environment..."
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python -m beamprofiler --camera sim "$@"
