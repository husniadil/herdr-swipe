"""Properties the gesture machine must hold for any sequence of touches.

The example tests next door say what specific gestures do. These say what must
never happen, whatever the fingers get up to -- including sequences nobody
would think to write down: a finger that lands twice, three that lift out of
order, a hand that rests for a second in the middle of a swipe.

Seeded, so a failure here is reproducible rather than a story about a run
nobody can repeat.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gestures  # noqa: E402

RUNS = 300
EVENTS = 60


def touch_sequence(rng, down):
    """A handful of touches changing the way a real event reports them."""
    changed = []
    for _ in range(rng.randint(0, 3)):
        if down and rng.random() < 0.4:
            identity = rng.choice(list(down))
            if rng.random() < 0.5:
                x, y = down.pop(identity)
                changed.append((identity, x, y, False))
            else:
                x, y = down[identity]
                x, y = x + rng.uniform(-0.3, 0.3), y + rng.uniform(-0.3, 0.3)
                down[identity] = (x, y)
                changed.append((identity, x, y, True))
        else:
            identity = rng.randint(1, 5)
            x, y = rng.uniform(0, 1), rng.uniform(0, 1)
            down[identity] = (x, y)
            changed.append((identity, x, y, True))
    return changed


class Properties(unittest.TestCase):
    def drive(self, seed, host_value):
        rng = random.Random(seed)
        machine = gestures.Gestures()
        now, down, actions = 1000.0, {}, 0

        def host():
            return host_value

        for _ in range(EVENTS):
            now += rng.choice([0.01, 0.05, 0.3, 1.0])
            swallow, action = machine.update(touch_sequence(rng, down), now, host)

            if not host_value:
                self.assertEqual((swallow, action), (False, None),
                                 f"seed {seed}: touched a machine we do not own")
            if action:
                verb, direction = action
                self.assertIn(verb, ("pane", "tab", "space", "attention"))
                self.assertIn(direction, ("left", "right", "up", "down", None))
                actions += 1
            if not down:
                self.assertLessEqual(actions, 1,
                                     f"seed {seed}: one session, {actions} actions")
                actions = 0
                self.assertEqual(machine.fingers, 0,
                                 f"seed {seed}: fingers left over between sessions")

        for identity, (x, y) in list(down.items()):
            machine.update([(identity, x, y, False)], now, host)
        swallow, _ = machine.update([], now + 10.0, host)
        self.assertFalse(swallow,
                         f"seed {seed}: still swallowing long after the hand left")

    def test_inside_a_terminal_we_take_over(self):
        for seed in range(RUNS):
            self.drive(seed, True)

    def test_anywhere_else_nothing_is_touched(self):
        for seed in range(RUNS):
            self.drive(seed, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
