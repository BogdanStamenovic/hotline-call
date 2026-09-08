"""The intent guesser that fills the gap while the real answer is computed."""

from __future__ import annotations

import pytest

from hotline_ios import speculate
from hotline_ios.speculate import NEUTRAL, Speculator


# -- the fallback, which must work with no model at all ---------------------


def cold() -> Speculator:
    """A speculator that never reaches an embedder."""
    return Speculator(endpoint="http://127.0.0.1:1")   # nothing listens there


def test_warming_against_a_dead_endpoint_reports_failure_rather_than_raising():
    s = cold()
    assert s.warm() is False
    assert s.embedder_ok is False


def test_it_still_classifies_with_no_embedder():
    """A missing model costs a less apt phrase, never a broken call."""
    s = cold(); s.warm()
    assert s.intent("Ne cujem te dobro, secka")[0] == "ZALBA"
    assert s.intent("Mozes li da proveris ovo")[0] == "ZADATAK"
    assert s.intent("Cao brate, kako si")[0] == "POZDRAV"


def test_a_fragment_too_short_to_judge_is_neutral():
    s = cold(); s.warm()
    assert s.intent("Pa")[0] == NEUTRAL
    assert s.intent("")[0] == NEUTRAL
    assert s.intent("   ")[0] == NEUTRAL


def test_unrecognised_text_is_neutral_rather_than_a_guess():
    s = cold(); s.warm()
    assert s.intent("nasumicne reci bez ikakvog znacenja ovde")[0] == NEUTRAL


# -- the clip mapping -------------------------------------------------------


def test_every_intent_has_a_clip():
    s = cold()
    for intent in speculate.SEEDS:
        assert s.holding_clip(intent), f"{intent} has no holding phrase"


def test_clips_alternate_so_one_phrase_does_not_grate():
    s = cold()
    first = s.holding_clip("PITANJE", turn=0)
    second = s.holding_clip("PITANJE", turn=1)
    assert first != second


def test_an_unknown_intent_falls_back_to_the_neutral_clip():
    s = cold()
    assert s.holding_clip("SOMETHING_ELSE") in speculate.HOLDING[NEUTRAL]


# -- the embedder path, skipped when ollama is not up -----------------------


def live() -> Speculator | None:
    s = Speculator()
    return s if s.warm() else None


@pytest.mark.parametrize("text,expected", [
    ("Jel imas pojma sto je ono palo", "PITANJE"),
    ("Interesuje me dokle se stiglo", "PITANJE"),
    ("Nesto mi grebe u zvuku", "ZALBA"),
    ("Uzasno se lomi, jedva te razumem", "ZALBA"),
    ("Dobro jutro, tu sam", "POZDRAV"),
    ("Ajde restartuj mi taj servis", "ZADATAK"),
    ("Treba mi da pogledas jednu stvar", "ZADATAK"),
])
def test_embeddings_classify_phrasings_the_keywords_never_saw(text, expected):
    """These are the cases the keyword fallback gets wrong.

    Measured 2026-09-08: embeddings 12/12 on this style of input, keywords 5/12,
    qwen2.5:1.5b 5/12, piccolo-gorgone:9b 2/12. This is why the embedder is the
    primary path and the regex is only the safety net.
    """
    s = live()
    if s is None:
        pytest.skip("ollama/bge-m3 not available")
    assert s.intent(text)[0] == expected


def test_the_embedder_is_fast_enough_to_sit_inside_a_turn():
    import time
    s = live()
    if s is None:
        pytest.skip("ollama/bge-m3 not available")
    began = time.monotonic()
    for _ in range(5):
        s.intent("Sta se desilo sa onim bagom juce")
    per_call = (time.monotonic() - began) / 5
    # It runs between chunks of his speech; anything near a tenth of a second
    # would be competing with the audio it is meant to be hiding.
    assert per_call < 0.15, f"{per_call*1000:.0f} ms per classification"
