"""The seam the doorbell plugs into.

Bogdan settled the architecture on 2026-08-25 by decoupling the thing that
*rings* from the thing he *talks through*: Telegram rings, his own app is the
interface. That collapsed this module considerably, and the collapse is the
point -- a ring transport now has exactly one job.

    ring the phone, or say plainly that you could not.

It used to also carry the audio, because every option on the table at the time
assumed the ringer and the talker were one program. They are not, and a ring
transport still only rings -- but on 2026-09-08 Bogdan asked for a real two-way
voice call over SIP, so `AudioFormat` is back from `parked/` and the media
package alongside it. The transport does not own the audio; it only has to be
able to describe what a given wire format is.

What has NOT changed, and must not: **a ring is not delivered because we asked
for it.** See `watch.py`.
"""

from __future__ import annotations

import enum

import numpy as np
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
class AudioFormat:
    """What a given transport actually puts on the wire.

    Never assume 48 kHz stereo here. Discord's pipeline could, because Discord
    is the only thing it talked to. SIP hands us 8 kHz mono G.711 and WebRTC
    hands us 48 kHz mono Opus, so the conversion has to be parameterised or the
    first non-Discord transport silently transcribes chipmunks.
    """

    rate: int
    channels: int = 1
    frame_ms: int = 20

    @property
    def frame_bytes(self) -> int:
        return int(self.rate * self.frame_ms / 1000) * self.channels * 2


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
    context: str = ""
    """What the CALLING agent knows and the voice on the phone needs.

    His design, 2026-09-10: *"an agent can do hotline-call --speak and then give
    the spawned sonnet the context it needs. Everything it needs. And then sonnet
    talks with me. And relays info back to the agent."*

    Before this existed, `--context` was appended to the app conversation for him
    to READ and never reached the model at all -- the voice that rang him knew
    only a one-line reason, so it could ask his question and then not discuss it.
    """
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


def frames(pcm: bytes, fmt: AudioFormat) -> list[bytes]:
    """Split a synthesis result into wire-sized frames, zero-padding the tail.

    Padding rather than dropping: a short final frame is a click on most
    codecs, and dropping it truncates the last syllable of every sentence.
    """
    size = fmt.frame_bytes
    if size <= 0:
        return [pcm] if pcm else []
    out = [pcm[i : i + size] for i in range(0, len(pcm), size)]
    if out and len(out[-1]) < size:
        out[-1] = out[-1] + b"\x00" * (size - len(out[-1]))
    return out


def as_int16(audio: "np.ndarray") -> bytes:
    """Float32 in [-1, 1] to little-endian int16, clipped."""
    if audio.size == 0:
        return b""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
