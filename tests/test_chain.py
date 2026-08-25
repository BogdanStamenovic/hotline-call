"""Falling through from one way of reaching him to the next.

The composition both reviewers arrived at: his own app when its socket is alive,
the stock SIP client when it is not, the Discord mention when neither works.
These tests pin the two decisions that are easy to get wrong -- that a decline
stops the chain, and that a chain over unconfirmable transports is refused
rather than silently trusted.
"""

import pytest

from hotline_ios.ring.base import (
    AudioFormat,
    CallDeclined,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
)
from hotline_ios.ring.chain import RingChain
from hotline_ios.ring.loopback import LoopbackTransport
from hotline_ios.ring.watch import ConfirmedRing

FMT = AudioFormat(rate=16_000, channels=1, frame_ms=20)
WHO = CallTarget(device="phone", agent="hotline-80")


def named(transport, name):
    transport.name = name
    return transport


async def test_the_first_working_transport_is_used():
    chain = RingChain([named(LoopbackTransport(FMT), "own-app"),
                       named(LoopbackTransport(FMT), "sip")])
    await chain.ring(WHO)
    assert chain.used == "own-app"


async def test_it_falls_through_to_the_next_and_says_so():
    seen: list[tuple[str, str]] = []
    dead = named(LoopbackTransport(FMT, reachable=False), "own-app")
    alive = named(LoopbackTransport(FMT), "sip")
    chain = RingChain([dead, alive], on_fallthrough=lambda n, w: seen.append((n, w)))
    await chain.ring(WHO)
    assert chain.used == "sip"
    # Loudly. A degradation nobody hears about is the failure being designed out.
    assert seen and seen[0][0] == "own-app"


async def test_a_decline_stops_the_chain_dead():
    # He saw it and said not now. Ringing him again by another route one second
    # later is exactly what he was declining.
    second = named(LoopbackTransport(FMT), "sip")
    chain = RingChain([named(LoopbackTransport(FMT, decline=True), "own-app"), second])
    with pytest.raises(CallDeclined):
        await chain.ring(WHO)
    assert second.rang == []


async def test_ringing_out_does_not_fall_through_by_default():
    # A phone that rang for 45s and was ignored has delivered the message.
    second = named(LoopbackTransport(FMT), "sip")
    chain = RingChain([named(LoopbackTransport(FMT, answer=False), "own-app"), second])
    with pytest.raises(CallUnanswered):
        await chain.ring(WHO, timeout=0.05)
    assert second.rang == []


async def test_but_it_can_be_asked_to():
    second = named(LoopbackTransport(FMT), "sip")
    chain = RingChain([named(LoopbackTransport(FMT, answer=False), "own-app"), second],
                      fall_through_on_unanswered=True)
    await chain.ring(WHO, timeout=0.05)
    assert chain.used == "sip"


async def test_everything_failing_is_one_error_naming_every_attempt():
    chain = RingChain([named(LoopbackTransport(FMT, reachable=False), "own-app"),
                       named(LoopbackTransport(FMT, reachable=False), "sip")])
    with pytest.raises(CallUnreachable) as exc:
        await chain.ring(WHO)
    assert "own-app" in str(exc.value) and "sip" in str(exc.value)


async def test_a_transport_that_raises_something_unexpected_is_survived():
    class Broken:
        name = "broken"
        rings_when_closed = True
        async def start(self): ...
        async def stop(self): ...
        async def ring(self, target, *, timeout=45.0):
            raise ZeroDivisionError("a bug in a transport")

    chain = RingChain([Broken(), named(LoopbackTransport(FMT), "sip")])
    await chain.ring(WHO)
    assert chain.used == "sip"


async def test_an_unconfirmable_chain_refuses_rather_than_trusting_silence():
    # A fall-through chain over transports that cannot report success does not
    # degrade -- it stops at the first one that fails to raise, and the call
    # vanishes. Wrapping each link is what makes the chain meaningful.
    chain = RingChain([
        ConfirmedRing(named(LoopbackTransport(FMT, confirms=False, answer=False), "own-app"),
                      confirm_within=0.1),
        ConfirmedRing(named(LoopbackTransport(FMT), "sip"), confirm_within=1.0),
    ])
    stream = await chain.ring(WHO)
    assert stream is not None
    assert chain.used.startswith("sip")


async def test_rings_when_closed_is_true_if_any_link_can():
    weak = named(LoopbackTransport(FMT), "mention")
    weak.rings_when_closed = False
    strong = named(LoopbackTransport(FMT), "sip")
    assert RingChain([weak, strong]).rings_when_closed is True
    assert RingChain([weak]).rings_when_closed is False


async def test_an_empty_chain_is_refused_at_construction():
    # A chain with nothing in it can never ring, and finding that out at ring
    # time means finding out when he needed it.
    with pytest.raises(ValueError):
        RingChain([])
