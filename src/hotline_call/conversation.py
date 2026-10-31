"""What to do once he actually picks up the phone.

`ring/sip.py` is a doorbell: it rings, and the ring is the message. This is the
other half -- the thing `SipTransport.on_answer` has a hook for, which the
daemon never passed.

**This is `server/talk.py`'s loop, generalised.** That script held real
conversations with him on 2026-09-08 -- *"Čujem te apsolutno, sve radi top"*,
913 frames out, 716 in, zero SRTP authentication failures -- and every rule in
here was paid for by a call that got it wrong first. It is not a second
implementation beside that one; `talk.py` calls this now.

The rules, and what each of them cost:

- **Never hang up on him.** His instruction, verbatim: wait for HIS hangup. A
  silent stretch is him thinking, not him leaving. An earlier version ended the
  call twice while he was still on it. Only a line with no RTP on it at all,
  for a long time, counts as gone.
- **`why == "timeout"` means he is still talking**, not that the turn is over.
  Ending there talks over him, which cut him off mid-sentence on the 19:10 call.
- **A farewell has to be at the END of what he said**, and *"ćao"* is not one:
  in Serbian it is a greeting at least as often, and he opened a call with
  *"Ćao brate"* to be hung up on mid-hello.
- **Anything he actually said gets answered**, whatever the turn ended on. He
  spoke three phrases and then the media went quiet; the dead-line check fired
  first, discarded all three, and he got no reply to something he had just said.
- **Speak from disk first.** `media/tts.py:Fillers` explains the seven seconds
  this saves.

**What degrades, and how.** No cvoiced means no voice, and a call that cannot
speak is worse than one that never connected -- so the handler gives up and
`_finish_answered` hangs up, and `place()` falls through to him answering in the
app exactly as before. No session leg means the call still takes his answer and
still delivers it. The one thing that must never happen is a call that holds his
phone and says nothing.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np

from .media.voicecall import SdpError, VoiceCall, parse_sdp_answer

log = logging.getLogger("hotline-call.conversation")

PRIME_SECONDS = 1.2
"""Silence before the first word, and the window barge-in is calibrated in.

Was 0.4. He heard a glitch at the start of the first live call, which was his
phone's jitter buffer not yet full, and 1.2 fixed it. `send_silence(calibrate=)`
also needs 25 frames -- 500 ms -- before it will trust a noise floor, and this
is the one moment in a call when whatever arrives is definitionally the line and
not either of us."""

MAX_CALL_SECONDS = 1800.0
"""A backstop, not a policy. He hangs the call up; this only stops a handler
that has lost track of that from holding his phone forever."""

DEAD_LINE_SECONDS = 90.0
"""No RTP at all for this long is the phone gone. Deliberately long: silence is
him thinking. A BYE, when we get one, ends the call immediately and this never
comes into it -- see `SipTransport.far_end_hung_up`."""

MAX_UTTERANCE_SECONDS = 180.0
"""How long one answer of his may run before we stop extending the turn."""

TURN_SECONDS = 30.0
AGENT_SLOW_AFTER = 3.0
FIRST_AUDIO_GRACE = 1.5
"""How long a streamed reply may take to its first audio before a holding
phrase goes out. Above the measured median (1.73 s under GPU contention,
~1.0 s expected without), because a filler blocks: one that starts just
before the real answer delays the answer by the filler's own length. So it
covers the slow tail, not the typical turn."""
"""When a second, apologetic filler goes out because the agent is still thinking."""

MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 240

FAREWELLS = (
    "prekini", "prekidam", "dovidjenja", "doviđenja", "cujemo se", "čujemo se",
    "prijatno", "zdravo i prijatno", "to je to", "hvala i prijatno",
)
"""**"ćao" is deliberately not here.** In Serbian it is a greeting at least as
often as a farewell, and it hung up on him mid-hello once already."""

FAREWELL_TAIL = 40
"""A farewell has to be near the END of what he said. "Reci mi kad završiš"
contains a farewell word and is not one."""


def is_farewell(text: str) -> bool:
    tail = text.lower()[-FAREWELL_TAIL:]
    return any(word in tail for word in FAREWELLS)


def sentences(text: str, *, minimum: int = MIN_CHUNK_CHARS,
              maximum: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split into speakable pieces, merging the short ones.

    Deliberately crude. The point is not correct segmentation, it is finding
    somewhere to break that does not land mid-word, so the pump has audio to
    send while the next piece is still being synthesised. Pieces shorter than
    `minimum` are merged: cvoiced has a fixed ~1.7 s floor per request, so ten
    tiny requests cost ten floors.
    """
    flat = " ".join(text.split())
    if not flat:
        return []
    chunks: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", flat):
        while len(part) > maximum:
            cut = part.rfind(" ", 0, maximum)
            if cut <= 0:
                cut = maximum
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if chunks and len(chunks[-1]) < minimum:
            chunks[-1] = f"{chunks[-1]} {part}".strip()
        elif part:
            chunks.append(part)
    return [chunk for chunk in chunks if chunk]


