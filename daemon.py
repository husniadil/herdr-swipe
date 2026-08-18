#!/usr/bin/env python3
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
  * macOS three-finger gestures must be ON, or the third finger is never
    reported to us and every three-finger feature dies without a trace.
  * The process needs Accessibility. It inherits that from the terminal that
    launched it, which is why having Herdr start this beats launching it as a
    standalone app.
"""

import ctypes
import ctypes.util
import math
import os
import queue
import sys
import threading
import time

import Cocoa
import Quartz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import herdr_nav  # noqa: E402

ITERM = "com.googlecode.iterm2"
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

TOUCHING = (Cocoa.NSTouchPhaseBegan | Cocoa.NSTouchPhaseMoved
            | Cocoa.NSTouchPhaseStationary)

_active = {}            # touch identity -> [x0, y0, x, y]
_fired = False
_peak = 0
_began_at = 0.0
_max_travel = 0.0
_in_iterm = None        # decided once per touch session, see below
_swallow_until = 0.0

os.makedirs(os.path.dirname(TRACE), exist_ok=True)
if os.path.exists(TRACE) and os.path.getsize(TRACE) > TRACE_MAX_BYTES:
    os.replace(TRACE, TRACE + ".1")
_trace_file = open(TRACE, "a", buffering=1)   # line buffered, opened once


def trace(msg):
    _trace_file.write(f"{time.strftime('%H:%M:%S')} [app] {msg}\n")


def accessibility_trusted():
    """PyObjC does not expose the AX trust API, so call it directly."""
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


def _iterm_in_front():
    """Is iTerm2 in front?

    frontmostApplication() alone is not enough: a hotkey window is a panel that
    never makes its application frontmost, so the workspace keeps naming
    whatever was there before. Fall back to the top of the window stack, a few
    deep, because notifications and overlays can sit above it.

    The window scan costs ~0.7ms against 0.02ms for the frontmost check, which
    is why the caller asks once per touch session rather than per event: this
    runs inside an active tap, on the input path for the whole machine.
    """
    app = Cocoa.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None and app.bundleIdentifier() == ITERM:
        return True

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


def in_iterm():
    global _in_iterm
    if _in_iterm is None:
        _in_iterm = _iterm_in_front()
    return _in_iterm


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
        except herdr_nav.HerdrUnavailable as exc:
            trace(f"  herdr unavailable: {exc}")
        except Exception as exc:                      # never kill the worker
            trace(f"  unexpected: {type(exc).__name__}: {exc}")


threading.Thread(target=_worker, daemon=True).start()


def dispatch(label, fn, *args):
    """Hand work to the worker. Never blocks: an active tap is the input path."""
    _work.put((label, fn, args))


def handle(proxy, etype, cg_event, refcon):
    global _fired, _peak, _began_at, _max_travel, _in_iterm, _swallow_until

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
        _began_at, _max_travel, _in_iterm = time.time(), 0.0, None
    _peak = max(_peak, n)

    for entry in _active.values():
        _max_travel = max(_max_travel,
                          math.hypot(entry[2] - entry[0], entry[3] - entry[1]))

    if n == 0:
        was_three = _peak >= TAB_FINGERS
        was_tap = (_peak == TAB_FINGERS and not _fired
                   and _max_travel < TAP_MAX_TRAVEL
                   and time.time() - _began_at < TAP_MAX_SECONDS)
        _fired, _peak = False, 0
        if was_three:
            _swallow_until = time.time() + MOMENTUM_SECONDS
        if was_tap and in_iterm():
            dispatch("TAP -> agent waiting", herdr_nav.attention)
        _in_iterm = None
        return None if time.time() < _swallow_until else cg_event

    # Once a third finger lands the session is ours for its whole duration,
    # including the two-finger moments while fingers settle or lift.
    swallow = _peak >= TAB_FINGERS or time.time() < _swallow_until
    passthrough = None if swallow else cg_event

    if _fired or not in_iterm():
        return passthrough

    dx = sum(e[2] - e[0] for e in _active.values()) / n
    dy = sum(e[3] - e[1] for e in _active.values()) / n
    horizontal = abs(dx) > THRESHOLD and abs(dx) > abs(dy) * AXIS_BIAS
    vertical = abs(dy) > THRESHOLD and abs(dy) > abs(dx) * AXIS_BIAS

    if n == SWIPE_FINGERS and horizontal:
        _fired = True
        side = "right" if dx > 0 else "left"
        dispatch(f"2-finger {side} -> pane", herdr_nav.pane_step, side)
    elif n == TAB_FINGERS and horizontal:
        _fired = True
        side = "right" if dx > 0 else "left"
        dispatch(f"3-finger {side} -> tab", herdr_nav.tab_step, side)
    elif n == TAB_FINGERS and vertical:
        _fired = True
        up = dy > 0
        dispatch(f"3-finger {'up' if up else 'down'} -> space",
                 herdr_nav.workspace_step,
                 "up" if up == UP_IS_PREVIOUS else "down")

    return passthrough


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
