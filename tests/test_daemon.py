"""The daemon, driven over real HTTP against hotline's real server.

Skipped rather than faked when hotline is not importable: this test's whole
value is that it uses hotline's actual httpd.Server, so a stub version of it
would assert nothing. The skip is loud.
"""

import asyncio
import json
import urllib.error
import urllib.request

import numpy as np
import pytest

hotline_httpd = pytest.importorskip(
    "hotline.httpd",
    reason="hotline not on sys.path -- run with PYTHONPATH=/home/bodas/data/hotline/src",
)

from hotline_ios.daemon import Service, build_server
from hotline_ios.ring.base import AudioFormat, CallTarget
from hotline_ios.ring.loopback import LoopbackTransport
from hotline_ios.ring.watch import ConfirmedRing


class Utterance:
    def __init__(self, audio, seconds): self.audio = audio; self.seconds = seconds


class OneShotSegmenter:
    """Deterministic stand-in for silero.

    The real VAD correctly refuses to hear speech in a synthetic tone, which is
    right of it -- so a daemon-wiring test that used it would be testing silero
    rather than the daemon.
    """
    def __init__(self): self.seen = 0
    def feed(self, mono):
        self.seen += 1
        if self.seen == 1:
            yield Utterance(mono, 1.0)

FMT = AudioFormat(rate=16_000, channels=1, frame_ms=20)


class Reply:
    def __init__(self, text): self.text = text; self.notice = ""


class FakePool:
    def __init__(self): self.asked = []
    async def ask(self, key, text, narrator=None, timeout=None, origin=None):
        self.asked.append((key, text, origin))
        return ("fresh", Reply("nothing is on fire"))


class FakeTranscriber:
    def transcribe(self, audio): return "yes go ahead"


class FakeSpeaker:
    rate = 16_000
    def __init__(self): self.said = []
    def synthesize(self, text):
        self.said.append(text)
        return np.zeros(1600, dtype=np.float32)


def post(port, path, payload, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(port, path, timeout=10):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read())


async def run_server(service, port):
    server = build_server(service, "127.0.0.1", port)
    await server.start()
    return server


async def test_health_reports_whether_the_ring_survives_a_locked_phone():
    service = Service(LoopbackTransport(FMT), FakePool())
    server = await run_server(service, 18790)
    try:
        body = await asyncio.to_thread(get, 18790, "/health")
        assert body["ok"] is True
        # The single most important fact about a ring transport, on the health
        # endpoint rather than buried in a doc.
        assert "rings_when_closed" in body
        assert body["transport"] == "loopback"
    finally:
        await server.close()


async def test_an_undeliverable_ring_is_reported_loudly_not_silently():
    inner = LoopbackTransport(FMT, confirms=False, answer=False)
    service = Service(ConfirmedRing(inner, confirm_within=0.2), FakePool())
    server = await run_server(service, 18791)
    try:
        body = await asyncio.to_thread(post, 18791, "/api/v1/call", {"reason": "the build is stuck"})
        assert body["state"] == "unreachable"
        # It must also show up on /health -- a ringer that has been quietly
        # failing is exactly what a health check exists to surface.
        health = await asyncio.to_thread(get, 18791, "/health")
        assert health["degradations"], health
    finally:
        await server.close()


async def test_declined_is_not_reported_as_a_failure():
    service = Service(LoopbackTransport(FMT, decline=True), FakePool())
    server = await run_server(service, 18792)
    try:
        body = await asyncio.to_thread(post, 18792, "/api/v1/call", {"reason": "ping"})
        assert body["state"] == "declined"
        assert not service.degradations  # a decline is an answer, not a fault
    finally:
        await server.close()


