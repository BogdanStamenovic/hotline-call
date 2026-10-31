"""hotline-calld, driven over real HTTP against hotline's real server.

Skipped rather than faked when hotline is not importable: this test's whole
value is that it uses hotline's actual httpd.Server, so a stub version of it
would assert nothing. The skip is loud.
"""

import asyncio
import json
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

hotline_httpd = pytest.importorskip(
    "hotline.httpd",
    reason="hotline not on sys.path -- run with PYTHONPATH=/home/bodas/data/hotline/src",
)

from hotline_call.daemon import Service, build_server
from hotline_call.ring.loopback import LoopbackTransport
from hotline_call.ring.watch import ConfirmedRing


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
    service = Service(LoopbackTransport())
    server = await run_server(service, 18790)
    try:
        body = await asyncio.to_thread(get, 18790, "/health")
        # NOT ok: this service's doorbell is a loopback, which rings nothing.
        # This assertion used to read `is True`, and that was the bug -- on
        # 2026-08-25 /health reported ok:true for 2.5 hours while every page
        # went nowhere. A health check whose job is to catch a dead pager must
        # not pass when the pager is a test double.
        assert body["ok"] is False
        assert body["fake"] is True
        # The single most important fact about a ring transport, on the health
        # endpoint rather than buried in a doc.
        assert "rings_when_closed" in body
        assert body["transport"] == "loopback"
    finally:
        await server.close()


async def test_an_undeliverable_ring_is_reported_loudly_not_silently():
    inner = LoopbackTransport(confirms=False, answer=False)
    service = Service(ConfirmedRing(inner, confirm_within=0.2))
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
    service = Service(LoopbackTransport(decline=True))
    server = await run_server(service, 18792)
    try:
        body = await asyncio.to_thread(post, 18792, "/api/v1/call", {"reason": "ping"})
        assert body["state"] == "declined"
        assert not service.degradations  # a decline is an answer, not a fault
    finally:
        await server.close()


async def test_rang_but_no_reply_in_time_also_closes_unanswered():
    # The other unanswered path: the ring connected but nothing came back within
    # the reply window. Same rule -- say unanswered, close it.
    service = Service(LoopbackTransport(answer=True))
    server = await run_server(service, 18806)
    try:
        body = await asyncio.to_thread(
            post, 18806, "/api/v1/call",
            {"reason": "ping", "ring_timeout": 0.05, "timeout": 0.05}, 20)
        assert body["state"] == "unanswered"
        log = service.calls[body["conversation"]]
        assert log.closed is True
        assert "unanswered" in [e.text for e in log.lines if e.kind == "state"]
    finally:
        await server.close()


async def test_loopback_is_always_allowed_even_with_an_allowlist_set():
    # A blocked agent runs on THIS box, and must not be locked out of the phone
    # by the allowlist that exists to keep everyone else out.
    service = Service(LoopbackTransport(),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      allow_ips={"100.108.255.28"})
    server = await run_server(service, 18796)
    try:
        body = await asyncio.to_thread(
            post, 18796, "/api/v1/call", {"reason": "x", "wait": False})
        assert body["state"] == "ringing"
    finally:
        await server.close()


def test_the_doorbell_is_assembled_from_a_list_in_order():
    # "We will do both" as a configuration rather than a fork.
    from hotline_call.daemon import build_transport

    one = build_transport(["loopback"], allow_fake=True)
    assert one.name == "loopback+confirmed"

    both = build_transport(["loopback", "loopback"], allow_fake=True)
    assert "+" in both.name and len(both.links) == 2


def test_each_link_is_confirmed_individually_not_the_chain_as_a_whole():
    # Wrapping the outside would let a silent failure inside look like success,
    # and the chain can only fall through on evidence.
    from hotline_call.daemon import build_transport
    from hotline_call.ring.watch import ConfirmedRing

    chain = build_transport(["loopback", "loopback"], allow_fake=True)
    assert all(isinstance(link, ConfirmedRing) for link in chain.links)


