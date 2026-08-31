"""The CLI contract, including the thing that makes it safe to adopt: when the
call path is unavailable it must behave exactly like `hotline-page`.
"""

import json
import os
import sys
import textwrap

import pytest

from hotline_ios import call_cli
from hotline_ios.client import CallOutcome, DaemonError


@pytest.fixture
def fake_page(tmp_path, monkeypatch):
    """A stand-in `hotline-page` that records how it was called."""
    log = tmp_path / "page.log"
    script = tmp_path / "hotline-page"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        open({str(log)!r}, "w").write(json.dumps(sys.argv[1:]))
        print("go ahead")
        """))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return log


def test_usage_error_without_a_reason(capsys):
    assert call_cli.main([]) == call_cli.EXIT_USAGE
    assert "say what you need" in capsys.readouterr().err


def test_answered_prints_only_his_reply(monkeypatch, capsys):
    monkeypatch.setattr(
        call_cli,
        "place_call",
        lambda *a, **kw: CallOutcome(state="answered", reply="yes, go ahead",
                                     waited_seconds=12.0, transport="sip"),
    )
    assert call_cli.main(["-q", "may I spend money"]) == call_cli.EXIT_ANSWERED
    out = capsys.readouterr()
    # Composability: stdout is his answer and nothing else.
    assert out.out == "yes, go ahead\n"


def test_declined_is_its_own_exit_code_and_does_not_fall_back(monkeypatch, fake_page):
    monkeypatch.setattr(
        call_cli, "place_call",
        lambda *a, **kw: CallOutcome(state="declined", transport="sip"),
    )
    assert call_cli.main(["-q", "ping"]) == call_cli.EXIT_DECLINED
    # The whole point: a decline must NOT be escalated through another channel.
    assert not fake_page.exists()


def test_a_dead_daemon_falls_back_to_hotline_page(monkeypatch, capfd, fake_page):
    def explode(*a, **kw):
        raise DaemonError("cannot reach hotline-iosd at http://127.0.0.1:8789")

    monkeypatch.setattr(call_cli, "place_call", explode)
    assert call_cli.main(["-q", "the build is blocked", "--source", "the ios build"]) == 0
    assert fake_page.exists()
    argv = json.loads(fake_page.read_text())
    assert argv[0] == "the build is blocked"
    assert "--source" in argv and "the ios build" in argv
    # capfd, not capsys: hotline-page is a real subprocess writing to the real
    # fd, and it inherits stdout on purpose so that $(hotline-call ...) still
    # captures his answer when the call path failed. That inheritance IS the
    # feature, so the test has to look at the fd.
    assert capfd.readouterr().out == "go ahead\n"


def test_ringing_out_falls_back_but_no_fallback_does_not(monkeypatch, fake_page):
    monkeypatch.setattr(
        call_cli, "place_call",
        lambda *a, **kw: CallOutcome(state="unanswered", transport="sip"),
    )
    assert call_cli.main(["-q", "--no-fallback", "hello"]) == call_cli.EXIT_UNANSWERED
    assert not fake_page.exists()

    assert call_cli.main(["-q", "hello"]) == 0
    assert fake_page.exists()


def test_no_fallback_reports_the_daemon_error(monkeypatch, capsys):
    def explode(*a, **kw):
        raise DaemonError("connection refused")

    monkeypatch.setattr(call_cli, "place_call", explode)
    assert call_cli.main(["-q", "--no-fallback", "hi"]) == call_cli.EXIT_UNDELIVERABLE
    assert "connection refused" in capsys.readouterr().err


def test_read_timeout_with_live_daemon_is_unanswered_not_undeliverable(
    monkeypatch, capsys
):
    """The 2026-09-01 bug: a call that rang and got no answer on the line was
    reported as a dead daemon. A CallTimeout whose daemon_up is True must read
    as 'rang, no answer', return EXIT_UNANSWERED under --no-fallback, and never
    claim the daemon is unreachable."""
    from hotline_ios.client import CallTimeout

    def timed_out(*a, **kw):
        raise CallTimeout(elapsed=530.0, daemon_up=True, url="http://127.0.0.1:8789")

    monkeypatch.setattr(call_cli, "place_call", timed_out)
    # No -q: the informational line is gated behind quiet, and asserting on it
    # is the point -- the failure was a misleading message, not a wrong code.
    assert call_cli.main(["--no-fallback", "test"]) == call_cli.EXIT_UNANSWERED
    err = capsys.readouterr().err
    assert "cannot reach" not in err
    assert "no answer on the call" in err


def test_read_timeout_with_live_daemon_falls_back_like_ringing_out(
    monkeypatch, fake_page
):
    from hotline_ios.client import CallTimeout

    def timed_out(*a, **kw):
        raise CallTimeout(elapsed=530.0, daemon_up=True, url="http://127.0.0.1:8789")

    monkeypatch.setattr(call_cli, "place_call", timed_out)
    assert call_cli.main(["-q", "test"]) == 0
    assert fake_page.exists()


def test_timeout_against_a_dead_daemon_is_still_undeliverable(monkeypatch, capsys):
    """daemon_up False means nothing answered a fresh connect -- the original
    dead-daemon path, exit 1."""
    from hotline_ios.client import CallTimeout

    def timed_out(*a, **kw):
        raise CallTimeout(elapsed=2.0, daemon_up=False, url="http://127.0.0.1:8789")

    monkeypatch.setattr(call_cli, "place_call", timed_out)
    assert call_cli.main(["-q", "--no-fallback", "test"]) == call_cli.EXIT_UNDELIVERABLE
    assert "unreachable" in capsys.readouterr().err


def test_post_classifies_read_timeout_as_calltimeout(monkeypatch):
    """Unit-level: _post must turn a socket timeout from urlopen into a
    CallTimeout, and probe the socket to fill daemon_up -- not raise a bare
    'cannot reach' DaemonError."""

    from hotline_ios import client

    def fake_urlopen(*a, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client, "_daemon_reachable", lambda url, timeout=2.0: True)
    with pytest.raises(client.CallTimeout) as ei:
        client._post("/api/v1/call", {}, url="http://127.0.0.1:8789", timeout=1.0)
    assert ei.value.daemon_up is True
    assert isinstance(ei.value, client.DaemonError)


def test_post_unwraps_timeout_nested_in_urlerror(monkeypatch):
    from hotline_ios import client

    def fake_urlopen(*a, **kw):
        raise client.urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client, "_daemon_reachable", lambda url, timeout=2.0: False)
    with pytest.raises(client.CallTimeout) as ei:
        client._post("/api/v1/call", {}, url="http://127.0.0.1:8789", timeout=1.0)
    assert ei.value.daemon_up is False


def test_post_connection_refused_stays_a_plain_daemon_error(monkeypatch):
    from hotline_ios import client

    def fake_urlopen(*a, **kw):
        raise client.urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(client.DaemonError) as ei:
        client._post("/api/v1/call", {}, url="http://127.0.0.1:8789", timeout=1.0)
    assert not isinstance(ei.value, client.CallTimeout)
    assert "cannot reach" in str(ei.value)
