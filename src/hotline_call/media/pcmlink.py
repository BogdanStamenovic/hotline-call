"""Carrying a call's audio as raw PCM over a local socket, for wa-call.

WhatsApp is the first transport here whose protocol stack is somebody else's
program: wa-call (Rust, whatsapp-rust) holds the session, the relay, the MLOW or
Opus codec and the encryption. What it hands this daemon is exactly what the
rest of the pipeline already speaks -- 16-bit linear PCM -- over a Unix stream
socket that hotline-calld listens on and wa-call connects to (`wa-call dial
--socket PATH`). So the only new piece is a pump that keeps the same 20 ms
clock as `MediaPump` and fills the same inbox, and `VoiceCall.over` takes it
from there: endpointing, barge-in, recording and every turn of the conversation
are untouched.

**Why a socket and not PipeWire virtual devices.** wa-call already had a
`--live` mode through `pw-cat`, and two null sinks would have bridged it with no
change to wa-call at all. Measured on archserver 2026-11-06, 20 ms frames
written in real time: one hop (`pw-cat --playback` into a 16 kHz null sink,
`pw-cat --record` of its monitor) took 135-255 ms, median 191 ms (n=11), and
crept up ~24 ms per second before resetting. A bridge needs that hop in each
direction, plus a process and a node to clean up after a crash. The same frame
over a Unix socket: median 0.005 ms, max 0.1 ms (n=200), and it dies with the
call.

The format is fixed by wa-call's engine: s16le, 16 kHz, mono. wa-call reads in
60 ms frames and writes in whatever it decoded; neither side relies on the
other's frame size, only on the byte stream staying sample-aligned.
"""

from __future__ import annotations

import logging
import select
import socket
import time

from .voicecall import FRAME_MS, MediaPump

log = logging.getLogger(__name__)

RATE = 16_000


class Linear16:
    """The "codec" of a link that is already PCM: encode and decode are identity.

    Kept behind the same interface as `codec.G711`/`codec.Opus` because
    `MediaPump` and `VoiceCall` read `rate`, `frame_samples`, `pt`, `errors` and
    `describe()` off whatever codec the call carries.
    """

    name = "L16"
    pt = -1
    rate = RATE
    clock = RATE

    def __init__(self) -> None:
        self.frame_samples = self.rate * FRAME_MS // 1000
        self.ts_step = self.frame_samples
        self.errors = 0

    def encode(self, frame: bytes) -> bytes:
        return frame

    def idle(self) -> bytes:
        return bytes(self.frame_samples * 2)

    def decode(self, payload: bytes) -> bytes:
        return payload

    def describe(self) -> str:
        return "linear PCM, 16 kHz (WhatsApp via wa-call)"


class PcmPump(MediaPump):
    """`MediaPump` with a byte stream for a wire instead of SRTP datagrams.

    Sends never block the clock: the socket is non-blocking and whatever the
    kernel will not take now waits in `_tx`, which is capped (oldest audio
    dropped first) so a stalled reader costs at most `max_backlog_ms` of delay
    rather than an ever-growing one. Drops are always an even number of bytes,
    so the far end stays sample-aligned however the stream was split.
    """

    def __init__(self, sock: socket.socket, codec: Linear16 | None = None, *,
                 max_backlog_ms: int = 200) -> None:
        super().__init__(sock, None, None, None, 0, codec or Linear16())
        self.name = "pcm-pump"
        sock.setblocking(False)
        self._tx = bytearray()
        self._rx = bytearray()
        self._cap = max(self.frame_bytes, self.codec.rate * 2 * max_backlog_ms // 1000)
        self.dropped_bytes = 0
        # The far end closed the stream: wa-call tore the call's media down.
        self.eof = False

    def _send(self, frame: bytes | None) -> None:
        if self.wire_out is not None:
            self.wire_out.append(frame if frame is not None else bytes(self.frame_bytes))
        self._tx += frame if frame is not None else self.codec.idle()
        self.frames_sent += 1
        over = len(self._tx) - self._cap
        if over > 0:
            over += over & 1
            del self._tx[:over]
            self.dropped_bytes += over
        if self.eof or not self._tx:
            return
        try:
            sent = self.sock.send(self._tx)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.eof = True
            return
        del self._tx[:sent]

    def _read(self, budget: float) -> None:
        if self.eof:
            time.sleep(max(0.001, budget))
            return
        try:
            ready, _, _ = select.select([self.sock], [], [], max(0.001, budget))
        except (OSError, ValueError):
            self.eof = True
            return
        if not ready:
            return
        try:
            data = self.sock.recv(8192)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.eof = True
            return
        if not data:
            self.eof = True
            log.info("wa-call closed the audio stream")
            return
        self._rx += data
        size = self.frame_bytes
        while len(self._rx) >= size:
            piece = bytes(self._rx[:size])
            del self._rx[:size]
            self.frames_received += 1
            if self.wire is not None:
                self.wire_at.append(self.frames_sent)
                self.wire.append(piece)
            self.inbox.put(piece)
