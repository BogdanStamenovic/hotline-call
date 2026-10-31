"""The fallback answerer: its watch, its state, its media, and that it stays lean.

The live half -- a real INVITE through linphone.org to a real Pi -- is
`bench/fallback_live.py`; this is everything that can be proven on one box.
"""

import json
import socket
import struct
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

from hotline_call import fallback
from hotline_call.media import codec, srtp
from hotline_call.ring.inbound import answer_offer

SRC = Path(__file__).resolve().parents[1] / "src"


def test_imports_without_numpy_or_libopus():
    # What the Pi has: no numpy, no soxr, no PyAV. A module-level import of any
    # of them anywhere on the fallback's path fails here before it fails there.
    code = (
        "import sys\n"
        "for m in ('numpy', 'soxr', 'av', 'faster_whisper', 'websockets'): sys.modules[m] = None\n"
        "import hotline_call.fallback, hotline_call.ring.inbound, hotline_call.client\n"
        "from hotline_call.media import codec\n"
        "assert codec.answer_lines('m=audio 1 RTP/SAVP 0\\r\\n', opus=True)\n"
        "print('lean')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}, check=False)
    assert out.returncode == 0, out.stderr
    assert "lean" in out.stdout


# ---- the watch ---------------------------------------------------------------

def test_watch_needs_a_sustained_outage():
    watch = fallback.Watch(60, now=0)
    # A calld restart: a few failed pings, then back. Never "down".
    for t in (10, 20, 30, 40, 50):
        assert watch.observe(False, t) == ""
    assert watch.observe(True, 55) == ""
    for t in (65, 75, 85, 95, 105):
        assert watch.observe(False, t) == ""
    assert watch.observe(False, 115) == "down"
    assert watch.observe(False, 125) == ""          # said once
    assert watch.observe(True, 130) == "up"
    assert watch.observe(True, 140) == ""


def test_instances_never_collide_with_production():
    prod = fallback.production_instance("hotline-caller", "sip.linphone.org")
    live = fallback.instance_for("hotline-caller", "sip.linphone.org", test=False)
    test = fallback.instance_for("hotline-caller", "sip.linphone.org", test=True)
    assert len({prod, live, test}) == 3
    # And production's is what SipInbound itself derives on archserver.
    from hotline_call.ring import inbound

    sip = inbound.SipInbound(user="hotline-caller", password="x", domain="sip.linphone.org",
                             peer="sip:him@x", instance=None)
    if socket.gethostname() == "archserver":
        assert sip.instance == prod


# ---- state -------------------------------------------------------------------

def test_state_is_one_callback_per_outage_and_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    state = fallback.State(path)
    assert state.he_called("sip:him@x") is True
    assert state.he_called("sip:him@x") is False
    again = fallback.State(path)                   # a pigion reboot
    assert again.data["pending_callback"]["calls"] == 2
    pending = again.take_pending()
    assert pending is not None
    assert json.loads(path.read_text())["pending_callback"] is None   # claimed on disk first
    assert again.take_pending() is None
    again.finish(pending, "placed: answered", retry=False)
    assert again.data["pending_callback"] is None
    assert again.data["last_callback"]["outcome"] == "placed: answered"


def test_state_retries_only_a_refused_callback_and_gives_up(tmp_path):
    state = fallback.State(tmp_path / "s.json")
    state.he_called("sip:him@x")
    for _ in range(fallback.CALLBACK_ATTEMPTS - 1):
        pending = state.take_pending()
        state.finish(pending, "unreachable", retry=True)
        assert state.data["pending_callback"] is not None
    pending = state.take_pending()
    state.finish(pending, "unreachable", retry=True)
    assert state.data["pending_callback"] is None


# ---- the clip and the media ------------------------------------------------------

def write_clip(directory: Path, frames: int = 10) -> None:
    directory.mkdir(parents=True)
    opus = [bytes([0xF8, i, 0xFE]) for i in range(frames)]
    (directory / "opus.bin").write_bytes(b"".join(struct.pack("!H", len(p)) + p for p in opus))
    (directory / "pcmu.bin").write_bytes(b"\x01" * 160 * frames)
    (directory / "manifest.json").write_text(json.dumps({
        "format": 1, "text": "x", "text_sha": "x", "seconds": frames * 0.02,
        "codecs": {
            "opus": {"file": "opus.bin", "framing": "u16len", "clock": 48000},
            "PCMU": {"file": "pcmu.bin", "framing": "fixed", "frame_bytes": 160, "clock": 8000},
        }}))


def test_clip_loads_both_framings(tmp_path):
    write_clip(tmp_path / "clip", frames=7)
    clip = fallback.Clip(tmp_path / "clip")
    assert clip.has("opus") and clip.has("PCMU") and not clip.has("PCMA")
    assert clip.frames["opus"][3] == bytes([0xF8, 3, 0xFE])
    assert len(clip.frames["PCMU"]) == 7
    # No PCMA clip: a second of silence rather than nothing at all.
    assert fallback.frames_for(clip, "PCMA")[0] == b"\xd5" * 160
    assert not fallback.Clip(tmp_path / "missing").frames


def test_opus_is_answered_without_libopus(monkeypatch):
    monkeypatch.setattr(codec, "opus_library", lambda: None)
    key, salt = srtp.new_key_salt()
    sdp = ("v=0\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 4000 RTP/SAVP 96 0 8\r\n"
           "a=rtpmap:96 opus/48000/2\r\n" + srtp.crypto_line(key, salt) + "\r\n")
    answer, agreed = answer_offer(sdp, "127.0.0.1", 5000, key, salt)
    assert "m=audio 5000 RTP/SAVP 0" in answer          # no libopus: G.711
    answer, agreed = answer_offer(sdp, "127.0.0.1", 5000, key, salt, opus=True)
    assert "m=audio 5000 RTP/SAVP 96" in answer         # pre-encoded Opus
    host, port, pt, crypto = fallback.parse_agreed(agreed)
    assert (host, port, pt) == ("127.0.0.1", 4000, 96)
    assert crypto.startswith("a=crypto:")
    assert codec.negotiate(agreed, opus=True).name == "opus"


def test_play_sends_decryptable_paced_rtp_and_latches():
    ours, theirs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), \
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    theirs.bind(("127.0.0.1", 0))
    decoy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    decoy.bind(("127.0.0.1", 0))
    key, salt = srtp.new_key_salt()
    his_key, his_salt = srtp.new_key_salt()
    # He sends one authenticated packet first, from `theirs`, while the SDP
    # named `decoy`: the stream must follow the packet, not the SDP.
    his = srtp.SrtpSession(his_key, his_salt)
    theirs.sendto(his.protect(struct.pack("!BBHII", 0x80, 0, 1, 1, 7) + b"\xff" * 160),
                  ours.getsockname())
    frames = [bytes([i]) * 160 for i in range(25)]
    stats = fallback.play(frames, sock=ours, remote=decoy.getsockname(), pt=0, ts_step=160,
                          key=key, salt=salt,
                          their_crypto=srtp.crypto_line(his_key, his_salt), frame_s=0.004)
    assert stats.sent == 25 and stats.received == 1 and stats.rx_auth_failures == 0
    assert stats.latched == "{}:{}".format(*theirs.getsockname())
    rx = srtp.SrtpSession(key, salt)
    theirs.settimeout(1)
    got, seqs, stamps = [], [], []
    while len(got) < 25:
        plain = rx.unprotect(theirs.recv(2048))
        _, mpt, seq, ts, _ = struct.unpack("!BBHII", plain[:12])
        got.append(plain[12:])
        seqs.append(seq)
        stamps.append(ts)
        if len(got) == 1:
            assert mpt & 0x80          # marker on the first packet
    assert got == frames
    assert all((b - a) & 0xFFFF == 1 for a, b in pairwise(seqs))
    assert all((b - a) & 0xFFFFFFFF == 160 for a, b in pairwise(stamps))
    for s in (ours, theirs, decoy):
        s.close()


