"""The seam that outcomes A, B and C plug into.

`SPEC.md` §2: whether Bogdan's iPhone can be made to ring by *his own* app
depends on an Apple Developer Program membership, and that answer was not known
when this was written. The rule that follows from it is that A/B/C must not be
three rewrites, so everything transport-specific lives behind `RingTransport`
and nothing above it knows which one is loaded.

There are two things a transport has to do and they are deliberately separate:

  *ring*   -- make the phone light up. Always leaves the local network. A push
              to a sleeping iPhone traverses Apple's APNs whatever we do; the
              only question is whose certificate signs it.
  *media*  -- carry the audio once answered. Never leaves Tailscale.

Keeping them in one object rather than two is a considered choice: in every
real transport the ring *establishes* the media path (a SIP INVITE carries the
SDP; a VoIP push exists to let the app open a WebRTC connection), so splitting
them would mean inventing a correlation id that the transports already have.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


class CallError(Exception):
    """Base for every way a call can fail to happen."""


class CallDeclined(CallError):
    """He pressed decline. A deliberate no, not a failure."""


class CallUnanswered(CallError):
    """It rang out. Distinct from `CallDeclined` because the escalation differs:
    an unanswered call is worth retrying or falling back to a page, a declined
    one is an answer."""


class CallUnreachable(CallError):
    """The ring could not be delivered at all -- no push token, transport down,
    phone off the network. The caller should fall back rather than retry."""


class CallState(enum.Enum):
    IDLE = "idle"
    RINGING = "ringing"
    ANSWERED = "answered"
    ENDED = "ended"


@dataclass(frozen=True)
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
    """Who to ring, and who they get when they answer.

    `agent` is passed straight to hotline's registry, so anything
    `Router.resolve` accepts works: a registered name (`hotline-80`), a derived
    session name, a directory, an ordinal (`newest`). None means the newest
    live session; `NEW` means spawn a fresh one.
    """

    device: str
    agent: str | None = None
    reason: str = ""
    caller_id: str = "Claude"
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class MediaStream(Protocol):
    """Full-duplex PCM, however the transport happens to carry it.

    Deliberately not an asyncio protocol/transport: those model a byte stream
    with no notion of a frame clock, and every one of these transports is
    frame-paced. `recv` returning None is the far end hanging up.
    """

    format: AudioFormat

    async def recv(self) -> bytes | None:
        """One inbound frame of interleaved little-endian int16, or None at end."""

    def send(self, pcm: bytes) -> None:
        """Queue outbound PCM in `format`. Must not block -- it is called from
        a synthesis callback."""

    def clear(self) -> int:
        """Drop everything queued outbound and return how many bytes went. This
        is barge-in, and it is the reason `send` is a queue rather than a write:
        you cannot un-send a write."""

    @property
    def pending_seconds(self) -> float:
        """How much queued audio is still unplayed. Barge-in needs this to tell
        'he interrupted me' from 'he answered after I finished'."""

    async def close(self) -> None: ...


@runtime_checkable
class RingTransport(Protocol):
    """One way of making the phone ring. Exactly one is loaded at a time.

    Implementations live beside this file:
      apns.py      outcome A -- our own app, PushKit VoIP push, WebRTC media
      sip.py       outcome C -- stock SIP client, self-hosted SIP, RTP media
      page.py      the existing Discord @mention, kept as the honest fallback
      loopback.py  no phone at all; what the tests and the CI harness use
    """

    name: str
    rings_when_closed: bool
    """Whether this transport can wake a phone whose app is not running. False
    for free-provisioning outcomes, and the single most important property to
    surface to Bogdan rather than bury -- a transport that only rings when he is
    already looking at the phone is not a ring."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> MediaStream:
        """Ring `target` and return the media stream once answered.

        Raises `CallDeclined`, `CallUnanswered` or `CallUnreachable`. Returning
        normally means audio is flowing.
        """

    def incoming(self) -> AsyncIterator[tuple[CallTarget, MediaStream]]:
        """Calls placed *by him*. Yields already-answered streams.

        Present on the protocol rather than a separate InboundTransport because
        every real transport gets both directions from the same socket; a
        transport with no inbound path returns an iterator that never yields.
        """


def silence(fmt: AudioFormat, seconds: float) -> bytes:
    return b"\x00" * (int(fmt.rate * seconds) * fmt.channels * 2)


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


def as_int16(audio: np.ndarray) -> bytes:
    """Float32 in [-1, 1] to little-endian int16, clipped."""
    if audio.size == 0:
        return b""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
