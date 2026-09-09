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
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np

from .media.voicecall import SdpError, VoiceCall, parse_sdp_answer

log = logging.getLogger("hotline-ios.conversation")

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
        greeting: str = "",
        deliver: Callable[[str], None] | None = None,
        ask: Callable[[str], str] | None = None,
        hung_up: Callable[[], bool] | None = None,
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
        # Spoken after the pre-rendered greeting: the question the ring was
        # placed to ask. Empty for a call that is just a conversation.
        self.greeting = greeting
        self.deliver = deliver
        self.ask = ask
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
                    outbound=call.recorded_outbound())
            log.info("call ended (%s) after %d turn(s): %s",
                     self.ended, self.turns, self.stats)

    # -- the conversation -------------------------------------------------

    def converse(self, call: VoiceCall, worker: ThreadPoolExecutor) -> None:
        call.send_silence(PRIME_SECONDS, calibrate=True)
        self.play(call, "greet", interruptible=True)
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

        def on_chunk(phrase: np.ndarray) -> None:
            captured.append(phrase)
            pending.append(worker.submit(self._transcribe, phrase))

        listened = 0.0
        why = "no-audio"
        while True:
            tail, why = call.receive_turn(max_seconds=self.turn_seconds, on_chunk=on_chunk)
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
