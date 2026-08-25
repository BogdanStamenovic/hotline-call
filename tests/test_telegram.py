"""The Telegram doorbell, against a stand-in MTProto client.

What is NOT tested here, and must not be claimed: that any of this makes his
phone ring. That needs an api_id, an api_hash, a second account with its own
phone number, and his handset. These cover the logic around the call -- that
being unconfigured fails early and loudly, that the request being accepted is
what counts as evidence it rang, and that declined, ignored and undeliverable
stay three different answers.
"""

import asyncio

import pytest

from hotline_ios.ring.base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable
from hotline_ios.ring.telegram import TelegramTransport

WHO = CallTarget(device="phone", reason="the build is stuck", caller_id="the ios build")


class FakeCall:
    id = 4242
    access_hash = 99


class FakeResult:
    phone_call = FakeCall()


class FakeClient:
    """Just enough MTProto to exercise the transport."""

    def __init__(self, *, fail: str | None = None):
        self.fail = fail
        self.requests: list[object] = []
        self.discarded = False

    async def get_input_entity(self, peer):
        if self.fail == "unknown-peer":
            raise ValueError("no such user")
        return f"entity:{peer}"

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(request)
        if name == "RequestCallRequest":
            if self.fail == "privacy":
                raise RuntimeError("USER_PRIVACY_RESTRICTED")
            return FakeResult()
        if name == "DiscardCallRequest":
            self.discarded = True
        return None

    def add_event_handler(self, handler, event):
        self.handler = handler
        return handler

    def remove_event_handler(self, handler):
        pass

    async def disconnect(self):
        pass


def transport(client, **kw):
    return TelegramTransport(api_id=1, api_hash="x", peer="bogdan", client=client, **kw)


async def test_being_unconfigured_fails_at_startup_not_at_ring_time():
    # A doorbell that only reveals it was never configured at the moment someone
    # needs it is worse than one that says so immediately.
    bare = TelegramTransport(api_id=0, api_hash="", peer="")
    with pytest.raises(CallUnreachable) as exc:
        await bare.start()
    assert "not configured" in str(exc.value)


async def test_the_request_being_accepted_is_the_evidence_it_rang():
    client = FakeClient()
    t = transport(client)
    task = asyncio.create_task(t.ring(WHO, timeout=0.2))
    # ConfirmedRing waits on exactly this.
    for _ in range(100):
        if t.ringing.is_set():
            break
        await asyncio.sleep(0.01)
    assert t.ringing.is_set()
    with pytest.raises(CallUnanswered):
        await task
    assert [type(r).__name__ for r in client.requests][0] == "RequestCallRequest"


async def test_it_always_discards_so_the_phone_stops_buzzing():
    # He is already reading the question in the app; leaving the call ringing
    # would keep buzzing him for no reason.
    client = FakeClient()
    t = transport(client)
    with pytest.raises(CallUnanswered):
        await t.ring(WHO, timeout=0.1)
    assert client.discarded


async def test_an_unknown_peer_is_unreachable_not_a_crash():
    t = transport(FakeClient(fail="unknown-peer"))
    with pytest.raises(CallUnreachable) as exc:
        await t.ring(WHO, timeout=0.1)
    assert "does not know" in str(exc.value)


async def test_privacy_restrictions_are_unreachable_so_the_chain_falls_through():
    t = transport(FakeClient(fail="privacy"))
    with pytest.raises(CallUnreachable):
        await t.ring(WHO, timeout=0.1)


async def test_it_composes_with_the_chain_and_falls_through_when_telegram_is_down():
    from hotline_ios.ring.chain import RingChain
    from hotline_ios.ring.loopback import LoopbackTransport
    from hotline_ios.ring.watch import ConfirmedRing

    sip = LoopbackTransport()
    sip.name = "sip"
    chain = RingChain([
        ConfirmedRing(transport(FakeClient(fail="privacy")), confirm_within=0.5),
        ConfirmedRing(sip, confirm_within=0.5),
    ])
    await chain.ring(WHO, timeout=0.5)
    # Exactly what "we will do both" has to mean at runtime.
    assert chain.used.startswith("sip")
