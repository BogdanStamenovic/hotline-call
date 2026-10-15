"""Which codec a call carries, how both sides agree on it, and the codecs themselves.

**Why this exists.** Until 2026-10-15 every call was G.711 at 8 kHz, which
throws away everything above 4 kHz before any model hears it. That was measured
as harmless for Whisper large-v3 on Serbian (see `voicecall.py`), and it was
not harmless for the duplex engine: PersonaPlex was trained on 24 kHz audio, and
on its first test call it misheard a lot of a line that had no consonants left.
His Linphone speaks Opus, so the fix is to ask for it.

**The shape.** A call is a negotiation followed by a codec object:

- `offer_lines()` is what goes in an INVITE: Opus first, G.711 after it.
- `negotiate()` reads the far end's answer and returns the one codec both sides
  have, in the answerer's order -- the answerer chooses, per RFC 3264.
- `answer_lines()` is the other side of the same exchange, for when we are the
  one being offered to. Nothing in the daemon answers an INVITE today; the
  loopback tests do, and an inbound path would.
- `open_codec()` turns the agreed choice into something with `encode(pcm)` and
  `decode(payload)`, both on 16-bit linear PCM at `codec.rate`.

The rest of the media path only ever sees linear PCM at `codec.rate` -- 48 kHz
for Opus, 8 kHz for G.711 -- and never a payload. That is the point of the
split: Opus encoders and decoders carry state from one frame to the next, so a
payload has to be decoded exactly once, in arrival order, by one owner (the
pump). Passing payloads around and decoding them wherever they are looked at,
which is what the G.711 code did, works for a lookup table and corrupts Opus.

**Why ctypes on libopus and not PyAV.** Both were measured on this box on 20 ms
frames at 48 kHz mono, 32 kbit/s: ctypes 0.062 ms encode / 0.017 ms decode,
PyAV 0.092 / 0.024. Either is nothing against a 20 ms budget. libopus directly
gives the controls that matter on a lossy relay path (in-band FEC, loss
percentage) as plain integers, with no per-frame frame objects. The library is
found as the system `libopus`, else the copy PyAV's wheel bundles (PyAV comes in
with faster-whisper), else not at all -- and then Opus is simply not offered and
the call is G.711, exactly as before. A missing codec costs quality, never the
call.

`HOTLINE_IOS_CODECS` (default `opus,pcmu,pcma`) is the order offered and the
kill switch: `pcmu,pcma` puts the line back to what it was without a deploy.

**Measured, loopback (bench/opus_loopback.py, 2026-10-15).** A fake Linphone
answering our real INVITE settles on Opus; a 6 kHz tone arrives at Whisper's
16 kHz input at -0.2 dB relative to 1 kHz (G.711: -216 dB, i.e. gone), 10 kHz
crosses each direction at about -2 dB. Opus adds 6.4 ms of algorithmic delay per
direction (309 samples at 48 kHz); G.711 adds none. Codec cost in the pump is
0.17 ms to encode and 0.03 ms to decode a 20 ms frame.

## Limitations

- **His phone has not been asked yet.** Everything above is against a stand-in.
  The call goes through linphone.org's media relay (flexisip), which should
  pass codecs through untouched; nothing here proves that it does.
- **Loss is not concealed.** A lost Opus packet is a gap, the same rule
  `receive_audio` applies to G.711 -- the encoder sends in-band FEC when the far
  end asks for it, but the decoder here does not use FEC to rebuild a lost frame.
- **One payload type per Opus call.** If the far end switches to G.711
  mid-call, those packets are counted in `foreign_frames` and dropped. A G.711
  call does follow a switch between mu-law and A-law.
- **The turn-taking thresholds were tuned on 8 kHz mu-law.** Levels are RMS and
  speech energy is mostly below 4 kHz, so they should carry over; the timbre
  check is fed 8 kHz explicitly so its tuned threshold still means what it did.
  Neither has been re-measured on a real Opus line.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import logging
import os
import re
from dataclasses import dataclass, field

import numpy as np

from . import pcm

log = logging.getLogger(__name__)

FRAME_MS = 20
"""Every codec here is sent in 20 ms frames, which is also what every frame
counter in `voicecall.py` assumes."""

OPUS_PT = 96
"""Dynamic payload type for our Opus offer. 96 because it is the first dynamic
number and what Linphone itself uses; the answer may renumber it and the
answer's number is the one used on the wire."""

OPUS_BITRATE = int(os.environ.get("HOTLINE_IOS_OPUS_BITRATE", "32000"))
"""32 kbit/s fullband voice. Half of G.711's 64 and, for speech, comfortably
past the point where Opus stops being the weak link. Capped further by the far
end's `maxaveragebitrate` if it sends one."""