def test_an_unknown_or_empty_doorbell_is_refused_at_startup():
    from hotline_call.daemon import build_transport

    with pytest.raises(SystemExit):
        build_transport(["carrier-pigeon"])
    with pytest.raises(SystemExit):
        build_transport([""])


def test_both_doorbells_assemble_in_order():
    """His instruction, as a configuration: "Okay we will do both".

    This value -- telegram,sip -- is the one build_transport's own docstring
    uses as the example, and for a while it was also the one it refused.
    """
    from hotline_call.daemon import build_transport

    chain = build_transport(["telegram", "sip"])
    assert [link.inner.name for link in chain.links] == ["telegram", "sip"]
    # Telegram cannot prove a ring landed; SIP gets a literal 180 Ringing. Both
    # are still wrapped, because the chain can only fall through on evidence.
    assert chain.rings_when_closed is True


# --- the loopback-lies defect, 2026-08-25 -------------------------------
#
# A daemon ran on `loopback` for 2.5 hours. /health said ok:true, every call
# returned exit 0 and "ringing via loopback+confirmed", and Bogdan was never
# contacted. Three separate layers each had enough information to catch it and
# none of them looked. One test per layer.


def test_loopback_declares_itself_fake() -> None:
    from hotline_call.ring.loopback import LoopbackTransport
    from hotline_call.ring.watch import ConfirmedRing

    assert LoopbackTransport().is_fake is True
    # Confirmation must not launder it: ConfirmedRing proves the doorbell said
    # it alerted the device, which a loopback does instantly about nothing.
    assert ConfirmedRing(LoopbackTransport()).is_fake is True


def test_a_chain_with_any_fake_link_is_fake() -> None:
    """ANY, not ALL -- the fake link answers first and the real one never runs."""
    from hotline_call.ring.chain import RingChain
    from hotline_call.ring.loopback import LoopbackTransport
    from hotline_call.ring.watch import ConfirmedRing

    class Real:
        name = "real"
        rings_when_closed = True
        is_fake = False

    chain = RingChain([ConfirmedRing(LoopbackTransport()), ConfirmedRing(Real())])
    assert chain.is_fake is True
    assert RingChain([ConfirmedRing(Real()), ConfirmedRing(Real())]).is_fake is False


def test_build_transport_refuses_loopback_unless_told_twice() -> None:
    import pytest

    from hotline_call.daemon import build_transport

    with pytest.raises(SystemExit) as caught:
        build_transport(["loopback"], allow_fake=False)
    assert "rings nothing" in str(caught.value)
    # Explicit opt-in still works, because the test suite needs it.
    assert build_transport(["loopback"], allow_fake=True) is not None


def test_outcome_reports_a_fake_doorbell() -> None:
    from hotline_call.daemon import Service
    from hotline_call.ring.loopback import LoopbackTransport

    service = Service(LoopbackTransport())
    assert service._outcome("c1", "answered", 0.0)["fake"] is True


def test_unknown_transport_is_refused_not_ignored() -> None:
    """`--transport sip` used to reach the body and go no further."""
    import pytest
    from hotline.httpd import HttpError

    from hotline_call.daemon import Service
    from hotline_call.ring.loopback import LoopbackTransport

    default = LoopbackTransport()
    service = Service(default)
    service.set_links({"loopback": default})

    assert service._resolve_transport("") is default
    assert service._resolve_transport("auto") is default
    assert service._resolve_transport("loopback") is default
    with pytest.raises(HttpError):
        service._resolve_transport("sip")


# -- the bug this whole media path exists to fix ---------------------------
#
# He answered a real call on 2026-09-09 at 15:44:57Z and heard silence, because
# `build_transport` constructed `SipTransport()` bare and nothing ever passed
# `on_answer`. 313 tests were green at the time. These are the ones that would
# not have been.


