"""Opus as the preferred codec, G.711 as the floor, and the audio between them.

What these cannot prove: that his iPhone, through linphone.org's relay, actually
answers with Opus. `LINPHONE_OFFER` is the shape of Linphone's default offer
(Opus on 96, speex, G.711, telephone-events) written from its documented
defaults, not a capture -- there is no capture of his phone's SDP on this box.
"""

from __future__ import annotations

import socket
import time

import numpy as np
import pytest

from hotline_ios.media import codec, pcm, rtp, srtp, voicecall

needs_opus = pytest.mark.skipif(codec.opus_library() is None, reason="no libopus")

LINPHONE_OFFER = (
    "v=0\r\n"
    "o=b0g13a 1 1 IN IP4 10.0.0.7\r\n"
    "s=Talk\r\n"
    "c=IN IP4 10.0.0.7\r\n"
    "t=0 0\r\n"
    "m=audio 7078 RTP/SAVP 96 97 98 0 8 101 99 100\r\n"
    "a=rtpmap:96 opus/48000/2\r\n"
    "a=fmtp:96 useinbandfec=1\r\n"
    "a=rtpmap:97 speex/16000\r\n"
    "a=rtpmap:98 speex/8000\r\n"
    "a=rtpmap:101 telephone-event/48000\r\n"
    "a=rtpmap:99 telephone-event/16000\r\n"
    "a=rtpmap:100 telephone-event/8000\r\n"
)


def answer_sdp(pts: str, *attrs: str, host="127.0.0.1", port=7078, key=None, salt=None):
    if key is None:
        key, salt = srtp.new_key_salt()
    return (
        "SIP/2.0 200 OK\r\n"
        "Content-Type: application/sdp\r\n"
        "\r\n"
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {host}\r\n"
        "s=Talk\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/SAVP {pts}\r\n"
        f"{srtp.crypto_line(key, salt)}\r\n"
        + "".join(f"{a}\r\n" for a in attrs)
    )


# -- the offer ---------------------------------------------------------------


@needs_opus
def test_the_offer_puts_opus_first_and_keeps_g711_behind_it():
    pts, attrs = codec.offer_lines("opus,pcmu,pcma")
    assert pts.split() == ["96", "0", "8", "101"]
    assert "a=rtpmap:96 opus/48000/2" in attrs   # RFC 7587: always /2
    assert any(a.startswith("a=fmtp:96 ") and "useinbandfec=1" in a for a in attrs)
    assert "a=rtpmap:0 PCMU/8000" in attrs


def test_the_kill_switch_takes_opus_out_of_the_offer():
    pts, attrs = codec.offer_lines("pcmu,pcma")
    assert pts.split() == ["0", "8", "101"]
    assert not any("opus" in a for a in attrs)


def test_pcmu_is_offered_even_when_the_setting_forgets_it():
    pts, _ = codec.offer_lines("pcma")
    assert pts.split() == ["8", "0", "101"]


def test_without_libopus_the_offer_is_g711_and_nothing_breaks(monkeypatch):
    monkeypatch.setattr(codec, "opus_library", lambda: None)
    pts, _ = codec.offer_lines("opus,pcmu,pcma")
    assert pts.split() == ["0", "8", "101"]


@needs_opus
def test_the_sip_invite_carries_the_codec_offer():
    from hotline_ios.ring.sip import SipTransport

    t = SipTransport(user="bogdan", password="x", peer="sip:him@sip.linphone.org")
    t._local = ("10.0.0.1", 5060)
    try:
        body = t._invite("cid", 1, "tag").split("\r\n\r\n", 1)[1]
    finally:
        t._close_media()
    media = next(line for line in body.split("\r\n") if line.startswith("m=audio"))
    assert media.split()[2:] == ["RTP/SAVP", "96", "0", "8", "101"]
    assert "a=rtpmap:96 opus/48000/2" in body
    assert "a=ptime:20" in body


# -- reading the answer ------------------------------------------------------


@needs_opus
def test_an_opus_answer_is_carried_as_opus():
    got = codec.negotiate(answer_sdp("96 101", "a=rtpmap:96 opus/48000/2",
                                     "a=fmtp:96 useinbandfec=1"), "opus,pcmu,pcma")
    assert got == codec.Choice("opus", 96, 48000, {"useinbandfec": "1"})


@needs_opus
def test_a_renumbered_opus_is_matched_by_name_and_sent_with_their_number():
    got = codec.negotiate(answer_sdp("111 0", "a=rtpmap:111 OPUS/48000/2"), "opus,pcmu")
    assert got is not None and (got.name, got.pt) == ("opus", 111)