# ---- the agreed choice ---------------------------------------------------

@dataclass(frozen=True)
class Choice:
    """One codec both sides have: its name, wire payload type and parameters."""

    name: str              # "opus", "PCMU", "PCMA"
    pt: int                # the payload type THEY named it -- what goes on the wire
    clock: int             # RTP clock rate from the rtpmap
    fmtp: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Known:
    name: str
    pt: int
    clock: int
    rtpmap: str
    fmtp: str = ""


_STATIC = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}
"""RFC 3551's static assignments, which a peer may use without an rtpmap."""

_TELEPHONE_EVENT = "a=rtpmap:101 telephone-event/8000"


def _catalogue() -> dict[str, _Known]:
    return {
        "opus": _Known("opus", OPUS_PT, 48000, "opus/48000/2",
                       # useinbandfec=1: we decode FEC, so they may send it.
                       # stereo=0: we want one channel; RFC 7587 still has the
                       # rtpmap say /2, always, so that is not a contradiction.
                       "useinbandfec=1;stereo=0;sprop-stereo=0"),
        "pcmu": _Known("PCMU", 0, 8000, "PCMU/8000"),
        "pcma": _Known("PCMA", 8, 8000, "PCMA/8000"),
    }


def preferred(env: str | None = None) -> list[_Known]:
    """What we offer, in order: configured, and only what can actually run."""
    wanted = env if env is not None else os.environ.get("HOTLINE_IOS_CODECS", "opus,pcmu,pcma")
    known = _catalogue()
    out: list[_Known] = []
    for name in (part.strip().lower() for part in wanted.split(",")):
        entry = known.get(name)
        if entry is None or entry in out:
            continue
        if entry.name == "opus" and opus_library() is None:
            continue
        out.append(entry)
    if not any(entry.name == "PCMU" for entry in out):
        # The floor. Every SIP client must take PCMU; an offer without it can
        # fail against a phone that would have been perfectly happy with it.
        out.append(known["pcmu"])
    return out


def offer_lines(env: str | None = None) -> tuple[str, list[str]]:
    """The payload list for `m=audio` and the attribute lines that follow it."""
    offered = preferred(env)
    pts = " ".join(str(entry.pt) for entry in offered) + " 101"
    attrs = []
    for entry in offered:
        attrs.append(f"a=rtpmap:{entry.pt} {entry.rtpmap}")
        if entry.fmtp:
            attrs.append(f"a=fmtp:{entry.pt} {entry.fmtp}")
    attrs.append(_TELEPHONE_EVENT)
    attrs.append(f"a=ptime:{FRAME_MS}")
    return pts, attrs


def _media_section(sdp: str) -> tuple[list[int], dict[int, tuple[str, int]], dict[int, dict[str, str]]]:
    """Payload types in m-line order, their rtpmaps, and their fmtp parameters."""
    pts: list[int] = []
    maps: dict[int, tuple[str, int]] = {}
    params: dict[int, dict[str, str]] = {}
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("m=audio "):
            pts = [int(p) for p in line.split()[3:] if p.isdigit()]
        elif line.startswith("a=rtpmap:"):
            match = re.match(r"a=rtpmap:(\d+)\s+([^/\s]+)/(\d+)", line)
            if match:
                maps[int(match.group(1))] = (match.group(2), int(match.group(3)))
        elif line.startswith("a=fmtp:"):
            match = re.match(r"a=fmtp:(\d+)\s+(.*)", line)
            if match:
                found: dict[str, str] = {}
                for item in match.group(2).split(";"):
                    key, _, value = item.strip().partition("=")
                    if key:
                        found[key.strip().lower()] = value.strip()
                params[int(match.group(1))] = found
    return pts, maps, params


def _identify(pt: int, maps: dict[int, tuple[str, int]]) -> tuple[str, int] | None:
    if pt in maps:
        name, clock = maps[pt]
        return name, clock
    return _STATIC.get(pt)


def negotiate(answer_sdp: str, env: str | None = None) -> Choice | None:
    """The codec an answer settled on, or None when it shares nothing with us.

    Matched by encoding name and clock, never by number alone: a dynamic payload
    type is the answerer's to name, and 96 in our offer may come back as 111.
    Taken in the ANSWER's order, because the answerer's first format is the one
    it intends to send (RFC 3264 6.1).
    """
    ours = {(entry.name.lower(), entry.clock) for entry in preferred(env)}
    pts, maps, params = _media_section(answer_sdp)
    for pt in pts:
        found = _identify(pt, maps)
        if found is None:
            continue
        name, clock = found
        if (name.lower(), clock) in ours:
            canonical = "opus" if name.lower() == "opus" else name.upper()
            return Choice(canonical, pt, clock, params.get(pt, {}))
    return None


