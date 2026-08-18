#!/bin/sh
# Runs once at install time. Creates the virtualenv the daemon runs in.
set -eu
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet pyobjc-framework-Cocoa pyobjc-framework-Quartz
echo "herdr-swipe: virtualenv ready"
