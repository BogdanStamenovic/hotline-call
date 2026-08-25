"""A doorbell with no phone behind it.

Every real transport needs Telegram credentials and a handset, so without this
nothing can be run at all -- and `SPEC.md` §8 is explicit that nothing counts
until it has been executed. It is what lets the confirmation and fall-through
logic be tested for real while the ring itself is still unbuilt.
"""

from __future__ import annotations

import asyncio

from .base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable


class LoopbackTransport:
    name = "loopback"
    rings_when_closed = True  # a fiction, and the tests say so

    def __init__(
        self,
        *,
        answer: bool = True,
        decline: bool = False,
        reachable: bool = True,
        confirms: bool = True,
        confirm_delay: float = 0.0,
        answer_delay: float = 0.0,
    ) -> None:
        self.answer = answer
        self.decline = decline
        self.reachable = reachable
        self.confirms = confirms
        self.confirm_delay = confirm_delay
        self.answer_delay = answer_delay
        # Positive evidence the device is alerting. A real transport sets this
        # when the far side accepts the request.
        self.ringing = asyncio.Event()
        self.rang: list[CallTarget] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> None:
        self.rang.append(target)
        if not self.reachable:
            raise CallUnreachable(f"loopback: {target.device} marked unreachable")
        if self.confirms:
            if self.confirm_delay:
                await asyncio.sleep(self.confirm_delay)
            self.ringing.set()
        if self.decline:
            raise CallDeclined("loopback: declined")
        if not self.answer:
            # Deliberately waits: ringing out and never ringing are different
            # answers and must not collapse into one instant exception.
            await asyncio.sleep(min(timeout, 3600.0))
            raise CallUnanswered(f"loopback: rang out after {timeout:.0f}s")
        if self.answer_delay:
            await asyncio.sleep(self.answer_delay)