def answer_lines(offer_sdp: str, env: str | None = None) -> tuple[Choice, str, list[str]] | None:
    """Answer an offer: our most preferred codec that it contains.

    The answerer's preference, not the offerer's order -- which is what lets a
    G.711-first offer still end up on Opus when we are the one answering, and is
    what Linphone itself does. Echoes the offer's payload number, so nothing on
    the wire is renumbered. None when the offer has nothing we can carry, which
    the caller answers with 488.
    """
    pts, maps, params = _media_section(offer_sdp)
    offered: dict[tuple[str, int], int] = {}
    for pt in pts:
        found = _identify(pt, maps)
        if found is not None:
            offered.setdefault((found[0].lower(), found[1]), pt)
    for entry in preferred(env):
        found_pt = offered.get((entry.name.lower(), entry.clock))
        if found_pt is None:
            continue
        pt = found_pt
        choice = Choice(entry.name, pt, entry.clock, params.get(pt, {}))
        attrs = [f"a=rtpmap:{pt} {entry.rtpmap}"]
        if entry.fmtp:
            attrs.append(f"a=fmtp:{pt} {entry.fmtp}")
        attrs.append(f"a=ptime:{FRAME_MS}")
        return choice, str(pt), attrs
    return None


# ---- the codecs ----------------------------------------------------------

class G711:
    """µ-law or A-law, 8 kHz. Stateless; kept behind the same interface as Opus."""

    rate = 8000
    clock = 8000

    def __init__(self, choice: Choice) -> None:
        self.choice = choice
        self.name = choice.name
        self.pt = choice.pt
        self.frame_samples = self.rate * FRAME_MS // 1000
        self.ts_step = self.clock * FRAME_MS // 1000
        alaw = choice.name == "PCMA"
        self._enc = pcm.alaw_encode if alaw else pcm.ulaw_encode
        self._dec = pcm.alaw_decode if alaw else pcm.ulaw_decode
        self.errors = 0

    def encode(self, frame: bytes) -> bytes:
        return self._enc(frame)

    def idle(self) -> bytes:
        # The canonical silent codes, 0xFF and 0xD5. Encoding zero through the
        # nearest-level table gives the other zero (0x7F, 0x55) -- the same
        # silence, but not the byte every G.711 tool expects to see.
        return (b"\xd5" if self.name == "PCMA" else b"\xff") * self.frame_samples

    def decode(self, payload: bytes) -> bytes:
        return self._dec(payload)

    def describe(self) -> str:
        return f"G.711 {'A' if self.name == 'PCMA' else 'mu'}-law, 8 kHz"


_OPUS_LIB: ctypes.CDLL | None = None
_OPUS_SEARCHED = False


def opus_library() -> ctypes.CDLL | None:
    """libopus, from the system or from PyAV's wheel, or None."""
    global _OPUS_LIB, _OPUS_SEARCHED
    if _OPUS_SEARCHED:
        return _OPUS_LIB
    _OPUS_SEARCHED = True
    candidates: list[str] = []
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    try:
        import av  # only to locate the libs its wheel ships

        libs = os.path.join(os.path.dirname(os.path.dirname(av.__file__)), "av.libs")
        candidates.extend(sorted(glob.glob(os.path.join(libs, "libopus*.so*"))))
    except ImportError:
        pass
    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_char_p, ctypes.c_int32]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                    ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_get_version_string.restype = ctypes.c_char_p
        _OPUS_LIB = lib
        log.info("opus: %s from %s", lib.opus_get_version_string().decode(), path)
        return lib
    log.warning("opus: no libopus found; calls will offer G.711 only")
    return None


# libopus request codes, from opus_defines.h. Stable ABI since 1.0.
_OPUS_APPLICATION_VOIP = 2048
_SET_BITRATE = 4002
_SET_MAX_BANDWIDTH = 4004
_SET_INBAND_FEC = 4012
_SET_PACKET_LOSS_PERC = 4014
_SET_SIGNAL = 4024
_SIGNAL_VOICE = 3001
_BANDWIDTHS = ((8000, 1101), (12000, 1102), (16000, 1103), (24000, 1104), (48000, 1105))


