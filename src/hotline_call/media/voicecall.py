"""Holding an answered SIP call open and carrying audio both ways.

`ring/sip.py` deliberately hangs up the instant he answers -- the ring *is* the
message there, and an answered call it cannot talk on is worse than none. This
is the other half: what to do when the point of the call is to talk.

**The asymmetry that is easy to get wrong.** SDES gives each side its own master
key. The key in OUR offer encrypts what WE send; the key in HIS answer decrypts
what HE sends. Two SrtpSessions, not one, and using the offer's key for both
directions produces a call that authenticates its own packets perfectly and
cannot read a single one of his.

**Why 8 kHz G.711 and not Opus.** Measured on this box on 2026-09-08: Whisper
large-v3 scores 12.5% WER on Serbian over a real G.711 mu-law roundtrip and
12.5% on the 16 kHz original -- the narrowband penalty is nil at that model
size. G.711 is also the one codec that cannot be negotiated away. Opus would
save bandwidth on a relay path that has bandwidth to spare, in exchange for a
binding we would have to carry.

**Pacing is not optional.** Audio must leave at wall-clock speed: a 20 ms frame
every 20 ms. Writing a whole utterance into the socket as fast as the loop can
run delivers several seconds of audio in a few milliseconds, which every jitter
buffer on the far end discards as a flood.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import time
from dataclasses import dataclass

import numpy as np

from . import pcm, rtp, srtp

log = logging.getLogger(__name__)

WIRE_RATE = 8000        # G.711 is 8 kHz, always
FRAME_MS = 20
FRAME_SAMPLES = WIRE_RATE * FRAME_MS // 1000   # 160
PT_PCMU = 0             # payload type 0 is mu-law and is never negotiated away


class SdpError(Exception):
    pass


@dataclass
class SdpAnswer:
    """The bits of his 200 OK's SDP that decide where audio goes and how."""

    host: str
    port: int
    srtp_key: bytes
    srtp_salt: bytes
    payload_types: list[int]

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)


def parse_sdp_answer(message: str) -> SdpAnswer:
    """Pull the media destination and SRTP key out of a 200 OK.

    Takes the whole SIP message rather than just the body: splitting the body
    off is the caller's job to get wrong, and the SDP is unambiguous enough to
    find in situ.
    """
    host = ""
    port = 0
    payload_types: list[int] = []
    key = salt = b""

    for raw in message.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("c=IN IP4 "):
            # A session-level c= may be overridden by a media-level one; last wins,
            # which is the same rule as taking the media section's own value.
            host = line[len("c=IN IP4 "):].split("/")[0].strip()
        elif line.startswith("m=audio "):
            parts = line.split()
            if len(parts) < 4:
                raise SdpError(f"malformed m=audio line: {line!r}")
            port = int(parts[1])
            payload_types = [int(p) for p in parts[3:] if p.isdigit()]
        elif line.startswith("a=crypto:"):
            if not key:  # first acceptable crypto line wins, per RFC 4568
                try:
                    _tag, key, salt = srtp.parse_crypto_line(line)
                except srtp.SrtpError:
                    continue  # a profile we do not implement; keep looking

    if not host:
        raise SdpError("no c=IN IP4 line: nowhere to send audio")
    if not port:
        raise SdpError("no m=audio port: the far end declined media")
    if not key:
        raise SdpError("no usable a=crypto line: cannot key SRTP for this call")
    if PT_PCMU not in payload_types:
        raise SdpError(f"far end did not offer PCMU; payload types were {payload_types}")
    return SdpAnswer(host, port, key, salt, payload_types)


