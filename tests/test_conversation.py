"""The answered call as a conversation, over real sockets.

The bug this file exists to catch is the one the 313 green tests in this repo
did not: a media engine that works perfectly and is never reached. So these
drive `AnsweredCall` itself -- the thing `SipTransport.on_answer` calls -- with a
stand-in phone on the other end of a real UDP socket, real SRTP, and real
G.711. The models are faked; everything between them is not.
"""

from __future__ import annotations

import pathlib
import socket
import threading
import time

import numpy as np
import pytest

from hotline_ios.conversation import AnsweredCall, is_farewell, sentences
from hotline_ios.media import pcm, rtp, srtp, voicecall

TONE_RATE = 8000


# -- splitting speech into speakable pieces --------------------------------


def test_a_short_line_is_one_piece():
    assert sentences("Right, on it.") == ["Right, on it."]


def test_a_long_answer_breaks_at_sentence_ends():
    text = ("The disk is at eighty one percent, which is fine for now. "
            "The thing that will bite you is the pacman cache, which is down to "
            "three hundred and thirty eight packages and thinning. "
            "I would not count on a package rollback.")
    pieces = sentences(text)
    assert len(pieces) > 1
    assert " ".join(pieces).split() == text.split()


def test_tiny_fragments_are_merged_rather_than_sent_alone():
    """cvoiced has a fixed ~1.7 s floor per request, so ten one-word requests
    cost ten floors and sound worse than one."""
    assert sentences("Yes. No. Maybe. Fine.") == ["Yes. No. Maybe. Fine."]


def test_a_sentence_longer_than_the_cap_is_split_between_words():
    text = "word " * 200
    for piece in sentences(text):
        assert len(piece) <= 240
        assert not piece.startswith("d ")  # never mid-word


# -- knowing when he has actually said goodbye -----------------------------


def test_cao_is_a_greeting_and_must_not_end_the_call():
    """He opened a call with "Ćao brate" and an earlier version hung up on him
    mid-hello. This is that bug, kept."""
    assert not is_farewell("Ćao brate, kako si")
    assert not is_farewell("ćao")


def test_a_real_farewell_ends_the_call():
    assert is_farewell("dobro, čujemo se")
    assert is_farewell("to je to, prekidam")


def test_a_farewell_word_in_the_middle_is_not_a_farewell():
    """"Reci mi kad završiš" contains one and is not one."""
    assert not is_farewell("prekini to i onda mi objasni sve ostalo polako molim te")


# -- a whole call, over real sockets ---------------------------------------


_LIVE: list = []


@pytest.fixture(autouse=True)
def _stop_pumps():
    yield
    while _LIVE:
        _LIVE.pop().close()