def test_magic_packet():
    packet = fallback.magic_packet("a8:a1:59:fd:4d:13")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("a8a159fd4d13")
    with pytest.raises(ValueError):
        fallback.magic_packet("nope")


# ---- the loop, with everything outward faked -----------------------------------------

class FakeInbound:
    def __init__(self) -> None:
        self.started = self.stopped = 0
        self.call = None

    def configured(self) -> str:
        return ""

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeCall:
    caller = "sip:him@x"


def service(tmp_path, monkeypatch, probes, **cfg):
    monkeypatch.setenv("SIP_USER", "hotline-caller")
    monkeypatch.setenv("SIP_PEER", "sip:him@x")
    config = fallback.Config(state_path=tmp_path / "state.json", clip_dir=tmp_path / "none",
                             down_after=cfg.pop("down_after", 0), wol_mac="a8:a1:59:fd:4d:13",
                             callback_grace=0, **cfg)
    inbounds: list[FakeInbound] = []
    placed: list[dict] = []
    woken: list[str] = []

    def make(_svc):
        inbounds.append(FakeInbound())
        return inbounds[-1]

    def place(reason, **kw):
        placed.append({"reason": reason, **kw})
        return type("Outcome", (), {"state": "answered"})()

    answers = iter(probes)
    svc = fallback.Fallback(config, inbound_factory=make, probe=lambda _url: next(answers),
                            wol=lambda mac, b, p: woken.append(mac) or ["x:9"], place=place,
                            health=lambda _url: {"ok": True, "inbound": {"registered": True}})
    return svc, inbounds, placed, woken


