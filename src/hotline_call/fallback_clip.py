"""Render the fallback's clip on archserver and pre-encode it into RTP payloads.

The Pi that plays it has no numpy and no libopus, so everything that needs
either happens here, once, at install time: cvoiced speaks the text in his
cloned voice, it is resampled to 48 kHz and 8 kHz, and encoded into the exact
20 ms payloads the Pi will send -- Opus, and G.711 mu-law and A-law for a phone
that offers no Opus. The Pi only adds RTP headers, SRTP and pacing.

The output is a directory (see `fallback.Clip` for the layout). It is never
committed: it is his voice.

A screen, not a verdict: the clip is rejected when it is implausibly short or
long for the text, silent, or clipped -- the failures a one-take render actually
has. Whether it SAYS the text is not checked here.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np

from .media import codec, pcm
from .media.tts import Voice

LEAD_MS = 300
"""Silence before the first word: the phone is still switching to the call
when the first packets land, and a clipped "Ćao" is the first thing he hears."""
TAIL_MS = 700
"""Silence after the last word, so the BYE does not cut it off."""
FRAME = codec.FRAME_MS / 1000


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _frames(pcm16: bytes, samples: int) -> list[bytes]:
    size = samples * 2
    out = [pcm16[i: i + size] for i in range(0, len(pcm16), size)]
    if out and len(out[-1]) < size:
        out[-1] = out[-1] + bytes(size - len(out[-1]))
    return out


def screen(audio: np.ndarray, rate: int, text: str) -> list[str]:
    """Why this take is unusable, or [] when it passes."""
    problems = []
    seconds = audio.size / rate if rate else 0.0
    words = len(text.split())
    # Serbian read aloud runs about 2-3.5 words a second; allow a wide band.
    if not words / 6.0 <= seconds <= words / 1.0 + 2.0:
        problems.append(f"{seconds:.1f} s is implausible for {words} words")
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    if rms < 0.01:
        problems.append(f"nearly silent (rms {rms:.4f})")
    clipped = float(np.mean(np.abs(audio) >= 0.999)) if audio.size else 0.0
    if clipped > 0.001:
        problems.append(f"{clipped:.2%} of samples clipped")
    return problems


def render(out: Path, text: str, *, voice: Voice | None = None, attempts: int = 3) -> int:
    voice = voice or Voice(takes=1, steps=0, timeout=180.0)
    audio = np.zeros(0, dtype=np.float32)
    rate = 24_000
    problems: list[str] = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        audio = voice.synthesize(text)
        rate = voice.rate
        problems = screen(audio, rate, text)
        print(f"render-clip: take {attempt}: {audio.size / rate:.2f} s of audio at {rate} Hz "
              f"in {time.monotonic() - started:.1f} s"
              + (f"; rejected: {'; '.join(problems)}" if problems else ""), file=sys.stderr)
        if not problems:
            break
    if problems:
        print(f"render-clip: no usable take after {attempts}: {'; '.join(problems)}",
              file=sys.stderr)
        return 1

    lead = np.zeros(int(rate * LEAD_MS / 1000), dtype=np.float32)
    tail = np.zeros(int(rate * TAIL_MS / 1000), dtype=np.float32)
    padded = np.concatenate([lead, audio.astype(np.float32), tail])
    out.mkdir(parents=True, exist_ok=True)
    codecs: dict[str, dict[str, object]] = {}

    wide = pcm.from_model(padded, rate, 48_000)
    if codec.opus_library() is not None:
        # Encoded with no fmtp from him: his offer's constraints are unknown
        # until he calls. 32 kbit/s fullband mono is what calld sends him live.
        opus = codec.Opus(codec.Choice("opus", codec.OPUS_PT, 48_000, {}))
        payloads = [opus.encode(frame) for frame in _frames(wide, 960)]
        if opus.errors or not all(payloads):
            print(f"render-clip: opus encoder failed on {opus.errors} frames", file=sys.stderr)
            return 1
        blob = b"".join(struct.pack("!H", len(p)) + p for p in payloads)
        (out / "opus.bin").write_bytes(blob)
        codecs["opus"] = {"file": "opus.bin", "framing": "u16len", "clock": 48_000,
                          "frames": len(payloads), "bytes": len(blob)}
    else:
        print("render-clip: no libopus here; the clip is G.711 only", file=sys.stderr)

    narrow = pcm.from_model(padded, rate, 8_000)
    for name, encode in (("PCMU", pcm.ulaw_encode), ("PCMA", pcm.alaw_encode)):
        frames = [encode(frame) for frame in _frames(narrow, 160)]
        blob = b"".join(frames)
        (out / f"{name.lower()}.bin").write_bytes(blob)
        codecs[name] = {"file": f"{name.lower()}.bin", "framing": "fixed", "frame_bytes": 160,
                        "clock": 8000, "frames": len(frames), "bytes": len(blob)}

    manifest = {
        "format": 1,
        "text": text,
        "text_sha": text_digest(text),
        "profile": voice.profile,
        "frame_ms": codec.FRAME_MS,
        "seconds": round(padded.size / rate, 2),
        "speech_seconds": round(audio.size / rate, 2),
        "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "codecs": codecs,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"render-clip: wrote {out}: {manifest['seconds']} s, "
          + ", ".join(f"{k} {v['frames']} frames" for k, v in codecs.items()), file=sys.stderr)
    return 0