class VoiceCall:
    """One answered call's audio, both directions.

    Synchronous on purpose. The whole exchange is a strictly ordered
    speak-then-listen conversation running in an executor thread; an asyncio
    protocol here would be more machinery for the same behaviour.
    """

    def __init__(
        self,
        media_sock: socket.socket,
        answer: SdpAnswer,
        our_key: bytes,
        our_salt: bytes,
        ssrc: int | None = None,
    ) -> None:
        self.sock = media_sock
        self.remote = answer.address
        # Ours encrypts outbound; his decrypts inbound. See the module docstring.
        self.tx = srtp.SrtpSession(our_key, our_salt)
        self.rx = srtp.SrtpSession(answer.srtp_key, answer.srtp_salt)
        self.ssrc = ssrc if ssrc is not None else int.from_bytes(struct.pack("!I", id(self) & 0xFFFFFFFF), "big")
        self._seq = 0
        self._ts = 0
        self.frames_sent = 0
        self.frames_received = 0
        self.auth_failures = 0

    # -- outbound ---------------------------------------------------------

    def send_audio(self, audio: np.ndarray, rate: int) -> float:
        """Speak. Float32 mono in [-1, 1] at `rate`, paced to real time.

        Returns the wall-clock seconds spent, which should track the audio's own
        duration closely -- a large gap means the pacing is broken.
        """
        wire = pcm.from_model(audio, rate=rate, out_rate=WIRE_RATE)
        ulaw = pcm.ulaw_encode(wire)
        began = time.monotonic()
        for i in range(0, len(ulaw) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            self._send_frame(ulaw[i:i + FRAME_SAMPLES])
            # Absolute schedule, not `sleep(0.02)`: sleeping a fixed amount per
            # frame accumulates every scheduling overshoot and the stream drifts
            # progressively later than the clock it is meant to track.
            due = began + (self.frames_sent * FRAME_MS / 1000.0)
            slack = due - time.monotonic()
            if slack > 0:
                time.sleep(slack)
        return time.monotonic() - began

    def _send_frame(self, payload: bytes) -> None:
        packet = rtp.build_packet(self._seq & 0xFFFF, self._ts & 0xFFFFFFFF,
                                  self.ssrc, payload)
        self.sock.sendto(self.tx.protect(packet), self.remote)
        self._seq += 1
        self._ts += FRAME_SAMPLES
        self.frames_sent += 1

    # How much silence to send before the first real audio of a call. His client
    # cannot play anything until its jitter buffer has filled, so whatever
    # arrives during that window is swallowed -- on the first live call he heard
    # the greeting's opening syllable clipped and described it as "malo glitch
    # na pocetku". 0.4 s was not enough; 1.2 s covers a buffer sized for the
    # 172 ms jitter measured on this path with room to spare, and costs only a
    # second of silence he is not listening to yet anyway.
    PRIMING_SECONDS = 1.2

    def send_silence(self, seconds: float) -> None:
        """Keep the stream alive while nothing is being said.

        Some far ends tear down a call whose media stops; more practically, a
        gap in the RTP timestamps is what makes his client's jitter buffer
        decide the network died.
        """
        quiet = b"\xff" * FRAME_SAMPLES  # mu-law silence is 0xFF, not 0x00
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            self._send_frame(quiet)
            time.sleep(FRAME_MS / 1000.0)

    # -- inbound ----------------------------------------------------------

    def receive_audio(self, seconds: float, out_rate: int = 16000) -> np.ndarray:
        """Listen for `seconds` and return what arrived, at `out_rate`.

        Missing packets are not concealed -- a gap comes back as a gap. This
        feeds an ASR model, and inventing audio to paper over loss is exactly
        the kind of helpfulness that produces a confident wrong transcript.
        """
        collected = bytearray()
        deadline = time.monotonic() + seconds
        original = self.sock.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sock.settimeout(min(0.5, remaining))
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                try:
                    plain = self.rx.unprotect(data)
                except srtp.AuthenticationFailure:
                    self.auth_failures += 1
                    continue
                except srtp.SrtpError:
                    continue
                parsed = rtp.parse_packet(plain)
                if parsed is None:
                    continue
                collected += parsed[3]
                self.frames_received += 1
        finally:
            self.sock.settimeout(original)

        if not collected:
            return np.zeros(0, dtype=np.float32)
        return pcm.to_model(pcm.ulaw_decode(bytes(collected)), rate=WIRE_RATE) \
            if out_rate == 16000 else pcm.resample(
                pcm.to_model(pcm.ulaw_decode(bytes(collected)), rate=WIRE_RATE),
                16000, out_rate)

    def stats(self) -> dict[str, int]:
        return {
            "frames_sent": self.frames_sent,
            "frames_received": self.frames_received,
            "auth_failures": self.auth_failures,
        }
