"""A transport with no phone in it.

Every other transport needs either Apple's push infrastructure or a SIP client
on a real handset, so without this there is no way to run the call pipeline in a
test or in CI, and `SPEC.md` §8 is explicit that nothing counts until it has
been executed. This is what makes the orchestrator runnable today, before the
outcome-A/B/C question is settled.

It is not a mock. It moves real PCM through a real `QueueStream` with real
framing, so everything above it -- segmentation, barge-in, turn handling,
narration -- is exercised for real. The only fiction is that the ring is
instantaneous and always answered, and that is configurable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..media.queue import QueueStream
from .base import (
    AudioFormat,
    CallDeclined,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
    MediaStream,
)


class LoopbackTransport:
    name = "loopback"
    rings_when_closed = True  # it is a fiction, and the tests say so

    def __init__(
        self,
        fmt: AudioFormat | None = None,
        *,
        answer: bool = True,
        decline: bool = False,
        reachable: bool = True,
        answer_delay: float = 0.0,
    ) -> None:
        self.format = fmt or AudioFormat(rate=16_000, channels=1)
        self.answer = answer
        self.decline = decline
        self.reachable = reachable
        self.answer_delay = answer_delay
        self.rang: list[CallTarget] = []
        self.streams: list[QueueStream] = []
        self.started = False
        self._inbound: asyncio.Queue[tuple[CallTarget, MediaStream]] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        for stream in self.streams:
            await stream.close()

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> MediaStream:
        self.rang.append(target)
        if not self.reachable:
            raise CallUnreachable(f"loopback: {target.device} marked unreachable")
        if self.decline:
            raise CallDeclined("loopback: declined")
        if not self.answer:
            raise CallUnanswered(f"loopback: rang out after {timeout:.0f}s")
        if self.answer_delay:
            await asyncio.sleep(self.answer_delay)
        stream = QueueStream(self.format)
        self.streams.append(stream)
        return stream

    async def incoming(self) -> AsyncIterator[tuple[CallTarget, MediaStream]]:
        while True:
            yield await self._inbound.get()

    # ---- test driving ----------------------------------------------------

    def place_incoming(self, target: CallTarget) -> QueueStream:
        """Simulate Bogdan calling in. Returns the stream his end holds."""
        stream = QueueStream(self.format)
        self.streams.append(stream)
        self._inbound.put_nowait((target, stream))
        return stream
