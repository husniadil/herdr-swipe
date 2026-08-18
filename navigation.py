"""Herdr navigation, one level per call.

    pane_step(direction)       move within the tab
    tab_step(direction)        move within the space
    workspace_step(direction)  move between spaces
    attention()                go to the agent waiting on you

Each function moves exactly one level and never escalates into another, so a
caller binding these to gestures gets a mapping that is knowable in advance.

Zero dependencies. Safe to call when Herdr is not running: every failure comes
back as HerdrUnavailable rather than an exception from the socket or the JSON.

Note on the transport: Herdr's socket serves exactly one request per connection
and then closes it. Only subscriptions keep a connection open. So every call
here opens its own connection; a local AF_UNIX connect costs well under a
millisecond.
"""

import json
import os
import socket

DEFAULT_SOCKET = os.path.expanduser("~/.config/herdr/herdr.sock")
BACK, FORWARD = "left", "right"

# Which agent states are actually asking for you, most urgent first.
# `blocked` is waiting on an answer; `done` finished unseen. `working`,
# `idle` and `unknown` are not requests for attention.
ATTENTION = ("blocked", "done")

# Directions are one axis with two ends. "up" and "left" both mean earlier in
# the list, "down" and "right" later, so a vertical gesture can use the same
# machinery as a horizontal one.
_ALIASES = {"left": BACK, "up": BACK, "right": FORWARD, "down": FORWARD}


class HerdrUnavailable(Exception):
    pass


def _direction(value):
    try:
        return _ALIASES[value]
    except KeyError:
        raise ValueError(
            f"direction must be one of {sorted(_ALIASES)}, got {value!r}"
        ) from None


