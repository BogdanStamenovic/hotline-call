"""Format conversion between whatever a transport carries and what the models want.

hotline's `audio.py` has `stereo48_to_mono16` and `mono_to_stereo48`, and those
are correct for Discord and only for Discord -- the rate and channel count are
baked into the function names. SIP is 8 kHz mono, WebRTC is 48 kHz mono. So this
is the same arithmetic with the constants passed in instead of compiled in.

It deliberately does not import from `hotline.audio` beyond the model rate: that
module loads faster-whisper and silero at import time, and the conversion has to
be importable in a test that has neither.
"""

from __future__ import annotations

import numpy as np

MODEL_RATE = 16_000
"""What silero and faster-whisper both want. Same constant as hotline.audio."""


def to_model(pcm: bytes, rate: int, channels: int = 1) -> np.ndarray:
    """Wire PCM (interleaved LE int16) to float32 mono at 16 kHz in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(pcm, dtype="<i2")
    if channels > 1:
        if samples.size % channels:
            samples = samples[: samples.size - (samples.size % channels)]
        samples = samples.reshape(-1, channels).mean(axis=1)
    scaled = (samples / 32768.0).astype(np.float32)
    return resample(scaled, rate, MODEL_RATE)


def from_model(audio: np.ndarray, rate: int, out_rate: int, out_channels: int = 1) -> bytes:
    """Float32 mono from a TTS model to wire PCM at the transport's format."""
    if audio.size == 0:
        return b""
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    # Piper hands back float32 already scaled; a caller passing int16-scaled data
    # is a mistake worth surviving rather than emitting a solid wall of clipping.
    # Deliberately NOT nested under the dtype check above -- that is where
    # hotline's `mono_to_stereo48` puts it, which means the rescue never fires
    # for the float32 case, which is the only case Piper actually produces.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.5:
        audio = audio / 32768.0
    converted = resample(audio, rate, out_rate)
    clipped = (np.clip(converted, -1.0, 1.0) * 32767.0).astype("<i2")
    if out_channels > 1:
        clipped = np.repeat(clipped, out_channels)
    return clipped.tobytes()


def resample(audio: np.ndarray, rate: int, out_rate: int) -> np.ndarray:
    """soxr when it is installed, linear interpolation when it is not.

    The fallback exists so the pipeline is unit-testable without the voice
    extras. It is genuinely worse -- no anti-aliasing, so downsampling folds
    high frequencies back into the band Whisper listens to -- and it is never
    what runs in production. Kept honest rather than silent: `resample.exact`
    says which one ran.
    """
    if rate == out_rate or audio.size == 0:
        return audio.astype(np.float32)
    try:
        import soxr

        resample.exact = True  # type: ignore[attr-defined]
        return soxr.resample(audio, rate, out_rate).astype(np.float32)
    except ImportError:
        resample.exact = False  # type: ignore[attr-defined]
        count = max(1, int(round(audio.size * out_rate / rate)))
        positions = np.linspace(0, audio.size - 1, count, dtype=np.float64)
        return np.interp(positions, np.arange(audio.size), audio).astype(np.float32)


resample.exact = True  # type: ignore[attr-defined]


# ---- G.711, for the SIP transport ---------------------------------------
#
# Both codecs are byte-for-byte lookup tables built once at import. Doing it
# this way rather than pulling in a codec library is the whole reason the SIP
# transport has no third-party dependency at all: G.711 is the one codec every
# SIP client on earth must support, and it is 30 lines of arithmetic.

def _build_ulaw_decode() -> np.ndarray:
    codes = np.arange(256, dtype=np.int32)
    inverted = ~codes & 0xFF
    sign = inverted & 0x80
    exponent = (inverted >> 4) & 0x07
    mantissa = inverted & 0x0F
    magnitude = ((mantissa << 3) + 0x84) << exponent
    values = (magnitude - 0x84).astype(np.int32)
    return np.where(sign != 0, -values, values).astype("<i2")


_ULAW_DECODE = _build_ulaw_decode()
_ULAW_ENCODE = np.zeros(0, dtype=np.uint8)


def _build_ulaw_encode() -> np.ndarray:
    """Encode by inverting the decode table over the full int16 range.

    Building the reverse map by nearest-neighbour search rather than
    reimplementing the segment arithmetic means the two directions cannot drift
    apart -- a round-trip is exact by construction, which is the property the
    tests actually check.
    """
    levels = np.sort(_ULAW_DECODE.astype(np.int32))
    order = np.argsort(_ULAW_DECODE.astype(np.int32))
    everything = np.arange(-32768, 32768, dtype=np.int32)
    index = np.searchsorted(levels, everything)
    index = np.clip(index, 1, len(levels) - 1)
    lower, upper = levels[index - 1], levels[index]
    pick = np.where(everything - lower <= upper - everything, index - 1, index)
    return order[pick].astype(np.uint8)


def ulaw_decode(payload: bytes) -> bytes:
    """G.711 µ-law to 16-bit LE PCM."""
    if not payload:
        return b""
    return _ULAW_DECODE[np.frombuffer(payload, dtype=np.uint8)].tobytes()


def ulaw_encode(pcm: bytes) -> bytes:
    """16-bit LE PCM to G.711 µ-law."""
    global _ULAW_ENCODE
    if not pcm:
        return b""
    if _ULAW_ENCODE.size == 0:
        _ULAW_ENCODE = _build_ulaw_encode()
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    return _ULAW_ENCODE[samples + 32768].tobytes()