async def test_a_full_call_answers_and_returns_what_he_said():
    transport = LoopbackTransport(FMT)
    pool = FakePool()
    speaker = FakeSpeaker()
    service = Service(transport, pool, transcriber=FakeTranscriber(), speaker=speaker,
                      segmenter_factory=OneShotSegmenter)
    server = await run_server(service, 18793)
    try:
        async def caller():
            return await asyncio.to_thread(
                post, 18793, "/api/v1/call",
                {"reason": "may I spend money on a UI agency", "source": "the ios build"})

        task = asyncio.create_task(caller())
        # Wait for the transport to have a stream, then talk into it.
        for _ in range(200):
            if transport.streams:
                break
            await asyncio.sleep(0.01)
        stream = transport.streams[0]
        stream.feed(b"\x10\x00" * 320)
        for _ in range(300):
            if pool.asked:
                break
            await asyncio.sleep(0.01)
        await stream.close()
        body = await asyncio.wait_for(task, timeout=15)

        assert body["state"] == "answered"
        assert body["reply"] == "yes go ahead"
        assert {"who": "claude", "text": "nothing is on fire"} in body["transcript"]
        # He must hear WHY the phone rang, or he answers to silence.
        assert any("the ios build here" in s for s in speaker.said), speaker.said
        # And the turn must be labelled as spoken, because a mishearing on a
        # bypassPermissions session has no undo.
        _key, _text, origin = pool.asked[0]
        assert origin is not None and getattr(origin, "kind", "") == "voice"
    finally:
        await server.close()


async def test_the_event_feed_replays_from_a_cursor_without_loss():
    transport = LoopbackTransport(FMT)
    service = Service(transport, FakePool(), transcriber=FakeTranscriber(), speaker=FakeSpeaker())
    server = await run_server(service, 18794)
    try:
        task = asyncio.create_task(asyncio.to_thread(
            post, 18794, "/api/v1/call", {"reason": "status", "wait": False}))
        body = await asyncio.wait_for(task, timeout=15)
        call_id = body["call_id"]

        first = await asyncio.to_thread(
            post, 18794, "/api/v1/events", {"call_id": call_id, "since": 0, "wait": 1})
        assert first["events"], first
        assert first["cursor"] >= 1
        # A reconnect from the same cursor -- what a wifi-to-cellular handover
        # looks like -- must not replay or lose anything.
        again = await asyncio.to_thread(
            post, 18794, "/api/v1/events",
            {"call_id": call_id, "since": first["cursor"], "wait": 0})
        assert all(e["seq"] > first["cursor"] for e in again["events"])
        assert first["gap"] is False
    finally:
        await server.close()


async def test_a_bad_call_id_is_404_not_an_empty_feed():
    service = Service(LoopbackTransport(FMT), FakePool())
    server = await run_server(service, 18795)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            await asyncio.to_thread(
                post, 18795, "/api/v1/events", {"call_id": "nope", "since": 0, "wait": 0})
        assert exc.value.code == 404
    finally:
        await server.close()


async def test_loopback_is_always_allowed_even_with_an_allowlist_set():
    # A blocked agent runs on THIS box, and must not be locked out of the phone
    # by the allowlist that exists to keep everyone else out.
    service = Service(LoopbackTransport(FMT), FakePool(),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      allow_ips={"100.108.255.28"})
    server = await run_server(service, 18796)
    try:
        body = await asyncio.to_thread(
            post, 18796, "/api/v1/call", {"reason": "x", "wait": False})
        assert body["state"] == "ringing"
    finally:
        await server.close()


async def test_a_call_nobody_hangs_up_is_ended_rather_than_leaked():
    # Found by running it: place() waited on the call forever, so a transport
    # that never closes its stream blocked the HTTP request indefinitely.
    service = Service(LoopbackTransport(FMT), FakePool(),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker())
    server = await run_server(service, 18797)
    try:
        body = await asyncio.to_thread(
            post, 18797, "/api/v1/call", {"reason": "hello", "timeout": 0.5}, 20)
        assert body["state"] == "ended"
        assert "exceeded" in body["detail"]
        assert body["waited_seconds"] < 10
    finally:
        await server.close()
