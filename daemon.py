#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pyobjc-framework-Cocoa",
#     "pyobjc-framework-Quartz",
# ]
# ///
"""HerdrSwipe: trackpad gestures for Herdr.

    two fingers,   left/right  -> pane
    three fingers, left/right  -> tab
    three fingers, up/down     -> space
    three fingers, tap         -> the agent waiting on you

One gesture, one meaning: no level escalates into another, so what a swipe will
do is knowable before you make it.

Three-finger events are swallowed, because macOS would otherwise turn them into
page navigation, Mission Control, or stray scrolling inside a TUI. Four-finger
gestures are left alone, so Mission Control and Spaces keep working there.

Two dependencies, both silent when missing:
  * macOS three-finger gestures must be ON, and three-finger drag must be OFF.
    Either way wrong, the third finger never reaches us and every three-finger
    feature dies without a trace.
  * The process needs Accessibility. It inherits that from the terminal that
    launched it, which is why having Herdr start this beats launching it as a
    standalone app.
"""

import ctypes
import ctypes.util
import fcntl
import math
import os
import queue
import sys
import threading
import time

import Cocoa
import Quartz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import navigation  # noqa: E402

ITERM = "com.googlecode.iterm2"

# Terminals whose gestures we take over. Override with HERDR_SWIPE_HOSTS, a
# comma-separated list of bundle identifiers.
DEFAULT_HOSTS = ",".join((
    ITERM,
    "com.apple.Terminal",
    "com.mitchellh.ghostty",
    "net.kovidgoyal.kitty",
    "com.github.wez.wezterm",
    "dev.warp.Warp-Stable",
))
HOSTS = frozenset(
    part.strip()
    for part in os.environ.get("HERDR_SWIPE_HOSTS", DEFAULT_HOSTS).split(",")
    if part.strip()
)
NS_GESTURE, NS_SCROLL = 29, 22

SWIPE_FINGERS, TAB_FINGERS = 2, 3
THRESHOLD = 0.08        # travel before a swipe counts
AXIS_BIAS = 2.0         # how much one axis must beat the other
TAP_MAX_TRAVEL = 0.03
TAP_MAX_SECONDS = 0.4
UP_IS_PREVIOUS = True   # fingers up walks up the space list

# Inertial scroll arrives after the fingers are gone, so a three-finger flick
# would leak a tail of scrolling unless we keep swallowing for a moment.
MOMENTUM_SECONDS = 0.6

TRACE = os.path.expanduser("~/.local/state/herdr-swipe/trace.log")
TRACE_MAX_BYTES = 1_000_000
PIDFILE = os.path.expanduser("~/.local/state/herdr-swipe/daemon.pid")

TOUCHING = (Cocoa.NSTouchPhaseBegan | Cocoa.NSTouchPhaseMoved
            | Cocoa.NSTouchPhaseStationary)

_active = {}            # touch identity -> [x0, y0, x, y]
_fired = False
_peak = 0
_began_at = 0.0
_max_travel = 0.0
_in_host = None         # decided once per touch session, see below
_swallow_until = 0.0

os.makedirs(os.path.dirname(TRACE), exist_ok=True)
if os.path.exists(TRACE) and os.path.getsize(TRACE) > TRACE_MAX_BYTES:
    os.replace(TRACE, TRACE + ".1")
_trace_file = open(TRACE, "a", buffering=1)   # line buffered, opened once


def trace(msg):
    _trace_file.write(f"{time.strftime('%H:%M:%S')} [app] {msg}\n")


def claim_singleton(timeout=5.0):
    """Take the exclusive lock, or return None if another daemon holds it.

    Two daemons mean every gesture fires twice, and a shell script cannot
    prevent that: kill-then-launch is two steps, so two concurrent launches
    interleave and both survive. The lock is held by the process that must be
    unique, which is the only place the guarantee can actually be made.

    The wait exists because a restart kills the previous daemon and starts the
    next one immediately; the new one should outlast that overlap, not lose to
    it. The handle is returned so the caller can keep it alive: closing it, or
    letting it be collected, releases the lock.
    """
    handle = open(PIDFILE, "a+")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            time.sleep(0.1)
            continue
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        return handle


def accessibility_trusted():
    """PyObjC does not expose the AX trust API, so call it directly."""
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


def _host_in_front():
    """Is one of the terminals in HOSTS in front?

    frontmostApplication() alone is not enough for iTerm2: a hotkey window is a
    panel that never makes its application frontmost, so the workspace keeps
    naming whatever was there before. Fall back to the top of the window stack,
    a few deep, because notifications and overlays can sit above it.

    That fallback stays iTerm2-only on purpose. No other terminal here has a
    window that hides its own app from the workspace, so running the scan for
    them would claim gestures whenever one merely sits in the background.

    The window scan costs ~0.7ms against 0.02ms for the frontmost check, which
    is why the caller asks once per touch session rather than per event: this
    runs inside an active tap, on the input path for the whole machine.
    """
    app = Cocoa.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None and app.bundleIdentifier() in HOSTS:
        return True

    if ITERM not in HOSTS:
        return False

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    checked = 0
    for win in info or []:
        if (win.get("kCGWindowBounds") or {}).get("Height", 0) < 100:
            continue      # menu bars, HUDs, slivers
        running = Cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            win.get("kCGWindowOwnerPID"))
        if running is not None and running.bundleIdentifier() == ITERM:
            return True
        checked += 1
        if checked >= 3:
            break
    return False


def in_host():
    global _in_host
    if _in_host is None:
        _in_host = _host_in_front()
    return _in_host


# Gestures are handled by one worker, in the order they were made. Two threads
# would each read the current focus and then write a new one, so a quick pair
# of swipes could land somewhere neither of them meant.
_work = queue.Queue()


