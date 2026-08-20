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
page navigation, Mission Control, or stray scrolling inside a TUI. Nothing here
acts on four fingers, and in practice Mission Control and Spaces keep working:
macOS reaches those gestures before a session tap does.

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
import os
import queue
import sys
import threading
import time

import Cocoa
import Quartz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gestures  # noqa: E402
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

# Every delta a scroll event carries, in the field width each is stored in.
# Claimed scroll events are neutralised rather than dropped: AppKit clients
# track a scroll gesture as a stream and stop the world until it ends --
# iTerm2's swipe-between-tabs tracker pumps a nested event loop on the main
# thread waiting for the next scroll event, up to 5s per silence. Dropping the
# stream mid-gesture therefore beachballs the very terminal the gesture was
# for. An event with its deltas zeroed still carries its phase, so trackers
# close cleanly while nothing actually scrolls.
_SCROLL_INT_FIELDS = (
    Quartz.kCGScrollWheelEventDeltaAxis1,
    Quartz.kCGScrollWheelEventDeltaAxis2,
    Quartz.kCGScrollWheelEventPointDeltaAxis1,
    Quartz.kCGScrollWheelEventPointDeltaAxis2,
)
_SCROLL_DOUBLE_FIELDS = (
    Quartz.kCGScrollWheelEventFixedPtDeltaAxis1,
    Quartz.kCGScrollWheelEventFixedPtDeltaAxis2,
)

# A gesture the worker cannot reach in this long has been overtaken by events.
# Herdr blocking for a couple of seconds turns a burst of swipes into a queue
# that drains afterwards, so focus jumps several times once it recovers, long
# after the hand that meant it moved on. Dropping beats replaying.
STALE_SECONDS = 1.0

TRACE = os.path.expanduser("~/.local/state/herdr-swipe/trace.log")
TRACE_MAX_BYTES = 1_000_000
PIDFILE = os.path.expanduser("~/.local/state/herdr-swipe/daemon.pid")

TOUCHING = (Cocoa.NSTouchPhaseBegan | Cocoa.NSTouchPhaseMoved
            | Cocoa.NSTouchPhaseStationary)

# What the fingers mean lives in gestures.py, which knows nothing about Cocoa
# and can therefore be driven by a test. This file owns the tap, the clock and
# the socket; it hands touches over and carries out what comes back.
_session = gestures.Gestures()


os.makedirs(os.path.dirname(TRACE), exist_ok=True)
_trace_file = open(TRACE, "a", buffering=1)   # line buffered, opened once
_trace_bytes = _trace_file.tell()
_trace_lock = threading.Lock()


def trace(msg):
    """Append one line, rotating at the cap.

    Rotating only at startup would be a cap in name alone: this daemon is
    started once and runs for weeks, so the file it actually writes is the one
    that never gets checked. The size is counted rather than stat()ed so the
    common path stays a single write.
    """
    global _trace_file, _trace_bytes
    line = f"{time.strftime('%H:%M:%S')} [app] {msg}\n"
    with _trace_lock:
        _trace_file.write(line)
        _trace_bytes += len(line)
        if _trace_bytes > TRACE_MAX_BYTES:
            _trace_file.close()
            os.replace(TRACE, TRACE + ".1")
            _trace_file = open(TRACE, "a", buffering=1)
            _trace_bytes = 0


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


# Gestures are handled by one worker, in the order they were made. Two threads
# would each read the current focus and then write a new one, so a quick pair
# of swipes could land somewhere neither of them meant.
_work = queue.Queue()


def _worker():
    while True:
        made_at, label, fn, args = _work.get()
        age = time.time() - made_at
        if age > STALE_SECONDS:
            trace(f"{label} (dropped, {age:.1f}s stale)")
            continue
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
    _work.put((time.time(), label, fn, args))


def handle(proxy, etype, cg_event, refcon):
    if etype in (Quartz.kCGEventTapDisabledByTimeout,
                 Quartz.kCGEventTapDisabledByUserInput):
        _session.forget()
        Quartz.CGEventTapEnable(_tap, True)
        trace("tap re-enabled after the system disabled it; touches forgotten")
        return cg_event

    changed = []
    if etype == NS_GESTURE:
        event = Cocoa.NSEvent.eventWithCGEvent_(cg_event)
        # A gesture event carries only the touches that CHANGED, not every
        # finger down, so tracking by identity is the only honest finger count.
        for touch in (event.allTouches() if event else None) or []:
            pos = touch.normalizedPosition()
            changed.append((touch.identity(), pos.x, pos.y,
                            bool(touch.phase() & TOUCHING)))

    swallow, action = _session.update(changed, time.time(), _host_in_front)

    if action:
        verb, direction = action
        if verb == "attention":
            dispatch("TAP -> agent waiting", navigation.attention)
        else:
            dispatch(f"{_session.fingers}-finger {direction} -> {verb}",
                     navigation.VERBS[verb], direction)

    if not swallow:
        return cg_event
    if etype != NS_SCROLL:
        return None
    # See _SCROLL_INT_FIELDS: a claimed scroll stream must end, not vanish.
    for field in _SCROLL_INT_FIELDS:
        Quartz.CGEventSetIntegerValueField(cg_event, field, 0)
    for field in _SCROLL_DOUBLE_FIELDS:
        Quartz.CGEventSetDoubleValueField(cg_event, field, 0.0)
    return cg_event


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