class Speculation:
    """An answer started at the first pause, held until the turn is known to be over.

    Produces into a queue exactly as `reply_streaming` consumes, so using it is
    handing the queue over; dropping it is setting `stop`. Audio is never sent
    from here -- nothing he hears can come from a turn he had not finished.
    """

    def __init__(self, text: str, ask_stream, speak_stream, commit) -> None:
        self.text = text
        self.commit = commit
        self.chunks: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.began = time.monotonic()
        self.thread = threading.Thread(
            target=_produce, args=(ask_stream, speak_stream, text, self.chunks, self.stop),
            daemon=True, name="call-speculate")
        self.thread.start()

    def drop(self) -> None:
        self.stop.set()


def _produce(ask_stream, speak_stream, said, chunks: queue.Queue, stop: threading.Event) -> None:
    """Agent tokens -> voice chunks -> `chunks`, until done, failed, or `stop`."""
    pieces = None
    try:
        pieces = speak_stream(ask_stream(said))
        for piece in pieces:
            chunks.put(("chunk", piece))
            if stop.is_set():
                break
        chunks.put(("done", None))
    except Exception as exc:
        if not stop.is_set():
            log.exception("the streaming leg failed")
        chunks.put(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        if pieces is not None:
            pieces.close()


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


class AnsweredCall:
    """One answered SIP call, from the 200 OK until one of us hangs up.

    Callable with exactly the arguments `SipTransport._finish_answered` passes,
    so installing it is an assignment and removing it is a deletion -- and a
    transport built without one still ACKs and hangs up, which `hotline-page`
    and the confirmed-ring path both depend on.
    """

    def __init__(
        self,
        *,
        speak: Callable[[str], tuple[np.ndarray, int]],
        transcribe: Callable[[np.ndarray], str],
        fillers: Any = None,
        opening: str = "greet",
        greeting: str = "",
        deliver: Callable[[str], None] | None = None,
        ask: Callable[[str], str] | None = None,
        ask_stream: Callable[[str], Iterator[str | None]] | None = None,
        speak_stream: Callable[[Iterable[str | None]], Iterator[tuple[np.ndarray, int, str]]] | None = None,
        ask_fork: Callable[[], tuple[Callable[[str], Iterator[str | None]], Callable[[], None]]] | None = None,
        hung_up: Callable[[], bool] | None = None,
        may_speculate: Callable[[str], bool] | None = None,
        stream_fixed_lines: bool = True,
        intent_filler: Callable[[str], str | None] | None = None,
        answer_seconds: Callable[[], float] | None = None,
        on_end: Callable[[str], None] | None = None,
        note: Callable[[str, str], None] | None = None,
        recorder: Any = None,
        model: str = "",
        turn_seconds: float = TURN_SECONDS,
        call_seconds: float = MAX_CALL_SECONDS,
        dead_line_seconds: float = DEAD_LINE_SECONDS,
        utterance_seconds: float = MAX_UTTERANCE_SECONDS,
    ) -> None:
        self.speak = speak
        self.transcribe = transcribe
        self.fillers = fillers
        # The pre-rendered clip that answers the pickup. Per call, because the
        # default one says "Zdravo Bogdane": on 2026-10-15 the first call to a
        # registry guest opened by greeting him as Bogdan.
        self.opening = opening
        # Spoken after the pre-rendered greeting: the question the ring was
        # placed to ask. Empty for a call that is just a conversation.
        self.greeting = greeting
        self.deliver = deliver
        self.ask = ask
        # Both or neither: a token stream with nothing to speak it, or a voice
        # stream with nothing feeding it, is just the slow path with extra steps.
        self.ask_stream = ask_stream if speak_stream is not None else None
        self.speak_stream = speak_stream
        # Speculation needs an agent it can throw away: `ask_fork()` returns a
        # copy's `ask_stream` and a `commit` that makes the copy's history the
        # real one. Without it there is no speculating, only the ordinary path.
        self.ask_fork = ask_fork if self.ask_stream is not None else None
        self._spec: Speculation | None = None
        self._spec_lock = threading.Lock()
        self._pause_gen = 0
        self.speculation = {"started": 0, "used": 0, "dropped": 0}
        # Asked before each speculation. The agent bridge says no while an
        # agent is on the line, or when the turn may be asking for one: a
        # speculative turn that reached an agent cannot be taken back.
        self.may_speculate = may_speculate
        self.stream_fixed_lines = stream_fixed_lines
        # His turn -> the name of a pre-rendered thinking sound ("Hm, pa...") to
        # play the instant he stops, while the answer is produced behind it.
        self.intent_filler = intent_filler
        # How long a streamed answer may take, asked per turn. `turn_seconds`
        # unless something says otherwise; a connected agent takes minutes.
        self.answer_seconds = answer_seconds
        # Called once with `ended` however the call ends, so whatever the call
        # borrowed (an interrupted agent) is always handed back.
        self.on_end = on_end
        self.hung_up = hung_up
        self.note = note or (lambda kind, text: None)
        # Off unless something handed one in. See `media/record.py` for why an
        # always-on recorder of his voice is not a default.
        self.recorder = recorder
        # What produced the live transcript, written beside it so a later
        # scorer knows which hypothesis it is comparing against.
        self.model = model
        self.recording = ""
        self._wire_at = 0
        self.turn_seconds = turn_seconds
        self.call_seconds = call_seconds
        self.dead_line_seconds = dead_line_seconds
        self.utterance_seconds = utterance_seconds
        # Filled in as the call runs, so a caller can report what happened
        # rather than that nothing raised.
        self.transcript: list[tuple[str, str]] = []
        self.stats: dict[str, int] = {}
        self.answered = ""
        self.turns = 0
        # Seconds from his turn ending to the first streamed audio, per turn.
        self.first_audio: list[float] = []
        self.ended = "not started"

    # -- the entry point --------------------------------------------------

    def __call__(self, reply: str, media_sock, srtp_key: bytes, srtp_salt: bytes) -> None:
        try:
            answer = parse_sdp_answer(reply)
        except SdpError as exc:
            # His client answered in a way we cannot carry audio over. Hang up
            # rather than hold the line: silence is the failure this exists to end.
            self.ended = f"no media: {exc}"
            log.error("answered call has no usable media: %s", exc)
            self.note("error", f"call answered but carried no media: {exc}")
            return
        if media_sock is None:
            self.ended = "no media socket"
            log.error("answered call has no media socket to speak on")
            return
        log.info("ANSWERED -- his media at %s:%d", answer.host, answer.port)
        call = VoiceCall(media_sock, answer, srtp_key, srtp_salt)
        if self.recorder is not None:
            call.record()
            self.recorder.rate, self.recorder.codec = call.rate, call.codec.describe()
        # One worker: faster-whisper is not thread safe for concurrent
        # transcribes on the same model, and serialising is fine because each is
        # far shorter than the speech still arriving behind it.
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        try:
            self.converse(call, worker)
        finally:
            worker.shutdown(wait=False)
            call.close()
            call.pump.join(timeout=1.0)
            self.stats = call.stats()
            if self.recorder is not None:
                self.recording = self.recorder.finish(
                    call.recorded(), self.stats, ended=self.ended,
                    outbound=call.recorded_outbound(),
                    inbound_at=call.inbound_clock())
            log.info("call ended (%s) after %d turn(s): %s",
                     self.ended, self.turns, self.stats)
            if self.on_end is not None:
                try:
                    self.on_end(self.ended)
                except Exception:
                    log.exception("the end-of-call hook failed")

    # -- the conversation -------------------------------------------------

    def converse(self, call: VoiceCall, worker: ThreadPoolExecutor) -> None:
        call.send_silence(PRIME_SECONDS, calibrate=True)
        if self.opening:
            self.play(call, self.opening, interruptible=True)
        if self.greeting:
            self.say(call, self.greeting)

        began = time.monotonic()
        dead_since: float | None = None
        while time.monotonic() - began < self.call_seconds:
            if self.gone():
                # NOT "he hung up". A BYE says the dialog ended at the far end,
                # which is not the same claim -- his phone will send one all by
                # itself if our ACK never reached it, and on 2026-09-10 that is
                # exactly what happened while he was still listening. The log
                # said he hung up on us, which would have told the next person
                # the rule was working. `SipTransport` owns the explanation and
                # says so loudly; this only reports what it can actually see.
                self.ended = "the far end ended the call"
                return
            said, captured, why = self.listen(call, worker)
            wire_end = call.recorded_frames

            if not captured and why == "no-audio":
                # No RTP at all and nothing captured: he hung up, or the media
                # died. Only a long stretch of it counts as over -- see
                # DEAD_LINE_SECONDS.
                dead_since = dead_since or time.monotonic()
                if time.monotonic() - dead_since > self.dead_line_seconds:
                    self.ended = "line went dead"
                    log.info("no media for %.0fs; he is gone", self.dead_line_seconds)
                    return
                continue
            dead_since = None

            if not said:
                # Two different situations that look identical from here, and
                # `receive_turn` is the only thing that can tell them apart.
                # "silence" means audio arrived and none of it was speech -- he
                # is on the line and not talking, so say NOTHING. Asking him to
                # repeat himself every few seconds while he thinks is worse than
                # dead air, and the loopback rehearsal did exactly that twice
                # into comfort noise. Only a turn he actually spoke into, which
                # then transcribed to nothing, is worth a "say that again".
                if why == "endpointed":
                    self.play(call, "notheard")
                else:
                    call.send_silence(1.0)
                continue

            self.turns += 1
            self.transcript.append(("you", said))
            log.info("turn %d (%s): %r", self.turns, why, said)
            if self.recorder is not None:
                self.recorder.turn(
                    self.turns, call.recorded(self._wire_at, wire_end), said,
                    model=self.model, reason=why,
                    outbound_span=(call.outbound_at(self._wire_at),
                                   call.outbound_at(max(0, wire_end - 1))))
            if call.his_level is None and captured:
                # The first thing he says is definitionally him -- he answered
                # the phone. Everything quieter or unlike it afterwards is the
                # room, and this is the reference that decides.
                call.enrol_voice(np.concatenate(captured))

            if is_farewell(said):
                self.hand_over(said)
                self.ended = "he said goodbye"
                self.play(call, "bye")
                return

            if self.turns == 1 and self.deliver is not None:
                # His answer to the question the ring was placed to ask. It goes
                # back to whoever is blocked on `hotline-call`, not to a session:
                # answering "da" at a fresh Claude answers the wrong thing.
                #
                # But he still gets a real answer out loud. The first version
                # played a one-word clip and went back to listening, and on the
                # live call that left him with "Dobro." and then sixteen seconds
                # of nothing -- he had answered the question and the phone went
                # quiet on him. The session is seeded with what the ring asked,
                # so it can acknowledge what he said rather than acknowledging
                # that something was said.
                self.hand_over(said)
                self.reply_to(call, said)
                continue

            self.reply_to(call, said)

        self.ended = "call length cap"
        self.play(call, "bye")

    def hand_over(self, said: str) -> None:
        """Give his words to whoever was waiting on this ring."""
        if self.deliver is None or self.answered:
            self.note("you", said)
            return
        self.answered = said
        try:
            self.deliver(said)
        except Exception:
            log.exception("could not deliver his answer to the waiting agent")

    def gone(self) -> bool:
        if self.hung_up is None:
            return False
        try:
            return bool(self.hung_up())
        except Exception:
            log.exception("hangup check raised; assuming the call is still up")
            return False

    # -- listening --------------------------------------------------------

    def listen(self, call: VoiceCall, worker: ThreadPoolExecutor
               ) -> tuple[str, list[np.ndarray], str]:
        """One turn of his: what he said, the audio, and why the turn ended.

        Phrases are transcribed as he finishes them rather than all at once at
        the end, on a worker thread so a slow model cannot delay the endpointer
        noticing that he has stopped. That moves nearly all of the recognition
        cost under his own speech.
        """
        pending: list = []
        captured: list[np.ndarray] = []
        # Where this turn starts in the recorded stream. A cursor into the
        # pump's own payload list rather than a timestamp, so the slice is exact.
        #
        # Wound BACK past whatever was retained while we were speaking, because
        # `receive_turn` begins with those frames and the recording has to be
        # what the model was actually given. Without this the benchmark audio was
        # missing up to a second of every overlapped turn -- the same words that
        # were missing from the transcript, which made it look like the model had
        # dropped them.
        self._wire_at = max(0, call.recorded_frames - len(call._pending_rx))

        # The pause transcription of the phrase in progress, if he has not
        # spoken since it was taken. At 350 ms the same phrase plus 200 ms of
        # silence is emitted as a chunk; reusing this result instead of running
        # Whisper on it again saves the GPU a pass and keeps the speculation's
        # text identical to the final one whenever he really had stopped.
        early: list = []
        self._drop_speculation()

        def on_chunk(phrase: np.ndarray) -> None:
            captured.append(phrase)
            pending.append(early.pop() if early else worker.submit(self._transcribe, phrase))

        def on_pause(phrase: np.ndarray) -> None:
            if self.ask_fork is None:
                return
            future = worker.submit(self._transcribe, phrase)
            early[:] = [future]
            with self._spec_lock:
                self._pause_gen += 1
                gen = self._pause_gen
            threading.Thread(target=self._speculate, args=(list(pending), future, gen),
                             daemon=True, name="call-early").start()

        def on_resume() -> None:
            early.clear()
            self._drop_speculation()

        listened = 0.0
        why = "no-audio"
        while True:
            tail, why = call.receive_turn(max_seconds=self.turn_seconds, on_chunk=on_chunk,
                                          on_pause=on_pause if self.ask_fork else None,
                                          on_resume=on_resume if self.ask_fork else None)
            if tail.size:
                captured.append(tail)
                pending.append(worker.submit(self._transcribe, tail))
            listened += self.turn_seconds
            # "timeout" means the cap hit while he was STILL TALKING. Ending the
            # turn there is talking over him.
            if why == "timeout" and listened < self.utterance_seconds:
                log.info("still going at %.0fs; keeping the line open", listened)
                continue
            break

        said = " ".join(future.result() for future in pending).strip()
        # Everything he said was noise or an invention. Do not hand that on: it
        # answers a question he never asked.
        return (said if len(said) >= 3 else ""), captured, why

    def _speculate(self, before: list, future, gen: int) -> None:
        """Once the pause transcription is in, start answering it - unless he
        has spoken again meanwhile, which moves `_pause_gen` on."""
        try:
            text = " ".join(f.result() for f in [*before, future]).strip()
        except Exception:
            return
        if len(text) < 3 or is_farewell(text) or gen != self._pause_gen:
            return
        if self.may_speculate is not None and not self.may_speculate(text):
            return
        ask_stream, commit = self.ask_fork()
        spec = Speculation(text, ask_stream, self.speak_stream, commit)
        with self._spec_lock:
            if gen != self._pause_gen:      # he resumed while it was starting
                spec.drop()
                return
            old, self._spec = self._spec, spec
        if old is not None:
            old.drop()
        self.speculation["started"] += 1
        log.info("speculating on %r", text[-80:])

    def _drop_speculation(self) -> None:
        with self._spec_lock:
            self._pause_gen += 1
            spec, self._spec = self._spec, None
        if spec is not None:
            spec.drop()
            self.speculation["dropped"] += 1

    def _transcribe(self, audio: np.ndarray) -> str:
        try:
            return self.transcribe(audio).strip()
        except Exception:
            log.exception("transcription failed; that phrase is lost")
            return ""

    # -- answering --------------------------------------------------------

    def reply_to(self, call: VoiceCall, said: str) -> None:
        """Hand a spoken turn to the session, keeping the line alive while it works.

        The agent runs on its own thread so nothing here goes quiet: a holding
        phrase goes out at once from disk, and a second, apologetic one if it is
        slow. A holding phrase asserts nothing, so unlike a speculative *answer*
        it cannot be contradicted by the rest of his sentence.
        """
        if self.ask_stream is not None:
            self.reply_streaming(call, said)
            return
        ask = self.ask
        if ask is None:
            self.play(call, "ack_mhm")
            return
        result: queue.Queue = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                result.put(("ok", ask(said)))
            except Exception as exc:
                log.exception("the session leg failed")
                result.put(("err", f"{type(exc).__name__}: {exc}"))

        thread = threading.Thread(target=work, daemon=True, name="call-agent")
        thread.start()
        self.play(call, "hold_nejasno_2")

        began = time.monotonic()
        nudged = False
        while thread.is_alive() and time.monotonic() - began < self.turn_seconds:
            if not nudged and time.monotonic() - began > AGENT_SLOW_AFTER:
                nudged = True
                self.play(call, "wait_proveri")
            else:
                call.send_silence(0.2)
        try:
            status, text = result.get(timeout=1.0)
        except queue.Empty:
            status, text = "err", "the session did not answer in time"
        if status != "ok" or not text.strip():
            self.note("error", text)
            text = "Izvini, nešto mi se zaglavilo. Pokušaj ponovo."
        else:
            self.note("claude", text)
        self.say(call, text)

    def reply_streaming(self, call: VoiceCall, said: str) -> None:
        """Answer while the answer is still being written.

        The agent's tokens go straight into cvoiced, and each clause is played
        the moment it is rendered. The first one arrives ~1-1.7 s after `said`
        (cvoice `bench/haiku_ws_bench.py`, 2026-10-30), so the holding phrase
        goes out only if it has NOT arrived within `FIRST_AUDIO_GRACE`.

        Barge-in closes the stream, which closes cvoiced's socket (it stops
        rendering) and the agent's HTTP response (it stops generating, and its
        history keeps only what was actually produced).
        """
        assert self.ask_stream is not None and self.speak_stream is not None
        with self._spec_lock:
            spec, self._spec = self._spec, None
        commit = None
        if spec is not None and _norm(spec.text) == _norm(said):
            self.speculation["used"] += 1
            log.info("speculation used: started %.2f s before the turn ended",
                     time.monotonic() - spec.began)
            chunks, stop, commit = spec.chunks, spec.stop, spec.commit
        else:
            if spec is not None:
                spec.drop()
                self.speculation["dropped"] += 1
                log.info("speculation dropped: %r is not %r", spec.text[-60:], said[-60:])
            chunks = queue.Queue()
            stop = threading.Event()
            threading.Thread(target=_produce, args=(self.ask_stream, self.speak_stream, said, chunks, stop),
                             daemon=True, name="call-stream").start()
        began = time.monotonic()
        spoken: list[str] = []
        held = nudged = False
        clip = None
        if self.intent_filler is not None and self.fillers is not None:
            try:
                clip = self.intent_filler(said)
            except Exception:
                log.exception("intent filler failed; answering without one")
        if clip and clip in self.fillers:
            # Blocking on purpose: it is a word, and half a word is worse than none.
            # The answer keeps being produced behind it.
            log.info("intent filler %s for %r", clip, said[-60:])
            self.play(call, clip)
            held = True
        elif self.intent_filler is not None:
            log.info("no intent filler for %r (picked %r)", said[-60:], clip)
        limit = self.turn_seconds
        if self.answer_seconds is not None:
            try:
                limit = float(self.answer_seconds())
            except Exception:
                log.exception("answer_seconds raised; using %.0f s", limit)
        while True:
            waited = time.monotonic() - began
            try:
                kind, item = chunks.get(timeout=0.05)
            except queue.Empty:
                if waited > limit:
                    kind, item = "err", "the stream did not answer in time"
                elif spoken:
                    continue
                elif not held and waited > FIRST_AUDIO_GRACE:
                    held = True
                    self.play(call, "hold_nejasno_2")
                    continue
                elif not nudged and waited > AGENT_SLOW_AFTER:
                    nudged = True
                    self.play(call, "wait_proveri")
                    continue
                else:
                    continue
            if kind == "done":
                break
            if kind == "err":
                stop.set()
                self.note("error", str(item))
                if not spoken:
                    self.say(call, "Izvini, nešto mi se zaglavilo. Pokušaj ponovo.")
                break
            audio, rate, text = item
            if not spoken:
                log.info("first streamed audio %.2f s after he stopped", waited)
                self.first_audio.append(round(waited, 3))
            if audio.size:
                call.send_audio(audio, rate, interruptible=True, flush=not spoken)
            spoken.append(text)
            if call.interrupted:
                log.info("he cut in %d streamed chunk(s) in", len(spoken))
                stop.set()
                break
        if commit is not None:
            # After the speculative stream has finished or been stopped, so the
            # copy's history holds the whole turn - or only what was said of it.
            spec.thread.join(timeout=5.0)
            commit()
        if spoken:
            text = " ".join(spoken)
            self.note("claude", text)
            self.transcript.append(("claude", text))

    # -- speaking ---------------------------------------------------------

    def play(self, call: VoiceCall, name: str, *, interruptible: bool = False) -> None:
        """A pre-rendered phrase, straight from disk.

        Fillers go out whole by default: a half-spoken "mhm" is worse than none,
        and only a real answer is worth interrupting.
        """
        clip = self.fillers.get(name) if self.fillers is not None else None
        if clip is None:
            return
        audio, rate = clip
        call.send_audio(audio, rate, interruptible=interruptible)
        text = getattr(self.fillers, "texts", {}).get(name, name)
        self.transcript.append(("claude", text))

    def say(self, call: VoiceCall, text: str) -> None:
        if self.speak_stream is not None and self.stream_fixed_lines:
            self._say_streamed(call, text)
            return
        self._say_whole(call, text)

    def _say_streamed(self, call: VoiceCall, text: str) -> None:
        """Fixed text through the streaming voice, so it gets the same seams and
        room tone as an answer. The whole-clip path pads each piece with digital
        silence, which on the 2026-10-31 call was the only silence left in it."""
        first = True
        try:
            for audio, rate, _piece in self.speak_stream(iter([text, None])):
                if audio.size:
                    call.send_audio(audio, rate, interruptible=True, flush=first)
                first = False
                if call.interrupted:
                    break
        except Exception:
            log.exception("streamed speech failed; saying it the old way")
            if first:
                self._say_whole(call, text)
                return
        self.transcript.append(("claude", text))

    def _say_whole(self, call: VoiceCall, text: str) -> None:
        """Speak, sentence by sentence, synthesising the next while this one plays.

        cvoiced runs at roughly 0.6x realtime, so the piece after the one being
        sent is always ready before the pump runs out of audio. Synthesising the
        whole answer first puts the entire generation time into silence at the
        front of every reply.
        """
        chunks = sentences(text)
        if not chunks:
            return
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts") as pool:
            ahead: Future | None = pool.submit(self.speak, chunks[0])
            for index in range(len(chunks)):
                assert ahead is not None
                try:
                    audio, rate = ahead.result()
                except Exception:
                    log.exception("synthesis failed; the rest of this line is lost")
                    return
                ahead = (pool.submit(self.speak, chunks[index + 1])
                         if index + 1 < len(chunks) else None)
                if audio.size:
                    # Flush before the first piece only. Flushing before each
                    # one deletes whatever he said during the piece before it,
                    # which on a three-sentence answer is most of his reply.
                    call.send_audio(audio, rate, interruptible=True,
                                    flush=(index == 0))
                if call.interrupted:
                    log.info("he cut in %d chunk(s) into %d", index + 1, len(chunks))
                    if ahead is not None:
                        ahead.cancel()
                    break
        self.transcript.append(("claude", text))
