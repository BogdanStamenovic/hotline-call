"""Which engine holds a call, and that the GPU is always handed back.

Bogdan, 2026-10-16: "Hotline-call uses stock personaplex from now on." For calls to
him only: a registry guest is called in Serbian and PersonaPlex speaks English. And
three things he did not have to ask for: cvoiced is stopped for a PersonaPlex call
and ALWAYS restarted afterwards (every other call and page speaks through it); a
PersonaPlex that cannot start costs the call its engine, never the call itself; and
the duplex transcript is not returned until it has all been delivered.

No GPU, no systemctl: `_prepare_duplex` and the handoff are faked.
"""
from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest

from hotline_ios.daemon import Service
from hotline_ios.events import EventLog
from hotline_ios.gpu_handoff import GpuHandoff
from hotline_ios.ring.base import CallUnreachable
from hotline_ios.ring.loopback import LoopbackTransport


class FakeHandoff:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def acquire(self) -> list[str]:
        self.log.append("acquire")
        return []

    def release(self) -> str:
        self.log.append("release")
        return "restarted cvoiced.service, healthy"


class Doorbell(LoopbackTransport):
    def __init__(self, log: list[str], name: str = "loopback", fail: bool = False) -> None:
        super().__init__()
        self.log, self.name, self.fail = log, name, fail

    async def ring(self, target, *, timeout: float = 45.0) -> None:
        self.log.append(f"ring:{target.address or 'him'}")
        if self.fail:
            raise CallUnreachable("the phone never confirmed it was ringing")


def make(log: list[str], default_engine: str = "personaplex", why: str = "", fail: bool = False):
    bell = Doorbell(log, fail=fail)
    service = Service(bell, pool=None, default_engine=default_engine,
                      gpu_handoff=lambda: FakeHandoff(log))
    sip = Doorbell(log, name="sip", fail=fail)
    service.set_links({"loopback": bell, "sip": sip})

    def prepare(doorbell, target, conversation, loop):
        log.append("prepare")
        return ([], [], why) if why else ([], [], "")

    service._prepare_duplex = prepare
    return service


async def test_a_call_to_him_is_personaplex_by_default_and_hands_the_gpu_back():
    log: list[str] = []
    body = await make(log).place({"reason": "status", "wait": False})
    assert log == ["acquire", "prepare", "ring:him", "release"]
    assert body["engine"] == "personaplex" and body["note"] == ""


async def test_a_guest_call_stays_on_the_serbian_path():
    log: list[str] = []
    body = await make(log).place({"reason": "mozes li da pogledas?", "to": "sip:milos@sip.linphone.org",
                                  "callee": "Milos", "wait": False})
    assert log == ["ring:sip:milos@sip.linphone.org"], "PersonaPlex speaks English; a guest is called in Serbian"
    assert body["engine"] == "classic"


async def test_classic_can_still_be_asked_for_and_never_touches_the_gpu():
    log: list[str] = []
    body = await make(log).place({"reason": "status", "engine": "classic", "wait": False})
    assert log == ["ring:him"]
    assert body["engine"] == "classic"


async def test_a_personaplex_that_cannot_start_falls_back_to_serbian_and_says_so():
    log: list[str] = []
    body = await make(log, why="CUDA out of memory").place({"reason": "status", "wait": False})
    # cvoiced back BEFORE the ring: the Serbian path speaks through it
    assert log == ["acquire", "prepare", "release", "ring:him"]
    assert body["state"] == "ringing", "a model that will not start must not cost the call"
    assert body["engine"] == "classic"
    assert "PersonaPlex did not start (CUDA out of memory)" in body["note"]


async def test_the_gpu_is_handed_back_even_when_the_ring_fails():
    log: list[str] = []
    body = await make(log, fail=True).place({"reason": "status", "wait": False})
    assert body["state"] == "unreachable"
    assert log[-1] == "release"


async def test_with_no_default_a_service_never_touches_the_gpu():
    # What every other test in this suite relies on: Service() alone is classic.
    log: list[str] = []
    body = await make(log, default_engine="").place({"reason": "status", "wait": False})
    assert log == ["ring:him"] and body["engine"] == "classic"


# ---- GpuHandoff -----------------------------------------------------------------------
def systemctl(calls: list[tuple[str, ...]], active: bool = True):
    def run(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        out = ("active" if active else "inactive") if args[0] == "is-active" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    return run


def test_handoff_stops_cvoiced_and_restarts_it_only_if_it_was_running():
    calls: list[tuple[str, ...]] = []
    h = GpuHandoff(health=lambda: {"ok": True}, run=systemctl(calls), ollama="http://127.0.0.1:9")
    h.acquire()
    assert ("stop", "cvoiced.service") in calls
    assert h.release() == "restarted cvoiced.service, healthy"
    assert ("start", "cvoiced.service") in calls
    calls.clear()
    idle = GpuHandoff(run=systemctl(calls, active=False), ollama="http://127.0.0.1:9")
    idle.acquire()
    assert idle.release() == "" and ("start", "cvoiced.service") not in calls


def test_handoff_reports_a_cvoiced_that_does_not_come_back_healthy():
    def down():
        raise ConnectionError("refused")

    h = GpuHandoff(health=down, run=systemctl([]), ollama="http://127.0.0.1:9", wait_s=1.5)
    h.acquire()
    assert "NOT healthy" in h.release()


def test_handoff_with_no_ollama_running_is_not_an_error():
    h = GpuHandoff(run=systemctl([], active=False), ollama="http://127.0.0.1:9")
    assert h.acquire() == []


# ---- the duplex transcript ------------------------------------------------------------
def test_a_duplex_transcript_is_returned_whole_not_cut_at_the_hangup():
    """`ended` is set when the line drops, BEFORE the post-call transcription
    appends the turns; stopping on it could hand back the first line only."""
    service = Service(LoopbackTransport(), pool=None)
    events = EventLog()

    class Spoken:
        turns = 3
        ended = "he hung up"            # already set: the old code stopped here
        transcribed = threading.Event()

    async def drive() -> list[dict[str, str]]:
        async def save() -> None:
            events.append("you", "hi")
            await asyncio.sleep(0.3)
            events.append("claude", "hello Bogdan")
            events.append("you", "bye")
            Spoken.transcribed.set()

        task = asyncio.ensure_future(save())
        got = await service._await_transcript(events, 5.0, 0, Spoken())
        await task
        return got

    turns = asyncio.run(drive())
    assert [t["text"] for t in turns] == ["hi", "hello Bogdan", "bye"]


@pytest.mark.parametrize("engine", ["", "auto"])
async def test_auto_and_empty_mean_the_default(engine):
    log: list[str] = []
    body = await make(log).place({"reason": "status", "engine": engine, "wait": False})
    assert body["engine"] == "personaplex"
