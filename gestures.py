"""The gesture state machine: touches in, a decision out.

Kept apart from the daemon on purpose. Recognising a gesture needs a trackpad,
a hand and an Accessibility grant, but deciding what a set of touches MEANS is
ordinary logic, and every daemon bug found so far lived here rather than in the
Cocoa plumbing. Nothing in this module imports Cocoa, so it can be driven from
a test at whatever speed and in whatever order the test likes.

The daemon owns the tap, the clock and the socket. This owns the question of
what the fingers are doing.
"""

SWIPE_FINGERS, TAB_FINGERS = 2, 3
THRESHOLD = 0.08        # travel before a swipe counts
AXIS_BIAS = 2.0         # how much one axis must beat the other
TAP_MAX_TRAVEL = 0.03
TAP_MAX_SECONDS = 0.4
UP_IS_PREVIOUS = True   # fingers up walks up the space list

# Inertial scroll arrives after the fingers are gone, so a flick would leak a
# tail of scrolling unless the session keeps swallowing for a moment.
MOMENTUM_SECONDS = 0.6


class Gestures:
    """One trackpad session at a time.

    Feed it the touches an event reported -- only the ones that CHANGED, which
    is all a gesture event carries -- and it answers with what to do and
    whether the event should reach the app underneath.
    """

    def __init__(self, swipe_fingers=SWIPE_FINGERS, tab_fingers=TAB_FINGERS,
                 threshold=THRESHOLD, axis_bias=AXIS_BIAS,
                 tap_max_travel=TAP_MAX_TRAVEL, tap_max_seconds=TAP_MAX_SECONDS,
                 up_is_previous=UP_IS_PREVIOUS,
                 momentum_seconds=MOMENTUM_SECONDS):
        self.swipe_fingers = swipe_fingers
        self.tab_fingers = tab_fingers
        self.threshold = threshold
        self.axis_bias = axis_bias
        self.tap_max_travel = tap_max_travel
        self.tap_max_seconds = tap_max_seconds
        self.up_is_previous = up_is_previous
        self.momentum_seconds = momentum_seconds
        self._active = {}          # identity -> [x0, y0, x, y]
        self._fired = False
        self._peak = 0
        self._began_at = 0.0
        self._max_travel = 0.0
        self._host = None          # asked once per session
        self._swallow_until = 0.0

    @property
    def fingers(self):
        """How many fingers this session has ever had. Zero between sessions."""
        return self._peak

    def forget(self):
        """Drop the session, for when the system disabled the tap.

        A touch is only ever removed by its own end event. If the tap was off
        while the fingers lifted, those events never arrive, and keeping the
        identities would leave a session that never closes: a peak that never
        drops and every later event swallowed for good.
        """
        self._active.clear()
        self._fired, self._peak = False, 0
        self._host = None

    def update(self, changed, now, in_host):
        """Take the touches an event reported. Returns (swallow, action).

        `changed` is (identity, x, y, touching) per touch, in normalized
        coordinates. `in_host` is a callable, asked at most once per session
        because it is expensive and this runs on the machine's input path.
        `action` is (verb, direction) for the daemon to carry out, or None.
        """
        for identity, x, y, touching in changed:
            if touching:
                entry = self._active.get(identity)
                if entry is None:
                    self._active[identity] = [x, y, x, y]
                else:
                    entry[2], entry[3] = x, y
            else:
                self._active.pop(identity, None)

        n = len(self._active)
        if n and not self._peak:                  # first finger of a session
            self._began_at, self._max_travel, self._host = now, 0.0, None
        self._peak = max(self._peak, n)

        for e in self._active.values():
            self._max_travel = max(
                self._max_travel, ((e[2] - e[0]) ** 2 + (e[3] - e[1]) ** 2) ** 0.5)

        if n == 0:
            return self._session_ended(now, in_host)

        # Outside a terminal we take over, the machine is none of our business:
        # swallowing three-finger events everywhere would kill page navigation
        # in every browser on the system.
        if not self._ask_host(in_host):
            return False, None

        # Once a third finger lands the session is ours for its whole duration,
        # including the two-finger moments while fingers settle or lift. A
        # recognised two-finger swipe claims the rest of its session too, or
        # one gesture would both move the pane and scroll what is inside it.
        # Recognise first, then decide about the event: a swipe that fires on
        # this very event has to swallow it too, or one scroll event's worth
        # still reaches the terminal underneath the pane it just moved.
        action = None if self._fired else self._recognise(n)
        swallow = (self._peak >= self.tab_fingers or self._fired
                   or now < self._swallow_until)
        return swallow, action

    def _session_ended(self, now, in_host):
        # A three-finger session, or any that fired, keeps swallowing briefly.
        claimed = self._peak >= self.tab_fingers or self._fired
        was_tap = (self._peak == self.tab_fingers and not self._fired
                   and self._max_travel < self.tap_max_travel
                   and now - self._began_at < self.tap_max_seconds)
        host = self._ask_host(in_host)
        self._fired, self._peak = False, 0
        if claimed and host:
            self._swallow_until = now + self.momentum_seconds
        self._host = None
        action = ("attention", None) if (was_tap and host) else None
        return now < self._swallow_until, action

    def _ask_host(self, in_host):
        if self._host is None:
            self._host = bool(in_host())
        return self._host

    def _recognise(self, n):
        # How many fingers the session ever had, not how many are left. Fingers
        # do not lift together, and without this a three-finger swipe whose
        # first finger leaves early finishes as a two-finger one: you ask for
        # the next tab and get the next pane.
        fingers = self._peak
        dx = sum(e[2] - e[0] for e in self._active.values()) / n
        dy = sum(e[3] - e[1] for e in self._active.values()) / n
        horizontal = abs(dx) > self.threshold and abs(dx) > abs(dy) * self.axis_bias
        vertical = abs(dy) > self.threshold and abs(dy) > abs(dx) * self.axis_bias
        side = "right" if dx > 0 else "left"

        if fingers == self.swipe_fingers and horizontal:
            self._fired = True
            return "pane", side
        if fingers == self.tab_fingers and horizontal:
            self._fired = True
            return "tab", side
        if fingers == self.tab_fingers and vertical:
            self._fired = True
            up = dy > 0
            return "space", "up" if up == self.up_is_previous else "down"
        return None