def test_a_g711_only_answer_still_gets_g711():
    assert codec.negotiate(answer_sdp("0 101"), "opus,pcmu,pcma") == codec.PCMU
    pcma = codec.negotiate(answer_sdp("8 0"), "opus,pcmu,pcma")
    assert pcma is not None and (pcma.name, pcma.pt) == ("PCMA", 8)


@needs_opus
def test_the_answerers_order_decides_not_ours():
    got = codec.negotiate(answer_sdp("0 96", "a=rtpmap:96 opus/48000/2"), "opus,pcmu")
    assert got == codec.PCMU


def test_an_answer_with_nothing_in_common_is_refused_by_the_call():
    with pytest.raises(voicecall.SdpError, match="PCMU"):
        voicecall.parse_sdp_answer(answer_sdp("97 101", "a=rtpmap:97 speex/16000"))


@needs_opus
def test_parse_sdp_answer_hands_the_call_its_codec():
    a = voicecall.parse_sdp_answer(answer_sdp("96 0 101", "a=rtpmap:96 opus/48000/2"))
    assert a.choice().name == "opus"


# -- answering an offer (the side we take when someone calls us) -------------


@needs_opus
def test_answering_linphones_offer_picks_opus_on_its_own_number():
    choice, pts, attrs = codec.answer_lines(LINPHONE_OFFER, "opus,pcmu,pcma")
    assert (choice.name, choice.pt, pts) == ("opus", 96, "96")
    assert choice.fmtp == {"useinbandfec": "1"}
    assert "a=rtpmap:96 opus/48000/2" in attrs


@needs_opus
def test_answering_prefers_opus_even_when_the_offer_lists_it_last():
    offer = LINPHONE_OFFER.replace("96 97 98 0 8", "0 8 97 98 96")
    choice, _, _ = codec.answer_lines(offer, "opus,pcmu,pcma")
    assert choice.name == "opus"


def test_answering_a_g711_only_offer_gives_g711():
    offer = LINPHONE_OFFER.replace("96 97 98 0 8 101 99 100", "8 0 101")
    choice, pts, _ = codec.answer_lines(offer, "opus,pcmu,pcma")
    assert (choice.name, pts) == ("PCMU", "0")


def test_an_offer_we_cannot_carry_gets_no_answer():
    offer = LINPHONE_OFFER.replace("96 97 98 0 8 101 99 100", "97 98")
    assert codec.answer_lines(offer, "opus,pcmu,pcma") is None


# -- the codec itself --------------------------------------------------------


def tones(freqs, seconds=1.0, rate=48000, amp=0.15):
    t = np.arange(int(seconds * rate)) / rate
    return sum(amp * np.sin(2 * np.pi * f * t) for f in freqs).astype(np.float32)


def band_db(audio: np.ndarray, rate: int, freq: float, ref: float = 1000.0) -> float:
    """Energy at `freq` relative to energy at `ref`, in dB."""
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    freqs = np.fft.rfftfreq(audio.size, 1 / rate)

    def at(f):
        return float(spectrum[(freqs > f - 60) & (freqs < f + 60)].sum()) + 1e-20

    return 10 * np.log10(at(freq) / at(ref))


@needs_opus
def test_opus_carries_what_g711_cannot():
    """The reason for all of this, measured on the codec alone."""
    sig = tones([1000, 6000, 10000])
    wire = pcm.from_model(sig, 48000, 48000)
    opus = codec.Opus(codec.Choice("opus", 96, 48000))
    n = opus.frame_samples * 2
    back = b"".join(opus.decode(opus.encode(wire[i:i + n]))
                    for i in range(0, len(wire) - n + 1, n))
    out = np.frombuffer(back, dtype="<i2").astype(np.float32) / 32768.0
    out = out[4800:]   # past the encoder's start-up
    assert band_db(out, 48000, 6000) > -6
    assert band_db(out, 48000, 10000) > -6
    assert opus.errors == 0


@needs_opus
def test_the_far_ends_bitrate_cap_is_honoured():
    capped = codec.Opus(codec.Choice("opus", 96, 48000, {"maxaveragebitrate": "20000"}))
    assert capped.bitrate == 20000


def test_g711_idle_frames_are_the_canonical_silent_codes():
    assert codec.G711(codec.PCMU).idle() == b"\xff" * 160
    assert codec.G711(codec.Choice("PCMA", 8, 8000)).idle() == b"\xd5" * 160


# -- a whole call, both directions, over SRTP on real sockets ------------------


_CALLS: list = []


@pytest.fixture(autouse=True)
def _hang_up():
    yield
    while _CALLS:
        _CALLS.pop().close()


