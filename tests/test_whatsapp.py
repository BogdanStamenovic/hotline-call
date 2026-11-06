"""The WhatsApp doorbell, against a fake wa-call daemon speaking its real protocol.

No WhatsApp account is linked on archserver, and nothing here may ring a real
phone, so `FakeWaCall` stands in for `wa-call daemon`: the same JSON-lines
control socket, the same events, and -- the part that matters -- it connects to
the audio socket a dial names and exchanges raw s16le 16 kHz with it, exactly
as `wa-call --socket` does. Numbers are fake (+381600000000).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import queue
import socket
import struct
import tempfile
import threading
import time

import pytest

from hotline_call.media.pcmlink import Linear16, PcmPump
from hotline_call.media.voicecall import VoiceCall
from hotline_call.ring.base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable
from hotline_call.ring.watch import ConfirmedRing
from hotline_call.ring.whatsapp import (
    WaCallClient,
    WhatsAppTransport,
    normalise_number,
    number_of,
    peer_number,
)

NUMBER = "+381600000000"


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    # AF_UNIX paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
    with tempfile.TemporaryDirectory(prefix="hlwa-") as d:
        monkeypatch.setenv("XDG_RUNTIME_DIR", d)
        yield pathlib.Path(d)


class FakeWaCall:
    """Enough of `wa-call daemon` to place, answer and end calls.

    `script` decides what happens after a dial: a list of event names emitted
    in order (with `delay` between), e.g. ["peer-ringing", "accepted"].
    """

    def __init__(self, path: pathlib.Path, *, logged_in: bool = True,
                 script: list[str] | None = None, delay: float = 0.05) -> None:
        self.path = path
        self.logged_in = logged_in
        self.script = script if script is not None else ["peer-ringing", "accepted"]
        self.delay = delay
        self.requests: list[dict] = []
        self.subscribers: list[socket.socket] = []
        self.audio: socket.socket | None = None
        self.heard = bytearray()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.closed = True
        self.sock.close()
        for s in self.subscribers:
            s.close()
        if self.audio is not None:
            self.audio.close()

    def emit(self, kind: str, **fields) -> None:
        line = (json.dumps({"event": kind, **fields}) + "\n").encode()
        for s in list(self.subscribers):
            try:
                s.sendall(line)
            except OSError:
                self.subscribers.remove(s)

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def _accept(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.split(b"\n", 1)[0])
        self.requests.append(req)
        op = req["op"]
        reply: dict = {"ok": True}
        if op == "events":
            conn.sendall(b'{"ok":true}\n')
            self.subscribers.append(conn)
            return
        if op == "status":
            reply.update(logged_in=self.logged_in, live=[], ringing=[])
        elif op in ("dial", "answer"):
            if op == "dial" and not self.logged_in:
                conn.sendall(b'{"ok":false,"error":"not linked/online yet"}\n')
                return
            cid = req.get("call_id") or "CALL1"
            reply["call_id"] = cid
            path = req["spec"]["audio"]["socket"]
            self.audio = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.audio.connect(path)
            threading.Thread(target=self._read_audio, daemon=True).start()
            if op == "dial":
                threading.Thread(target=self._play_script, args=(cid,), daemon=True).start()
        elif op == "hangup":
            reply["call_id"] = req.get("call_id")
            self.emit("ended", call_id=req.get("call_id"), by="us")
        conn.sendall((json.dumps(reply) + "\n").encode())
        conn.close()

    def _play_script(self, cid: str) -> None:
        for kind in self.script:
            time.sleep(self.delay)
            fields = {"declined": True} if kind == "rejected" else {}
            self.emit(kind, call_id=cid, **fields)

    def _read_audio(self) -> None:
        while self.audio is not None:
            try:
                data = self.audio.recv(65536)
            except OSError:
                return
            if not data:
                return
            self.heard += data

    def say(self, samples: list[int]) -> None:
        """The peer talking: raw s16le into the audio socket, as wa-call writes it."""
        assert self.audio is not None
        self.audio.sendall(struct.pack(f"<{len(samples)}h", *samples))


@pytest.fixture
def fake(runtime):
    fakes: list[FakeWaCall] = []

    def make(**kw) -> tuple[FakeWaCall, WhatsAppTransport]:
        f = FakeWaCall(runtime / "wa-call.sock", **kw)
        fakes.append(f)
        return f, WhatsAppTransport(client=WaCallClient(f.path, timeout=2.0), bogdan="",
                                    answer_from="")
    yield make
    for f in fakes:
        f.close()


def guest(number: str = NUMBER) -> CallTarget:
    return CallTarget(device="phone", reason="test", address=f"whatsapp:{number}",
                      callee="Test", metadata={"whatsapp": number})


# ---- numbers ---------------------------------------------------------------


def test_numbers_are_normalised_to_e164():
    assert normalise_number("+381 60 000-0000") == NUMBER
    assert normalise_number("00381600000000") == NUMBER
    assert normalise_number("381600000000@s.whatsapp.net") == NUMBER
    assert normalise_number("381600000000:12@s.whatsapp.net") == NUMBER
    assert normalise_number("whatsapp:+381600000000") == NUMBER


def test_blank_or_garbage_is_not_a_number():
    for raw in ("", "   ", "milos", "+12", "sip:x@sip.linphone.org"):
        assert normalise_number(raw) == ""


def test_a_lid_is_not_mistaken_for_a_phone_number():
    assert peer_number({"peer": "123456789012345@lid"}) == ""
    assert peer_number({"peer": "123456789012345@lid",
                        "caller_pn": "381600000000@s.whatsapp.net"}) == NUMBER


def test_target_number_comes_from_metadata_or_a_whatsapp_address():
    assert number_of(guest()) == NUMBER
    assert number_of(CallTarget(device="p", address="whatsapp:+381600000000")) == NUMBER
    assert number_of(CallTarget(device="p", address="sip:x@sip.linphone.org")) == ""


# ---- the PCM link ----------------------------------------------------------


def test_pcm_pump_carries_both_directions_at_the_call_clock():
    ours, theirs = socket.socketpair()
    pump = PcmPump(ours)
    call = VoiceCall.over(pump)
    try:
        assert call.rate == 16000 and call.frame_samples == 320
        theirs.sendall(struct.pack("<960h", *([1000] * 960)))   # one 60 ms wa-call frame
        pieces = [pump.inbox.get(timeout=2) for _ in range(3)]
        assert all(len(p) == 640 for p in pieces)
        pump.enqueue([struct.pack("<320h", *([7] * 320))])
        theirs.settimeout(2)
        got = b""
        deadline = time.monotonic() + 2
        while b"\x07\x00" not in got and time.monotonic() < deadline:
            got += theirs.recv(65536)
        assert b"\x07\x00" in got
        # It is a metronome: with nothing queued it keeps sending silence.
        time.sleep(0.2)
        assert call.frames_sent >= 8
    finally:
        call.close()
        call.pump.join(1)
        theirs.close()


def test_pcm_pump_bounds_its_backlog_and_stays_sample_aligned():
    ours, theirs = socket.socketpair()
    ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    pump = PcmPump(ours, max_backlog_ms=100)
    for _ in range(400):                     # 8 s of audio that nobody reads
        pump._send(bytes(640))
    assert len(pump._tx) <= 16000 * 2 // 10 + 640
    assert pump.dropped_bytes % 2 == 0 and pump.dropped_bytes > 0
    theirs.close()
    ours.close()


def test_pcm_pump_notices_the_far_end_closing():
    ours, theirs = socket.socketpair()
    pump = PcmPump(ours)
    pump.start()
    theirs.close()
    deadline = time.monotonic() + 2
    while not pump.eof and time.monotonic() < deadline:
        time.sleep(0.02)
    pump.stop()
    pump.join(1)
    assert pump.eof


def test_linear16_is_identity():
    codec = Linear16()
    frame = struct.pack("<320h", *range(320))
    assert codec.decode(codec.encode(frame)) == frame
    assert codec.idle() == bytes(640)


# ---- ringing ---------------------------------------------------------------


class Holder:
    """Stands in for `AnsweredCall`: holds the call, talks a bit, listens a bit."""

    def __init__(self, fake: FakeWaCall) -> None:
        self.fake = fake
        self.heard: list[bytes] = []
        self.rate = 0

    def hold(self, call: VoiceCall) -> None:
        self.rate = call.rate
        call.pump.enqueue([struct.pack("<320h", *([5] * 320))] * 5)
        self.fake.say([300] * 960)
        try:
            self.heard.append(call.pump.inbox.get(timeout=2))
        finally:
            time.sleep(0.2)
            call.close()
            call.pump.join(1)


async def test_an_answered_whatsapp_call_is_held_over_the_audio_socket(fake):
    f, wa = fake()
    holder = Holder(f)
    wa.on_answer = holder
    await ConfirmedRing(wa, confirm_within=2.0).ring(guest(), timeout=5)
    assert holder.rate == 16000
    assert holder.heard and struct.unpack("<h", holder.heard[0][:2])[0] == 300
    assert b"\x05\x00" in bytes(f.heard), "what the voice said never reached wa-call"
    dial = next(r for r in f.requests if r["op"] == "dial")
    assert dial["to"] == NUMBER and dial["spec"]["audio"]["socket"]
    assert "hangup" in f.ops()
    # The per-call audio socket is gone afterwards.
    assert not list(pathlib.Path(os.environ["XDG_RUNTIME_DIR"], "hotline-call").glob("*.sock"))


async def test_no_daemon_is_unreachable_so_a_chain_falls_through(runtime):
    wa = WhatsAppTransport(client=WaCallClient(runtime / "nothing.sock"), answer_from="")
    with pytest.raises(CallUnreachable, match="no wa-call daemon"):
        await wa.ring(guest(), timeout=2)


async def test_an_unlinked_daemon_is_unreachable(fake):
    f, wa = fake(logged_in=False)
    with pytest.raises(CallUnreachable, match="not linked"):
        await wa.ring(guest(), timeout=2)
    assert "dial" not in f.ops()


async def test_a_decline_is_an_answer_not_a_failure(fake):
    f, wa = fake(script=["peer-ringing", "rejected"])
    with pytest.raises(CallDeclined):
        await ConfirmedRing(wa, confirm_within=2.0).ring(guest(), timeout=5)


async def test_ringing_out_is_unanswered(fake):
    f, wa = fake(script=["peer-ringing"])
    with pytest.raises(CallUnanswered):
        await ConfirmedRing(wa, confirm_within=2.0).ring(guest(), timeout=0.6)
    assert "hangup" in f.ops(), "a call that rang out must be ended, not left ringing"


async def test_no_sign_it_rang_is_unreachable_and_the_dial_is_hung_up(fake):
    # wa-call accepted the dial but the phone never sent preaccept (offline):
    # ConfirmedRing gives up, and the call must not keep ringing behind its back.
    f, wa = fake(script=["connected"])
    with pytest.raises(CallUnreachable):
        await ConfirmedRing(wa, confirm_within=0.5).ring(guest(), timeout=30)
    deadline = time.monotonic() + 3
    while "hangup" not in f.ops() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert "hangup" in f.ops()


async def test_him_over_whatsapp_needs_his_number(fake):
    f, wa = fake()
    with pytest.raises(CallUnreachable, match="HOTLINE_CALL_WA_BOGDAN"):
        await wa.ring(CallTarget(device="phone", reason="x"), timeout=1)


def test_status_reports_daemon_and_link_state(fake, runtime):
    f, wa = fake(logged_in=False)
    st = wa.status()
    assert st["daemon"] is True and st["logged_in"] is False and "error" in st
    down = WhatsAppTransport(client=WaCallClient(runtime / "nothing.sock"), answer_from="")
    assert down.status()["daemon"] is False


# ---- him calling us --------------------------------------------------------


async def test_an_allowed_number_is_answered_and_held(fake):
    f, _ = fake()
    held = queue.Queue()

    class Leg:
        def hold(self, call):
            held.put(call.rate)
            call.close()
            call.pump.join(1)

        def abandon(self, why):
            held.put(f"abandoned: {why}")

    wa = WhatsAppTransport(client=WaCallClient(f.path, timeout=2.0), answer_from=NUMBER)
    wa.on_call = lambda number, hung_up: Leg() if number == NUMBER else None
    await wa.start()
    try:
        deadline = time.monotonic() + 3
        while not f.subscribers and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        f.emit("ringing", call_id="IN1", peer="123@lid", caller_pn="381600000000@s.whatsapp.net",
               direction="incoming")
        assert await asyncio.to_thread(held.get, True, 3) == 16000
        answer = next(r for r in f.requests if r["op"] == "answer")
        assert answer["call_id"] == "IN1"
    finally:
        await wa.stop()


async def test_other_callers_are_left_to_ring_his_phone(fake):
    f, _ = fake()
    wa = WhatsAppTransport(client=WaCallClient(f.path, timeout=2.0), answer_from=NUMBER)
    asked: list[str] = []
    wa.on_call = lambda number, hung_up: asked.append(number)
    await wa.start()
    try:
        deadline = time.monotonic() + 3
        while not f.subscribers and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        f.emit("ringing", call_id="IN2", peer="381611111111@s.whatsapp.net", direction="incoming")
        f.emit("ringing", call_id="IN3", peer="999@lid", direction="incoming")
        await asyncio.sleep(0.5)
        assert asked == [] and "answer" not in f.ops()
    finally:
        await wa.stop()


def test_nothing_is_answered_when_no_number_is_allowed(fake):
    f, wa = fake()
    assert wa.answer_from == set()
    asyncio.run(wa.start())
    assert wa._inbound is None


# ---- routing in the daemon ------------------------------------------------

hotline_httpd = pytest.importorskip("hotline.httpd")


class Rec:
    """A doorbell that records who it was asked to ring, or fails as told."""

    rings_when_closed = True

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.rung: list[CallTarget] = []

    async def start(self): pass
    async def stop(self): pass

    async def ring(self, target, *, timeout=45.0):
        self.rung.append(target)
        if self.fail:
            raise CallUnreachable(f"{self.name}: not linked")


def service_with(**links):
    from hotline_call.daemon import Service

    default = Rec("default")
    service = Service(default)
    service.set_links(links)
    return service, default


async def test_a_whatsapp_only_guest_is_rung_on_whatsapp_as_a_guest():
    wa = Rec("whatsapp")
    service, default = service_with(sip=Rec("sip"), whatsapp=wa)
    out = await service.place({"reason": "r", "whatsapp": "+381 60 000 0000",
                               "callee": "Test", "wait": False})
    assert out["via"] == "whatsapp" and out["state"] == "ringing"
    target = wa.rung[0]
    # Read as a guest -- never as him -- even with no SIP address.
    assert target.address == f"whatsapp:{NUMBER}" and target.callee == "Test"
    assert not default.rung


async def test_auto_follows_the_given_order_and_falls_back_to_sip():
    wa, sip = Rec("whatsapp", fail=True), Rec("sip")
    service, _ = service_with(sip=sip, whatsapp=wa)
    out = await service.place({"reason": "r", "to": "sip:t@sip.linphone.org",
                               "whatsapp": NUMBER, "via": "auto",
                               "via_order": ["whatsapp", "sip"], "wait": False})
    assert len(wa.rung) == 1 and len(sip.rung) == 1
    assert out["via"] == "sip"
    assert any("whatsapp" in d for d in service.degradations)


async def test_without_an_order_sip_comes_first():
    wa, sip = Rec("whatsapp"), Rec("sip")
    service, _ = service_with(sip=sip, whatsapp=wa)
    await service.place({"reason": "r", "to": "sip:t@sip.linphone.org",
                         "whatsapp": NUMBER, "wait": False})
    assert sip.rung and not wa.rung


async def test_explicit_via_is_not_swapped_for_another_channel():
    wa, sip = Rec("whatsapp", fail=True), Rec("sip")
    service, _ = service_with(sip=sip, whatsapp=wa)
    out = await service.place({"reason": "r", "to": "sip:t@sip.linphone.org",
                               "whatsapp": NUMBER, "via": "whatsapp", "wait": False})
    assert out["state"] == "unreachable" and not sip.rung


async def test_via_whatsapp_without_a_number_is_a_usage_error():
    service, _ = service_with(sip=Rec("sip"), whatsapp=Rec("whatsapp"))
    with pytest.raises(hotline_httpd.HttpError) as exc:
        await service.place({"reason": "r", "to": "sip:t@sip.linphone.org", "via": "whatsapp"})
    assert exc.value.status == 400


async def test_a_whatsapp_guest_never_falls_back_to_his_doorbell():
    service, default = service_with(sip=Rec("sip"))
    with pytest.raises(hotline_httpd.HttpError):
        await service.place({"reason": "r", "whatsapp": NUMBER, "wait": False})
    assert not default.rung


async def test_calls_to_him_keep_the_configured_doorbell():
    wa = Rec("whatsapp")
    service, default = service_with(sip=Rec("sip"), whatsapp=wa)
    await service.place({"reason": "r", "wait": False})
    assert default.rung and not wa.rung
    await service.place({"reason": "r", "via": "whatsapp", "wait": False})
    assert wa.rung and wa.rung[0].address == ""


def test_the_whatsapp_doorbell_can_be_built_by_name():
    from hotline_call.daemon import build_transport

    link = build_transport(["whatsapp"])
    assert isinstance(link, ConfirmedRing) and link.inner.name == "whatsapp"


def test_the_real_conversation_runs_over_the_pcm_link():
    """`AnsweredCall.hold` -- the same object a SIP call uses -- on a wa-call stream."""
    import numpy as np

    from hotline_call.conversation import AnsweredCall

    ours, theirs = socket.socketpair()
    heard = bytearray()
    stop = threading.Event()

    def far_end():
        # The peer: 1.5 s of speech-level tone after the greeting, then silence.
        theirs.settimeout(0.02)
        tone = (np.sin(np.arange(960) * 2 * np.pi * 220 / 16000) * 8000).astype("<i2").tobytes()
        start = time.monotonic()
        while not stop.is_set():
            t = time.monotonic() - start
            theirs.sendall(tone if 1.0 < t < 2.5 else bytes(1920))
            try:
                heard.extend(theirs.recv(65536))
            except (TimeoutError, BlockingIOError):
                pass
            except OSError:
                return
            time.sleep(0.06)

    threading.Thread(target=far_end, daemon=True).start()
    said: list[tuple[str, str]] = []
    call = AnsweredCall(
        speak=lambda text: (np.full(3200, 0.2, dtype=np.float32), 16000),
        transcribe=lambda audio: "zdravo",
        opening="",
        greeting="test",
        deliver=lambda text: said.append(("you", text)),
        ask=lambda text: "doviđenja",
        call_seconds=4.0,
        dead_line_seconds=2.0,
        turn_seconds=3.0,
        utterance_seconds=2.0,
    )
    began = time.monotonic()
    try:
        call.hold(VoiceCall.over(PcmPump(ours)))
    finally:
        stop.set()
        theirs.close()
    assert call.stats["frames_sent"] > 50
    assert call.stats["frames_received"] > 50
    assert any(who == "you" and "zdravo" in text for who, text in said), call.transcript
    assert ("claude", "doviđenja") in call.transcript
    print(f"held {time.monotonic() - began:.1f}s, ended: {call.ended}")
    samples = np.frombuffer(bytes(heard[: len(heard) // 2 * 2]), dtype="<i2")
    assert np.abs(samples).max() > 3000, "the greeting never reached wa-call"