class HoldsCalls:
    """A transport that can hold an answered call, i.e. has `on_answer`.

    Deliberately not a SipTransport: this asserts that the DAEMON installs a
    handler on whatever inside the doorbell can take one, which is a wiring
    question and not a SIP one.
    """

    name = "holds"
    rings_when_closed = True

    def __init__(self):
        self.on_answer = None
        self.during_ring = "never rang"
        self.ringing = asyncio.Event()

    async def start(self): pass
    async def stop(self): pass

    async def ring(self, target, timeout=45.0):
        self.ringing.set()
        self.during_ring = self.on_answer


class FakeFillers:
    def get(self, name): return None


async def test_the_daemon_installs_an_answered_call_handler_for_the_ring():
    inner = HoldsCalls()
    service = Service(ConfirmedRing(inner, confirm_within=1.0),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      fillers=FakeFillers())
    await service.place({"reason": "should I restart it?", "wait": False})
    assert inner.during_ring is not None, "the ring went out with no way to answer it"
    assert callable(inner.during_ring)
    # And taken away again: a doorbell with a handler holds a call it may have
    # nothing to say on, which hotline-page and the confirmed-ring path both
    # depend on it not doing.
    assert inner.on_answer is None


async def test_a_daemon_with_no_voice_rings_and_says_so_rather_than_going_silent():
    inner = HoldsCalls()
    service = Service(ConfirmedRing(inner, confirm_within=1.0))
    assert not service.can_talk
    await service.place({"reason": "status", "wait": False})
    assert inner.during_ring is None, "it must ACK and hang up, not hold a mute call"


async def test_his_spoken_answer_reaches_a_caller_already_waiting_for_it():
    """The cursor race. `place()` used to read `events.latest` AFTER the ring,
    so an answer appended DURING the call was already behind it and
    `hotline-call` waited its full timeout for a question answered out loud."""
    inner = HoldsCalls()
    service = Service(ConfirmedRing(inner, confirm_within=1.0),
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      fillers=FakeFillers())

    async def ring(target, timeout=45.0):
        inner.ringing.set()
        # What the media thread does when he speaks: append his words to the
        # conversation, from inside the ring.
        conversation = next(iter(service.calls))
        service._append(conversation, "you", "da, samo napred")

    inner.ring = ring
    body = await asyncio.wait_for(
        service.place({"reason": "should I?", "timeout": 5.0}), timeout=15)
    assert body["state"] == "answered", body
    assert body["reply"] == "da, samo napred"
    assert body["waited_seconds"] < 3, "it waited for a question already answered"


async def test_a_handler_that_cannot_be_built_still_lets_the_phone_ring():
    """This runs inside place()'s try, whose except clauses are the ring
    outcomes. An exception escaping it is not a silent call -- it is a 500 and
    no ring at all, which is worse than the bug this path fixes."""
    class Exploding:
        def synthesize(self, text): raise AssertionError("never called")

    inner = HoldsCalls()
    service = Service(ConfirmedRing(inner, confirm_within=1.0),
                      transcriber=FakeTranscriber(), speaker=Exploding())

    def boom(*args, **kwargs):
        raise RuntimeError("no session, no context, nothing")

    service._answered_call = boom
    body = await service.place({"reason": "status", "wait": False})
    assert body["state"] == "ringing"
    assert inner.during_ring is None
    assert inner.ringing.is_set(), "the phone must still have rung"


