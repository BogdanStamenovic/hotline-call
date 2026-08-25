"""Outcome B's transport: ring his own app, nothing leaving the tailnet.

The tests that matter are the four silent-death paths -- force-quit, reboot,
expired certificate, iOS reclaiming the process. All four look identical from
here: nothing is polling. So the property being pinned is that an absent app is
detected BEFORE the ring rather than after a 45 second ring-out, because that is
what lets the chain reach Linphone while he is still near the phone.
"""

import asyncio

import pytest

from hotline_ios.ring.base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable
from hotline_ios.ring.local import ACK_WINDOW, LocalTransport
from hotline_ios.ring.watch import ConfirmedRing

WHO = CallTarget(device="phone", agent="hotline-80", reason="the build is stuck",
                 caller_id="the ios build")


async def test_it_never_claims_to_ring_a_closed_app():
    # Not configurable on purpose: it cannot wake a process that is not running,
    # and that is a fact rather than a setting.
    assert LocalTransport().rings_when_closed is False


async def test_an_app_that_is_not_polling_fails_fast_rather_than_ringing_out():
    transport = LocalTransport()
    with pytest.raises(CallUnreachable) as exc:
        await transport.ring(WHO, timeout=45)
    assert "not running" in str(exc.value)


async def test_a_full_ring_answer_and_media():
    transport = LocalTransport(rtp_host="127.0.0.1")

    async def app():
        # The app's long-poll IS the presence signal.
        ring = await transport.poll(timeout=5)
        assert ring is not None
        assert ring["from"] == "the ios build"
        assert ring["reason"] == "the build is stuck"
        transport.acknowledge(ring["call_id"])       # put it on screen
        await asyncio.sleep(0.05)
        transport.settle(ring["call_id"], answered=True)

    await transport.poll(timeout=0.01)               # register presence
    task = asyncio.create_task(app())
    stream = await transport.ring(WHO, timeout=5)
    await task
    assert stream is not None
    await transport.stop()


async def test_an_app_that_takes_the_ring_but_never_acks_is_unreachable():
    # The case that matters: the process is alive enough to poll but iOS refused
    # to show the call -- Do Not Disturb, already in a call, or the system just
    # said no. Presence is not proof, and the acknowledgement is.
    transport = LocalTransport()
    await transport.poll(timeout=0.01)

    async def app():
        await transport.poll(timeout=5)              # takes it, never acks

    task = asyncio.create_task(app())
    import hotline_ios.ring.local as local
    original = local.ACK_WINDOW
    local.ACK_WINDOW = 0.2
    try:
        with pytest.raises(CallUnreachable) as exc:
            await transport.ring(WHO, timeout=5)
        assert "did not acknowledge" in str(exc.value)
    finally:
        local.ACK_WINDOW = original
        task.cancel()


async def test_declining_and_ringing_out_stay_distinguishable():
    transport = LocalTransport()
    await transport.poll(timeout=0.01)

    async def decline():
        ring = await transport.poll(timeout=5)
        transport.acknowledge(ring["call_id"])
        transport.settle(ring["call_id"], answered=False)

    task = asyncio.create_task(decline())
    with pytest.raises(CallDeclined):
        await transport.ring(WHO, timeout=5)
    await task

    async def ignore():
        ring = await transport.poll(timeout=5)
        transport.acknowledge(ring["call_id"])       # rings, never settled

    task = asyncio.create_task(ignore())
    with pytest.raises(CallUnanswered):
        await transport.ring(WHO, timeout=0.3)
    task.cancel()


async def test_presence_goes_stale_rather_than_latching():
    transport = LocalTransport()
    await transport.poll(timeout=0.01)
    assert transport.present
    import hotline_ios.ring.local as local
    original = local.PRESENCE_WINDOW
    local.PRESENCE_WINDOW = 0.05
    try:
        await asyncio.sleep(0.1)
        assert not transport.present
    finally:
        local.PRESENCE_WINDOW = original


async def test_it_composes_with_confirmed_ring():
    # The whole point of the ack: ConfirmedRing has something real to wait on,
    # so a dead app degrades instead of vanishing.
    transport = LocalTransport()
    ring = ConfirmedRing(transport, confirm_within=0.2)
    with pytest.raises(CallUnreachable):
        await ring.ring(WHO, timeout=5)
