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
    service = Service(LoopbackTransport(), FakePool())
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
    inner = LoopbackTransport(confirms=False, answer=False)
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
    service = Service(LoopbackTransport(decline=True), FakePool())
    server = await run_server(service, 18792)
    try:
        body = await asyncio.to_thread(post, 18792, "/api/v1/call", {"reason": "ping"})
        assert body["state"] == "declined"
        assert not service.degradations  # a decline is an answer, not a fault
    finally:
        await server.close()


async def test_the_question_is_waiting_in_the_app_before_it_even_rings():
    # He must never answer a phone to silence. Whether he opens the app during
    # the ring or an hour later, the question is already there.
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18804)
    try:
        body = await asyncio.to_thread(
            post, 18804, "/api/v1/call",
            {"reason": "may I spend money on a UI agency", "source": "the ios build",
             "wait": False})
        page = await asyncio.to_thread(
            post, 18804, "/api/v1/events",
            {"call_id": body["conversation"], "since": 0, "wait": 0})
        asked = [e for e in page["events"] if e["kind"] == "claude"]
        assert asked and "the ios build: may I spend money" in asked[0]["text"]
    finally:
        await server.close()


async def test_ringing_out_leaves_the_conversation_open_for_him():
    # It rang and he did not pick up. He may open the app five minutes later,
    # and closing the conversation here would throw away the question he is
    # about to answer.
    service = Service(LoopbackTransport(answer=False), FakePool())
    server = await run_server(service, 18805)
    try:
        body = await asyncio.to_thread(
            post, 18805, "/api/v1/call", {"reason": "ping", "ring_timeout": 0.05}, 20)
        assert body["state"] == "unanswered"
        page = await asyncio.to_thread(
            post, 18805, "/api/v1/events",
            {"call_id": body["conversation"], "since": 0, "wait": 0})
        assert page["closed"] is False
    finally:
        await server.close()


async def test_his_reply_in_the_app_comes_back_on_hotline_calls_stdout():
    """The contract that must survive a change of doorbell.

    Telegram rings; he answers by typing in the app. A blocked agent still gets
    his words back, and does not need to know those are two different programs.
    """
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18806)
    try:
        async def caller():
            return await asyncio.to_thread(
                post, 18806, "/api/v1/call",
                {"reason": "may I spend money", "timeout": 20}, 40)

        task = asyncio.create_task(caller())
        # Find the conversation the ring opened, then answer it as the app would.
        for _ in range(200):
            if service.calls:
                break
            await asyncio.sleep(0.01)
        conversation = next(iter(service.calls))
        await asyncio.sleep(0.1)
        service.calls[conversation].append("you", "yes, go ahead", at=0.0)

        body = await asyncio.wait_for(task, timeout=30)
        assert body["state"] == "answered"
        assert body["reply"] == "yes, go ahead"
    finally:
        await server.close()


async def test_the_event_feed_replays_from_a_cursor_without_loss():
    transport = LoopbackTransport()
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
    service = Service(LoopbackTransport(), FakePool())
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
    service = Service(LoopbackTransport(), FakePool(),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      allow_ips={"100.108.255.28"})
    server = await run_server(service, 18796)
    try:
        body = await asyncio.to_thread(
            post, 18796, "/api/v1/call", {"reason": "x", "wait": False})
        assert body["state"] == "ringing"
    finally:
        await server.close()


async def test_delegating_returns_a_conversation_and_the_answer_arrives_on_the_feed():
    """What the app actually does: send an instruction, follow the reply.

    The answer deliberately does NOT come back on this response -- a task can
    take minutes and an HTTP request open that long is one that dies on a
    network handover.
    """
    pool = FakePool()
    service = Service(LoopbackTransport(), pool)
    server = await run_server(service, 18801)
    try:
        sent = await asyncio.to_thread(
            post, 18801, "/api/v1/say", {"text": "what is the status", "agent": "hotline-80"})
        conversation = sent["conversation"]
        assert conversation

        seen: list[dict] = []
        for _ in range(50):
            page = await asyncio.to_thread(
                post, 18801, "/api/v1/events",
                {"call_id": conversation, "since": 0, "wait": 1}, 15)
            seen = page["events"]
            if any(e["kind"] == "claude" for e in seen):
                break
        kinds = [e["kind"] for e in seen]
        assert "you" in kinds and "claude" in kinds, seen
        assert next(e for e in seen if e["kind"] == "claude")["text"] == "nothing is on fire"

        # The turn must be labelled as typed rather than spoken: there is no STT
        # in this path any more, and claiming a mis-hearing risk that does not
        # exist makes the label useless where it does.
        _key, _text, origin = pool.asked[0]
        assert getattr(origin, "kind", "") == "phone"
    finally:
        await server.close()