def test_await_transcript_returns_both_sides_and_stops_when_the_call_ends() -> None:
    """His instruction, 2026-09-11: "It should get the whole convo."

    The daemon used to hand back only the first thing he said. Everything after
    it was written into the conversation and never returned, so an agent that
    rang to have a conversation got one sentence of it.
    """
    import asyncio

    from hotline_call.daemon import Service
    from hotline_call.calllog import CallLog as EventLog
    from hotline_call.ring.loopback import LoopbackTransport

    class Spoken:
        turns = 2
        ended = "not started"

    service = Service(LoopbackTransport())
    events = EventLog()
    spoken = Spoken()

    async def drive() -> list[dict[str, str]]:
        async def talk() -> None:
            await asyncio.sleep(0.05)
            events.append("you", "da, radi")
            events.append("claude", "vazi, prosledjujem")
            events.append("state", "ignored")     # not a side of the conversation
            await asyncio.sleep(0.05)
            events.append("you", "to je sve")
            # Terminal path sets `ended`; until then the sentinel means live.
            spoken.ended = "he said goodbye"

        task = asyncio.ensure_future(talk())
        got = await service._await_transcript(events, 5.0, 0, spoken)
        await task
        return got

    turns = asyncio.run(drive())
    assert [t["text"] for t in turns] == ["da, radi", "vazi, prosledjujem", "to je sve"]
    assert [t["who"] for t in turns] == ["you", "claude", "you"]


def test_await_transcript_gives_up_at_the_deadline_if_the_call_never_ends() -> None:
    """A call that never reports an end must not hold the caller past its timeout."""
    import asyncio
    import time

    from hotline_call.daemon import Service
    from hotline_call.calllog import CallLog as EventLog
    from hotline_call.ring.loopback import LoopbackTransport

    class Spoken:
        turns = 1
        ended = "not started"

    service = Service(LoopbackTransport())
    events = EventLog()
    events.append("you", "samo jedna rec")

    began = time.monotonic()
    turns = asyncio.run(service._await_transcript(events, 0.4, 0, Spoken()))
    assert [t["text"] for t in turns] == ["samo jedna rec"]
    assert time.monotonic() - began < 3.0


# -- on-demand models -----------------------------------------------------
# His rule, 2026-09-11: "nothing should be loaded prematurely. Everything
# should be loaded on demand. If an agent does hotline call then everything
# needed gets loaded and then the call made. After the call is done then
# everything unloaded."


class CountingModel:
    """A transcriber/speaker that records its own load/unload calls."""

    def __init__(self) -> None:
        self.loads = 0
        self.unloads = 0

    def load(self) -> None:
        self.loads += 1

    def unload(self) -> bool:
        self.unloads += 1
        return True

    def synthesize(self, text):
        import numpy as np

        return np.zeros(8, dtype="float32")

    def transcribe(self, audio) -> str:
        return ""


def _settle(service, want: int, field: str) -> None:
    """Wait for the warm/cool threads, which are deliberately off the loop."""
    for _ in range(200):
        if getattr(service.transcriber, field) >= want:
            return
        time.sleep(0.01)


@pytest.mark.asyncio
async def test_a_ring_loads_the_models_and_the_end_of_the_call_unloads_them():
    """The whole of his instruction in one pass: nothing is loaded until a call
    needs it, and it is dropped when the call is over."""
    ears, voice = CountingModel(), CountingModel()
    service = Service(ConfirmedRing(HoldsCalls(), confirm_within=1.0),
                      transcriber=ears, speaker=voice)

    assert ears.loads == 0 and voice.loads == 0, "constructing a Service must load nothing"

    await service.place({"reason": "status", "wait": False})

    _settle(service, 1, "loads")
    assert ears.loads == 1 and voice.loads == 1, "the ring must warm both halves"
    _settle(service, 1, "unloads")
    # The voice stays resident: cvoiced leaks VRAM on every unload/load cycle.
    assert ears.unloads == 1 and voice.unloads == 0, "the end of the call drops the ears only"


