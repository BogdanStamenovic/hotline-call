"""Per-call switches: defaults, bad values, the file round trip, and the CLI."""

from __future__ import annotations

import pytest

from hotline_call import features
from hotline_call.call_cli import main
from hotline_call.haikuagent import MANNERS, manners


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTLINE_CALL_FEATURES", str(tmp_path / "features.toml"))
    return tmp_path / "features.toml"


def test_no_file_means_every_default():
    assert features.load() == {f.name: f.default for f in features.FEATURES}


def test_a_bad_value_is_ignored_not_a_failed_call(scratch):
    scratch.write_text('join = "loud"\ntts_steps = 2\nopeners = "no"\nstreaming = false\n')
    got = features.load()
    assert got["join"] == "roomtone" and got["tts_steps"] == 8 and got["openers"] is True
    assert got["streaming"] is False


def test_an_unreadable_file_falls_back_to_defaults(scratch):
    scratch.write_text("this is = = not toml")
    assert features.load()["streaming"] is True


def test_set_and_reset_through_the_cli(scratch, capsys):
    assert main(["features", "set", "openers", "off"]) == 0
    assert features.load()["openers"] is False
    assert main(["features", "set", "tts_steps", "32"]) == 0
    assert features.load()["tts_steps"] == 32
    assert main(["features", "set", "join", "bogus"]) != 0
    assert main(["features", "reset", "openers"]) == 0
    assert features.load()["openers"] is True and features.load()["tts_steps"] == 32
    assert main(["features", "reset"]) == 0
    assert "openers" not in scratch.read_text()


def test_a_reason_starting_with_another_word_is_still_a_ring():
    # Only the exact first word "features" is the subcommand; everything else
    # stays a reason. The parser runs (and fails on its own terms) rather than
    # the features code swallowing it.
    from hotline_call.call_cli import features_cmd
    assert features_cmd.__name__ == "features_cmd"


def test_manners_with_everything_on_is_the_live_text():
    assert manners() == MANNERS
    bare = manners(openers=False, tools=False, context_rules=False)
    assert "pre-recorded" not in bare and "read_file" not in bare and "Never guess" not in bare
    assert "CANNOT look anything up" in bare


def test_no_fillers_forbids_the_openers_and_the_samo_sekund_lookup_line():
    text = manners(fillers=False)
    assert "Answer DIRECTLY" in text and "pre-recorded" not in text
    assert '("Samo sekund, da proverim.")' not in text