class Peer:
    """His phone, as far as media goes: its own SRTP keys and its own codec."""

    def __init__(self, choice: codec.Choice) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.key, self.salt = srtp.new_key_salt()
        self.codec = codec.open_codec(choice)

    def say(self, audio: np.ndarray, rate: int, to) -> None:
        session = srtp.SrtpSession(self.key, self.salt)
        wire = pcm.from_model(audio, rate, self.codec.rate)
        n = self.codec.frame_samples * 2
        for seq, i in enumerate(range(0, len(wire) - n + 1, n)):
            packet = rtp.build_packet(seq, seq * self.codec.ts_step, 0xFEED,
                                      self.codec.encode(wire[i:i + n]), self.codec.pt)
            self.sock.sendto(session.protect(packet), to)
            time.sleep(0.002)   # a little pacing; the pump reads between ticks

    def hear(self, our_key, our_salt, frames: int) -> np.ndarray:
        session = srtp.SrtpSession(our_key, our_salt)
        self.sock.settimeout(1.0)
        out = []
        for _ in range(frames):
            try:
                data, _ = self.sock.recvfrom(4096)
            except TimeoutError:
                break
            parsed = rtp.parse_packet(session.unprotect(data))
            assert parsed is not None and parsed[0] == self.codec.pt
            out.append(self.codec.decode(parsed[3]))
        return np.frombuffer(b"".join(out), dtype="<i2").astype(np.float32) / 32768.0


def connect(choice: codec.Choice):
    peer = Peer(choice)
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    host, port = peer.sock.getsockname()
    attrs = ["a=rtpmap:96 opus/48000/2"] if choice.name == "opus" else []
    answer = voicecall.parse_sdp_answer(
        answer_sdp(f"{choice.pt} 101", *attrs, host=host, port=port, key=peer.key, salt=peer.salt))
    key, salt = srtp.new_key_salt()
    call = voicecall.VoiceCall(ours, answer, key, salt)
    _CALLS.append(call)
    return call, peer, (key, salt), ours.getsockname()


WIDE = [1000, 6000, 10000]


@needs_opus
def test_his_wideband_voice_reaches_the_model_with_its_top_end():
    """Inbound: what Whisper gets now has content above 4 kHz."""
    call, peer, _, ours = connect(codec.Choice("opus", 96, 48000))
    assert call.rate == 48000
    call.record()
    peer.say(tones(WIDE, 1.2), 48000, ours)
    model = call.receive_audio(1.0)                 # 16 kHz: Whisper's rate
    assert band_db(model[1600:], 16000, 6000) > -6
    line = np.frombuffer(call.recorded(), dtype="<i2").astype(np.float32) / 32768.0
    assert band_db(line[4800:], 48000, 10000) > -6  # and the line itself is fullband
    assert call.stats()["codec_errors"] == 0


@needs_opus
def test_our_voice_reaches_him_with_its_top_end():
    """Outbound: TTS at 24 kHz arrives at his decoder with 10 kHz intact."""
    call, peer, (key, salt), _ = connect(codec.Choice("opus", 96, 48000))
    call.send_audio(tones(WIDE, 1.0, rate=24000), rate=24000)
    heard = peer.hear(key, salt, 60)
    assert band_db(heard[9600:], 48000, 10000) > -6


def test_a_g711_peer_still_gets_g711_and_it_is_narrowband():
    """The control: same signal, G.711 line, nothing survives above 4 kHz."""
    call, peer, _, ours = connect(codec.PCMU)
    assert call.rate == 8000
    peer.say(tones(WIDE, 1.2, rate=8000 * 6), 48000, ours)
    model = call.receive_audio(1.0)
    assert band_db(model[1600:], 16000, 6000) < -30


def test_a_recording_is_written_at_the_lines_own_rate(tmp_path):
    import json
    import wave
    from pathlib import Path

    from hotline_ios.media.record import Recorder

    rec = Recorder(str(tmp_path))
    rec.rate, rec.codec = 48000, "Opus 48 kHz mono, 32 kbit/s"
    one_second = bytes(48000 * 2)
    rec.turn(1, one_second, "zdravo")
    where = rec.finish(one_second, {}, outbound=one_second)
    with wave.open(f"{where}/turn-01.wav") as clip:
        assert (clip.getframerate(), clip.getnframes()) == (48000, 48000)
    manifest = json.loads(Path(where, "manifest.json").read_text())
    assert manifest["rate"] == 48000 and manifest["turns"][0]["seconds"] == 1.0
    assert manifest["codec"].startswith("Opus")
