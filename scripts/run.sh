#!/bin/sh
# Runs at every Herdr startup. Herdr expects this to detach and exit, so the
# daemon is launched in the background.
#
# Launching it from Herdr is not merely convenient: a child process inherits
# the terminal's Accessibility grant, so nobody has to approve a second app.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Absolute paths throughout, so the running process carries a command line that
# can be matched again. With a relative path the pattern below never matches,
# and a restart silently leaves the old daemon running alongside the new one --
# two daemons means every gesture fires twice.
DAEMON="$ROOT/daemon.py"
PYTHON="$ROOT/.venv/bin/python"

# Python caches imports, so a running daemon keeps the code it started with.
# Replacing it is how an update takes effect.
pkill -f "$DAEMON" 2>/dev/null || true

if [ ! -x "$PYTHON" ]; then
    echo "herdr-swipe: virtualenv missing, run scripts/setup.sh" >&2
    exit 1
fi

nohup "$PYTHON" "$DAEMON" >/dev/null 2>&1 &
echo "herdr-swipe: daemon started (pid $!)"