@pytest.mark.asyncio
async def test_a_second_call_does_not_unload_the_models_under_the_first():
    """Two agents can ring him in the same minute. Unloading when the first one
    finishes would pull `large-v3` out from under the second mid-sentence, so
    the hold is refcounted rather than a bool."""
    ears, voice = CountingModel(), CountingModel()
    service = Service(ConfirmedRing(HoldsCalls(), confirm_within=1.0),
                      transcriber=ears, speaker=voice)

    service._warm_models()
    service._warm_models()
    assert service._calls_holding_models == 2

    service._cool_models()
    assert ears.unloads == 0, "the first call to end must not unload for the second"

    service._cool_models()
    _settle(service, 1, "unloads")
    assert ears.unloads == 1 and voice.unloads == 0, "the last one out unloads the ears; the voice stays resident"


@pytest.mark.asyncio
async def test_a_model_that_will_not_load_costs_the_voice_and_never_the_ring():
    """Same rule `build_voice` follows: a missing model must cost the voice leg,
    not the doorbell. A mute call still beats a silent one."""
    class WillNotLoad(CountingModel):
        def load(self) -> None:
            self.loads += 1
            raise RuntimeError("no cuda today")

    inner = HoldsCalls()
    ears = WillNotLoad()
    service = Service(ConfirmedRing(inner, confirm_within=1.0),
                      transcriber=ears, speaker=CountingModel())

    body = await service.place({"reason": "status", "wait": False})
    _settle(service, 1, "loads")
    assert body["state"] == "ringing"
    assert inner.ringing.is_set(), "the phone must still have rung"


@pytest.mark.asyncio
async def test_a_daemon_with_no_voice_leg_loads_nothing_at_all():
    """`can_talk` is False, so no call can carry audio and no model is wanted.
    Warming here would hold 1.9 GB for a doorbell that only ever rings."""
    ears = CountingModel()
    service = Service(LoopbackTransport(), transcriber=ears, speaker=None)
    await service.place({"reason": "status", "wait": False})
    time.sleep(0.1)
    assert ears.loads == 0, "a daemon that cannot talk must not load a transcriber"


# ---- ringing a registry person rather than him ---------------------------


async def test_a_registry_call_refuses_rather_than_ringing_him_by_fallback():
    """The failure this guard exists to make impossible.

    Every doorbell but SIP reaches exactly one person, and that person is
    Bogdan. If `to` were allowed to fall through to one of them, a question
    meant for somebody in the registry would ring HIM, he would answer it, and
    the caller would be told it was delivered. Refusing out loud is the only
    honest option.
    """
    from hotline_call.ring.loopback import LoopbackTransport

    inner = LoopbackTransport()
    service = Service(inner)
    service.set_links({"loopback": inner})
    import urllib.error

    import pytest

    server = await run_server(service, 18811)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            await asyncio.to_thread(
                post, 18811, "/api/v1/call",
                {"reason": "can you review it?", "to": "sip:milos@sip.linphone.org",
                 "callee": "Milos"},
            )
        # 503, not a 200 carrying a cheerful "answered" from the loopback.
        assert exc.value.code == 503
        assert not service.degradations
    finally:
        await server.close()


async def test_a_registry_call_goes_to_the_sip_link_not_the_configured_chain():
    from hotline_call.ring.loopback import LoopbackTransport

    rung: list[object] = []

    class FakeSip(LoopbackTransport):
        name = "sip"

        async def ring(self, target, *, timeout: float = 45.0) -> None:
            rung.append(target)

    default = LoopbackTransport()
    sip = FakeSip()
    service = Service(default)
    service.set_links({"loopback": default, "sip": sip})
    server = await run_server(service, 18812)
    try:
        await asyncio.to_thread(
            post, 18812, "/api/v1/call",
            {"reason": "can you review it?", "to": "sip:milos@sip.linphone.org",
             "callee": "Milos", "wait": False},
        )
        assert len(rung) == 1, "the sip link was not the one rung"
        assert rung[0].address == "sip:milos@sip.linphone.org"
        assert rung[0].callee == "Milos"
    finally:
        await server.close()


