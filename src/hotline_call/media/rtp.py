"""RTP over UDP, carrying G.711 µ-law, with a jitter buffer.

**Why not a library.** RFC 3550's fixed header is twelve bytes and the only
codec every SIP client on earth is required to support is a 256-entry lookup
table. Adding a dependency to carry that would put machinery Bogdan has not read
between his phone and a shell running with bypassed permissions, for no gain --
the same argument hotline's `httpd.py` makes about not importing a web
framework, and the same trade.

**Why a jitter buffer, which is the part that is not optional.** The path to his
phone was measured, not assumed: `tailscale ping` never establishes a direct
connection and every packet relays through a DERP server, at 92-623 ms with
172 ms of jitter. Feeding that straight into a 20 ms frame clock produces
audible chop on every packet that arrives late, which is most of them. A small
buffer trades a fixed delay for continuity, and on a conversation with a human
that is the right trade up to roughly 200 ms.

**Why µ-law and not Opus, for now.** G.711 at 64 kbit/s through a relay is
wasteful and Opus at 16-24 kbit/s would tolerate loss far better. But Opus needs
`libopus` through a binding, and G.711 is the codec that cannot be negotiated
away. So this is the floor that always works; Opus belongs here as a second
payload type once there is a phone on the other end to negotiate with, and the
SDP already advertises the µ-law-only offer honestly rather than promising
something unimplemented.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
from collections import deque

from ..ring.base import AudioFormat
from .pcm import ulaw_decode, ulaw_encode

log = logging.getLogger("hotline-ios.rtp")

PT_PCMU = 0
"""Payload type 0 is G.711 µ-law. Fixed by RFC 3551; never negotiated away."""

PCMU_FORMAT = AudioFormat(rate=8_000, channels=1, frame_ms=20)
SAMPLES_PER_FRAME = 160  # 20 ms at 8 kHz
RTP_HEADER = 12

JITTER_TARGET_MS = 120
"""Held before playout. Chosen against the measured 172 ms of jitter on the DERP
path: enough to absorb the common case, short enough that a conversation does
not feel like a satellite link."""


def build_packet(seq: int, timestamp: int, ssrc: int, payload: bytes) -> bytes:
    # V=2, no padding, no extension, no CSRC, no marker.
    return struct.pack("!BBHII", 0x80, PT_PCMU, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


def parse_packet(data: bytes) -> tuple[int, int, int, bytes] | None:
    """Returns (payload_type, seq, timestamp, payload), or None if unusable."""
    if len(data) < RTP_HEADER:
        return None
    first, marker_pt, seq, timestamp, _ssrc = struct.unpack("!BBHII", data[:RTP_HEADER])
    if (first >> 6) != 2:
        return None
    csrc_count = first & 0x0F
    offset = RTP_HEADER + 4 * csrc_count
    if first & 0x10:  # extension header present
        if len(data) < offset + 4:
            return None
        words = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        offset += 4 + 4 * words
    if offset > len(data):
        return None
    return marker_pt & 0x7F, seq, timestamp, data[offset:]


class JitterBuffer:
    """Reorder by sequence number and hold a little before playing out.

    Deliberately simple: hold N frames, emit in sequence order, and on a gap
    emit the previous frame again rather than silence. Repeating the last frame
    is a cheap packet-loss concealment that sounds markedly better than a hole
    -- a hole is a click, a repeat is a smear, and the ear forgives a smear.
    """

    def __init__(self, target_ms: int = JITTER_TARGET_MS, frame_ms: int = 20) -> None:
        self.depth = max(1, target_ms // frame_ms)
        self._frames: dict[int, bytes] = {}
        self._next: int | None = None
        self._last = b"\x00" * SAMPLES_PER_FRAME * 2
        self.concealed = 0
        self.reordered = 0
        self.dropped_late = 0

    def push(self, seq: int, pcm: bytes) -> None:
        if self._next is not None:
            # Wrap-safe distance: sequence numbers are 16-bit and do roll over.
            behind = (self._next - seq) & 0xFFFF
            if 0 < behind < 0x8000:
                self.dropped_late += 1
                return
            if seq != self._next:
                self.reordered += 1
        self._frames[seq] = pcm

    def pop(self) -> bytes | None:
        """One frame for playout, or None while still filling."""
        if len(self._frames) < self.depth and self._next is None:
            return None
        if self._next is None:
            self._next = min(self._frames)
        frame = self._frames.pop(self._next, None)
        self._next = (self._next + 1) & 0xFFFF
        if frame is None:
            if not self._frames:
                return None
            self.concealed += 1
            return self._last
        self._last = frame
        return frame


class RtpStream(asyncio.DatagramProtocol):
    """A `MediaStream` over RTP. Structurally compatible, no inheritance."""

    def __init__(self, fmt: AudioFormat | None = None) -> None:
        self.format = fmt or PCMU_FORMAT
        self.remote: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self._out: deque[bytes] = deque()
        self._in: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=400)
        self._jitter = JitterBuffer(frame_ms=self.format.frame_ms)
        self._seq = random.randint(0, 0xFFFF)
        self._timestamp = random.randint(0, 0xFFFFFFFF)
        self._ssrc = random.randint(0, 0xFFFFFFFF)
        self._closed = False
        self._clock: asyncio.Task[None] | None = None
        self.received = 0
        self.sent = 0

    # ---- asyncio.DatagramProtocol ---------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = parse_packet(data)
        if parsed is None:
            return
        payload_type, seq, _timestamp, payload = parsed
        if payload_type != PT_PCMU:
            # Anything else means the far end ignored our SDP. Say so once
            # rather than decoding noise as if it were speech.
            log.warning("ignoring RTP payload type %d; only PCMU is offered", payload_type)
            return
        if self.remote is None:
            # Latch onto wherever media actually arrives from. Symmetric RTP:
            # the SDP address is frequently wrong behind NAT and the packet's
            # source is right by definition.
            self.remote = addr
        self.received += 1
        self._jitter.push(seq, ulaw_decode(payload))

    # ---- MediaStream ----------------------------------------------------

    async def recv(self) -> bytes | None:
        if self._closed and self._in.empty():
            return None
        return await self._in.get()

    def send(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        size = SAMPLES_PER_FRAME * 2
        for start in range(0, len(pcm), size):
            frame = pcm[start : start + size]
            if len(frame) < size:
                frame = frame + b"\x00" * (size - len(frame))
            self._out.append(frame)

    def clear(self) -> int:
        dropped = sum(len(frame) for frame in self._out)
        self._out.clear()
        return dropped

    @property
    def pending_seconds(self) -> float:
        if not self._out:
            return 0.0
        return sum(len(f) for f in self._out) / (self.format.rate * self.format.channels * 2)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._clock is not None:
            self._clock.cancel()
        if self.transport is not None:
            self.transport.close()
        try:
            self._in.put_nowait(None)
        except asyncio.QueueFull:
            pass

    # ---- the frame clock -------------------------------------------------

    def start_clock(self) -> None:
        if self._clock is None:
            self._clock = asyncio.create_task(self._run_clock())

    async def _run_clock(self) -> None:
        """Drive both directions at a steady 20 ms.

        One clock rather than two: RTP is symmetric and a single tick keeps the
        outbound timestamp and the inbound playout on the same cadence, which is
        what stops slow drift between them over a long call.
        """
        interval = self.format.frame_ms / 1000.0
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        try:
            while not self._closed:
                next_tick += interval
                await asyncio.sleep(max(0.0, next_tick - loop.time()))

                inbound = self._jitter.pop()
                if inbound is not None:
                    try:
                        self._in.put_nowait(inbound)
                    except asyncio.QueueFull:
                        pass

                if self.transport is not None and self.remote is not None:
                    frame = self._out.popleft() if self._out else None
                    if frame is not None:
                        packet = build_packet(
                            self._seq, self._timestamp, self._ssrc, ulaw_encode(frame)
                        )
                        self.transport.sendto(packet, self.remote)
                        self.sent += 1
                    # The timestamp advances whether or not we sent, because it
                    # is a sample clock, not a packet counter. Freezing it
                    # during silence makes the far end treat the next packet as
                    # arriving early and discard it.
                    self._seq = (self._seq + 1) & 0xFFFF
                self._timestamp = (self._timestamp + SAMPLES_PER_FRAME) & 0xFFFFFFFF
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("RTP clock died")
            raise
