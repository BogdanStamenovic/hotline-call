"""A concrete `MediaStream` built on two queues.

Every transport ends up wanting the same thing: inbound frames arrive from a
socket callback on some other thread, outbound frames are produced in bursts by
a TTS call and must be handed to the wire at a steady 20 ms. So the queue pair
is written once here and the transports subclass or embed it.

The outbound side is a deque rather than an `asyncio.Queue` for one reason:
barge-in. Interrupting Claude mid-sentence means discarding everything already
synthesised, and `asyncio.Queue` has no supported way to drop its contents --
`_queue.clear()` reaches inside it and breaks its unfinished-task accounting.
hotline hit exactly this and solved it the same way in `StreamSource`.
"""

from __future__ import annotations

import asyncio
from collections import deque

from ..ring.base import AudioFormat, frames


class QueueStream:
    """Full-duplex PCM over two in-memory queues.

    Implements `MediaStream` structurally -- there is no inheritance, because
    `MediaStream` is a Protocol and making it a base class would force every
    transport that already has a stream object to wrap rather than be one.
    """

    def __init__(self, fmt: AudioFormat, *, max_inbound: int = 200) -> None:
        self.format = fmt
        # Bounded: if nothing is draining inbound audio the call is already
        # broken, and an unbounded queue turns that into an OOM twenty minutes
        # later instead of a visible drop now. 200 frames is 4 s at 20 ms.
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_inbound)
        self._outbound: deque[bytes] = deque()
        self._outbound_ready = asyncio.Event()
        self._closed = False
        self.dropped_inbound = 0

    # ---- inbound (transport -> us) --------------------------------------

    def feed(self, pcm: bytes) -> None:
        """Called by the transport, possibly from another thread's callback via
        `loop.call_soon_threadsafe`. Never blocks; drops on overflow and counts
        the drop, because blocking here would stall the socket reader."""
        if self._closed:
            return
        try:
            self._inbound.put_nowait(pcm)
        except asyncio.QueueFull:
            self.dropped_inbound += 1

    async def recv(self) -> bytes | None:
        if self._closed and self._inbound.empty():
            return None
        return await self._inbound.get()

    # ---- outbound (us -> transport) --------------------------------------

    def send(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        for frame in frames(pcm, self.format):
            self._outbound.append(frame)
        self._outbound_ready.set()

    def clear(self) -> int:
        dropped = sum(len(frame) for frame in self._outbound)
        self._outbound.clear()
        self._outbound_ready.clear()
        return dropped

    @property
    def pending_seconds(self) -> float:
        if not self._outbound:
            return 0.0
        total = sum(len(frame) for frame in self._outbound)
        return total / (self.format.rate * self.format.channels * 2)

    def take(self) -> bytes | None:
        """One outbound frame for the wire, or None if nothing is queued.

        Returns None rather than silence so the transport decides what a gap
        means: RTP wants comfort noise or nothing at all, WebRTC wants silence
        to keep the clock, and a test wants to see the gap.
        """
        if not self._outbound:
            self._outbound_ready.clear()
            return None
        return self._outbound.popleft()

    async def wait_outbound(self) -> None:
        await self._outbound_ready.wait()

    # ---- lifecycle -------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outbound.clear()
        self._outbound_ready.set()
        with __import__("contextlib").suppress(asyncio.QueueFull):
            self._inbound.put_nowait(None)

    @property
    def closed(self) -> bool:
        return self._closed