def _worker():
    while True:
        label, fn, args = _work.get()
        trace(label)
        try:
            level, target = fn(*args)
            trace(f"  herdr: {level} -> {target}" if level
                  else "  herdr: nowhere to go")
        except navigation.HerdrUnavailable as exc:
            trace(f"  herdr unavailable: {exc}")
        except Exception as exc:                      # never kill the worker
            trace(f"  unexpected: {type(exc).__name__}: {exc}")


threading.Thread(target=_worker, daemon=True).start()


def dispatch(label, fn, *args):
    """Hand work to the worker. Never blocks: an active tap is the input path."""
    _work.put((label, fn, args))


def handle(proxy, etype, cg_event, refcon):
    global _fired, _peak, _began_at, _max_travel, _in_host, _swallow_until

    if etype in (Quartz.kCGEventTapDisabledByTimeout,
                 Quartz.kCGEventTapDisabledByUserInput):
        Quartz.CGEventTapEnable(_tap, True)
        trace("tap re-enabled after the system disabled it")
        return cg_event

    if etype == NS_GESTURE:
        event = Cocoa.NSEvent.eventWithCGEvent_(cg_event)
        # A gesture event carries only the touches that CHANGED, not every
        # finger down, so tracking by identity is the only honest finger count.
        for touch in (event.allTouches() if event else None) or []:
            pos = touch.normalizedPosition()
            if touch.phase() & TOUCHING:
                entry = _active.get(touch.identity())
                if entry is None:
                    _active[touch.identity()] = [pos.x, pos.y, pos.x, pos.y]
                else:
                    entry[2], entry[3] = pos.x, pos.y
            else:
                _active.pop(touch.identity(), None)

    n = len(_active)
    if n and not _peak:                    # first finger of a new session
        _began_at, _max_travel, _in_host = time.time(), 0.0, None
    _peak = max(_peak, n)

    for entry in _active.values():
        _max_travel = max(_max_travel,
                          math.hypot(entry[2] - entry[0], entry[3] - entry[1]))

    if n == 0:
        # A three-finger session, or any session that fired, keeps swallowing
        # briefly: inertial scroll arrives after the fingers are gone.
        claimed = _peak >= TAB_FINGERS or _fired
        was_tap = (_peak == TAB_FINGERS and not _fired
                   and _max_travel < TAP_MAX_TRAVEL
                   and time.time() - _began_at < TAP_MAX_SECONDS)
        _fired, _peak = False, 0
        if claimed:
            _swallow_until = time.time() + MOMENTUM_SECONDS
        if was_tap and in_host():
            dispatch("TAP -> agent waiting", navigation.attention)
        _in_host = None
        return None if time.time() < _swallow_until else cg_event

    # Once a third finger lands the session is ours for its whole duration,
    # including the two-finger moments while fingers settle or lift. A
    # recognised two-finger swipe claims the rest of its session too: without
    # that the scroll keeps flowing to the terminal, so one gesture both moves
    # the pane and scrolls what is inside it. Two-finger scrolling that never
    # becomes a swipe is untouched.
    swallow = _peak >= TAB_FINGERS or _fired or time.time() < _swallow_until
    passthrough = None if swallow else cg_event

    if _fired or not in_host():
        return passthrough

    dx = sum(e[2] - e[0] for e in _active.values()) / n
    dy = sum(e[3] - e[1] for e in _active.values()) / n
    horizontal = abs(dx) > THRESHOLD and abs(dx) > abs(dy) * AXIS_BIAS
    vertical = abs(dy) > THRESHOLD and abs(dy) > abs(dx) * AXIS_BIAS

    if n == SWIPE_FINGERS and horizontal:
        _fired = True
        side = "right" if dx > 0 else "left"
        dispatch(f"2-finger {side} -> pane", navigation.pane_step, side)
    elif n == TAB_FINGERS and horizontal:
        _fired = True
        side = "right" if dx > 0 else "left"
        dispatch(f"3-finger {side} -> tab", navigation.tab_step, side)
    elif n == TAB_FINGERS and vertical:
        _fired = True
        up = dy > 0
        dispatch(f"3-finger {'up' if up else 'down'} -> space",
                 navigation.workspace_step,
                 "up" if up == UP_IS_PREVIOUS else "down")

    return passthrough


# CI can prove the dependencies resolve and the module imports, but it cannot
# prove anything about gestures: there is no trackpad and no Accessibility
# grant on a runner. Stop here rather than leaving a daemon running in one.
if "--check-imports" in sys.argv:
    print(f"herdr-swipe: imports fine (trusted={accessibility_trusted()})")
    raise SystemExit(0)

_lock = claim_singleton()
if _lock is None:
    trace(f"another daemon already holds {PIDFILE}; exiting")
    raise SystemExit(0)

_tap = Quartz.CGEventTapCreate(
    Quartz.kCGSessionEventTap,
    Quartz.kCGHeadInsertEventTap,
    Quartz.kCGEventTapOptionDefault,       # active: three-finger events are ours
    (1 << NS_GESTURE) | (1 << NS_SCROLL),
    handle,
    None,
)
_trusted = accessibility_trusted()
trace(f"daemon started | pid={os.getpid()} trusted={_trusted} "
      f"tap={_tap is not None}")
if _tap is None:
    trace("  CGEventTapCreate returned nil -> no Accessibility permission")
    raise SystemExit(1)
if not _trusted:
    # The tap object exists either way; only AXIsProcessTrusted tells the truth.
    trace("  not trusted: the tap will never receive anything")

_source = Quartz.CFMachPortCreateRunLoopSource(None, _tap, 0)
Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), _source,
                          Quartz.kCFRunLoopCommonModes)
Quartz.CGEventTapEnable(_tap, True)
Quartz.CFRunLoopRun()
