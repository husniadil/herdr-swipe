"""Tests for the gesture state machine.

These are the ones that could not exist while the logic lived inside the tap
callback. Every daemon bug found so far is in here as a case: fingers counted
by identity, a session that never closed, three-finger events eaten outside a
terminal, a swipe that also scrolled.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gestures  # noqa: E402


def HOST():
    """A listed terminal is in front."""
    return True


def ELSEWHERE():
    """Something else is in front: a browser, the Finder, anything."""
    return False


class Pad:
    """A trackpad you can put fingers on, one event at a time."""

    def __init__(self, host=HOST, **kwargs):
        self.g = gestures.Gestures(**kwargs)
        self.host = host
        self.now = 100.0
        self.down = {}

    def touch(self, ids, dx=0.0, dy=0.0, dt=0.02):
        """Put these fingers down, or move the ones already there by (dx, dy)."""
        self.now += dt
        changed = []
        for i in ids:
            x, y = self.down.get(i, (0.5, 0.5))
            x, y = x + dx, y + dy
            self.down[i] = (x, y)
            changed.append((i, x, y, True))
        return self.g.update(changed, self.now, self.host)

    def lift(self, ids, dt=0.02):
        self.now += dt
        changed = [(i, *self.down.pop(i), False) for i in ids]
        return self.g.update(changed, self.now, self.host)

    def wait(self, seconds):
        self.now += seconds
        return self.g.update([], self.now, self.host)


class Recognition(unittest.TestCase):
    def test_two_fingers_sideways_moves_a_pane(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertEqual(pad.touch([1, 2], dx=0.2)[1], ("pane", "right"))

    def test_two_fingers_back_the_other_way(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertEqual(pad.touch([1, 2], dx=-0.2)[1], ("pane", "left"))

    def test_three_fingers_sideways_moves_a_tab(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        self.assertEqual(pad.touch([1, 2, 3], dx=0.2)[1], ("tab", "right"))

    def test_three_fingers_upward_moves_a_space(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        # Fingers up walks up the list, which is "left" to the navigation side.
        self.assertEqual(pad.touch([1, 2, 3], dy=0.2)[1], ("space", "up"))

    def test_a_diagonal_smear_is_not_a_gesture(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertIsNone(pad.touch([1, 2], dx=0.2, dy=0.2)[1])

    def test_a_short_drag_is_below_the_threshold(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertIsNone(pad.touch([1, 2], dx=0.01)[1])

    def test_four_fingers_mean_nothing_here(self):
        pad = Pad()
        pad.touch([1, 2, 3, 4])
        self.assertIsNone(pad.touch([1, 2, 3, 4], dx=0.3)[1])

    def test_one_session_fires_once(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertEqual(pad.touch([1, 2], dx=0.2)[1], ("pane", "right"))
        self.assertIsNone(pad.touch([1, 2], dx=0.2)[1])
        self.assertIsNone(pad.touch([1, 2], dx=0.2)[1])


class FingerCounting(unittest.TestCase):
    def test_fingers_are_counted_by_identity_across_events(self):
        # An event reports only what changed. Counting per event would see one
        # finger here, not three, and no three-finger gesture would ever fire.
        pad = Pad()
        pad.touch([1])
        pad.touch([2])
        pad.touch([3])
        self.assertEqual(pad.touch([1, 2, 3], dx=0.2)[1], ("tab", "right"))

    def test_a_finger_that_lifts_leaves_the_count(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.lift([3])
        # Two fingers left, but the session peaked at three, so this is still
        # the tab gesture the hand asked for -- not the pane one.
        self.assertEqual(pad.touch([1, 2], dx=0.2)[1], ("tab", "right"))


class Tap(unittest.TestCase):
    def test_three_fingers_down_and_straight_up_is_a_tap(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        self.assertEqual(pad.lift([1, 2, 3])[1], ("attention", None))

    def test_a_tap_that_travelled_is_a_swipe_not_a_tap(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.touch([1, 2, 3], dx=0.05)
        self.assertIsNone(pad.lift([1, 2, 3])[1])

    def test_a_tap_held_too_long_is_a_rest_not_a_tap(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.wait(1.0)
        self.assertIsNone(pad.lift([1, 2, 3])[1])

    def test_two_fingers_down_and_up_is_not_a_tap(self):
        pad = Pad()
        pad.touch([1, 2])
        self.assertIsNone(pad.lift([1, 2])[1])


class Swallowing(unittest.TestCase):
    def test_a_three_finger_session_is_swallowed(self):
        pad = Pad()
        self.assertTrue(pad.touch([1, 2, 3])[0])

    def test_plain_two_finger_scrolling_passes_through(self):
        pad = Pad()
        self.assertFalse(pad.touch([1, 2])[0])
        self.assertFalse(pad.touch([1, 2], dx=0.01)[0])

    def test_a_recognised_two_finger_swipe_claims_the_rest_of_itself(self):
        # Otherwise the pane moves and the terminal scrolls at the same time.
        pad = Pad()
        pad.touch([1, 2])
        self.assertTrue(pad.touch([1, 2], dx=0.2)[0])
        self.assertTrue(pad.touch([1, 2], dx=0.05)[0])

    def test_momentum_keeps_swallowing_after_the_fingers_are_gone(self):
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.lift([1, 2, 3])
        self.assertTrue(pad.wait(0.1)[0])        # inertial tail
        self.assertFalse(pad.wait(1.0)[0])       # long past it

    def test_nothing_is_touched_outside_a_terminal_we_take_over(self):
        # Swallowing three-finger events everywhere would kill page navigation
        # in every browser on the machine.
        pad = Pad(host=ELSEWHERE)
        self.assertEqual(pad.touch([1, 2, 3]), (False, None))
        self.assertEqual(pad.touch([1, 2, 3], dx=0.3), (False, None))
        self.assertEqual(pad.lift([1, 2, 3]), (False, None))

    def test_the_host_is_asked_once_per_session(self):
        calls = []

        def host():
            calls.append(1)
            return True

        pad = Pad(host=host)
        pad.touch([1, 2])
        pad.touch([1, 2], dx=0.05)
        pad.touch([1, 2], dx=0.05)
        pad.lift([1, 2])
        self.assertEqual(len(calls), 1)
        pad.touch([9, 8])
        self.assertEqual(len(calls), 2)


class LostEvents(unittest.TestCase):
    def test_forgetting_the_session_lets_the_trackpad_recover(self):
        # The system disables the tap mid-gesture; the lifts for the fingers
        # already down are never delivered. Without forgetting them the peak
        # stays at three and every later event is swallowed for good.
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.g.forget()
        pad.down.clear()
        self.assertFalse(pad.touch([7, 8])[0])
        self.assertFalse(pad.touch([7, 8], dx=0.01)[0])

    def test_without_forgetting_the_trackpad_stays_dead(self):
        # The failure the line above prevents, spelled out.
        pad = Pad()
        pad.touch([1, 2, 3])
        pad.down.clear()                      # their lifts never arrive
        self.assertTrue(pad.touch([7, 8])[0])
        self.assertTrue(pad.touch([7, 8], dx=0.01)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
