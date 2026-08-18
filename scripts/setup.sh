#!/bin/sh
# Fallback only. With uv present nothing here is needed: the daemon declares
# its dependencies inline (PEP 723) and `uv run --script` handles the rest.
#
# This exists for a machine that has python3 but not uv. The dependency list is
# read out of the script's own metadata block so there is one source of truth.
set -eu
cd "$(dirname "$0")/.."

DEPS=$(sed -n '/^# \/\/\/ script/,/^# \/\/\/$/p' daemon.py \
       | sed -n 's/^#  *"\(.*\)",$/\1/p')
[ -n "$DEPS" ] || { echo "herdr-swipe: no dependencies found in daemon.py" >&2; exit 1; }

find_tool() {
    if command -v "$1" >/dev/null 2>&1; then command -v "$1"; return 0; fi
    for dir in "$HOME/.local/bin" "$HOME/.cargo/bin" /opt/homebrew/bin \
               /usr/local/bin "$HOME/.local/share/mise/shims" /usr/bin; do
        [ -x "$dir/$1" ] && { echo "$dir/$1"; return 0; }
    done
    return 1
}

if ! PY=$(find_tool python3); then
    cat >&2 <<'MSG'
herdr-swipe: found neither uv nor python3.

Install uv, which also fetches a Python interpreter for you:

    curl -LsSf https://astral.sh/uv/install.sh | sh

then re-run:

    herdr plugin action invoke herdr-swipe.restart
MSG
    exit 1
fi

echo "herdr-swipe: building virtualenv with $PY"
rm -rf .venv
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
# shellcheck disable=SC2086
.venv/bin/pip install --quiet $DEPS
echo "herdr-swipe: virtualenv ready ($(.venv/bin/python -V 2>&1))"