async def test_saying_nothing_is_a_400():
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18802)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            await asyncio.to_thread(post, 18802, "/api/v1/say", {"text": "   "})
        assert exc.value.code == 400
    finally:
        await server.close()


async def test_the_agent_list_survives_a_missing_registry():
    # It reads hotline's registry and live sessions; neither is guaranteed to be
    # there, and an empty list is a far better answer than a 500 on the one
    # screen he opens first.
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18803)
    try:
        body = await asyncio.to_thread(post, 18803, "/api/v1/agents", {})
        assert isinstance(body["agents"], list)
    finally:
        await server.close()


async def test_dismissing_a_conversation_closes_it_and_is_idempotent():
    # He can dismiss a question he is not going to answer. Doing it twice is a
    # 200, not a 404 -- the app can send it just after the agent gave up, and
    # turning that race into a failure shows him an error for something that
    # worked.
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18807)
    try:
        body = await asyncio.to_thread(
            post, 18807, "/api/v1/call", {"reason": "ping", "wait": False})
        conversation = body["conversation"]
        await asyncio.to_thread(post, 18807, "/api/v1/hangup", {"call_id": conversation})
        await asyncio.to_thread(post, 18807, "/api/v1/hangup", {"call_id": conversation})
        page = await asyncio.to_thread(
            post, 18807, "/api/v1/events", {"call_id": conversation, "since": 0, "wait": 0})
        assert page["closed"] is True
    finally:
        await server.close()


def test_the_doorbell_is_assembled_from_a_list_in_order():
    # "We will do both" as a configuration rather than a fork.
    from hotline_ios.daemon import build_transport

    one = build_transport(["loopback"])
    assert one.name == "loopback+confirmed"

    both = build_transport(["loopback", "loopback"])
    assert "+" in both.name and len(both.links) == 2


def test_each_link_is_confirmed_individually_not_the_chain_as_a_whole():
    # Wrapping the outside would let a silent failure inside look like success,
    # and the chain can only fall through on evidence.
    from hotline_ios.daemon import build_transport
    from hotline_ios.ring.watch import ConfirmedRing

    chain = build_transport(["loopback", "loopback"])
    assert all(isinstance(link, ConfirmedRing) for link in chain.links)


def test_an_unknown_or_empty_doorbell_is_refused_at_startup():
    from hotline_ios.daemon import build_transport

    with pytest.raises(SystemExit):
        build_transport(["carrier-pigeon"])
    with pytest.raises(SystemExit):
        build_transport([""])


async def test_the_app_can_find_a_question_it_did_not_open():
    """The gap found by running it: a ring opens a conversation on the SERVER.

    The phone was not involved and has no id for it, so without a listing the
    question sits there and the app cannot find it.
    """
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18808)
    try:
        await asyncio.to_thread(
            post, 18808, "/api/v1/call",
            {"reason": "may I spend money", "source": "the ios build", "wait": False})

        listing = await asyncio.to_thread(post, 18808, "/api/v1/conversations", {})
        rows = listing["conversations"]
        assert len(rows) == 1
        assert rows[0]["waiting"] is True
        assert "the ios build: may I spend money" in rows[0]["asked"]

        await asyncio.to_thread(
            post, 18808, "/api/v1/reply",
            {"conversation": rows[0]["conversation"], "text": "yes, go ahead"})

        after = await asyncio.to_thread(post, 18808, "/api/v1/conversations", {})
        assert after["conversations"][0]["answered"] is True
        assert after["conversations"][0]["waiting"] is False
    finally:
        await server.close()


async def test_replying_to_a_conversation_that_does_not_exist_is_a_404():
    service = Service(LoopbackTransport(), FakePool())
    server = await run_server(service, 18809)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            await asyncio.to_thread(
                post, 18809, "/api/v1/reply", {"conversation": "nope", "text": "hi"})
        assert exc.value.code == 404
    finally:
        await server.close()


def test_both_doorbells_assemble_in_order():
    """His instruction, as a configuration: "Okay we will do both".

    This value -- telegram,sip -- is the one build_transport's own docstring
    uses as the example, and for a while it was also the one it refused.
    """
    from hotline_ios.daemon import build_transport

    chain = build_transport(["telegram", "sip"])
    assert [link.inner.name for link in chain.links] == ["telegram", "sip"]
    # Telegram cannot prove a ring landed; SIP gets a literal 180 Ringing. Both
    # are still wrapped, because the chain can only fall through on evidence.
    assert chain.rings_when_closed is True
