# herdr-swipe

Trackpad gestures for [Herdr](https://herdr.dev). Move between panes, tabs and
spaces without reaching for the keyboard, and jump straight to the agent that
is waiting on you.

Herdr already has keybindings for panes, tabs and spaces. It has none for the
question you actually ask all day while running a fleet: *who needs me right
now?* That is what the tap is for.

## Gestures

| Gesture | Does |
| --- | --- |
| Two fingers, left / right | Move one pane, within the tab |
| Three fingers, left / right | Move one tab, within the space |
| Three fingers, up / down | Move one space |
| Three fingers, tap | Focus the agent waiting on you |

One gesture, one meaning. No level escalates into another, so what a swipe will
do is knowable before you make it.

Two-finger scrolling is untouched. Four-finger gestures are left to macOS, so
Mission Control and Spaces keep working there.

**Panes that are stacked rather than side by side** are still reachable: when
there is no neighbour in the direction you swiped, focus moves to the next pane
in reading order — left to right, top to bottom. Every pane is reachable by
repeating one gesture, and swiping back always returns you where you were.

**The tap** visits `blocked` agents before `done` ones: one is holding a
question open, the other merely finished while you were looking elsewhere.
Repeated taps walk through every waiting agent in sidebar order.

## Requirements

macOS only. The daemon reads raw trackpad touches through a `CGEventTap`, which
has no equivalent elsewhere.

Two prerequisites, both of which fail **silently** when missing:

**Three-finger gestures must be enabled in macOS.** System Settings → Trackpad →
More Gestures → "Swipe between pages" must include three fingers. With them off,
macOS never reports a third finger to any application, and every three-finger
feature here dies without a symptom or a log line. Enabling them costs nothing:
this plugin swallows three-finger events, so macOS will not act on them.

**Your terminal needs Accessibility.** System Settings → Privacy & Security →
Accessibility. The daemon inherits the grant from the terminal that launched it,
so no second approval is needed — which is exactly why Herdr starts it rather
than it being a standalone app. If you toggle the permission on while the
terminal is already running, restart the terminal: a running process keeps the
answer it got at launch.

## Install

```sh
herdr plugin install husniadil/herdr-swipe --yes
```

There is no build step. The daemon declares its dependencies inline
([PEP 723](https://peps.python.org/pep-0723/)) and runs under
[uv](https://docs.astral.sh/uv/), which fetches a Python interpreter itself —
so this works on a machine with no Python at all.

Without uv, a virtualenv is built from the same dependency list on first
launch, using whatever `python3` is present. With neither, the plugin says so
and points at uv's one-line installer.

Then swipe. Herdr starts the daemon at every session, and only ever one of it:
the daemon holds an exclusive lock, so a late arrival exits rather than
doubling every gesture.

To restart it after changing the code, or to turn the gestures off:

```sh
herdr plugin action invoke herdr-swipe.restart
herdr plugin action invoke herdr-swipe.stop
```

**Disabling or unlinking the plugin does not stop the daemon.** Herdr has no
shutdown hook, so nothing tears it down; use the `stop` action.

## Tuning

Constants at the top of `daemon.py`:

| Name | Default | Meaning |
| --- | --- | --- |
| `THRESHOLD` | `0.08` | How far fingers travel before a swipe counts |
| `AXIS_BIAS` | `2.0` | How much one axis must beat the other |
| `TAP_MAX_TRAVEL` | `0.03` | Beyond this a tap was really a swipe |
| `TAP_MAX_SECONDS` | `0.4` | Longer than this is a hold, not a tap |
| `UP_IS_PREVIOUS` | `true` | Flip if the space direction reads backwards |

Restart the daemon after editing: Python caches imports, so a running daemon
keeps the code it started with.

### Which terminals it takes over

Gestures are only claimed while one of these is in front — anywhere else macOS
keeps them:

| Terminal | Bundle identifier |
| --- | --- |
| iTerm2 | `com.googlecode.iterm2` |
| Apple Terminal | `com.apple.Terminal` |
| kitty | `net.kovidgoyal.kitty` |
| WezTerm | `com.github.wez.wezterm` |
| Warp | `dev.warp.Warp-Stable` |

Set `HERDR_SWIPE_HOSTS` to a comma-separated list of bundle identifiers to
replace that set — adding a terminal needs no code change:

```sh
HERDR_SWIPE_HOSTS=com.googlecode.iterm2,com.mitchellh.ghostty
```

Find an identifier with
`defaults read /Applications/Foo.app/Contents/Info CFBundleIdentifier`. Warp
ships a different one per release channel, so Preview is not the id above.

The check is which terminal is in front, not which one runs herdr. Swiping in a
listed terminal that has no herdr in it still moves herdr wherever it lives.

## Gotchas

**An agent stuck on a question is a magnet.** Every tap goes to it until you
answer, because it really is the thing waiting on you. If taps seem to ignore
your other agents, one of them has an open prompt.

**Three-finger movement no longer scrolls.** Those events are swallowed so they
cannot leak into a TUI as stray scrolling. Two-finger scrolling is unaffected.

**`herdr plugin link` does not run `[[build]]`.** Only `install` does. A linked
development checkout therefore has no virtualenv, so `run.sh` builds one on
first launch instead of failing.

## How it works

`daemon.py` runs an active `CGEventTap`, tracks touches by identity, and calls
`navigation.py`, which speaks Herdr's socket directly.

Three things this had to work around, each of which cost real time to find:

**A gesture event carries only the touches that changed**, not every finger
currently down. Counting per event makes the finger count flicker and destroys
any movement baseline, so touches are tracked by identity across events.

**`NSEventTypeSwipe` no longer reaches terminals.** iTerm2's own gesture
bindings sit on `swipeWithEvent:`, which modern macOS never calls for a
three-finger swipe, so binding a gesture there does nothing at all. Reading raw
touches sidesteps that entirely.

**A hotkey window never becomes the frontmost application.** It is a panel, so
`NSWorkspace.frontmostApplication()` keeps naming whatever was in front before
it appeared. The window stack has to be consulted instead.

The tap is active rather than listen-only, so three-finger events belong to this
plugin instead of to macOS. That puts it on the input path for the whole
machine, so the callback does no blocking work: gestures are handed to a single
worker thread, in order, and the expensive frontmost-application check happens
once per touch rather than once per event.

## Development

```sh
herdr plugin link .                       # register this checkout
python3 -m unittest discover -s tests     # the navigation policy
herdr plugin action invoke herdr-swipe.restart
```

The tests cover `navigation.py` against a fake Herdr socket, including the
detail most likely to break a client: one request per connection, then close.
The daemon is deliberately not covered. Gesture recognition needs a trackpad, a
hand, and an Accessibility grant, none of which a CI runner has, and a test
faking all three would only assert that the fake works.

Restart the daemon after editing: Python caches imports, so a running daemon
keeps the code it started with.

### Which terminals it takes over

Gestures are only claimed while one of these is in front — anywhere else macOS
keeps them:

| Terminal | Bundle identifier |
| --- | --- |
| iTerm2 | `com.googlecode.iterm2` |
| Apple Terminal | `com.apple.Terminal` |
| kitty | `net.kovidgoyal.kitty` |
| WezTerm | `com.github.wez.wezterm` |
| Warp | `dev.warp.Warp-Stable` |

Set `HERDR_SWIPE_HOSTS` to a comma-separated list of bundle identifiers to
replace that set — adding a terminal needs no code change:

```sh
HERDR_SWIPE_HOSTS=com.googlecode.iterm2,com.mitchellh.ghostty
```

Find an identifier with
`defaults read /Applications/Foo.app/Contents/Info CFBundleIdentifier`. Warp
ships a different one per release channel, so Preview is not the id above.

The check is which terminal is in front, not which one runs herdr. Swiping in a
listed terminal that has no herdr in it still moves herdr wherever it lives.

## License

MIT
