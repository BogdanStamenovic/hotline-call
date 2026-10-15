"""An answered call carried by a full-duplex speech model instead of the ASR -> LLM -> TTS loop.

Opt-in only (`hotline-call --engine personaplex`). The default call path never
imports this module.

The model process (pp-stream, PersonaPlex-7B q4_k on moshi.cpp) is started when
the phone starts ringing, because its voice + persona prefill takes ~5 s and the
ring hides that. Both directions then stream continuously, 20 ms at a time: his
audio goes in whether or not he is talking, the model's goes out whether or not
it is. There is no VAD, endpointer or barge-in logic here on purpose -- turn
taking, back-channels and yielding to interruption are the model's job, and a
gate in front of it would turn a duplex model back into a walkie-talkie.

Why no ASR or cvoiced: PersonaPlex at context 3000 takes ~7.3 GiB of the 4060's
8 GiB, so the call cannot share the GPU with Whisper or the Serbian TTS. The
transcript is the model's own text stream; his side is recorded but not
transcribed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .media.duplex import DuplexEngine
from .media.pcm import ulaw_decode, ulaw_encode
from .media.voicecall import FRAME_MS, FRAME_SAMPLES, SdpError, VoiceCall, parse_sdp_answer

log = logging.getLogger(__name__)

LAB = Path(os.environ.get("HOTLINE_IOS_DUPLEX_LAB", str(Path.home() / "data/duplex-lab")))
DEFAULT_CMD = str(LAB / "pp-stream/run-pp-stream")
FAIL_CLIP = LAB / "pp-stream/engine-failed.wav"
DEFAULT_PROMPT = (
    "You are Ada, a friendly assistant. Bogdan, your developer, is calling you on the phone "
    "to test you. Answer his questions helpfully, keep answers short and conversational."
)
MAX_SECONDS = float(os.environ.get("HOTLINE_IOS_DUPLEX_MAX_S", "600"))
# The outbound queue the pump drains. Two frames is enough to ride out a late
# tick; more is added latency he hears on every reply.
OUT_QUEUE_FRAMES = 2


def brief_prompt(reason: str, context: str = "", source: str = "") -> str:
    """The persona prompt for a call placed by an agent: what to tell him and ask him.

    PersonaPlex has no tool loop and no way to look anything up mid-call, so the
    whole brief has to be in here. It paraphrases what it is given; facts that
    must survive verbatim should be short and stated plainly.
    """
    who = f" on behalf of {source}" if source and source not in ("an agent", "Claude") else ""
    parts = [f"You are Ada, an assistant phoning Bogdan{who}. "
             f"Your message for him: {reason.strip()}"]
    if context.strip():
        parts.append(f"Background: {context.strip()}")
    parts.append("Greet him, deliver the message accurately and early, ask your question, "
                 "then listen and reply briefly and conversationally.")
    return " ".join(parts)


class DuplexCall:
    """The `on_answer` handler for one ring. Starts the model at construction."""

    def __init__(self, *, prompt: str = "", voice: str = "", note: Callable[[str, str], None],
                 deliver: Callable[[str], None], hung_up: Callable[[], bool] | None,
                 record_dir: Path | None = None, label: str = "") -> None:
        self.note = note
        self.deliver = deliver
        self.hung_up = hung_up
        self.record_dir = record_dir or (LAB / "calls")
        self.label = label
        self.answered = False
        self.turns = 0
        self.stats: dict[str, object] = {}
        # The sentinel daemon._await_transcript waits on; anything else means done.
        self.ended = "not started"
        cmd = [os.environ.get("HOTLINE_IOS_DUPLEX_CMD", DEFAULT_CMD),
               "-p", prompt or DEFAULT_PROMPT, "-v", voice or "NATF2"]
        self.engine = DuplexEngine(cmd, log=lambda line: None if line.startswith("TXT") else log.debug("pp: %s", line))
        self.setup_error: str | None = None
        self.setup_seconds: float | None = None
        self._ready = threading.Thread(target=self._start, daemon=True, name="pp-start")
        self._ready.start()

    def _start(self) -> None:
        try:
            self.setup_seconds = self.engine.start()
            log.info("personaplex ready in %.1fs", self.setup_seconds)
        except Exception as exc:  # noqa: BLE001 -- surfaced on the call, not raised into the ring
            self.setup_error = str(exc)
            log.exception("personaplex failed to start")

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until the model has prefilled. Called BEFORE the phone rings, so a
        model that cannot start costs a failed call, never him answering to silence
        (2026-10-15 19:16: it did exactly that, an OOM abort during prefill)."""
        self._ready.join(timeout)
        return self.engine.ready.is_set() and not self.engine.dead.is_set() and not self.setup_error

    def close(self) -> None:
        self.engine.close()

    def _say_failure(self, call: VoiceCall) -> None:
        """Audible failure: a short spoken notice, then the call ends. Never silence."""
        try:
            with wave.open(str(FAIL_CLIP)) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                rate = w.getframerate()
            call.send_audio(pcm.astype(np.float32) / 32768.0, rate)
        except Exception:
            log.exception("could not play the failure notice")

    def _gone(self) -> bool:
        if self.hung_up is None:
            return False
        try:
            return bool(self.hung_up())
        except Exception:
            log.exception("hangup check raised; assuming the call is still up")
            return False

    def __call__(self, reply: str, media_sock, srtp_key: bytes, srtp_salt: bytes) -> None:
        try:
            answer = parse_sdp_answer(reply)
        except SdpError as exc:
            self.ended = f"no media: {exc}"
            self.note("error", f"call answered but carried no media: {exc}")
            return
        if media_sock is None:
            self.ended = "no media socket"
            return
        self.answered = True
        call = VoiceCall(media_sock, answer, srtp_key, srtp_salt)
        call.record()
        try:
            self._converse(call)
        finally:
            call.close()
            self.engine.close()
            self._save(call)

    def _converse(self, call: VoiceCall) -> None:
        pump = call.pump
        # If he picked up before the prefill finished, he hears silence until it
        # has; bounded so a wedged model cannot hold the line forever.
        self._ready.join(timeout=90)
        if self.setup_error or not self.engine.ready.is_set() or self.engine.dead.is_set():
            self.ended = f"model not ready: {self.setup_error or 'timeout'}"
            self.note("error", f"personaplex did not start: {self.ended}")
            self._say_failure(call)
            return
        # Discard what queued up while he waited for the model; the model must
        # start from "now", not from a backlog it would answer seconds late.
        while not pump.inbox.empty():
            pump.inbox.get_nowait()
        began = time.monotonic()
        self.answer_offset = len(pump.wire_out or [])
        tick = FRAME_MS / 1000.0
        next_tick = time.monotonic()
        silence = bytes(FRAME_SAMPLES * 2)
        n = 0
        while True:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
            next_tick += tick
            if time.monotonic() - next_tick > 0.2:
                next_tick = time.monotonic() + tick  # resync after a stall, no burst
            # His side: one frame per tick, silence across his phone's suppressed
            # gaps, and drain a backlog rather than let it become latency.
            backlog = pump.inbox.qsize()
            if backlog > 5:
                for _ in range(backlog - 2):
                    pump.inbox.get_nowait()
            try:
                frame = ulaw_decode(pump.inbox.get_nowait())
            except Exception:  # queue.Empty
                frame = silence
            self.engine.push(frame)
            if pump.queued() < OUT_QUEUE_FRAMES:
                pump.enqueue([ulaw_encode(self.engine.pull(FRAME_SAMPLES))])
            n += 1
            if n % 25 == 0:
                if self._gone():
                    self.ended = "he hung up"
                    break
                if self.engine.dead.is_set():
                    self.ended = "model process exited"
                    break
                if time.monotonic() - began > MAX_SECONDS:
                    self.ended = f"max duration {MAX_SECONDS:.0f}s"
                    break
        self.stats = {
            "seconds": round(time.monotonic() - began, 1),
            "setup_seconds": self.setup_seconds,
            "underruns": self.engine.underruns,
            "frame_ms": self.engine.stats[-1][1:] if self.engine.stats else None,
            "late_frames": pump.late_frames,
            "ended": self.ended,
        }
        log.info("duplex call ended (%s): %s", self.ended, self.stats)
        if self.ended == "model process exited":
            self.note("error", "personaplex died mid-call")
            self._say_failure(call)

    def _save(self, call: VoiceCall) -> None:
        text = "".join(p for _, p in self.engine.text).strip()
        st = None
        summary = (f"[personaplex duplex call, {self.stats.get('seconds', 0)}s, ended: {self.ended}] model text stream: {text or '(nothing)'}")
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            pump = call.pump
            wire_in, wire_out, at = pump.wire or [], pump.wire_out or [], pump.wire_at
            # Stereo on the outbound clock: L = him (placed at the frame index it
            # arrived on), R = what we sent. Exact to 20 ms; see MediaPump.wire_at.
            n = len(wire_out)
            left = np.zeros(n * FRAME_SAMPLES, dtype="<i2")
            for payload, idx in zip(wire_in, at, strict=False):
                if idx < n:
                    pcm = np.frombuffer(ulaw_decode(payload), dtype="<i2")
                    left[idx * FRAME_SAMPLES: idx * FRAME_SAMPLES + pcm.size] = pcm
            right = np.frombuffer(ulaw_decode(b"".join(wire_out)), dtype="<i2")
            st = np.stack([left[: right.size], right[: left.size]], 1)
            path = self.record_dir / f"{stamp}-duplex.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(st.astype("<i2").tobytes())
            import json
            (self.record_dir / f"{stamp}-duplex.json").write_text(json.dumps({
                "label": self.label, "stats": self.stats,
                "answer_offset_frames": getattr(self, "answer_offset", 0),
                "text_timed": [(round(i * 0.08, 2), p) for i, p in self.engine.text],
            }, indent=1))
            summary += f" (recording {path})"
            log.info("duplex call recorded to %s", path)
        except Exception:
            log.exception("could not save the duplex recording")
        turns = self._transcribe(st)
        if not any(who == "you" for _, who, _ in turns):
            # place() blocks until a "you" entry arrives, so there must be one.
            turns.append((float("inf"), "you", "(nothing transcribed from his side)"))
        for _, who, said in turns:
            if who == "you":
                self.deliver(said)
            else:
                self.note("claude", said)
        self.turns = len(turns)
        try:
            (self.record_dir / f"{stamp}-duplex.txt").write_text(
                "".join(f"{who}: {said}\n" for _, who, said in turns))
        except Exception:  # noqa: BLE001 -- stamp unset when the recording itself failed
            pass
        if self.ended == "not started":
            self.ended = "done"
        log.info("%s", summary)

    def _transcribe(self, stereo: np.ndarray | None) -> list[tuple[float, str, str]]:
        """Both sides of the recording, as time-ordered turns. After the model has
        exited, so Whisper gets the GPU PersonaPlex was holding.

        Falls back to the model's own text stream for its side if Whisper is not
        available at all: a worse transcript beats none.
        """
        model_text = [(i * 0.08, p) for i, p in self.engine.text]
        fallback = [(0.0, "claude", "".join(p for _, p in model_text).strip())] if model_text else []
        if stereo is None or stereo.size == 0:
            return fallback
        try:
            import soxr
            from faster_whisper import WhisperModel

            from .media.ears import preload_cuda

            try:
                preload_cuda()
                whisper = WhisperModel(os.environ.get("HOTLINE_IOS_DUPLEX_ASR", "large-v3"),
                                       device="cuda", compute_type="int8_float16")
            except Exception as exc:  # noqa: BLE001
                log.warning("duplex transcript: GPU whisper unavailable (%s); using CPU small", exc)
                whisper = WhisperModel("small", device="cpu", compute_type="int8")
            turns: list[tuple[float, str, str]] = []
            for channel, who in ((0, "you"), (1, "claude")):
                audio = soxr.resample(stereo[:, channel].astype(np.float32) / 32768.0, 8000, 16000)
                segments, _ = whisper.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                 condition_on_previous_text=False)
                for seg in segments:
                    text = seg.text.strip()
                    if text:
                        turns.append((float(seg.start), who, text))
            del whisper
            import gc

            gc.collect()
        except Exception:
            log.exception("duplex transcript failed; returning the model's text stream only")
            return fallback
        turns.sort(key=lambda t: t[0])
        merged: list[tuple[float, str, str]] = []
        for t, who, text in turns:
            if merged and merged[-1][1] == who:
                merged[-1] = (merged[-1][0], who, merged[-1][2] + " " + text)
            else:
                merged.append((t, who, text))
        return merged