async def test_a_call_with_no_address_is_still_addressed_to_him():
    from hotline_call.ring.loopback import LoopbackTransport

    rung: list[object] = []

    class Recording(LoopbackTransport):
        async def ring(self, target, *, timeout: float = 45.0) -> None:
            rung.append(target)

    inner = Recording()
    service = Service(inner)
    server = await run_server(service, 18813)
    try:
        await asyncio.to_thread(
            post, 18813, "/api/v1/call", {"reason": "ping", "wait": False})
        assert rung[0].address == ""
        assert rung[0].callee == "Bogdan"
    finally:
        await server.close()


# -- him calling us ----------------------------------------------------------


class FakeInboundCall:
    caller = "sip:him@sip.linphone.org"
    def __init__(self): self.over = False
    def far_end_hung_up(self): return self.over


class RecordingAgent:
    made: list = []
    def __init__(self, context="", **kw):
        self.context = context
        RecordingAgent.made.append(self)
    def start(self): pass
    def reply(self, heard): return "ok"


@pytest.fixture
def recording_agent(monkeypatch):
    from hotline_call import callagent
    RecordingAgent.made = []
    monkeypatch.setattr(callagent, "CallAgent", RecordingAgent)
    monkeypatch.setenv("HOTLINE_IOS_INBOUND_ENGINE", "classic")
    return RecordingAgent


async def test_his_call_is_told_he_called_not_that_an_agent_rang_him(recording_agent):
    service = Service(LoopbackTransport(), transcriber=FakeTranscriber(),
                      speaker=FakeSpeaker(), fillers=FakeFillers())
    leg = await service._inbound_leg(FakeInboundCall())
    context = recording_agent.made[0].context
    assert "Bogdan called YOU" in context
    assert "rang him to ask" not in context
    # The greeting clip is the whole opening: there is no question to read out.
    assert leg.handler.opening == "greet"
    assert leg.handler.greeting == ""
    assert service._calls_holding_models == 1
    assert len(service.calls) == 1
    conversation = next(iter(service.calls))

    leg.abandon("cancelled before answer")
    leg.abandon("twice must not release twice")
    await asyncio.sleep(0.05)
    assert service._calls_holding_models == 0
    assert service.calls[conversation].closed


async def test_his_call_is_busy_while_personaplex_holds_the_gpu(recording_agent):
    from hotline_call.ring.inbound import Busy

    service = Service(LoopbackTransport(), transcriber=FakeTranscriber(),
                      speaker=FakeSpeaker())
    async with service._gpu_lock:
        with pytest.raises(Busy):
            await service._inbound_leg(FakeInboundCall())


async def test_a_daemon_that_cannot_talk_does_not_answer_him(recording_agent):
    service = Service(LoopbackTransport())
    assert await service._inbound_leg(FakeInboundCall()) is None
    assert service.calls == {}


async def test_the_sip_thread_gets_its_handler_from_the_loop(recording_agent):
    service = Service(LoopbackTransport(), transcriber=FakeTranscriber(),
                      speaker=FakeSpeaker())
    service.loop = asyncio.get_running_loop()
    leg = await asyncio.get_running_loop().run_in_executor(
        None, service.inbound_call, FakeInboundCall())
    assert callable(leg) and hasattr(leg, "abandon")
    leg.abandon("test over")


def test_inbound_switched_off_starts_nothing(monkeypatch):
    from hotline_call.daemon import start_inbound

    monkeypatch.setenv("HOTLINE_IOS_INBOUND", "0")
    service = Service(LoopbackTransport())
    start_inbound(service)
    assert service.inbound is None


def test_inbound_that_cannot_start_is_a_degradation_not_a_crash(monkeypatch):
    from hotline_call.daemon import start_inbound

    monkeypatch.setenv("HOTLINE_IOS_INBOUND", "1")
    for name in ("SIP_USER", "SIP_PASSWORD", "SIP_PEER"):
        monkeypatch.delenv(name, raising=False)
    service = Service(LoopbackTransport())
    start_inbound(service)
    assert service.inbound is None
    assert any("inbound calls unavailable" in note for note in service.degradations)


