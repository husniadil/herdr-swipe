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


class PaneStep(unittest.TestCase):
    def test_uses_herdr_adjacency_when_there_is_a_neighbour(self):
        herdr = FakeHerdr({
            "pane.neighbor": {"neighbor": {"neighbor_pane_id": "w1:p2"}},
            "pane.focus_direction": {},
        })
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.pane_step("right", herdr.path),
                         ("pane", "w1:p2"))
        self.assertIn("pane.focus_direction", herdr.methods())

    def test_falls_through_to_reading_order_when_stranded(self):
        # w1:p2 is full width: no left, no right. Without the fallback a
        # horizontal gesture leaves you there for good.
        herdr = FakeHerdr({
            "pane.neighbor": {"neighbor": {}},
            "pane.layout": {"layout": layout(
                "w1:t1", [("w1:p1", 0, 0), ("w1:pA", 100, 0),
                          ("w1:p2", 0, 10), ("w1:p3", 0, 20)], "w1:p2")},
            "pane.focus": {},
        })
        self.addCleanup(herdr.close)
        self.assertEqual(navigation.pane_step("right", herdr.path),
                         ("pane", "w1:p3"))

    def test_left_is_the_exact_inverse_of_right(self):
        replies = {"pane.neighbor": {"neighbor": {}},
                   "pane.layout": {"layout": MIXED}, "pane.focus": {}}
        herdr = FakeHerdr(replies)
        self.addCleanup(herdr.close)
        forward = navigation.pane_step("right", herdr.path)[1]
        back = navigation.pane_step("left", herdr.path)[1]
        self.assertEqual(forward, "w1:p2")   # pA -> next in reading order
        self.assertEqual(back, "w1:p1")      # pA -> previous


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
