"""The seam the doorbell plugs into.

Bogdan settled the architecture on 2026-08-25 by decoupling the thing that
*rings* from the thing he *talks through*: Telegram rings, his own app is the
interface. That collapsed this module considerably, and the collapse is the
point -- a ring transport now has exactly one job.

    ring the phone, or say plainly that you could not.

It used to also carry the audio, because every option on the table at the time
assumed the ringer and the talker were one program. They are not, so there is no
`MediaStream` here any more. See `parked/` for that work; it is kept because if
Telegram turns out not to be on his phone, the SIP branch is where we go back to.

What has NOT changed, and must not: **a ring is not delivered because we asked
for it.** See `watch.py`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class CallError(Exception):
    """Base for every way a ring can fail to happen."""


class CallDeclined(CallError):
    """He pressed decline. A deliberate no, and an answer in itself."""


class CallUnanswered(CallError):
    """It rang out. Distinct from `CallDeclined` because the response differs:
    an unanswered ring is worth escalating, a declined one is not."""


class CallUnreachable(CallError):
    """The ring could not be delivered at all. The caller should fall through
    to another transport rather than retry this one."""


class CallState(enum.Enum):
    IDLE = "idle"
    RINGING = "ringing"
    ANSWERED = "answered"
    ENDED = "ended"


@dataclass
class CallTarget:
    """Who to ring, why, and which session he lands in when he answers.

    `agent` is passed to hotline's registry, so anything `Router.resolve`
    accepts works: a registered name (`hotline-80`), a derived session name, a
    directory, an ordinal. None means the newest live session.
    """

    device: str
    agent: str | None = None
    reason: str = ""
    caller_id: str = "Claude"
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class RingTransport(Protocol):
    """One way of making his phone ring."""

    name: str

    rings_when_closed: bool
    """Whether this can reach him when the app is not running.

    On the protocol rather than in a document because it is the single fact that
    decides whether a transport delivers the feature at all, and a fact that
    important should be impossible to lose track of.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> None:
        """Ring, and return only once the phone has actually rung.

        Raises `CallDeclined`, `CallUnanswered` or `CallUnreachable`. Returning
        normally means it rang; it does not mean he has said anything, which
        arrives separately through the app.
        """