# -- the call's copy in the iPhone app -----------------------------------------
#
# Before the split one process rang him and wrote the app's conversation. Now
# the call lives here and the app's store lives in hotline-iosd, so these pin
# the seam: every line goes to the mirror in order, the question is there
# before the phone rings, and his typed answer -- forwarded by hotline-iosd --
# still unblocks the caller.


class FakeMirror:
    def __init__(self):
        self.sent: list[tuple] = []

    def open(self, conversation, agent, kind):
        self.sent.append(("open", conversation, agent, kind))

    def append(self, conversation, kind, text):
        self.sent.append(("append", conversation, kind, text))

    def close(self, conversation):
        self.sent.append(("close", conversation))

    def status(self):
        return {"url": "fake", "posted": len(self.sent), "failed": 0, "last_error": None}


async def test_the_question_reaches_the_app_before_it_even_rings():
    # He must never answer a phone to silence. Whether he opens the app during
    # the ring or an hour later, the question is already there.
    mirror = FakeMirror()
    seen_at_ring: list[tuple] = []

    class Watching(LoopbackTransport):
        async def ring(self, target, *, timeout: float = 45.0) -> None:
            seen_at_ring.extend(mirror.sent)

    service = Service(Watching(), mirror=mirror)
    body = await service.place({"reason": "may I spend money on a UI agency",
                                "source": "the ios build", "agent": "hotline-80",
                                "context": "the build is green", "wait": False})
    conversation = body["conversation"]
    assert seen_at_ring[0] == ("open", conversation, "hotline-80", "ring")
    assert seen_at_ring[1] == ("append", conversation, "claude",
                               "the ios build: may I spend money on a UI agency")
    assert seen_at_ring[2] == ("append", conversation, "summary", "the build is green")


async def test_ringing_out_closes_the_call_in_the_app_too():
    mirror = FakeMirror()
    service = Service(LoopbackTransport(answer=False), mirror=mirror)
    body = await service.place({"reason": "ping", "ring_timeout": 0.05})
    assert body["state"] == "unanswered"
    conversation = body["conversation"]
    assert ("append", conversation, "state", "unanswered") in mirror.sent
    assert mirror.sent[-1] == ("close", conversation)
    # A late answer forwarded by hotline-iosd is accepted, not a 404: he can
    # still open the app and answer, as before the split.
    assert service.typed_reply(conversation, "yes")["known"] is True


async def test_his_reply_in_the_app_comes_back_on_hotline_calls_stdout():
    """The contract that must survive a change of doorbell -- and of process.

    He answers by typing in the app; hotline-iosd forwards it here over HTTP.
    A blocked agent still gets his words back.
    """
    mirror = FakeMirror()
    service = Service(LoopbackTransport(), mirror=mirror)
    server = await run_server(service, 18806)
    try:
        task = asyncio.create_task(asyncio.to_thread(
            post, 18806, "/api/v1/call", {"reason": "may I spend money", "timeout": 20}, 40))
        for _ in range(200):
            if service.calls:
                break
            await asyncio.sleep(0.01)
        conversation = next(iter(service.calls))
        forwarded = await asyncio.to_thread(
            post, 18806, "/api/v1/call/reply", {"conversation": conversation,
                                                "text": "yes, go ahead"})
        assert forwarded == {"conversation": conversation, "known": True, "waiting": True}
        body = await asyncio.wait_for(task, timeout=30)
        assert body["state"] == "answered"
        assert body["reply"] == "yes, go ahead"
        # Not echoed back into the app: that is where he typed it.
        assert ("append", conversation, "you", "yes, go ahead") not in mirror.sent
    finally:
        await server.close()


async def test_a_reply_to_a_call_this_process_does_not_hold_is_not_an_error():
    service = Service(LoopbackTransport())
    server = await run_server(service, 18807)
    try:
        body = await asyncio.to_thread(
            post, 18807, "/api/v1/call/reply", {"conversation": "nope", "text": "hi"})
        assert body == {"conversation": "nope", "known": False}
    finally:
        await server.close()


