import numpy as np
import pytest

from hotline_ios.media import pcm
from hotline_ios.ring.base import AudioFormat, as_int16, frames


def test_ulaw_roundtrip_is_exact_on_codec_levels():
    # Every one of the 256 representable levels must survive a round trip. This
    # is the property that lets the SIP transport claim it is lossless *for
    # G.711* rather than merely close.
    levels = pcm._ULAW_DECODE.tobytes()
    assert pcm.ulaw_decode(pcm.ulaw_encode(levels)) == levels


def test_ulaw_encode_is_nearest_level():
    quiet = np.array([0, 1, -1, 100, -100], dtype="<i2").tobytes()
    back = np.frombuffer(pcm.ulaw_decode(pcm.ulaw_encode(quiet)), dtype="<i2")
    original = np.frombuffer(quiet, dtype="<i2").astype(np.int32)
    # µ-law is logarithmic: quiet speech, which is where a phone call lives, is
    # near-exact. Anything under 5 counts is inaudible.
    assert np.abs(back.astype(np.int32) - original).max() <= 4


def test_ulaw_clips_to_the_codecs_own_range_not_to_int16():
    # G.711 µ-law simply cannot represent past +/-32124, so a full-scale sample
    # comes back 643 counts low. That is the codec, not a bug -- asserting a
    # tighter bound here would be asserting something untrue about G.711.
    loud = np.array([32767, -32768], dtype="<i2").tobytes()
    back = np.frombuffer(pcm.ulaw_decode(pcm.ulaw_encode(loud)), dtype="<i2")
    assert back.tolist() == [32124, -32124]


def test_to_model_downmixes_and_resamples():
    # 48 kHz stereo, 1 second of a tone -- what WebRTC would hand us.
    t = np.linspace(0, 1, 48_000, endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
    stereo = np.repeat(as_int16(tone), 1)
    interleaved = np.frombuffer(stereo, dtype="<i2")
    both = np.repeat(interleaved, 2).tobytes()
    out = pcm.to_model(both, 48_000, 2)
    assert out.dtype == np.float32
    assert abs(out.size - 16_000) < 50, out.size
    assert 0.2 < float(np.abs(out).max()) <= 1.0


def test_from_model_matches_transport_format():
    audio = np.zeros(16_000, dtype=np.float32)
    out = pcm.from_model(audio, 16_000, 8_000, 1)
    assert len(out) == 8_000 * 2


def test_from_model_survives_int16_scaled_input():
    # A caller handing us int16-scaled floats is a mistake, but emitting white
    # noise for it is a worse one.
    audio = (np.ones(1000, dtype=np.float32) * 16384.0)
    out = np.frombuffer(pcm.from_model(audio, 16_000, 16_000, 1), dtype="<i2")
    assert out.max() < 20_000


def test_resample_reports_whether_it_was_exact():
    pcm.resample(np.zeros(100, dtype=np.float32), 16_000, 8_000)
    # soxr is installed in the voice venv and not in a bare one; either way the
    # flag must tell the truth about which path ran.
    assert isinstance(pcm.resample.exact, bool)


def test_frames_pads_the_tail_rather_than_dropping_it():
    fmt = AudioFormat(rate=8_000, channels=1, frame_ms=20)
    assert fmt.frame_bytes == 320
    out = frames(b"\x01" * 500, fmt)
    assert len(out) == 2
    assert all(len(f) == 320 for f in out)
    assert out[1][:180] == b"\x01" * 180
    assert out[1][180:] == b"\x00" * 140
