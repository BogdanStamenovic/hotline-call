"""One call's own record of what was said, and a way to wait for the next line.

This is the call daemon's working memory for a call in flight: the question it
rang with, what he said, what the voice said, how it ended. `place()` waits on
it for his answer and reads the whole conversation back out of it.

**It is not the conversation the iPhone app shows him.** That one is
hotline-ios's, persisted in its store and served on its event feed; this daemon
mirrors every line into it over HTTP (`appmirror.py`). The split is deliberate:
a call must still ring, be answered out loud and hand the caller his words
when hotline-iosd is down, so the call's own record cannot live on the far side
of an HTTP request. Nothing here is persisted -- a call does not outlive the
process holding its RTP socket, and the durable copy is hotline-ios's.

Every method runs on the event loop. The media threads hand their appends over
with `loop.call_soon_threadsafe`, exactly as they did when this was hotline-ios's
`EventLog`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Line:
    seq: int
    kind: str
    text: str
    at: float


@dataclass
class CallLog:
    lines: list[Line] = field(default_factory=list)
    closed: bool = False
    _arrived: asyncio.Event | None = None

    @property
    def latest(self) -> int:
        return self.lines[-1].seq if self.lines else 0

    def append(self, kind: str, text: str) -> Line:
        line = Line(seq=self.latest + 1, kind=kind, text=text, at=time.time())
        self.lines.append(line)
        self._wake()
        return line

    def close(self) -> None:
        self.closed = True
        self._wake()

    def since(self, cursor: int) -> list[Line]:
        return [line for line in self.lines if line.seq > cursor]

    async def wait(self, cursor: int, timeout: float) -> list[Line]:
        """Lines after `cursor`, waiting up to `timeout` for the first one.

        Returns early, possibly empty, when the log closes: a closed call has
        nothing more coming, and a waiter parked on it would only burn its
        whole timeout finding that out.
        """
        found = self.since(cursor)
        if found or self.closed or timeout <= 0:
            return found
        if self._arrived is None:
            self._arrived = asyncio.Event()
        arrived = self._arrived
        try:
            await asyncio.wait_for(arrived.wait(), timeout)
        except TimeoutError:
            pass
        return self.since(cursor)

    def _wake(self) -> None:
        # One Event per generation: everybody parked on the old one wakes, and
        # the next waiter gets a fresh one rather than a set flag it would
        # return through immediately.
        if self._arrived is not None:
            self._arrived.set()
            self._arrived = None
