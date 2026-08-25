"""One live call, independent of how it rang and how the audio arrives.

This is `hotline.voice.VoiceCall` with Discord taken out. It is written as a
sibling rather than a subclass on purpose: two thirds of `VoiceCall` is
`discord.VoiceClient` lifecycle, a `discord.AudioSource` subclass and a pile of
monkeypatches for py-cord receive bugs. None of that survives contact with a
different transport, and inheriting it would drag py-cord in as a hard
dependency of a service that must run when Discord is down.

What is reused, unmodified and by import, is everything that is genuinely
transport-independent:

  hotline.audio.Segmenter/Transcriber/Speaker   silero VAD, whisper, piper
  hotline.pool.SessionPool.ask                  routing, stand-ins, narration
  hotline.voice.speakable                       markdown -> speech
  hotline.provenance.Origin                     how a turn labels itself

`SPEC.md` §4 says do not fork that pipeline. This does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .media.pcm import to_model
from .ring.base import CallTarget, MediaStream

log = logging.getLogger("hotline-ios.call")

NARRATE_AFTER = 3.0
"""Seconds before a turn is slow enough to be worth narrating. Same value as
hotline's voice path, and for the same reason: narrating a turn that is about to
finish just talks over the answer."""

NARRATE_EVERY = 4.0
BARGE_IN_RMS = 0.02
BARGE_IN_MIN_PENDING = 0.2
TURN_TIMEOUT = 900.0


@dataclass
class TurnEvent:
    """Something worth showing on the phone's screen mid-call.

    `SPEC.md` §5 asks for 'what tool Claude is running right now' as a visual,
    not only as speech. hotline already produces these; all that is missing is a
    second consumer. Kept as a plain dataclass so the websocket layer can encode
    it without importing hotline's internals.
    """

    kind: str  # "heard" | "tool" | "summary" | "said" | "state" | "error"
    text: str
    tool: str | None = None
    at: float = field(default_factory=time.time)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "text": self.text, "at": self.at}
        if self.tool:
            out["tool"] = self.tool
        return out


class CallSession:
    """Drive one answered call to completion.

    The transport hands over a `MediaStream` and this owns everything after
    that: segmentation, transcription, one-turn-at-a-time routing into a Claude
    session, narration, synthesis and barge-in.
    """

    def __init__(
        self,
        pool: Any,
        transcriber: Any,
        speaker: Any,
        stream: MediaStream,
        target: CallTarget,
        key: str,
        *,
        segmenter_factory: Callable[[], Any] | None = None,
        speakable: Callable[[str], str] | None = None,
        origin_factory: Callable[[], Any] | None = None,
        on_event: Callable[[TurnEvent], None] | None = None,
    ) -> None:
        self.pool = pool
        self.transcriber = transcriber
        self.speaker = speaker
        self.stream = stream
        self.target = target
        self.key = key
        self.transcript: list[tuple[str, str]] = []
        self.events: list[TurnEvent] = []
        self._on_event = on_event
        self._speakable = speakable or (lambda text: text)
        self._origin_factory = origin_factory
        self._segmenter_factory = segmenter_factory or _default_segmenter
        self._segmenter: Any | None = None
        self._busy = False
        self._tasks: set[asyncio.Task[None]] = set()
        self.ended = asyncio.Event()

    # ---- events ----------------------------------------------------------

    def emit(self, kind: str, text: str, tool: str | None = None) -> None:
        event = TurnEvent(kind=kind, text=text, tool=tool)
        self.events.append(event)
        if self._on_event is not None:
            # A broken subscriber must not take the call down with it. The phone
            # losing its transcript view is a cosmetic failure; the call
            # dropping because of it is not.
            try:
                self._on_event(event)
            except Exception:
                log.exception("call event subscriber failed")

    # ---- the loop --------------------------------------------------------

    async def run(self) -> None:
        """Consume inbound audio until the far end hangs up."""
        self.emit("state", "answered")
        try:
            await self._consume()
        except asyncio.CancelledError:
            raise
        except Exception:
            # hotline learned this the hard way: a bare create_task swallows the
            # exception until garbage collection, and a dead consumer is
            # indistinguishable from "the audio never arrived".
            log.exception("call consumer died")
            raise
        finally:
            for task in list(self._tasks):
                task.cancel()
            for task in list(self._tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self.ended.set()
            self.emit("state", "ended")

    async def _consume(self) -> None:
        fmt = self.stream.format
        while True:
            pcm = await self.stream.recv()
            if pcm is None:
                return
            mono = to_model(pcm, fmt.rate, fmt.channels)
            if mono.size == 0:
                continue

            # Barge-in before segmentation completes, not after: waiting for a
            # finished utterance means talking over the whole interruption.
            if self.stream.pending_seconds > BARGE_IN_MIN_PENDING and _is_speech(mono):
                dropped = self.stream.clear()
                if dropped:
                    log.info("barge-in: dropped %d bytes of queued speech", dropped)

            if self._segmenter is None:
                self._segmenter = self._segmenter_factory()
            for utterance in self._segmenter.feed(mono):
                task = asyncio.create_task(self._turn(utterance.audio, utterance.seconds))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    # ---- one turn --------------------------------------------------------

    async def _turn(self, audio: np.ndarray, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        began = time.monotonic()
        text = (await loop.run_in_executor(None, self.transcriber.transcribe, audio)).strip()
        if len(text) < 2:
            return
        self.transcript.append(("you", text))
        self.emit("heard", text)
        log.info("heard (%.1fs, stt %.2fs): %r", seconds, time.monotonic() - began, text)

        if self._busy:
            # Discard rather than queue: by the time the first turn finishes a
            # spoken follow-up is usually stale, and answering it late is worse
            # than not answering it.
            await self.say("Hang on, still working on the last one.")
            return

        self._busy = True
        started = time.monotonic()
        last_narration = [started]

        def narrate(event: Any) -> None:
            kind = getattr(event, "kind", "")
            if kind not in ("tool", "summary"):
                return
            detail = getattr(event, "detail", "")
            tool = getattr(event, "tool", None)
            now = time.monotonic()
            # The phone gets every event -- a screen can show a list. Only the
            # throttled subset is spoken, because speech is serial and the
            # narration would otherwise outrun the answer it is covering for.
            self.emit(kind, detail, tool)
            if now - started < NARRATE_AFTER or now - last_narration[0] < NARRATE_EVERY:
                return
            last_narration[0] = now
            asyncio.run_coroutine_threadsafe(self.say(detail), loop)

        try:
            kwargs: dict[str, Any] = {"narrator": narrate, "timeout": TURN_TIMEOUT}
            if self._origin_factory is not None:
                kwargs["origin"] = self._origin_factory()
            _route, reply = await self.pool.ask(self.key, text, **kwargs)
            answer = reply.text
            if getattr(reply, "notice", ""):
                answer = f"Heads up, {reply.notice}. {answer}"
        except Exception as exc:
            log.exception("call turn failed")
            answer = f"Something broke on my side. {type(exc).__name__}."
            self.emit("error", str(exc))
        finally:
            self._busy = False

        self.transcript.append(("claude", answer))
        self.emit("said", answer)
        log.info("answered in %.1fs (%d chars)", time.monotonic() - started, len(answer))
        await self.say(self._speakable(answer))

    # ---- speaking --------------------------------------------------------

    async def say(self, text: str) -> None:
        if not text.strip():
            return
        loop = asyncio.get_running_loop()
        fmt = self.stream.format
        pcm = await loop.run_in_executor(None, self._synthesize, text, fmt)
        self.stream.send(pcm)

    def _synthesize(self, text: str, fmt: Any) -> bytes:
        from .media.pcm import from_model

        audio = self.speaker.synthesize(text)
        return from_model(audio, self.speaker.rate, fmt.rate, fmt.channels)

    async def hangup(self) -> None:
        await self.stream.close()
        self.ended.set()


def _is_speech(mono: np.ndarray) -> bool:
    """Cheap energy gate for barge-in only.

    Deliberately not the VAD: this runs on every inbound frame, and being
    slightly wrong costs an unnecessary pause rather than a wrong answer. Copied
    in spirit from hotline's `VoiceCall._is_speech` for exactly that reasoning.
    """
    if mono.size == 0:
        return False
    return bool(np.sqrt(np.mean(mono.astype(np.float64) ** 2)) > BARGE_IN_RMS)


def _default_segmenter() -> Any:
    from hotline.audio import Segmenter

    return Segmenter()
