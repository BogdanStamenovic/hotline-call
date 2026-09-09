"""Keeping a call's audio, so a second model can be scored without a second call.

**Why this exists.** The handoff rejected `sam8000` turbo-serbian because
`large-v3` measured 3.8 WER points better on telephony -- on somebody else's
audio, in somebody else's conditions. His voice, his accent, his phone and
linphone.org's relay are the thing being measured, and none of that was ever
kept: before this, nothing in the repo wrote a single sample to disk. A call
produced one model's guess and no way to check it.

**Why mu-law and not what Whisper was given.** The pump hands over G.711 at
8 kHz. `pcm.to_model` turns that into 16 kHz float, and that float is a faithful
*rendering* of an 8 kHz signal -- but stored as a 16 kHz file it stops looking
like one, and the next person to score a model against it will reach for
whatever resampling their model prefers and quietly measure a different path.
So what is written is decoded straight from the payloads, at the rate they
arrived at.

**Off unless asked**, via `HOTLINE_IOS_RECORD_DIR`. He is on these calls. An
always-on recorder of his voice is not a default anyone should ship for him.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
import wave

from . import pcm

log = logging.getLogger(__name__)

WIRE_RATE = 8000


class Recorder:
    """One call's audio and transcripts, under one directory."""

    def __init__(self, root: str = "", *, note: str = "") -> None:
        self.root = pathlib.Path(root).expanduser()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.directory = self.root / f"call-{stamp}"
        self.turns: list[dict[str, object]] = []
        self.note = note
        self.started = time.time()
        self.failed = ""

    def turn(self, index: int, wire: bytes, text: str, model: str = "",
             reason: str = "", outbound_span: tuple[int, int] | None = None) -> None:
        """One turn of his, as it arrived, beside what the live model made of it."""
        name = f"turn-{index:02d}.wav"
        try:
            self._write(self.directory / name, wire)
        except OSError as exc:
            # A recording is a nice-to-have. Losing it must never cost the call.
            self.failed = f"{type(exc).__name__}: {exc}"
            log.error("could not record turn %d: %s", index, self.failed)
            return
        self.turns.append({
            "turn": index,
            "file": name,
            "seconds": round(len(wire) / WIRE_RATE, 3),
            "heard": text,
            "model": model,
            "ended": reason,
            "at": round(time.time() - self.started, 2),
            # Which outbound frames we were sending while this turn arrived, so
            # `outbound.wav` can be sliced at the same instant. Wall clock does
            # not survive his phone's silence suppression; this does.
            "outbound_frames": list(outbound_span) if outbound_span else None,
        })
        log.info("recorded turn %d: %.1fs -> %s", index, len(wire) / WIRE_RATE, name)

    def finish(self, wire: bytes, stats: dict | None = None, ended: str = "",
               outbound: bytes = b"", inbound_at: list[int] | None = None) -> str:
        """Both streams and a manifest. Returns where it went.

        `outbound.wav` is what WE sent: gapless, 50 frames a second, and the
        only way to tell his voice arriving over ours from our own voice coming
        back off his handset. Those want opposite handling and look identical
        from the inbound side alone.

        `alignment.json` is what makes the two comparable -- one outbound frame
        index per inbound frame. His phone suppresses silence, so inbound frame
        *i* is not the same instant as outbound frame *i*, and the first version
        of the analyser sliced one by the other's indices and would have compared
        unrelated audio.
        """
        try:
            if wire:
                self._write(self.directory / "inbound.wav", wire)
            if outbound:
                self._write(self.directory / "outbound.wav", outbound)
            manifest = {
                "started": self.started,
                "note": self.note,
                "rate": WIRE_RATE,
                "codec": "G.711 mu-law, as received",
                "outbound_seconds": round(len(outbound) / WIRE_RATE, 2),
                "ended": ended,
                "stats": stats or {},
                "turns": self.turns,
            }
            (self.directory / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False))
            if inbound_at:
                # Its own file, not the manifest: it is one integer per inbound
                # frame -- thousands of them -- and a manifest nobody can read is
                # a manifest nobody reads.
                (self.directory / "alignment.json").write_text(json.dumps(
                    {"outbound_frame_per_inbound_frame": inbound_at,
                     "frame_ms": 20, "rate": WIRE_RATE}))
        except OSError as exc:
            self.failed = f"{type(exc).__name__}: {exc}"
            log.error("could not finish the recording: %s", self.failed)
            return ""
        log.info("recording: %d turn(s), %.1fs inbound -> %s",
                 len(self.turns), len(wire) / WIRE_RATE, self.directory)
        return str(self.directory)

    def _write(self, path: pathlib.Path, wire: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(WIRE_RATE)
            handle.writeframes(pcm.ulaw_decode(wire))


def from_environment(note: str = "") -> Recorder | None:
    """A recorder if HOTLINE_IOS_RECORD_DIR is set, and None otherwise."""
    import os

    where = os.environ.get("HOTLINE_IOS_RECORD_DIR", "").strip()
    return Recorder(where, note=note) if where else None
