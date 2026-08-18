#!/bin/sh
# Stops the daemon. Herdr has no shutdown hook, so disabling or unlinking the
# plugin leaves it running; this is the way to actually turn the gestures off.
set -eu
DAEMON=$(cd "$(dirname "$0")/.." && pwd)/daemon.py
PIDFILE="$HOME/.local/state/herdr-swipe/daemon.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "herdr-swipe: not running"
    exit 0
fi

PID=$(head -1 "$PIDFILE" 2>/dev/null || true)
case "$PID" in
    ''|*[!0-9]*) echo "herdr-swipe: no valid pid recorded"; exit 0 ;;
esac

# The pid is only trustworthy if it still belongs to our daemon: a crash can
# leave the file behind, and the OS reuses pid numbers.
case "$(ps -p "$PID" -o command= 2>/dev/null)" in
    *"$DAEMON"*) ;;
    *) echo "herdr-swipe: not running (stale pid $PID)"; exit 0 ;;
esac

if kill "$PID" 2>/dev/null; then
    echo "herdr-swipe: stopped (pid $PID)"
else
    echo "herdr-swipe: not running (stale pid $PID)"
fi