def join_callback(svc):
    if svc.callback_thread is not None:
        svc.callback_thread.join(5)


def test_outage_registers_answers_wakes_and_calls_back_once(tmp_path, monkeypatch):
    svc, inbounds, placed, woken = service(tmp_path, monkeypatch,
                                           [False, False, True, True, True])
    svc.tick()                                     # down (down_after 0): register
    assert len(inbounds) == 1 and inbounds[0].started == 1
    svc._on_call(FakeCall())                       # he calls
    svc._on_call(FakeCall())                       # and again
    for thread in list(__import__("threading").enumerate()):
        if thread.name == "wol":
            thread.join(0.5)
    assert woken                                   # magic packet on the first call
    svc.tick()                                     # still down
    svc.tick()                                     # up: unregister, call back
    join_callback(svc)
    assert inbounds[0].stopped == 1
    assert len(placed) == 1
    assert placed[0]["reason"] == fallback.CALLBACK_REASON
    assert placed[0]["wait"] is False
    assert "2 puta" in placed[0]["context"]
    svc.tick()
    svc.tick()
    join_callback(svc)
    assert len(placed) == 1                        # once per outage
    svc.stop()


def test_a_callback_owed_before_a_restart_is_still_made(tmp_path, monkeypatch):
    fallback.State(tmp_path / "state.json").he_called("sip:him@x")
    svc, inbounds, placed, _ = service(tmp_path, monkeypatch, [True])
    svc.tick()
    join_callback(svc)
    assert not inbounds                            # calld up: never registered
    assert len(placed) == 1


def test_never_unregisters_under_a_call(tmp_path, monkeypatch):
    svc, inbounds, _, _ = service(tmp_path, monkeypatch, [False, True, True])
    svc.tick()
    inbounds[0].call = object()
    svc.tick()
    assert inbounds[0].stopped == 0
    inbounds[0].call = None
    svc.tick()
    assert inbounds[0].stopped == 1


def test_test_mode_forces_down_and_takes_only_our_own_account(tmp_path, monkeypatch):
    svc, inbounds, placed, _ = service(tmp_path, monkeypatch, [], test=True)
    svc.tick()                                     # no probe at all
    assert len(inbounds) == 1
    assert svc._peer() == "sip:hotline-caller@sip.linphone.org"
    assert svc.instance == fallback.instance_for("hotline-caller", "sip.linphone.org", test=True)
    fallback.State(svc.cfg.state_path).he_called("x")
    svc.state = fallback.State(svc.cfg.state_path)
    svc.tick()
    assert not placed                              # forced down never calls back
