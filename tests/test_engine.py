"""Which engine holds a call.

A call to him takes the daemon's default engine (Haiku since 2026-11-03); a
registry guest is called on the classic Serbian path; "classic" can always be
asked for. An engine name the daemon does not know -- "personaplex", removed on
2026-11-03, or a typo -- costs the call its engine choice, never the call itself.
"""
from __future__ import annotations

import asyncio

import pytest

from hotline_call import daemon
from hotline_call.daemon import Service
from hotline_call.ring.base import CallUnreachable
from hotline_call.ring.loopback import LoopbackTransport


class Doorbell(LoopbackTransport):
    def __init__(self, log: list[str], name: str = "loopback", fail: bool = False) -> None:
        super().__init__()
        self.log, self.name, self.fail = log, name, fail

    async def ring(self, target, *, timeout: float = 45.0) -> None:
        self.log.append(f"ring:{target.address or 'him'}")
        if self.fail:
            raise CallUnreachable("the phone never confirmed it was ringing")


def make(log: list[str], default_engine: str = "haiku", fail: bool = False) -> Service:
    bell = Doorbell(log, fail=fail)
    service = Service(bell, default_engine=default_engine)
    sip = Doorbell(log, name="sip", fail=fail)
    service.set_links({"loopback": bell, "sip": sip})
    return service


async def test_a_call_to_him_takes_the_default_engine():
    log: list[str] = []
    body = await make(log).place({"reason": "status", "wait": False})
    assert log == ["ring:him"]
    assert body["engine"] == "haiku" and body["note"] == ""


async def test_a_guest_call_stays_on_the_serbian_path():
    log: list[str] = []
    body = await make(log).place({"reason": "mozes li da pogledas?", "to": "sip:milos@sip.linphone.org",
                                  "callee": "Milos", "wait": False})
    assert log == ["ring:sip:milos@sip.linphone.org"]
    assert body["engine"] == "classic"


async def test_classic_can_still_be_asked_for():
    log: list[str] = []
    body = await make(log).place({"reason": "status", "engine": "classic", "wait": False})
    assert log == ["ring:him"]
    assert body["engine"] == "classic"


async def test_with_no_default_a_service_is_classic():
    # What every other test in this suite relies on: Service() alone is classic.
    log: list[str] = []
    body = await make(log, default_engine="").place({"reason": "status", "wait": False})
    assert log == ["ring:him"] and body["engine"] == "classic"


@pytest.mark.parametrize("engine", ["", "auto"])
async def test_auto_and_empty_mean_the_default(engine):
    log: list[str] = []
    body = await make(log).place({"reason": "status", "engine": engine, "wait": False})
    assert body["engine"] == "haiku"


@pytest.mark.parametrize("engine", ["personaplex", "PersonaPlex", "nemotron"])
async def test_an_unknown_engine_rings_on_the_default_and_says_so(engine):
    log: list[str] = []
    body = await make(log).place({"reason": "status", "engine": engine, "wait": False})
    assert log == ["ring:him"]
    assert body["state"] == "ringing", "an engine name must never cost the call"
    assert body["engine"] == "haiku"
    assert f"unknown engine {engine.lower()!r}" in body["note"]


async def test_an_unknown_engine_on_a_guest_call_is_classic():
    log: list[str] = []
    body = await make(log).place({"reason": "x", "engine": "personaplex",
                                  "to": "sip:milos@sip.linphone.org", "wait": False})
    assert body["engine"] == "classic" and "unknown engine" in body["note"]


async def test_an_unknown_engine_note_survives_a_failed_ring():
    log: list[str] = []
    body = await make(log, fail=True).place({"reason": "x", "engine": "personaplex", "wait": False})
    assert body["state"] == "unreachable" and "unknown engine" in body["note"]


@pytest.mark.parametrize(("raw", "want"), [
    (None, "haiku"), ("haiku", "haiku"), ("classic", ""), ("", ""),
    ("personaplex", "haiku"), (" Haiku ", "haiku"),
])
def test_the_default_engine_from_the_environment(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("HOTLINE_IOS_DEFAULT_ENGINE", raising=False)
    else:
        monkeypatch.setenv("HOTLINE_IOS_DEFAULT_ENGINE", raw)
    assert daemon.default_engine() == want


# ---- calls do not queue behind each other ---------------------------------------------
class SlowDoorbell(LoopbackTransport):
    """Rings for `hold` seconds, as if the call were in progress, logging when."""

    def __init__(self, log: list[str], hold: float) -> None:
        super().__init__()
        self.log, self.hold = log, hold

    async def ring(self, target, *, timeout: float = 45.0) -> None:
        self.log.append(f"ring-start:{target.reason}")
        await asyncio.sleep(self.hold)
        self.log.append(f"ring-end:{target.reason}")


@pytest.mark.parametrize("engine", ["classic", "haiku"])
async def test_calls_overlap_each_other(engine):
    log: list[str] = []
    service = Service(SlowDoorbell(log, 0.3), default_engine="haiku")
    a = asyncio.ensure_future(service.place({"reason": "a", "engine": engine, "wait": False}))
    await asyncio.sleep(0.05)
    await service.place({"reason": "b", "engine": engine, "wait": False})
    await a
    assert log.index("ring-start:b") < log.index("ring-end:a"), "a call must not queue behind another"
