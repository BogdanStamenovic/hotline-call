"""An unconfirmed ring must be an error, not a silence.

The platform fact behind this: an iOS packet tunnel provider is suspended when
the phone locks (Apple Developer Forums 756941, answered "100%, no" by an Apple
engineer). So no transport can be trusted to have rung merely because we asked
it to, and every option fails silently in its own way. These tests pin the rule
that silence degrades loudly.
"""


import pytest

from hotline_ios.ring.base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable
from hotline_ios.ring.loopback import LoopbackTransport
from hotline_ios.ring.watch import ConfirmedRing

WHO = CallTarget(device="phone", agent="hotline-80")


async def test_a_confirmed_ring_passes_straight_through():
    inner = LoopbackTransport()
    ring = ConfirmedRing(inner, confirm_within=1.0)
    await ring.ring(WHO)          # returning at all is the success
    assert inner.rang == [WHO]


async def test_a_transport_that_never_confirms_is_unreachable_not_unanswered():
    # The distinction that matters: "it rang and he ignored it" is an answer,
    # "it never rang" is a broken doorbell, and they need opposite responses.
    seen: list[str] = []
    inner = LoopbackTransport(confirms=False, answer=False)
    ring = ConfirmedRing(inner, confirm_within=0.2, on_degrade=seen.append)
    with pytest.raises(CallUnreachable):
        await ring.ring(WHO)
    assert seen and "no ring confirmation" in seen[0]


async def test_a_transport_with_no_confirmation_channel_fails_closed():
    class Mute:
        name = "mute"
        rings_when_closed = True

        async def start(self): ...
        async def stop(self): ...

        async def ring(self, target, *, timeout=45.0):
            raise AssertionError("must not be trusted")

    seen: list[str] = []
    ring = ConfirmedRing(Mute(), confirm_within=0.2, on_degrade=seen.append)
    with pytest.raises(CallUnreachable):
        await ring.ring(WHO)
    assert seen and "cannot confirm" in seen[0]


async def test_a_slow_but_real_confirmation_is_accepted():
    # The path to his phone is DERP-relayed and measured at 92-623ms, and iOS
    # starts the VPN from an on-demand rule with a documented 5-10s wait. Slow
    # is normal here; absent is not.
    inner = LoopbackTransport(confirm_delay=0.15)
    ring = ConfirmedRing(inner, confirm_within=2.0)
    await ring.ring(WHO)
    assert inner.rang == [WHO]


async def test_ringing_out_after_a_real_ring_is_still_unanswered():
    inner = LoopbackTransport(answer=False, confirms=True)
    ring = ConfirmedRing(inner, confirm_within=1.0)
    with pytest.raises(CallUnanswered):
        await ring.ring(WHO, timeout=0.2)


async def test_an_instant_decline_beats_the_confirmation_race():
    # A decline can land before confirmation does; the attempt is the answer and
    # must not be second-guessed into "unreachable".
    inner = LoopbackTransport(decline=True, confirms=False)
    ring = ConfirmedRing(inner, confirm_within=2.0)
    with pytest.raises(CallDeclined):
        await ring.ring(WHO)


async def test_a_broken_degrade_notifier_does_not_mask_the_real_error():
    def explode(why):
        raise ValueError("pager is down too")

    ring = ConfirmedRing(LoopbackTransport(confirms=False, answer=False),
                         confirm_within=0.2, on_degrade=explode)
    with pytest.raises(CallUnreachable):
        await ring.ring(WHO)