class Opus:
    """Opus at 48 kHz mono in 20 ms frames, with the far end's fmtp honoured."""

    rate = 48000
    clock = 48000
    MAX_FRAME = 5760   # 120 ms at 48 kHz: the longest packet Opus can carry

    def __init__(self, choice: Choice, bitrate: int = OPUS_BITRATE) -> None:
        lib = opus_library()
        if lib is None:
            raise RuntimeError("Opus was negotiated but libopus cannot be loaded")
        self._lib = lib
        self.choice = choice
        self.name = "opus"
        self.pt = choice.pt
        self.frame_samples = self.rate * FRAME_MS // 1000
        self.ts_step = self.clock * FRAME_MS // 1000
        self.errors = 0
        err = ctypes.c_int()
        self._enc = lib.opus_encoder_create(self.rate, 1, _OPUS_APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self._enc:
            raise RuntimeError(f"opus_encoder_create failed ({err.value})")
        self._dec = lib.opus_decoder_create(self.rate, 1, ctypes.byref(err))
        if err.value != 0 or not self._dec:
            raise RuntimeError(f"opus_decoder_create failed ({err.value})")

        # What the far end asked to RECEIVE. Their fmtp describes their decoder,
        # so it constrains our encoder and nothing else.
        fmtp = choice.fmtp
        cap = fmtp.get("maxaveragebitrate", "")
        if cap.isdigit():
            bitrate = min(bitrate, int(cap))
        self.bitrate = bitrate
        self._ctl(_SET_BITRATE, bitrate)
        self._ctl(_SET_SIGNAL, _SIGNAL_VOICE)
        playback = fmtp.get("maxplaybackrate", "")
        if playback.isdigit():
            band = next((code for hz, code in _BANDWIDTHS if int(playback) <= hz), 1105)
            self._ctl(_SET_MAX_BANDWIDTH, band)
        if fmtp.get("useinbandfec") == "1":
            # The relay path to his phone was measured at 92-623 ms with 172 ms
            # of jitter; FEC is what Opus offers against exactly that, and it
            # only costs bits when the encoder thinks loss is happening.
            self._ctl(_SET_INBAND_FEC, 1)
            self._ctl(_SET_PACKET_LOSS_PERC, 10)
        self._out = ctypes.create_string_buffer(1500)
        self._pcm = (ctypes.c_int16 * self.MAX_FRAME)()

    def _ctl(self, request: int, value: int) -> None:
        # opus_encoder_ctl is variadic; ctypes passes the int correctly on the
        # SysV ABI as long as argtypes is NOT set for it.
        self._lib.opus_encoder_ctl(ctypes.c_void_p(self._enc), ctypes.c_int(request),
                                   ctypes.c_int(value))

    def encode(self, frame: bytes) -> bytes:
        count = len(frame) // 2
        n = self._lib.opus_encode(self._enc, frame, count, self._out, len(self._out))
        if n < 0:
            self.errors += 1
            return b""
        return self._out.raw[:n]

    def idle(self) -> bytes:
        # Through the encoder, not a cached packet: Opus is predictive, and a
        # silent frame the encoder never saw would leave its state describing
        # the end of the last sentence.
        return self.encode(bytes(self.frame_samples * 2))

    def decode(self, payload: bytes) -> bytes:
        n = self._lib.opus_decode(self._dec, payload, len(payload), self._pcm, self.MAX_FRAME, 0)
        if n < 0:
            self.errors += 1
            return b""
        return ctypes.string_at(self._pcm, n * 2)

    def describe(self) -> str:
        return f"Opus 48 kHz mono, {self.bitrate // 1000} kbit/s"

    def __del__(self) -> None:
        lib = getattr(self, "_lib", None)
        if lib is None:
            return
        if getattr(self, "_enc", None):
            lib.opus_encoder_destroy(self._enc)
            self._enc = None
        if getattr(self, "_dec", None):
            lib.opus_decoder_destroy(self._dec)
            self._dec = None


def open_codec(choice: Choice) -> G711 | Opus:
    if choice.name == "opus":
        return Opus(choice)
    return G711(choice)


PCMU = Choice("PCMU", 0, 8000)
"""The floor, as a choice -- what a call is when nothing else was agreed."""


def silence(codec: G711 | Opus) -> bytes:
    """One frame of linear-PCM silence at the codec's rate."""
    return bytes(codec.frame_samples * 2)


def level(frame: bytes) -> float:
    """RMS of a linear-PCM frame in [0, 1]."""
    samples = np.frombuffer(frame, dtype="<i2")
    if not samples.size:
        return 0.0
    return float(np.sqrt(np.mean((samples / 32768.0) ** 2)))