def call(method, params=None, path=DEFAULT_SOCKET, timeout=2.0):
    """Send one request, return its result. One connection, one request."""
    req = json.dumps({"id": "nav", "method": method, "params": params or {}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((req + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise HerdrUnavailable(f"{method}: connection closed early")
                buf += chunk
    except OSError as exc:
        raise HerdrUnavailable(f"{method}: {exc}") from exc

    # A malformed or unexpected reply is still Herdr being unavailable to us;
    # callers should never have to catch JSON or key errors from in here.
    try:
        msg = json.loads(buf.split(b"\n", 1)[0])
    except ValueError as exc:
        raise HerdrUnavailable(f"{method}: unparseable reply: {exc}") from exc
    if "error" in msg:
        raise HerdrUnavailable(msg["error"].get("message", "unknown error"))
    try:
        return msg["result"]
    except KeyError:
        raise HerdrUnavailable(f"{method}: reply carried no result") from None


def _step(items, current_id, key, direction):
    """Neighbour of current_id in a wrapped ordering, or None if it is alone."""
    if len(items) < 2:
        return None
    ids = [x[key] for x in sorted(items, key=lambda x: x.get("number", 0))]
    try:
        i = ids.index(current_id)
    except ValueError:
        return None
    return ids[(i + (1 if direction == FORWARD else -1)) % len(ids)]


def _reading_order(snapshot):
    """Every pane in the session, in the order the sidebar presents them.

    Space by space, tab by tab, then panes top-left to bottom-right. Derived
    from the snapshot rather than parsed out of pane ids: Herdr numbers panes
    past 9 with letters (w5:pA), so any arithmetic on the id is wrong.
    """
    layouts = {layout["tab_id"]: layout for layout in snapshot.get("layouts", [])}
    order = []
    for workspace in sorted(snapshot.get("workspaces", []),
                            key=lambda w: w.get("number", 0)):
        tabs = [t for t in snapshot.get("tabs", [])
                if t["workspace_id"] == workspace["workspace_id"]]
        for tab in sorted(tabs, key=lambda t: t.get("number", 0)):
            panes = (layouts.get(tab["tab_id"]) or {}).get("panes") or []
            order += [p["pane_id"] for p in
                      sorted(panes, key=lambda p: (p["rect"]["y"], p["rect"]["x"]))]
    return order


def pane_step(direction, path=DEFAULT_SOCKET):
    """Move one pane within the current tab. Returns ("pane", id) or (None, None)."""
    direction = _direction(direction)

    # Herdr's own adjacency first, so the move matches what you see. Omitting
    # pane_id targets the UI-focused pane, which is what a gesture means.
    neighbour = call("pane.neighbor", {"direction": direction}, path)["neighbor"]
    if neighbour.get("neighbor_pane_id"):
        call("pane.focus_direction", {"direction": direction}, path)
        return "pane", neighbour["neighbor_pane_id"]

    # No neighbour that way. A horizontal gesture would otherwise strand you in
    # a full-width pane, which has no left or right at all, so fall through to
    # the next pane in reading order: left to right, top to bottom.
    layout = call("pane.layout", {}, path)["layout"]
    panes = layout.get("panes") or []
    if len(panes) < 2:
        return None, None
    ids = [p["pane_id"] for p in
           sorted(panes, key=lambda p: (p["rect"]["y"], p["rect"]["x"]))]
    try:
        i = ids.index(layout.get("focused_pane_id"))
    except ValueError:
        return None, None
    target = ids[(i + (1 if direction == FORWARD else -1)) % len(ids)]
    call("pane.focus", {"pane_id": target}, path)
    return "pane", target


def tab_step(direction, path=DEFAULT_SOCKET):
    """Move one tab within the current space, wrapping at the ends.

    The tab's own last-focused pane is restored; with a gesture per level there
    is no reason to override where the user left off.
    """
    direction = _direction(direction)
    snap = call("session.snapshot", path=path)["snapshot"]
    tabs = [t for t in snap["tabs"]
            if t["workspace_id"] == snap["focused_workspace_id"]]
    target = _step(tabs, snap["focused_tab_id"], "tab_id", direction)
    if not target:
        return None, None
    call("tab.focus", {"tab_id": target}, path)
    return "tab", target


def workspace_step(direction, path=DEFAULT_SOCKET):
    """Move one space, wrapping at the ends. The space keeps its own focus."""
    direction = _direction(direction)
    snap = call("session.snapshot", path=path)["snapshot"]
    target = _step(snap["workspaces"], snap["focused_workspace_id"],
                   "workspace_id", direction)
    if not target:
        return None, None
    call("workspace.focus", {"workspace_id": target}, path)
    return "workspace", target


def attention(path=DEFAULT_SOCKET):
    """Focus the next agent asking for attention. Returns (state, pane_id).

    Walks the panes in sidebar order starting after the current one, so
    repeated calls visit every waiting agent instead of bouncing between two.
    `blocked` agents come before `done` ones: one is holding a question open,
    the other merely finished while you were looking elsewhere.
    """
    agents = call("agent.list", path=path)["agents"]
    waiting = {a["pane_id"]: a for a in agents
               if a.get("agent_status") in ATTENTION}
    if not waiting:
        return None, None

    snap = call("session.snapshot", path=path)["snapshot"]
    order = _reading_order(snap)
    try:
        start = order.index(snap["focused_pane_id"]) + 1
    except ValueError:
        start = 0
    rotated = order[start:] + order[:start]

    for state in ATTENTION:
        for pane_id in rotated:
            agent = waiting.get(pane_id)
            if agent is None or agent["agent_status"] != state:
                continue
            # Focusing a pane in another space needs the space and tab brought
            # forward first; pane.focus alone does not carry them.
            call("workspace.focus", {"workspace_id": agent["workspace_id"]}, path)
            call("tab.focus", {"tab_id": agent["tab_id"]}, path)
            call("pane.focus", {"pane_id": pane_id}, path)
            return state, pane_id

    return None, None


VERBS = {"pane": pane_step, "tab": tab_step, "space": workspace_step}


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "pane:right"
    try:
        if arg == "attention":
            level, target = attention()
        elif ":" in arg and arg.split(":", 1)[0] in VERBS:
            what, direction = arg.split(":", 1)
            level, target = VERBS[what](direction)
        else:
            print(f"usage: {sys.argv[0]} "
                  f"{{{'|'.join(VERBS)}}}:{{left|right|up|down}} | attention")
            raise SystemExit(2)
    except HerdrUnavailable as exc:
        print(f"herdr unavailable: {exc}")
        raise SystemExit(1)
    print(f"{level} -> {target}" if level else "no-op (nowhere to go)")
