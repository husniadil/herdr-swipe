#!/bin/sh
# Runs at every Herdr startup, and from the "restart" action. Herdr expects
# this to detach and exit, so the daemon is launched in the background.
#
# Launching it from Herdr is not merely convenient: a child process inherits
# the terminal's Accessibility grant, so nobody has to approve a second app.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DAEMON="$ROOT/daemon.py"
PIDFILE="$HOME/.local/state/herdr-swipe/daemon.pid"

# Paths are probed explicitly rather than trusted to PATH. Herdr may be started
# from a login context where a version manager has not been activated, and then
# a tool that is plainly there in an interactive shell is simply absent here.
find_tool() {
    if command -v "$1" >/dev/null 2>&1; then command -v "$1"; return 0; fi
    for dir in "$HOME/.local/bin" "$HOME/.cargo/bin" /opt/homebrew/bin \
               /usr/local/bin "$HOME/.local/share/mise/shims" /usr/bin; do
        [ -x "$dir/$1" ] && { echo "$dir/$1"; return 0; }
    done
    return 1
}

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

# The daemon declares its own dependencies inline (PEP 723), so uv needs no
# build step and no checked-in virtualenv -- it can even fetch a Python for a
# machine that has none.
if UV=$(find_tool uv); then
    nohup "$UV" run --script "$DAEMON" >/dev/null 2>&1 &
    echo "herdr-swipe: daemon started via uv (pid $!)"
elif [ -x "$ROOT/.venv/bin/python" ]; then
    nohup "$ROOT/.venv/bin/python" "$DAEMON" >/dev/null 2>&1 &
    echo "herdr-swipe: daemon started via virtualenv (pid $!)"
else
    # No uv, no virtualenv: build one from the dependencies declared in the
    # script itself, so the two paths cannot drift apart.
    echo "herdr-swipe: no uv and no virtualenv, building one"
    "$ROOT/scripts/setup.sh"
    nohup "$ROOT/.venv/bin/python" "$DAEMON" >/dev/null 2>&1 &
    echo "herdr-swipe: daemon started via virtualenv (pid $!)"
fi
