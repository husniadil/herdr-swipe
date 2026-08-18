"""Tests for the navigation policy: the part with decisions in it.

The daemon is deliberately not covered. Gesture recognition needs a trackpad,
a hand, and an Accessibility grant, none of which exist on a CI runner, and a
test that fakes all three would only assert that the fake works.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_herdr import FakeHerdr  # noqa: E402

import navigation  # noqa: E402


def layout(tab_id, panes, focused):
    return {"tab_id": tab_id, "focused_pane_id": focused,
            "panes": [{"pane_id": p, "rect": {"x": x, "y": y, "width": 100,
                                              "height": 10}}
                      for p, x, y in panes]}


# Two panes side by side above two full-width ones, which is the layout that
# used to strand you: the wide panes have no left or right neighbour at all.
MIXED = layout("w1:t1",
               [("w1:p1", 0, 0), ("w1:pA", 100, 0),
                ("w1:p2", 0, 10), ("w1:p3", 0, 20)],
               "w1:pA")


# One tall pane on the left, two stacked on the right. This is the layout that
# exposed the old mix of Herdr adjacency and positional fallback: Herdr reports
# the pane left of w1:pC as w1:p1, jumping over w1:pB, and reports nothing to
# the left of w1:p1 at all. Forward visited all three, backward ping-ponged
# between two, and w1:pB could not be reached going left.
LSHAPE = [("w1:p1", 0, 0), ("w1:pB", 50, 0), ("w1:pC", 50, 50)]

# Herdr's real answers for that layout, taken from a live pane.neighbor probe.
# The fake serves them so a reversion to adjacency-first reproduces the actual
# bug here rather than dying on a missing canned reply.
ADJACENT = {
    ("w1:p1", "left"): None, ("w1:p1", "right"): "w1:pB",
    ("w1:pB", "left"): "w1:p1", ("w1:pB", "right"): None,
    ("w1:pC", "left"): "w1:p1", ("w1:pC", "right"): None,
}


class PaneStep(unittest.TestCase):
    def walk(self, direction, start, steps):
        """Follow the focus for real: each step re-reads a layout that moved."""
        visited, focus = [], start
        for _ in range(steps):
            herdr = FakeHerdr({
                "pane.layout": {"layout": layout("w1:t1", LSHAPE, focus)},
                "pane.focus": {},
                # Served, but never consulted: spatial adjacency is not the
                # inverse of itself, so relying on it cannot give a true cycle.
                "pane.neighbor": {"neighbor": (
                    {"neighbor_pane_id": ADJACENT[(focus, direction)]}
                    if ADJACENT[(focus, direction)] else {})},
                "pane.focus_direction": {},
            })
            self.addCleanup(herdr.close)
            _, focus = navigation.pane_step(direction, herdr.path)
            visited.append(focus)
        return visited

    def test_forward_cycles_through_every_pane(self):
        self.assertEqual(self.walk("right", "w1:p1", 3),
                         ["w1:pB", "w1:pC", "w1:p1"])

    def test_backward_cycles_through_every_pane(self):
        # The one that used to fail: w1:pB was unreachable going left.
        self.assertEqual(self.walk("left", "w1:pC", 3),
                         ["w1:pB", "w1:p1", "w1:pC"])

    def test_left_is_the_exact_inverse_of_right(self):
        forward = self.walk("right", "w1:pB", 1)[0]
        back = self.walk("left", forward, 1)[0]
        self.assertEqual(back, "w1:pB")

    def test_a_lone_pane_has_nowhere_to_go(self):
        herdr = FakeHerdr({"pane.layout": {"layout": layout(
            "w1:t1", [("w1:p1", 0, 0)], "w1:p1")}})
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.pane_step("right", herdr.path), (None, None))


class Attention(unittest.TestCase):
    SNAPSHOT = {"snapshot": {
        "focused_workspace_id": "w1", "focused_tab_id": "w1:t1",
        "focused_pane_id": "w1:p1",
        "workspaces": [{"workspace_id": "w1", "number": 1}],
        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "number": 1}],
        "layouts": [MIXED],
    }}

    def _agents(self, *pairs):
        return {"agents": [{"pane_id": p, "agent_status": s,
                            "workspace_id": "w1", "tab_id": "w1:t1"}
                           for p, s in pairs]}

    def test_order_comes_from_the_layout_not_from_the_pane_id(self):
        # Herdr numbers panes past 9 with letters, so w1:pA cannot be parsed
        # into a number. Sorting by a parsed id sent it to the front of the
        # queue; on screen it sits second, between p1 and p2.
        #
        # Focus on the last pane, so the cycle wraps and the two orderings
        # disagree about who comes first: the layout says p1, a parsed id says
        # pA. Anything less specific than this passes either way.
        snapshot = dict(self.SNAPSHOT)
        snapshot["snapshot"] = dict(self.SNAPSHOT["snapshot"],
                                    focused_pane_id="w1:p3")
        herdr = FakeHerdr({
            "agent.list": self._agents(("w1:p1", "blocked"), ("w1:pA", "blocked")),
            "session.snapshot": snapshot,
            "workspace.focus": {}, "tab.focus": {}, "pane.focus": {},
        })
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.attention(herdr.path), ("blocked", "w1:p1"))

    def test_blocked_outranks_done(self):
        herdr = FakeHerdr({
            "agent.list": self._agents(("w1:pA", "done"), ("w1:p3", "blocked")),
            "session.snapshot": self.SNAPSHOT,
            "workspace.focus": {}, "tab.focus": {}, "pane.focus": {},
        })
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.attention(herdr.path), ("blocked", "w1:p3"))

    def test_nothing_waiting_is_not_an_error(self):
        herdr = FakeHerdr({"agent.list": self._agents(("w1:p1", "idle"))})
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.attention(herdr.path), (None, None))


class ReplyShape(unittest.TestCase):
    """A reply Herdr never sends must still arrive as HerdrUnavailable.

    Without this the module raises KeyError from inside itself, which reads as
    a bug in the caller rather than as Herdr having changed under us.
    """

    def test_a_renamed_field_is_herdr_unavailable(self):
        herdr = FakeHerdr({"pane.neighbor": {"neighbour": {}}})   # British spelling
        self.addCleanup(herdr.close)
        with self.assertRaises(navigation.HerdrUnavailable):
            navigation.pane_step("right", herdr.path)

    def test_a_bad_direction_is_still_a_valueerror(self):
        # The wrapper must not swallow the caller's own mistake.
        herdr = FakeHerdr({})
        self.addCleanup(herdr.close)
        with self.assertRaises(ValueError):
            navigation.pane_step("sideways", herdr.path)


class Transport(unittest.TestCase):
    def test_a_missing_socket_is_reported_not_raised_raw(self):
        with self.assertRaises(navigation.HerdrUnavailable):
            navigation.call("ping", path="/nonexistent/herdr.sock")

    def test_an_error_reply_becomes_herdr_unavailable(self):
        herdr = FakeHerdr({})            # every method answers with an error
        self.addCleanup(herdr.close)
        with self.assertRaises(navigation.HerdrUnavailable):
            navigation.call("pane.neighbor", {"direction": "left"}, herdr.path)

    def test_direction_aliases(self):
        with self.assertRaises(ValueError):
            navigation.pane_step("sideways")


if __name__ == "__main__":
    unittest.main(verbosity=2)