class Phone:
    """A stand-in for his Linphone: it holds the far end of the media socket,
    speaks when told to, and records what it was sent."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.our_key, self.our_salt = srtp.new_key_salt()
        self.their_key, self.their_salt = srtp.new_key_salt()
        self.heard: list[bytes] = []
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._listen, daemon=True)

    def start(self, our_addr) -> None:
        self.our_addr = our_addr
        self._reader.start()

    def _listen(self) -> None:
        session = srtp.SrtpSession(self.our_key, self.our_salt)
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            try:
                parsed = rtp.parse_packet(session.unprotect(data))
            except Exception:
                continue
            if parsed is not None:
                self.heard.append(parsed[3])

    def stop(self) -> None:
        self._stop.set()

    def say(self, audio: np.ndarray, rate: int = TONE_RATE) -> None:
        session = srtp.SrtpSession(self.their_key, self.their_salt)
        wire = pcm.from_model(audio.astype(np.float32), rate=rate, out_rate=8000)
        ulaw = pcm.ulaw_encode(wire)
        n = voicecall.FRAME_SAMPLES
        for i in range(0, len(ulaw) - n + 1, n):
            seq = i // n
            packet = rtp.build_packet(seq & 0xFFFF, seq * n, 0xFEED, ulaw[i:i + n])
            self.sock.sendto(session.protect(packet), self.our_addr)

    def say_after(self, delay: float, audio: np.ndarray, rate: int = TONE_RATE):
        thread = threading.Thread(
            target=lambda: (time.sleep(delay), self.say(audio, rate)), daemon=True)
        thread.start()
        return thread

    @property
    def seconds_heard(self) -> float:
        return len(self.heard) * voicecall.FRAME_MS / 1000.0


def speech(seconds, rate=TONE_RATE, amp=0.35):
    t = np.arange(int(seconds * rate)) / rate
    sig = np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 360 * t)
    return (sig * amp * (1 + 0.2 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)


def quiet(seconds, rate=TONE_RATE, amp=0.0008):
    return (np.random.randn(int(seconds * rate)) * amp).astype(np.float32)


class _Fillers:
    """Stands in for the pre-rendered clips, and records which went out."""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.texts: dict[str, str] = {}

    def get(self, name: str):
        self.played.append(name)
        return (speech(0.3), TONE_RATE)


def hears(words: str):
    """A stand-in for `Ears`: words out of speech, nothing out of silence.

    Returning a constant for every phrase would be the wrong fake -- a turn is
    transcribed in pieces, and the trailing piece is the pause that ended it.
    Real `Ears` runs `vad_filter=True` and returns "" for that; a fake that does
    not would hide a genuine duplication bug behind a false one.
    """
    def transcribe(audio):
        level = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
        return words if level > 0.02 else ""
    return transcribe


def dial(**kwargs) -> tuple[AnsweredCall, Phone, str]:
    """An `AnsweredCall` and the phone it is talking to, wired but not started.

    Returns the 200 OK too, so the handler is invoked exactly the way
    `SipTransport._finish_answered` invokes it -- through the SDP, not around it.
    """
    phone = Phone()
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    phone.start(ours.getsockname())
    host, port = phone.sock.getsockname()
    reply = (
        "SIP/2.0 200 OK\r\n"
        "Content-Type: application/sdp\r\n"
        "\r\n"
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {host}\r\n"
        "s=Talk\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/SAVP 0 8\r\n"
        f"{srtp.crypto_line(phone.their_key, phone.their_salt)}\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    said: list[str] = []
    delivered: list[str] = []

    defaults = dict(
        greeting="Claude here. Should I restart the daemon?",
        speak=lambda text: (said.append(text), (speech(0.4), TONE_RATE))[1],
        transcribe=hears("da samo napred"),
        fillers=None,
        deliver=delivered.append,
        ask=None,
        turn_seconds=2.0,
        call_seconds=8.0,
        dead_line_seconds=2.0,
        utterance_seconds=2.0,
    )
    defaults.update(kwargs)
    handler = AnsweredCall(**defaults)  # type: ignore[arg-type]
    handler.said = said               # type: ignore[attr-defined]
    handler.delivered = delivered     # type: ignore[attr-defined]
    handler.sock = ours               # type: ignore[attr-defined]
    handler.phone = phone             # type: ignore[attr-defined]
    return handler, phone, reply


def run(handler, phone, reply):
    def go():
        handler(reply, handler.sock, phone.our_key, phone.our_salt)
    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread


def test_he_hears_a_voice_and_it_hears_him():
    """The whole point, in one test: audio leaves, audio arrives, and what
    arrived is turned into words that reach whoever was waiting."""
    handler, phone, reply = dial()
    # Enough silence to calibrate on, then something to say.
    phone.say_after(0.05, quiet(1.2))
    phone.say_after(1.5, np.concatenate([speech(1.2), quiet(1.2)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()

    assert not thread.is_alive(), "the handler never returned"
    assert handler.said[0].startswith("Claude here."), handler.said
    assert phone.seconds_heard > 1.0, "his phone was sent almost nothing"
    assert handler.delivered == ["da samo napred"]
    assert handler.answered == "da samo napred"
    assert handler.stats["frames_received"] > 50
    assert handler.stats["auth_failures"] == 0


def test_the_answer_the_waiting_agent_gets_is_his_words_and_not_the_session_s():
    """A ring exists because an agent asked him something. What comes back on
    hotline-call has to be what HE said -- the session's reply to it is for his
    ear, and confusing the two answers the caller with the wrong voice."""
    handler, phone, reply = dial(ask=lambda text: "svakako, uradio sam to")
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert handler.delivered == ["da samo napred"]
    assert handler.answered == "da samo napred"


def test_his_first_turn_is_answered_out_loud_rather_than_with_a_one_word_clip():
    """On the live call of 2026-09-10 he answered the question, heard "Dobro.",
    and then got sixteen seconds of nothing. The session is seeded with what the
    ring asked; it can acknowledge what he actually said."""
    asked: list[str] = []
    handler, phone, reply = dial(
        ask=lambda text: (asked.append(text), "Važi, krećem odmah.")[1])
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert asked == ["da samo napred"], asked
    assert "Važi, krećem odmah." in handler.said, handler.said


def test_a_later_turn_does_go_to_a_session_and_is_spoken_back():
    asked: list[str] = []
    handler, phone, reply = dial(
        ask=lambda text: (asked.append(text), "Disk je pun osamdeset jedan posto.")[1],
        call_seconds=14.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    phone.say_after(5.5, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=30)
    phone.stop()
    assert asked == ["da samo napred", "da samo napred"], asked
    assert "Disk je pun osamdeset jedan posto." in handler.said, handler.said


def test_it_does_not_hang_up_on_him_for_being_quiet():
    """His instruction, verbatim: wait for HIS hangup. A silent stretch is him
    thinking, and an earlier version ended the call twice while he was there.
    Audio is still flowing here -- only a line with no RTP on it counts as gone."""
    handler, phone, reply = dial(call_seconds=4.0, dead_line_seconds=60.0)
    phone.say_after(0.05, quiet(6.0))
    began = time.monotonic()
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert not thread.is_alive()
    assert handler.ended == "call length cap", handler.ended
    assert time.monotonic() - began >= 4.0, "it gave up on him early"
    assert handler.delivered == []


def test_comfort_noise_is_not_him_mumbling():
    """Found by a loopback rehearsal, which said "say that again" twice into a
    line carrying nothing but comfort noise. Audio arriving is not him speaking,
    and asking him to repeat a silence is worse than dead air."""
    fillers = _Fillers()
    handler, phone, reply = dial(fillers=fillers, call_seconds=5.0,
                                 dead_line_seconds=60.0,
                                 transcribe=hears("never said"))
    phone.say_after(0.05, quiet(8.0))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert not thread.is_alive()
    assert "notheard" not in fillers.played, fillers.played


def test_a_turn_he_spoke_into_that_transcribes_to_nothing_does_ask_him_to_repeat():
    fillers = _Fillers()
    handler, phone, reply = dial(fillers=fillers, call_seconds=6.0,
                                 dead_line_seconds=60.0,
                                 transcribe=lambda audio: "")
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.5)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert "notheard" in fillers.played, fillers.played


def test_a_line_with_no_rtp_at_all_is_eventually_accepted_as_gone():
    handler, phone, reply = dial(call_seconds=60.0, dead_line_seconds=1.0)
    phone.stop()  # his phone sends nothing at all
    began = time.monotonic()
    thread = run(handler, phone, reply)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert handler.ended == "line went dead"
    assert time.monotonic() - began < 30


def test_he_says_goodbye_and_that_ends_it():
    handler, phone, reply = dial(transcribe=hears("važi, čujemo se"), call_seconds=60.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.2)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert not thread.is_alive()
    assert handler.ended == "he said goodbye"
    assert handler.delivered == ["važi, čujemo se"], "his last words still reach the agent"


def test_a_hangup_ends_the_call_without_waiting_for_the_cap():
    """His phone sending BYE has to end this, or the handler holds the line --
    and the agent that rang -- until the call-length cap runs out."""
    hung_up = [False]
    handler, phone, reply = dial(hung_up=lambda: hung_up[0], call_seconds=30.0)
    phone.say_after(0.05, quiet(2.0))

    def hang_up_soon():
        time.sleep(2.0)
        hung_up[0] = True
    threading.Thread(target=hang_up_soon, daemon=True).start()

    began = time.monotonic()
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert not thread.is_alive()
    assert handler.ended == "the far end ended the call"
    assert time.monotonic() - began < 15, "it waited for the cap instead of the hangup"


def test_a_200_with_no_media_hangs_up_instead_of_holding_a_silent_call():
    noted: list[tuple[str, str]] = []
    handler, phone, reply = dial(note=lambda kind, text: noted.append((kind, text)))
    phone.stop()
    broken = reply.replace("c=IN IP4", "x=IN IP4")
    handler(broken, handler.sock, phone.our_key, phone.our_salt)
    assert handler.ended.startswith("no media")
    assert noted and noted[0][0] == "error"


def test_a_voice_that_will_not_synthesise_does_not_hang_the_call():
    def broken(_text):
        raise RuntimeError("cvoiced is down")

    handler, phone, reply = dial(speak=broken, call_seconds=4.0)
    phone.say_after(0.05, quiet(4.0))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert not thread.is_alive(), "a dead TTS must not wedge an answered call"


def test_a_transcriber_that_raises_loses_the_phrase_and_not_the_call():
    def broken(_audio):
        raise RuntimeError("no model")

    handler, phone, reply = dial(transcribe=broken, call_seconds=5.0)
    phone.say_after(0.05, quiet(1.2))
    phone.say_after(1.5, np.concatenate([speech(1.0), quiet(1.2)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert not thread.is_alive()
    assert handler.delivered == []


def test_a_session_that_fails_is_said_out_loud_rather_than_dropped():
    def broken(_text):
        raise RuntimeError("no session")

    handler, phone, reply = dial(ask=broken, call_seconds=14.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    phone.say_after(5.5, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=30)
    phone.stop()
    assert any("zaglavilo" in line for line in handler.said), handler.said


# -- keeping the audio, for scoring a second model later -------------------


def test_a_recorded_turn_is_kept_as_it_arrived_not_as_whisper_saw_it(tmp_path):
    """8 kHz mu-law off the wire, not the 16 kHz float the model was handed. The
    two carry the same information and only the first still looks like a phone
    line to whoever scores a model against it a week from now."""
    import json
    import wave

    from hotline_ios.media.record import Recorder

    recorder = Recorder(str(tmp_path))
    handler, phone, reply = dial(recorder=recorder, model="large-v3", call_seconds=6.0,
                                 dead_line_seconds=60.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()

    where = pathlib.Path(handler.recording)
    manifest = json.loads((where / "manifest.json").read_text())
    assert manifest["rate"] == 8000
    assert manifest["turns"], manifest
    first = manifest["turns"][0]
    assert first["heard"] == "da samo napred"
    assert first["model"] == "large-v3"
    assert first["ended"] == "endpointed"

    with wave.open(str(where / first["file"])) as clip:
        assert clip.getframerate() == 8000
        assert clip.getsampwidth() == 2
        assert clip.getnchannels() == 1
        assert clip.getnframes() > 8000, "less than a second of a turn he spoke into"

    with wave.open(str(where / "inbound.wav")) as whole:
        assert whole.getnframes() >= clip.getnframes()


def test_nothing_is_written_unless_a_recorder_was_asked_for(tmp_path):
    """He is on these calls. An always-on recorder of his voice is not a default."""
    handler, phone, reply = dial(call_seconds=4.0, dead_line_seconds=60.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert handler.recording == ""
    assert not list(tmp_path.iterdir())


def test_a_recorder_that_cannot_write_does_not_cost_the_call(tmp_path):
    from hotline_ios.media.record import Recorder

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way")
    recorder = Recorder(str(blocked))
    handler, phone, reply = dial(recorder=recorder, call_seconds=5.0,
                                 dead_line_seconds=60.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=25)
    phone.stop()
    assert not thread.is_alive()
    assert handler.delivered == ["da samo napred"], "the call carried on regardless"
    assert recorder.failed
