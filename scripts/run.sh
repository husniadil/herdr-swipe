#!/bin/sh
# Runs at every Herdr startup, and from the "restart" action. Herdr expects
# this to detach and exit, so the daemon is launched in the background.
#
# Launching it from Herdr is not merely convenient: a child process inherits
# the terminal's Accessibility grant, so nobody has to approve a second app.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
DAEMON="$ROOT/daemon.py"
PIDFILE="$HOME/.local/state/herdr-swipe/daemon.pid"

# `herdr plugin link` registers a plugin without running its [[build]], so a
# development checkout arrives here with no virtualenv. Build on demand rather
# than failing, which also makes startup self-healing after a partial install.
if [ ! -x "$PYTHON" ]; then
    echo "herdr-swipe: no virtualenv, building one"
    "$ROOT/scripts/setup.sh"
fi

# Ask the running daemon to go, by the pid it recorded. Signalling by pid
# rather than by matching a command line means a path with regex characters in
# it cannot turn this into a wider kill than intended.
#
# Only asking, though: two concurrent launches would still interleave here.
# The daemon holds an exclusive lock and a late arrival exits on its own, so
# one survives regardless of how this races.
if [ -f "$PIDFILE" ]; then
    OLD=$(head -1 "$PIDFILE" 2>/dev/null || true)
    case "$OLD" in
        ''|*[!0-9]*) : ;;
        *) kill "$OLD" 2>/dev/null || true ;;
    esac
fi

nohup "$PYTHON" "$DAEMON" >/dev/null 2>&1 &
echo "herdr-swipe: daemon started (pid $!)"
