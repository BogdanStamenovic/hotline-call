"""The CLI contract, including the thing that makes it safe to adopt: when the
call path is unavailable it must behave exactly like `hotline-page`.
"""

import json
import os
import sys
import textwrap
from pathlib import Path

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