async def test_dismissing_it_in_the_app_ends_the_wait_and_is_idempotent():
    service = Service(LoopbackTransport())
    server = await run_server(service, 18808)
    try:
        task = asyncio.create_task(asyncio.to_thread(
            post, 18808, "/api/v1/call", {"reason": "ping", "timeout": 20}, 40))
        for _ in range(200):
            if service.calls:
                break
            await asyncio.sleep(0.01)
        conversation = next(iter(service.calls))
        for _ in range(2):
            ended = await asyncio.to_thread(
                post, 18808, "/api/v1/call/hangup", {"call_id": conversation})
            assert ended["ended"] is True
        body = await asyncio.wait_for(task, timeout=30)
        assert body["state"] == "unanswered"
        assert body["waited_seconds"] < 10
    finally:
        await server.close()


def test_open_calls_ignores_closed_ones_and_reap_drops_them() -> None:
    service = Service(LoopbackTransport())
    for _ in range(3):
        service._open_conversation(None, "ring")
    a, b, c = list(service.calls)
    assert service.open_calls() == 3
    service._close_conversation(a)
    service._close_conversation(b)
    assert service.open_calls() == 1
    for call_id in (a, b, c):
        service.call_opened[call_id] -= 7200
    assert service.reap(older_than=3600) == 2
    assert set(service.calls) == {c}


async def test_the_app_mirror_posts_in_order_with_the_key():
    """AppMirror against a real HTTP server standing in for hotline-iosd."""
    from hotline.httpd import Server

    from hotline_call.appmirror import AppMirror

    got: list[tuple[str, dict, str]] = []
    fake_ios = Server(["127.0.0.1"], 18809, log=lambda m: None)

    for path in ("/api/v1/conversations/open", "/api/v1/conversations/append",
                 "/api/v1/conversations/close"):
        async def handler(request, path=path):
            got.append((path, request.json(), request.headers.get("x-hotline-key", "")))
            return 200, {"ok": True}
        fake_ios.route("POST", path)(handler)

    await fake_ios.start()
    try:
        mirror = AppMirror(url="http://127.0.0.1:18809", api_key="k")
        service = Service(LoopbackTransport(answer=False), mirror=mirror)
        body = await service.place({"reason": "ping", "ring_timeout": 0.05})
        assert await asyncio.to_thread(mirror.flush, 5.0)
        paths = [p.rsplit("/", 1)[-1] for p, _b, _k in got]
        assert paths[0] == "open" and paths[-1] == "close"
        assert all(key == "k" for _p, _b, key in got)
        assert all(b["conversation"] == body["conversation"] for _p, b, _k in got)
        assert mirror.status()["failed"] == 0
    finally:
        await fake_ios.close()


async def test_an_app_that_is_down_costs_the_app_its_copy_and_never_the_call():
    from hotline_call.appmirror import AppMirror

    mirror = AppMirror(url="http://127.0.0.1:9", api_key="")   # nothing listens on 9
    inner = HoldsCalls()
    service = Service(ConfirmedRing(inner, confirm_within=1.0), mirror=mirror,
                      transcriber=FakeTranscriber(), speaker=FakeSpeaker(),
                      fillers=FakeFillers())

    async def ring(target, timeout=45.0):
        inner.ringing.set()
        service._append(next(iter(service.calls)), "you", "da")

    inner.ring = ring
    body = await asyncio.wait_for(service.place({"reason": "ok?", "timeout": 5.0}), 15)
    assert body["state"] == "answered" and body["reply"] == "da"
    assert await asyncio.to_thread(mirror.flush, 10.0)
    status = mirror.status()
    # The open failed once; the appends behind it are skipped, not each retried.
    assert status["failed"] == 1 and "open" in status["last_error"]
