"""Make the suite run the same way however it is invoked.

Two paths have to be on `sys.path`: this package's `src`, and hotline's -- the
daemon imports hotline's `httpd`, `pool` and `agents` over PYTHONPATH rather
than as an installed dependency.

**Why this is not left to the caller.** `test_daemon.py` guards its import with
`importorskip`, so without hotline on the path the entire 18-test HTTP surface
is silently skipped and the run reports "36 passed, 1 skipped" -- which reads as
green. Two agents got two different counts for the same suite today, and that is
exactly the shape of thing that hides a broken test rather than showing it.

Finding it here means `pytest tests` from `server/` is enough, and the skip only
survives if hotline genuinely is not on the machine.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Alongside this repo, which is where it lives on archserver. A relative guess
# rather than an absolute path so a clone elsewhere still works.
_HOTLINE = Path(__file__).resolve().parents[3] / "hotline" / "src"
if _HOTLINE.is_dir() and str(_HOTLINE) not in sys.path:
    sys.path.insert(0, str(_HOTLINE))


import pytest


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never let a test touch the real database.

    The daemon's default store path is beside hotline's own `agents.json`, under
    `XDG_STATE_HOME`, and that file belongs to the instance serving his phone.
    A suite that wrote conversations into it would corrupt live state and would
    also stop being deterministic the moment it read anything back. Autouse
    rather than opt-in: forgetting it in one new test is exactly the failure
    this is here to make impossible.
    """
    monkeypatch.setenv("HOTLINE_IOS_DB", str(tmp_path / "hotline-ios.db"))


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch):
    """Nothing in this suite may shell out to the real tmux.

    Two separate reasons, and the second is the serious one. The box this runs
    on has live agent sessions on it, including whichever one is running the
    suite, so a test that reached a real `tmux send-keys` could type into
    somebody's work. And a roster test that read the real session list would
    assert against whatever happened to be running, which is flaky in a way that
    looks like a bug in this code.

    Defaults are "no tmux server at all". A test that needs panes overrides
    `sessions`/`exists`; `_tmux` itself raises, so nothing can reach a real
    subprocess by a path nobody thought to stub.
    """
    tmuxen = pytest.importorskip("hotline.tmuxen")

    def forbidden(*args, **kwargs):
        raise AssertionError(f"the suite tried to shell out to tmux: {args}")

    monkeypatch.setattr(tmuxen, "_tmux", forbidden)
    monkeypatch.setattr(tmuxen, "_detached_tmux", forbidden)
    monkeypatch.setattr(tmuxen, "sessions", lambda: set())
    monkeypatch.setattr(tmuxen, "exists", lambda name: False)
