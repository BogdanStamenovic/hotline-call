"""Outcome B: ring his own app, with nothing leaving the tailnet.

This is the only design in which "everything over Tailscale" is literally true,
doorbell included. There is no push, no APNs, and no third party: the app holds
a connection to this server, the server writes a ring down it, and the app calls
`CXProvider.reportNewIncomingCall` itself. CallKit takes no entitlement, which
is the whole reason it is possible on a free Apple ID.

**What it cannot do, stated first because it is the thing that decides whether
it should ever be first in the chain.** A push wakes a dead process. This
cannot. If the app has been force-quit, if the phone has rebooted, if the
signing certificate has expired, or if iOS reclaimed the process, there is
nothing on the other end and nothing rings. Worse, all four of those are
*silent* -- the call simply never arrives.

So this transport is built to **prove** it rang rather than assume it, and that
is not decoration: it is what makes `RingChain` able to fall through to
something that still works. Presence is a fact about the last few seconds, not a
flag someone set once:

  * the app holds a long-poll open; while one is open, the device is present
  * a ring is handed to that long-poll and the app must **acknowledge** it
  * no acknowledgement inside the window means not rung, which `ConfirmedRing`
    turns into `CallUnreachable`, which the chain turns into Linphone or a page

The generous window is deliberate. `tailscale ping` to his phone never
establishes a direct connection -- every packet relays through a DERP server at
92-623 ms -- and iOS starts the VPN from an on-demand rule, which a Tailscale
contributor measured at 5-10 s. Slow is normal here; absent is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

from ..media.rtp import PCMU_FORMAT, RtpStream
from .base import (
    AudioFormat,
    CallDeclined,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
    MediaStream,
)

log = logging.getLogger("hotline-ios.ring.local")

PRESENCE_WINDOW = 45.0
"""How stale a long-poll may be before the device counts as gone. Longer than
the app's poll interval so an ordinary round trip never looks like an absence."""

ACK_WINDOW = 10.0
"""How long the app has to say it put a call on screen."""


class Pending:
    """One ring waiting for the app to pick it up and acknowledge it."""

    def __init__(self, call_id: str, target: CallTarget) -> None:
        self.call_id = call_id
        self.target = target
        self.delivered = asyncio.Event()
        self.acked = asyncio.Event()
        self.answered: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

    def as_json(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "from": self.target.caller_id,
            "reason": self.target.reason,
            "agent": self.target.agent or "",
        }


class LocalTransport:
    """Ring his own app over the tailnet. No push anywhere in the path."""

    name = "own-app"

    def __init__(self, fmt: AudioFormat | None = None, *, rtp_host: str = "100.72.2.62") -> None:
        self.format = fmt or PCMU_FORMAT
        self.rtp_host = rtp_host
        self.ringing = asyncio.Event()
        self.last_seen: float = 0.0
        self.pending: dict[str, Pending] = {}
        self._waiting: asyncio.Event = asyncio.Event()
        self._inbound: asyncio.Queue[tuple[CallTarget, MediaStream]] = asyncio.Queue()
        self._streams: list[RtpStream] = []

    @property
    def rings_when_closed(self) -> bool:
        """Honest answer: no.

        A property rather than a class attribute so nobody is tempted to set it
        True after seeing it work once with the app in the foreground. It cannot
        wake a process that is not running, and that is not a configuration.
        """
        return False

    @property
    def present(self) -> bool:
        return (time.monotonic() - self.last_seen) < PRESENCE_WINDOW

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        for stream in self._streams:
            await stream.close()

    # ---- what the app calls ---------------------------------------------

    async def poll(self, timeout: float = 25.0) -> dict[str, object] | None:
        """The app's long-poll. Returns a ring to show, or None.

        Holding this open IS the presence signal, which is why presence cannot
        drift out of sync with reality: there is only one fact, not a flag and a
        heartbeat that can disagree.
        """
        self.last_seen = time.monotonic()
        for pending in self.pending.values():
            if not pending.delivered.is_set():
                pending.delivered.set()
                return pending.as_json()
        try:
            await asyncio.wait_for(self._waiting.wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            self.last_seen = time.monotonic()
            return None
        self.last_seen = time.monotonic()
        self._waiting.clear()
        for pending in self.pending.values():
            if not pending.delivered.is_set():
                pending.delivered.set()
                return pending.as_json()
        return None

    def acknowledge(self, call_id: str) -> bool:
        """The app saying it put a call on the screen. This is the proof."""
        pending = self.pending.get(call_id)
        if pending is None:
            return False
        pending.acked.set()
        self.ringing.set()
        return True

    def settle(self, call_id: str, answered: bool) -> bool:
        pending = self.pending.get(call_id)
        if pending is None or pending.answered.done():
            return False
        pending.answered.set_result(answered)
        return True

    # ---- RingTransport ---------------------------------------------------

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> MediaStream:
        if not self.present:
            # Do not even try. Failing here rather than after a 45 s ring-out is
            # what lets the chain reach Linphone while he is still near the
            # phone.
            raise CallUnreachable(
                f"the app has not been seen for "
                f"{time.monotonic() - self.last_seen:.0f}s -- it is not running"
            )

        self.ringing.clear()
        call_id = uuid.uuid4().hex[:12]
        pending = Pending(call_id, target)
        self.pending[call_id] = pending
        self._waiting.set()

        try:
            try:
                await asyncio.wait_for(pending.acked.wait(), ACK_WINDOW)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise CallUnreachable(
                    f"the app did not acknowledge the ring within {ACK_WINDOW:.0f}s"
                ) from exc

            try:
                answered = await asyncio.wait_for(pending.answered, timeout)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise CallUnanswered(f"rang for {timeout:.0f}s with no answer") from exc

            if not answered:
                raise CallDeclined("he declined")

            loop = asyncio.get_running_loop()
            stream = RtpStream(self.format)
            await loop.create_datagram_endpoint(
                lambda: stream, local_addr=(self.rtp_host, 0)
            )
            stream.start_clock()
            self._streams.append(stream)
            return stream
        finally:
            self.pending.pop(call_id, None)

    async def incoming(self) -> AsyncIterator[tuple[CallTarget, MediaStream]]:
        while True:
            yield await self._inbound.get()
