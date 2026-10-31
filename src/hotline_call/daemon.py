"""`hotline-calld` -- the service a Claude session calls to ring a phone.

It owns everything about a phone call: the doorbell (`ring/`), the voice on an
answered call (`conversation.py`, `haikuagent.py`, `duplexcall.py`), the models
that voice needs, and the inbound line he can dial (`ring/inbound.py`). Agents
reach it through `hotline-call`, which posts `/api/v1/call` here.

**Why this is its own daemon and not part of hotline-iosd.** It was, until
2026-10-31, when Bogdan moved call semantics out of the iOS app's server
(Discord 1566035363657420962). The iOS app's backbone -- the roster, the agent
channels, the store, the delegation path -- answers questions about agents and
the app; this answers questions about a phone call. They fail differently (a
GPU that will not hand over, a SIP registrar that refuses us) and they restart
for different reasons.

**What it still needs from hotline-ios.** The iPhone app shows every ring as a
conversation he can answer by typing. That conversation lives in hotline-iosd's
store, so this daemon mirrors each call into it (`appmirror.py`) and hotline-iosd
forwards his typed answer back here (`/api/v1/call/reply`). The mirror is best
effort: with hotline-iosd down a call still rings, still talks, and still hands
the caller his spoken answer -- it just does not show up in the app.

It still reuses hotline's `httpd.Server` -- roughly a hundred lines Bogdan has
already read -- rather than adding a web framework, and gates on the same
source-IP allowlist and key that `hotlined` and hotline-iosd use.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .calllog import CallLog
from .ring.base import (
    CallDeclined,
    CallError,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
)

log = logging.getLogger("hotline-calld")

DEFAULT_PORT = 8792
"""Beside hotlined's 8788 and hotline-iosd's 8789. 8790 and 8791 are taken on
the tailnet address (the .ipa beam and classwatch), so this skips them."""

LOOPBACK = "127.0.0.1"
"""Always bound: hotline-call, hotline-iosd's proxy and every local agent use it."""

DEFAULT_HOST = "100.72.2.62"
"""The tailnet address, so an agent on another box can ring him directly.
Overridable with HOTLINE_CALL_HOST; `--host 127.0.0.1` binds loopback alone."""

PING_PATH = "/api/v1/ping"

REAP_AFTER = 3600.0
"""How long a finished call's log is kept in memory. hotline-ios holds the
durable copy; this only has to outlive a caller reading its own outcome."""


class _Whole:
    """An agent whose `reply` is a whole string - what the non-streamed path wants."""

    def __init__(self, reply) -> None:
        self.reply = reply


class Service:
    def __init__(
        self,
        transport: Any,
        *,
        transcriber: Any = None,
        speaker: Any = None,
        allow_ips: set[str] | None = None,
        api_key: str = "",
        fillers: Any = None,
        mirror: Any = None,
        default_engine: str = "",
        gpu_handoff: Any = None,
        gpu_wait_s: float = 180.0,
    ) -> None:
        self.transport = transport
        # Who talks on a call to HIM when the caller does not say: "" is the Serbian
        # ASR + Claude + TTS loop, "personaplex" the English duplex model. A call to
        # any other SIP address (a registry guest, in Serbian) never takes this default.
        # Empty here so a Service built in a test never touches the GPU or cvoiced;
        # main() sets it from HOTLINE_IOS_DEFAULT_ENGINE.
        self.default_engine = default_engine
        # () -> gpu_handoff.GpuHandoff, or None to leave ollama and cvoiced alone
        self.gpu_handoff = gpu_handoff
        # Held by a PersonaPlex call from handing the GPU over to handing it back.
        # Any call placed meanwhile waits for it: a second PersonaPlex would not fit
        # on the card, and a classic call would have no voice while cvoiced is down.
        # Classic calls only wait; they do not hold it, so they still overlap freely.
        self._gpu_lock = asyncio.Lock()
        self.gpu_wait_s = gpu_wait_s
        self.transcriber = transcriber
        self.speaker = speaker
        # How many calls currently want the models up. Not a bool: two agents
        # can ring him in the same minute, and the first to hang up must not
        # unload `large-v3` out from under the second one mid-sentence.
        self._calls_holding_models = 0
        self._models_lock = threading.Lock()
        self.allow_ips = allow_ips or set()
        self.api_key = api_key
        # Pre-rendered Serbian holding phrases. None is a working call with more
        # dead air in it, never a broken one -- see `media/tts.py:Fillers`.
        self.fillers = fillers
        # `appmirror.AppMirror`, or None to keep calls out of the app entirely
        # (the tests, and a box with no hotline-ios).
        self.mirror = mirror
        # conversation -> its CallLog. The call's own record; see calllog.py.
        self.calls: dict[str, CallLog] = {}
        self.call_opened: dict[str, float] = {}
        # name -> transport, so a caller's --transport can be honoured or
        # refused rather than silently ignored. Populated by set_links(); a
        # Service built directly in a test just has the one default.
        self.links: dict[str, Any] = {}
        self.started = time.time()
        # False until the doorbell's start() has succeeded. A transport that
        # could not start cannot ring, and /health reporting ok:true in that
        # state is the same lie as reporting ok:true on a loopback.
        self.ring_ready = False
        self.degradations: list[str] = []
        self.listen_hosts: list[str] = []
        self.listen_port = DEFAULT_PORT
        # The running loop, for `inbound_call` on the SIP thread. Set in main().
        self.loop: asyncio.AbstractEventLoop | None = None
        # `ring.inbound.SipInbound` when he can call us, else None.
        self.inbound: Any = None

    def bound_to(self, hosts: Sequence[str], port: int) -> None:
        self.listen_hosts = list(hosts)
        self.listen_port = int(port)

    # ---- the call's own record, mirrored into the app ---------------------

    def _open_conversation(self, agent: str | None, kind: str) -> tuple[str, CallLog]:
        """Mint a conversation for one call and tell the app about it.

        `kind` is `"ring"` (an agent waiting on his answer, which the app pins
        as blocked) or `"say"` (he dialled us). The id is minted here rather
        than by hotline-iosd so the call has one name on both sides even when
        the app is down for the open and back by the hang-up.
        """
        conversation = uuid.uuid4().hex[:12]
        events = CallLog()
        self.calls[conversation] = events
        self.call_opened[conversation] = time.time()
        if self.mirror is not None:
            self.mirror.open(conversation, agent, kind)
        return conversation, events

    def _append(self, conversation: str, kind: str, text: str) -> Any:
        """Record one line of a call, here and in the app. Event-loop only."""
        events = self.calls.get(conversation)
        line = events.append(kind, text) if events is not None else None
        if self.mirror is not None:
            self.mirror.append(conversation, kind, text)
        return line

    def _close_conversation(self, conversation: str, *, mirror: bool = True) -> None:
        events = self.calls.get(conversation)
        if events is not None and not events.closed:
            events.close()
        if mirror and self.mirror is not None:
            self.mirror.close(conversation)

    # ---- auth ------------------------------------------------------------

    def authorise(self, request: Any) -> None:
        from hotline.httpd import HttpError

        peer = getattr(request, "peer", "")
        # Loopback is always allowed: hotline-call runs on this box, and a
        # blocked agent must not also be locked out of the phone.
        if self.allow_ips and peer not in self.allow_ips and peer not in ("127.0.0.1", "::1"):
            raise HttpError(403, f"{peer} is not on the allowlist")
        if self.api_key:
            supplied = request.headers.get("x-hotline-key", "")
            if supplied != self.api_key:
                raise HttpError(401, "bad or missing X-Hotline-Key")

    # ---- ringing him ------------------------------------------------------

    def set_links(self, links: dict[str, Any]) -> None:
        """Register the individually-addressable doorbells."""
        self.links = dict(links)

    def open_calls(self) -> int:
        """Calls still awaiting an answer or still on the line."""
        return sum(1 for events in self.calls.values() if not events.closed)

    def reap(self, *, older_than: float = REAP_AFTER) -> int:
        """Forget finished calls after an hour. hotline-ios keeps the record."""
        now = time.time()
        stale = [
            call_id
            for call_id, events in self.calls.items()
            if events.closed and now - self.call_opened.get(call_id, now) > older_than
        ]
        for call_id in stale:
            self.calls.pop(call_id, None)
            self.call_opened.pop(call_id, None)
        return len(stale)

    def _resolve_transport(self, requested: str) -> Any:
        """Pick the doorbell for one call, or refuse.

        `--transport sip` used to reach the request body and go no further --
        `place()` read `self.transport` unconditionally. A flag that silently
        does nothing is worse than no flag at all, because it is reached for
        exactly when the configured doorbell is already suspect. Now it either
        works or it is a 400.
        """
        requested = (requested or "").strip().lower()
        if not requested or requested == "auto":
            return self.transport
        if requested in self.links:
            return self.links[requested]
        from hotline.httpd import HttpError

        known = ", ".join(sorted(self.links)) or "none"
        raise HttpError(400, f"unknown transport {requested!r}; this daemon has: {known}")

    # ---- giving an answered ring a voice ---------------------------------

    def _warm_models(self) -> None:
        """Load what an answered call needs, in the background, once.

        Called as the phone starts ringing rather than at startup. His rule,
        2026-09-11: nothing is loaded until a call needs it, and everything is
        dropped when the call ends.

        **Why this runs beside the ring instead of before it.** His words were
        "everything needed gets loaded and then the call made", and taken
        literally that delays the INVITE by the load. Measured on this box the
        same day: `large-v3` is 3.5 s and cvoiced is ~7.6 s, so a literal
        reading makes an urgent call ring about eight seconds late -- against a
        project whose whole speculative-input effort exists to save three to
        five. The SIP ring is dead time that already absorbs a 4.7 s CallAgent
        seed for exactly this reason, so the load hides there instead. If he
        picks up before it finishes, the first `synthesize` waits rather than
        the doorbell.

        Failures are logged and swallowed: a model that will not load must cost
        the voice leg, never the ring. That is the same rule `build_voice`
        follows and the reason a mute call beats a silent one.
        """
        def work() -> None:
            for name, thing in (("ears", self.transcriber), ("voice", self.speaker)):
                loader = getattr(thing, "load", None)
                if loader is None:
                    continue
                try:
                    began = time.monotonic()
                    loader()
                    log.info("warmed %s in %.1fs", name, time.monotonic() - began)
                except Exception:
                    log.exception("could not warm %s; the call may be mute", name)

        with self._models_lock:
            self._calls_holding_models += 1
            if self._calls_holding_models > 1:
                # Another call already has them up. Loading is idempotent, but
                # a second thread would serialise behind the first one's lock
                # for no gain.
                return
        threading.Thread(target=work, name="warm-models", daemon=True).start()

    def _cool_models(self) -> None:
        """Drop the models once the LAST call using them has ended.

        Refcounted because two agents can ring him in the same minute -- the
        case `_voice_leg` already exists to handle. Unloading on the first call
        to finish would pull `large-v3` out from under the second one mid-
        sentence.

        Runs on a thread: `Voice.unload` is an HTTP round trip to cvoiced and
        this is called from the event loop's `finally`, where blocking would
        stall every other conversation the daemon is holding.
        """
        with self._models_lock:
            self._calls_holding_models = max(0, self._calls_holding_models - 1)
            if self._calls_holding_models:
                return

        # The voice stays loaded unless asked otherwise. cvoiced leaks ~780 MB of
        # VRAM on every /unload + /load cycle (measured 2026-10-31: 2488 -> 3270
        # -> 4034 -> 4796 MiB after each unload), and one cycle per call filled
        # the 8 GB card within a few calls: CUDA OOM, no greeting, and every
        # phrase transcribed to nothing ("nisam te čuo" on a loop). A resident
        # voice costs ~2 GB and never grows.
        keep_voice = os.environ.get("HOTLINE_CALL_UNLOAD_VOICE", "0") != "1"
        things = [("ears", self.transcriber)] + ([] if keep_voice else [("voice", self.speaker)])

        def work() -> None:
            for name, thing in things:
                unloader = getattr(thing, "unload", None)
                if unloader is None:
                    continue
                try:
                    unloader()
                except Exception:
                    log.exception("could not unload %s; its memory stays held", name)

        threading.Thread(target=work, name="cool-models", daemon=True).start()

    @contextlib.contextmanager
    def _voice_leg(self, doorbell: Any, target: CallTarget, conversation: str,
                   engine: str = "", prepared: Any = None) -> Any:
        """Give this one ring a voice, and take it away again afterwards.

        Installed per ring rather than once at construction -- which is where
        `SipTransport(on_answer=...)` makes it look like it belongs -- because
        the handler has to know *which* conversation it is answering, and
        `place()` is the only place that knows. A handler built at startup would
        have to guess, and the guess would be wrong the moment two agents ring
        him in the same minute.

        Taking it away again is not tidiness. A `SipTransport` with no handler
        ACKs the 200 and hangs up at once, and `hotline-page` and the
        confirmed-ring path both depend on that: a ring that holds a call it has
        nothing to say on is worse than one that does not.
        """
        if engine == "personaplex":
            yield from self._duplex_leg(doorbell, prepared)
            return
        # Nothing between here and the yield may raise. This runs inside
        # `place()`'s try, whose except clauses are the ring outcomes -- so an
        # exception escaping here is not a silent call, it is a 500 and NO ring
        # at all, which is worse than the bug this whole path fixes.
        try:
            links = holds_calls(doorbell) if self.can_talk else []
            handlers = [self._answered_call(target, conversation, link, engine) for link in links]
        except Exception:
            log.exception("could not build the answered-call handler; ringing anyway")
            yield None
            return
        if not links:
            # Nothing can hold an answered call, so nothing will need a model.
            yield None
            return
        # From here there IS a voice leg, so the models are wanted. Started now,
        # while it rings; released in the `finally` below, however it ends.
        self._warm_models()
        restore = [(link, link.on_answer) for link in links]
        for link, handler in zip(links, handlers, strict=True):
            link.on_answer = handler
        try:
            yield handlers[0] if len(handlers) == 1 else _the_one_that_ran(handlers)
        finally:
            # Unconditionally, however the ring ended. A handler left installed
            # would make the NEXT ring -- possibly hotline-page's -- hold a call
            # it has nothing to say on and answer it with a stale question.
            for link, was in restore:
                link.on_answer = was
            # Same scope, same rule. `on_answer` runs inside `doorbell.ring()`,
            # so by the time this block runs the call is over -- declined,
            # unanswered, or hung up -- and nothing needs the GPU any more.
            self._cool_models()

    def _prepare_duplex(self, doorbell: Any, target: CallTarget, conversation: str,
                        loop: Any) -> tuple[list[Any], list[Any], str]:
        """Start PersonaPlex for every link that can hold the call and wait until it
        is READY. Runs in an executor, BEFORE the ring: if the model cannot start,
        the call fails without his phone ever ringing.

        Deliberately does NOT warm Whisper or cvoiced, and does not need
        `can_talk`: PersonaPlex alone takes up to ~7.3 GiB of the 8 GiB card, so
        the default call models must stay unloaded for it to fit at all.
        """
        from .duplexcall import DuplexCall, brief_prompt

        def append(kind: str, text: str) -> None:
            loop.call_soon_threadsafe(self._append, conversation, kind, text)

        links = holds_calls(doorbell)
        if not links:
            return [], [], "no transport here can hold an answered call"
        prompt = brief_prompt(target.reason, target.context, target.caller_id)
        handlers = [DuplexCall(prompt=prompt, note=append,
                               deliver=lambda text: append("you", text),
                               hung_up=getattr(link, "far_end_hung_up", None),
                               label=f"{target.caller_id}: {target.reason}")
                    for link in links]
        for handler in handlers:
            if not handler.wait_ready(90.0):
                why = handler.setup_error or "timed out waiting for the model"
                for h in handlers:
                    h.close()
                return [], [], f"personaplex did not start: {why}"
        return links, handlers, ""

    def _duplex_leg(self, doorbell: Any, prepared: Any) -> Any:
        """`_voice_leg` for `engine=personaplex`, with handlers `_prepare_duplex` built."""
        links, handlers = prepared or ([], [])
        if not links:
            yield None
            return
        restore = [(link, link.on_answer) for link in links]
        for link, handler in zip(links, handlers, strict=True):
            link.on_answer = handler
        try:
            yield handlers[0] if len(handlers) == 1 else _the_one_that_ran(handlers)
        finally:
            for link, was in restore:
                link.on_answer = was
            for handler in handlers:
                handler.close()

    @property
    def can_talk(self) -> bool:
        """Whether an answered call could carry audio at all.

        Both halves or neither: a call that can speak and not hear leaves him
        talking to something that never replies, and one that can hear and not
        speak is the silence this whole path exists to end."""
        return self.speaker is not None and self.transcriber is not None

    def _intent_filler(self):
        """The turn -> thinking-sound picker, warmed once per process (the seed
        embeddings are a few hundred ms the first time; ollama keeps bge-m3)."""
        from .speculate import Speculator, intent_filler

        if getattr(self, "_speculator", None) is None:
            self._speculator = Speculator()

            def warm(spec=self._speculator):
                # The first embed loads bge-m3 into ollama: 3 s was not enough on
                # a busy GPU (2026-10-31), so the warm-up alone gets 30. Until it
                # lands, and if it never does, the keyword fallback picks.
                spec.timeout, short = 30.0, spec.timeout
                try:
                    spec.warm()
                finally:
                    spec.timeout = short

            threading.Thread(target=warm, daemon=True, name="speculator-warm").start()
        spec = self._speculator
        return lambda said: intent_filler(spec, said)

    def _answered_call(self, target: CallTarget, conversation: str, link: Any,
                       engine: str = "", *, inbound: bool = False) -> Any:
        """The handler `SipTransport` calls when he picks up.

        Everything crossing into it is a plain callable, so `conversation.py`
        needs neither this daemon nor hotline to be tested -- and so `talk.py`
        can build the same object out of different parts.
        """
        from .callagent import CallAgent, default_context, guest_agent
        from .conversation import AnsweredCall
        from .media.record import from_environment

        loop = asyncio.get_running_loop()
        speaker, transcriber = self.speaker, self.transcriber
        speakable = _speakable()

        from . import features as _features

        # Read fresh for every call: he flips a switch, rings, listens.
        feat = _features.load()

        def speak(text: str) -> tuple[Any, int]:
            try:
                audio = speaker.synthesize(text, steps=feat["tts_steps"])
            except TypeError:            # a speaker without per-call steps (hotline.audio)
                audio = speaker.synthesize(text)
            return audio, int(getattr(speaker, "rate", 24_000))

        def append(kind: str, text: str) -> None:
            # Called from the media thread. `_append` touches a CallLog whose
            # waiters are parked on the event loop, so it is handed back there
            # rather than called across threads.
            loop.call_soon_threadsafe(self._append, conversation, kind, text)

        agent = None
        if os.environ.get("HOTLINE_IOS_CALL_SESSION", "1") != "0" and inbound:
            # He dialled us. Nobody rang him and nothing is waiting on an
            # answer, so the "an agent rang you to ask X" brief would be a lie
            # the voice repeats back to him.
            agent = CallAgent(default_context(
                "THIS CALL: Bogdan called YOU. He dialled hotline himself from his\n"
                "phone; no agent rang him and there is no question waiting for an\n"
                "answer. You have already greeted him -- let him say why he called,\n"
                "and everything he says on this call is for you."
            ))
            agent.start()
        elif os.environ.get("HOTLINE_IOS_CALL_SESSION", "1") != "0" and target.address:
            # Ringing somebody who is not him. Different manners, different
            # language, and no `call_context.txt` -- see `guest_agent`. The
            # calling agent's brief is the ONLY thing this voice knows.
            who = (f"{target.caller_id} zove osobu {target.callee} u Bogdanovo ime.\n"
                   f"RAZLOG: {target.reason}")
            brief = target.context.strip()
            agent = guest_agent(
                target.callee,
                f"{who}\n\nSTA AGENT KOJI ZOVE ZELI DA ZNAS:\n{brief}" if brief else who,
            )
            agent.start()
        elif os.environ.get("HOTLINE_IOS_CALL_SESSION", "1") != "0":
            # The calling agent's own briefing goes in FIRST and is labelled as
            # the authority, because it is current and this file is not: nothing
            # regenerates call_context.txt and it is dated in its own first line.
            brief = target.context.strip()
            agent = CallAgent(default_context(
                (f"WHAT THE CALLING AGENT WANTS YOU TO KNOW -- this is current and\n"
                 f"outranks anything above it that disagrees:\n{brief}\n\n" if brief else "")
                + f"THIS CALL: {target.caller_id} rang him to ask -- {target.reason}\n"
                "He has just been asked that and is answering it out loud. His first\n"
                "answer goes back to the agent that rang; anything after it is yours."
            ))
            # Seeded WHILE the phone rings, not after he answers. Costs ~4.7 s
            # and there are usually more than that between the INVITE and him
            # picking up; `CallAgent.reply` waits for it if there were not.
            agent.start()

        ask_stream = speak_stream = ask_fork = None
        bridge = None
        if engine == "haiku" and agent is not None and not target.address:
            # His test, 2026-10-30: Haiku 5.5 over the API, its tokens streamed
            # straight into cvoiced. The CallAgent seeded above stays as the
            # fallback for a missing key or an old cvoiced; its seed costs
            # nothing he hears, it runs while the phone rings.
            from .haikuagent import HaikuAgent

            # "spoji me sa <agent>": his request, 2026-10-31. Only on his own
            # calls (not a guest's), and only with the Haiku voice, which is
            # what understands the name. See agentbridge.py.
            from .haikuagent import manners as build_manners

            # With a thinking sound already played for him, the voice must not
            # add its own "Važi," on top: it answers directly.
            base = build_manners(openers=feat["opener_nudge"], tools=feat["tools"],
                                 context_rules=feat["context_rules"],
                                 fillers=feat["filler_words"] and not feat["intent_fillers"])
            manners, extra = base, None
            if os.environ.get("HOTLINE_IOS_BRIDGE", "1") != "0" and feat["bridge"]:
                from .agentbridge import AgentBridge

                bridge = AgentBridge(mode=feat["bridge_mode"])
                manners, extra = f"{base}\n- {bridge.manners()}", bridge.tools()
            haiku = HaikuAgent(agent.context, manners=manners,   # already call_context.txt + this call
                               extra_tools=extra, tools=feat["tools"],
                               thinking=feat["thinking"], keepalive=feat["keepalive"],
                               date=feat["context_rules"])
            if haiku.failed:
                log.error("call %s: haiku engine unavailable (%s); classic voice instead",
                          conversation, haiku.failed)
                self._append(conversation, "error",
                             f"haiku engine unavailable ({haiku.failed}); the classic voice took the call")
            elif not hasattr(speaker, "stream"):
                log.error("call %s: the TTS client cannot stream; classic voice instead", conversation)
            else:
                def voice_stream(deltas, _speaker=speaker):
                    return _speaker.stream(deltas, steps=feat["tts_steps"], join=feat["join"],
                                           openers=feat["openers"])

                ask_stream = haiku.reply_stream
                if bridge is not None:
                    ask_stream = bridge.stream_for(haiku)
                haiku.start()   # opens the API connection while the phone rings
                if feat["streaming"]:
                    speak_stream = voice_stream
                    if feat["speculation"]:
                        def ask_fork(haiku=haiku):
                            twin = haiku.fork()
                            return twin.reply_stream, lambda: haiku.adopt(twin)
                else:
                    # Streaming off: the Haiku brain on the old whole-reply path,
                    # so streaming can be judged alone. Same ask, spoken by `say`.
                    whole = ask_stream
                    agent = _Whole(lambda heard, f=whole: "".join(d for d in f(heard) if d))
                    ask_stream = None

        return AnsweredCall(
            speak=speak,
            transcribe=lambda audio: str(transcriber.transcribe(audio)),
            fillers=self.fillers,
            # Plays while cvoiced is still warming. Without some clip here a
            # guest hears ~9 s of silence after picking up; `greet` itself names
            # Bogdan, so a guest gets its own.
            opening="greet_guest" if target.address else "greet",
            # Inbound: the clip ("Zdravo Bogdane, ovde hotline. Slušam te.") is
            # the whole greeting. There is no question to read out.
            greeting="" if inbound else str(speakable(
                # Serbian, like the guest manners themselves. The TTS voice is a
                # Serbian one and the ASR runs with a Serbian hint, so an English
                # greeting is mispronounced on the way out and mis-heard on the
                # way back.
                f"{target.callee}, {target.reason}"
                if target.address else f"{target.caller_id}: {target.reason}"
            )),
            deliver=lambda text: append("you", text),
            ask=agent.reply if agent is not None else None,
            ask_stream=ask_stream,
            speak_stream=speak_stream,
            ask_fork=ask_fork,
            hung_up=getattr(link, "far_end_hung_up", None),
            stream_fixed_lines=feat["streamed_greeting"],
            intent_filler=self._intent_filler() if (feat["intent_fillers"] and ask_stream is not None) else None,
            may_speculate=bridge.may_speculate if bridge is not None else None,
            # Generous only when an agent is (or is being) put on the line: a
            # Claude session's turn is tens of seconds, not Haiku's one or two.
            answer_seconds=(lambda b=bridge: (b.answer_seconds if b.connected
                                              else b.connect_seconds) + 20.0)
            if bridge is not None else None,
            on_end=(lambda why, b=bridge: b.release(why)) if bridge is not None else None,
            note=append,
            recorder=from_environment(f"{target.caller_id}: {target.reason}"),
            model=repr(transcriber),
        )

    # ---- him calling us ---------------------------------------------------

    def inbound_call(self, call: Any) -> Any:
        """`SipInbound.on_call`. Runs on the SIP thread; the work runs on the loop.

        `_answered_call` and `_open_conversation` touch loop-owned state, so
        they are built there and handed back. Bounded, because the SIP thread
        is also what keeps the registration alive.
        """
        if self.loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(self._inbound_leg(call), self.loop)
        return future.result(timeout=10.0)

    async def _inbound_leg(self, call: Any) -> Any:
        from .ring.inbound import Busy

        if self._gpu_lock.locked():
            # A PersonaPlex call has cvoiced down and the card full; the
            # inbound voice would have nothing to speak with.
            raise Busy("a PersonaPlex call holds the GPU")
        if not self.can_talk:
            log.error("inbound call from %s: no voice or no ears here; not answering",
                      call.caller)
            return None
        conversation, _events = self._open_conversation(None, "say")
        self._append(conversation, "state", "he called hotline")
        target = CallTarget(device="phone", agent=None, reason="Bogdan called hotline",
                            caller_id="Bogdan", context="", address="", callee="Bogdan")
        from . import features as _features

        engine = os.environ.get("HOTLINE_IOS_INBOUND_ENGINE", "").strip().lower() \
            or _features.load()["inbound_engine"]
        if engine in ("classic", "auto"):
            engine = ""
        # Warmed while it rings, exactly as an outbound ring does, and released
        # by `_InboundLeg` however the call ends -- answered or cancelled.
        self._warm_models()
        try:
            handler = self._answered_call(target, conversation, call, engine, inbound=True)
        except Exception:
            self._cool_models()
            self._close_conversation(conversation)
            raise
        log.info("inbound call %s from %s: engine %s", conversation, call.caller,
                 engine or "classic")
        return _InboundLeg(self, conversation, handler, asyncio.get_running_loop())

    async def place(self, body: dict[str, Any]) -> dict[str, Any]:
        """Ring him, then wait for him to answer in the app.

        This is `hotline-page`'s contract kept intact across a change of
        doorbell. The ring is a Telegram call; the *answer* comes back through
        the app, as a typed reply into the conversation this opens. So a blocked
        agent still gets his words on stdout and does not need to know that the
        thing which rang and the thing he replied in are different programs.

        The alternative -- ring and exit, and let the reply arrive as an
        injected message -- would have quietly broken every caller that does
        `answer=$(hotline-call ...)`, which is all of them.
        """
        reason = str(body.get("reason", "")).strip()
        if not reason:
            from hotline.httpd import HttpError

            raise HttpError(400, "reason is required")

        agent = body.get("agent") or None
        # `to` rings somebody who is not him: a registry person's own Linphone
        # account, dialed with our credentials. Absent, everything below behaves
        # exactly as it did before hotline-registry existed.
        to = str(body.get("to", "")).strip()
        target = CallTarget(
            device=str(body.get("device", "phone")),
            agent=str(agent) if agent else None,
            reason=reason,
            caller_id=str(body.get("source", "Claude")),
            context=str(body.get("context", "")),
            address=to,
            callee=str(body.get("callee", "")).strip() or ("someone" if to else "Bogdan"),
        )
        ring_timeout = float(body.get("ring_timeout", 45.0))
        reply_timeout = float(body.get("timeout", 900.0))
        wait = bool(body.get("wait", True))

        doorbell = self._resolve_transport(str(body.get("transport", "")))
        if to:
            # SIP is the only transport that can dial an arbitrary address. Every
            # other one -- Telegram, the local app socket, loopback -- reaches
            # exactly one person, and that person is HIM. Falling through to one
            # of those on a registry call would ring Bogdan with a question meant
            # for somebody else and report it as delivered, which is the worst
            # failure this endpoint has available to it. So: resolve to the bare
            # SIP link, or refuse out loud.
            from hotline.httpd import HttpError

            sip_link = self.links.get("sip")
            if sip_link is None:
                raise HttpError(
                    503,
                    "calling someone other than Bogdan needs the sip transport, "
                    f"and this daemon has: {', '.join(sorted(self.links)) or 'none'}",
                )
            if str(body.get("transport", "")).strip().lower() not in ("", "auto", "sip"):
                raise HttpError(400, "'to' can only be rung over the sip transport")
            doorbell = sip_link

        conversation, events = self._open_conversation(agent, "ring")
        # Put the question in the conversation before ringing, so that whenever
        # he opens the app -- during the ring, or an hour later -- it is already
        # there and he never answers a phone to silence.
        self._append(conversation, "claude", f"{target.caller_id}: {reason}")
        if str(body.get("context", "")):
            self._append(conversation, "summary", str(body["context"])[:1200])
        began = time.monotonic()
        # Taken BEFORE the ring and not after. Since the call can now carry
        # audio, his answer can arrive as an entry appended *during*
        # `doorbell.ring()`; a cursor read afterwards would already be past it
        # and `_await_reply` would wait its full timeout for a question he had
        # answered out loud thirty seconds earlier. Nothing could append during a
        # ring before this change, which is why the old ordering was fine and is
        # not any more.
        cursor = events.latest

        engine = str(body.get("engine", "") or "").strip().lower()
        if engine in ("", "auto"):
            engine = "" if to else self.default_engine
        if engine == "classic":
            engine = ""
        prepared = None
        handoff = None
        note = ""
        loop = asyncio.get_running_loop()
        holds_gpu = False
        if engine == "personaplex" or self._gpu_lock.locked():
            # ~180 s covers a PersonaPlex call at its 150 s cap plus the transcription
            try:
                await asyncio.wait_for(self._gpu_lock.acquire(), self.gpu_wait_s)
                holds_gpu = True
            except TimeoutError:
                note = (f"waited {self.gpu_wait_s:.0f} s for another PersonaPlex call to "
                        "finish and went ahead anyway")
                log.warning("call %s: %s", conversation, note)
            if holds_gpu and engine != "personaplex":
                self._gpu_lock.release()   # it only had to wait for cvoiced to be back
                holds_gpu = False
        if engine == "personaplex":
            if self.gpu_handoff is not None:
                handoff = self.gpu_handoff()
                await loop.run_in_executor(None, handoff.acquire)
            try:
                links, handlers, why = await loop.run_in_executor(
                    None, self._prepare_duplex, doorbell, target, conversation, loop)
            except Exception as exc:
                log.exception("call %s: preparing personaplex raised", conversation)
                links, handlers, why = [], [], f"{type(exc).__name__}: {exc}"
            if why:
                # Not rung on PersonaPlex. Bogdan's rule (2026-10-16): place the call on
                # the Serbian path instead of failing it, and say so. cvoiced has to be
                # back first, because that path speaks through it.
                log.error("call %s: personaplex did not start (%s); ringing on the Serbian path", conversation, why)
                if handoff is not None:
                    restored = await loop.run_in_executor(None, handoff.release)
                    handoff = None
                    if "NOT healthy" in restored or "could not" in restored:
                        note = f"; {restored}"
                note = f"PersonaPlex did not start ({why}); the call went on the Serbian voice path instead{note}"
                self._append(conversation, "error", note)
                engine = ""
                if holds_gpu:
                    self._gpu_lock.release()
                    holds_gpu = False
            else:
                prepared = (links, handlers)
        try:
            try:
                with self._voice_leg(doorbell, target, conversation, engine, prepared) as spoken:
                    await doorbell.ring(target, timeout=ring_timeout)
            finally:
                # However the ring ended -- answered and hung up, declined, unanswered,
                # unreachable, or an exception -- cvoiced comes back before anything else:
                # every other call and page speaks through it.
                try:
                    if handoff is not None:
                        restored = await loop.run_in_executor(None, handoff.release)
                        if "NOT healthy" in restored or "could not" in restored:
                            note = f"{note}; {restored}" if note else restored
                            self.degradations.append(restored)
                finally:
                    if holds_gpu:
                        self._gpu_lock.release()
        except CallDeclined as exc:
            self._append(conversation, "state", "declined")
            self._close_conversation(conversation)
            return self._outcome(conversation, "declined", began, str(exc), transport=doorbell, engine=engine or "classic", note=note)
        except CallUnanswered as exc:
            # It rang and he did not pick up. Record that it went unanswered and
            # close it, at his instruction (2026-09-01, overriding SPEC 3's
            # keep-open rule). Closing does NOT lose a late answer: `reply()`
            # does not gate on `closed` and `_channel()` reads a closed row back
            # in, so he can still open the app and answer. Close only drops it
            # from `active_calls` and the waiting roster -- the leak he asked to
            # stop.
            self._append(conversation, "state", "unanswered")
            self._close_conversation(conversation)
            return self._outcome(conversation, "unanswered", began, str(exc), transport=doorbell, engine=engine or "classic", note=note)
        except (CallUnreachable, CallError) as exc:
            self.degradations.append(str(exc))
            log.warning("call %s undeliverable: %s", conversation, exc)
            self._append(conversation, "error", str(exc))
            self._close_conversation(conversation)
            return self._outcome(conversation, "unreachable", began, str(exc), transport=doorbell, engine=engine or "classic", note=note)

        if not wait:
            return self._outcome(conversation, "ringing", began, "not waiting", transport=doorbell, engine=engine or "classic", note=note)

        # He may already have answered out loud, in which case the entry is
        # sitting past `cursor` and this returns at once.
        if spoken is not None and spoken.answered:
            log.info("call %s answered by voice after %d turn(s): %s",
                     conversation, spoken.turns, spoken.stats)
        reply = await self._await_reply(events, reply_timeout, cursor)
        if not reply:
            # Rang, connected, but no answer came back in time. Same rule as the
            # ring-out above: say it went unanswered, then close it. He can still
            # answer later; this only stops it counting as an active call.
            self._append(conversation, "state", "unanswered")
            self._close_conversation(conversation)
            return self._outcome(conversation, "unanswered", began,
                                 f"rang, but nothing came back within {reply_timeout:.0f}s",
                                 transport=doorbell, engine=engine or "classic", note=note)
        transcript: list[dict[str, str]] = [{"who": "you", "text": reply}]
        turns_spoken = int(getattr(spoken, "turns", 0) or 0)
        if spoken is not None and turns_spoken:
            # He is on the line. Wait out the call and hand back all of it; the
            # first turn is already in `reply` for anything that only wants that.
            left = max(0.0, reply_timeout - (time.monotonic() - began))
            whole = await self._await_transcript(events, left, cursor, spoken)
            if whole:
                transcript = whole
        return self._outcome(conversation, "answered", began, "", reply,
                             transport=doorbell, transcript=transcript,
                             engine=engine or "classic", note=note)

    async def _await_reply(self, events: CallLog, timeout: float,
                           cursor: int | None = None) -> str:
        """Block until he answers -- typed in the app, or spoken on the call.

        `cursor` is passed in by `place()`, which takes it before the ring. The
        default of "wherever the log is now" is only correct for a caller that
        could not have been answered already.
        """
        deadline = time.monotonic() + timeout
        if cursor is None:
            cursor = events.latest
        while time.monotonic() < deadline:
            found = await events.wait(cursor, min(20.0, deadline - time.monotonic()))
            for entry in found:
                cursor = max(cursor, entry.seq)
                if entry.kind == "you":
                    return entry.text
            if events.closed:
                break
        return ""

    async def _await_transcript(self, events: CallLog, timeout: float, cursor: int,
                                spoken: Any) -> list[dict[str, str]]:
        """Everything he said on the call, not only the answer to the question.

        His instruction, 2026-09-11: *"It should get the whole convo."* The
        calling agent rang to have a conversation; returning only the first turn
        threw the rest away -- it was written into the conversation log and never
        handed back.

        Two things keep this from blocking a caller that is not on a voice call.
        It only runs once a spoken turn has actually happened (`spoken.turns`),
        so a typed answer in the app still returns the instant it lands; and
        `ended` is the sentinel `"not started"` until a terminal path sets it, so
        the moment the call finishes -- goodbye, hangup, dead line, length cap --
        this stops. The caller's own deadline caps it either way.
        """
        said: list[dict[str, str]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = await events.wait(cursor, min(5.0, max(0.1, deadline - time.monotonic())))
            for entry in found:
                cursor = max(cursor, entry.seq)
                # Both sides. `client.CallOutcome.transcript` has always been
                # typed for this and the daemon never filled it, so an agent
                # asking for the conversation got None.
                if entry.kind in ("you", "claude"):
                    said.append({"who": entry.kind, "text": entry.text})
            done = getattr(spoken, "transcribed", None)
            if done is not None:
                # A duplex call: the transcript is delivered after the line drops, so
                # "ended" alone is too early. Stop once it is all delivered, after one
                # short drain for appends still queued on the event loop.
                finished = done.is_set()
            else:
                finished = getattr(spoken, "ended", "not started") != "not started"
            if events.closed or finished:
                if done is not None:
                    for entry in await events.wait(cursor, 0.3):
                        cursor = max(cursor, entry.seq)
                        if entry.kind in ("you", "claude"):
                            said.append({"who": entry.kind, "text": entry.text})
                break
        return said

    def _outcome(
        self,
        call_id: str,
        state: str,
        began: float,
        detail: str = "",
        reply: str = "",
        transport: Any = None,
        transcript: list[dict[str, str]] | None = None,
        engine: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        used = transport if transport is not None else self.transport
        out: dict[str, Any] = {
            "call_id": call_id,
            "conversation": call_id,
            "state": state,
            "reply": reply,
            "transcript": (transcript if transcript is not None
                           else ([{"who": "you", "text": reply}] if reply else [])),
            "detail": detail,
            "waited_seconds": round(time.monotonic() - began, 1),
            "transport": getattr(used, "name", "?"),
            "rings_when_closed": bool(getattr(used, "rings_when_closed", False)),
            # Callers cannot otherwise tell a ring from a no-op: "answered via
            # loopback+confirmed" and "answered via sip+confirmed" are the same
            # sentence. This is the field that distinguishes them, and
            # hotline-call refuses to report success when it is true.
            "fake": bool(getattr(used, "is_fake", False)),
            # who held the call ("personaplex" or "classic"), and why that is not what
            # was asked for when PersonaPlex fell back
            "engine": engine,
            "note": note,
        }
        return out

    # ---- what the app forwards -------------------------------------------

    def typed_reply(self, conversation: str, text: str) -> dict[str, Any]:
        """His answer typed in the app, forwarded by hotline-iosd.

        Lands in the call's log so a `place()` waiting on it returns, exactly as
        a typed reply did when both lived in one process. Not mirrored back:
        hotline-iosd already has it -- that is where he typed it.

        An unknown conversation is not an error. hotline-iosd forwards every
        reply it gets, and most conversations it holds are not calls this
        process is waiting on (a restart, a delegation, an hour-old ring).
        """
        events = self.calls.get(conversation)
        if events is None:
            return {"conversation": conversation, "known": False}
        events.append("you", text)
        return {"conversation": conversation, "known": True, "waiting": not events.closed}

    def hang_up(self, call_id: str) -> dict[str, Any]:
        """He dismissed the conversation in the app; stop waiting on it.

        Ends the wait in `place()` (the log closes, `_await_reply` returns).
        Does not touch an RTP leg in progress: dismissing a question in the app
        while on the phone about it is not a request to cut the line.
        """
        events = self.calls.get(call_id)
        if events is not None and not events.closed:
            events.append("state", "ended")
            self._close_conversation(call_id, mirror=False)
        return {"call_id": call_id, "ended": events is not None}


class _InboundLeg:
    """One inbound call's handler, plus the cleanup a ring gets from `_voice_leg`.

    Called by `SipInbound` like any `on_answer` handler once it has answered;
    `abandon` instead when he hangs up while it is still ringing. Either way the
    models are released and the conversation closed exactly once.
    """

    def __init__(self, service: Service, conversation: str, handler: Any,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.service = service
        self.conversation = conversation
        self.handler = handler
        self.loop = loop
        self._done = threading.Lock()
        self._finished = False

    def __call__(self, reply: str, media_sock: Any, key: bytes, salt: bytes) -> None:
        try:
            self.handler(reply, media_sock, key, salt)
        finally:
            ended = getattr(self.handler, "ended", "") or "ended"
            self._finish(f"call over: {ended}")

    def abandon(self, why: str) -> None:
        self._finish(f"not answered: {why}")

    def _finish(self, note: str) -> None:
        with self._done:
            if self._finished:
                return
            self._finished = True
        self.service._cool_models()

        def close() -> None:
            self.service._append(self.conversation, "state", note)
            self.service._close_conversation(self.conversation)

        with contextlib.suppress(RuntimeError):  # the loop is gone: shutting down
            self.loop.call_soon_threadsafe(close)



def _speakable():
    try:
        from hotline.voice import speakable

        return speakable
    except Exception:  # noqa: BLE001 - unpolished speech beats no answer
        return lambda text: text



def build_server(service: Service, host: str | Sequence[str], port: int) -> Any:
    from hotline.httpd import HttpError, Server

    if isinstance(host, str):
        hosts = [h for h in dict.fromkeys([host, LOOPBACK]) if h]
    else:
        hosts = list(host)
    server = Server(hosts, port, log=lambda message: log.info("%s", message))
    service.bound_to(hosts, port)

    @server.route("GET", PING_PATH)
    async def ping(request: Any) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True}

    @server.route("GET", "/health")
    async def health(request: Any) -> tuple[int, dict[str, Any]]:
        fake = bool(getattr(service.transport, "is_fake", False))
        service.reap()
        return 200, {
            # NOT ok when the doorbell is fake. A health check that reports ok
            # while the pager rings nothing is the exact failure this endpoint
            # exists to catch, and it reported ok:true for 2.5 hours.
            "ok": (not fake) and service.ring_ready,
            "fake": fake,
            "ring_ready": service.ring_ready,
            "can_talk": service.can_talk,
            "uptime_seconds": round(time.time() - service.started, 1),
            "transport": getattr(service.transport, "name", "?"),
            "rings_when_closed": bool(getattr(service.transport, "rings_when_closed", False)),
            "transports_available": sorted(service.links),
            # He can call us: the persistent registration's own state, or null
            # when inbound is switched off.
            "inbound": service.inbound.status() if service.inbound is not None else None,
            "active_calls": service.open_calls(),
            "default_engine": service.default_engine or "classic",
            "listening_on": [f"{h}:{service.listen_port}" for h in service.listen_hosts],
            # Whether the app is getting its copy of each call.
            "app_mirror": service.mirror.status() if service.mirror is not None else None,
            "degradations": service.degradations[-5:],
        }

    @server.route("POST", "/api/v1/call")
    async def place(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        return 200, await service.place(request.json())

    @server.route("POST", "/api/v1/call/reply")
    async def typed_reply(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        conversation = str(body.get("conversation", ""))
        text = str(body.get("text", "")).strip()
        if not conversation or not text:
            raise HttpError(400, "conversation and text are required")
        return 200, service.typed_reply(conversation, text)

    @server.route("POST", "/api/v1/call/hangup")
    async def hangup(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        call_id = str(request.json().get("call_id", ""))
        if not call_id:
            raise HttpError(400, "call_id is required")
        return 200, service.hang_up(call_id)

    return server

def build_voice() -> tuple[Any, Any, Any]:
    """The three things an answered call needs: a voice, ears, and holding phrases.

    A missing voice or missing ears means the daemon still rings, still opens a
    conversation and still takes a typed answer in the app -- it simply hangs up
    on the 200 the way it always did. That degradation is the point: a missing
    model must cost the voice leg and never the doorbell. Missing fillers cost
    only dead air.

    **Why `large-v3` on the GPU is the default.** Measured on this box on
    2026-09-09, on 2.5 s of his own cloned Serbian put through a real G.711
    8 kHz roundtrip:

    | model | where | time | what it heard |
    |---|---|---|---|
    | `large-v3` | cuda int8_float16 | **0.38 s** | word-perfect |
    | `large-v3` | cpu int8 | 5.39 s | word-perfect |
    | `small` | cpu int8 | 0.99 s | *"Zdravo o The Hotline, imam pitanju za tebe"* |

    Fast and wrong is the worse failure on a phone call, so `small` is only the
    fallback. The GPU is available at all because `cvoiced` at `takes=1` holds
    2406 MiB rather than the 6452 it holds once it has loaded its take-scorer --
    which leaves 5.7 GB, and `large-v3` int8_float16 wants about 1.9.
    """
    if os.environ.get("HOTLINE_IOS_VOICE", "1") == "0":
        log.info("voice leg disabled by HOTLINE_IOS_VOICE=0; calls will ring and hang up")
        return None, None, None
    speaker: Any = None
    transcriber: Any = None
    fillers: Any = None
    try:
        from .media.tts import Voice

        speaker = Voice()
        health = speaker.health()
        log.info("tts: %s, model_loaded=%s", speaker, health.get("model_loaded"))
    except Exception as exc:  # noqa: BLE001 - a mute doorbell still rings
        log.error("no voice for answered calls: %s", exc)
        speaker = None
    try:
        from .media.ears import Ears

        transcriber = Ears(
            model=os.environ.get("HOTLINE_IOS_ASR_MODEL", "large-v3"),
            device=os.environ.get("HOTLINE_IOS_ASR_DEVICE", "cuda"),
            compute_type=os.environ.get("HOTLINE_IOS_ASR_COMPUTE", "int8_float16"),
            language=os.environ.get("HOTLINE_IOS_ASR_LANGUAGE", "sr"),
        )
    except Exception as exc:  # noqa: BLE001 - same rule as above
        log.error("no transcriber for answered calls: %s", exc)
        transcriber = None
    try:
        from .media.tts import Fillers

        fillers = Fillers()
        fillers.load()
    except Exception as exc:  # noqa: BLE001 - dead air, not a dead call
        log.error("no holding phrases: %s", exc)
        fillers = None
    return speaker, transcriber, fillers


def holds_calls(doorbell: Any) -> list[Any]:
    """Every transport inside `doorbell` that can hold an answered call.

    Walked rather than passed in, because what `place()` is handed depends on
    `HOTLINE_IOS_RING`: one `ConfirmedRing` around a `SipTransport`, or a
    `RingChain` of several. Both wrappers expose what they wrap (`inner`,
    `links`), so this needs no cooperation from either.
    """
    if doorbell is None:
        return []
    inner = getattr(doorbell, "inner", None)
    if inner is not None:
        return holds_calls(inner)
    links = getattr(doorbell, "links", None)
    if links is not None:
        return [found for link in links for found in holds_calls(link)]
    return [doorbell] if hasattr(doorbell, "on_answer") else []


def _the_one_that_ran(handlers: Sequence[Any]) -> Any:
    """Which handler actually took the call, when a chain had several.

    Only one link in a chain ever answers -- the chain stops at the first that
    works -- so this is a lookup, not a merge."""
    for handler in handlers:
        if getattr(handler, "turns", 0) or getattr(handler, "answered", ""):
            return handler
    return handlers[0] if handlers else None


def build_transport(
    names: Sequence[str],
    *,
    confirm_within: float = 8.0,
    allow_fake: bool | None = None,
) -> Any:
    """Assemble the doorbell from a list of names, in order.

    "We will do both" is his instruction, and this is what makes it a
    configuration rather than a fork: `HOTLINE_IOS_RING=telegram,sip` and the
    chain falls through from one to the next. Each link is wrapped in
    `ConfirmedRing` individually rather than the chain as a whole, because the
    chain can only fall through on evidence -- wrapping the outside would let a
    silent failure inside look like success.
    """
    from .ring.chain import RingChain
    from .ring.watch import ConfirmedRing

    if allow_fake is None:
        allow_fake = os.environ.get("HOTLINE_IOS_ALLOW_FAKE", "") == "1"

    links: list[Any] = []
    for name in names:
        name = name.strip().lower()
        if not name:
            continue
        if name == "telegram":
            from .ring.telegram import TelegramTransport

            links.append(TelegramTransport())
        elif name == "sip":
            from .ring.sip import SipTransport

            links.append(SipTransport())
        elif name == "loopback":
            from .ring.loopback import LoopbackTransport

            if not allow_fake:
                raise SystemExit(
                    "hotline-calld: refusing to start with the 'loopback' doorbell, "
                    "which rings nothing. It was left running for 2.5 hours on "
                    "2026-08-25 and every caller got exit 0 while he was never "
                    "contacted. If that is genuinely what you want, set "
                    "HOTLINE_IOS_ALLOW_FAKE=1 -- saying it twice is the point."
                )
            links.append(LoopbackTransport())
        else:
            raise SystemExit(
                f"hotline-calld: unknown ring transport {name!r}; "
                "known: telegram, sip, loopback"
            )
    if not links:
        raise SystemExit("hotline-calld: no ring transport configured; set HOTLINE_IOS_RING")
    wrapped = [ConfirmedRing(link, confirm_within=confirm_within) for link in links]
    return wrapped[0] if len(wrapped) == 1 else RingChain(wrapped)


def build_links(names: Sequence[str], *, confirm_within: float = 8.0,
                allow_fake: bool | None = None) -> dict[str, Any]:
    """The same doorbells, individually addressable by name.

    So that `--transport sip` on a daemon configured `telegram,sip` selects the
    SIP leg instead of being ignored. Built by calling build_transport once per
    name, which keeps one construction path rather than two that can drift.
    """
    out: dict[str, Any] = {}
    for name in names:
        name = name.strip().lower()
        if not name:
            continue
        out[name] = build_transport([name], confirm_within=confirm_within,
                                    allow_fake=allow_fake)
    return out


def start_inbound(service: Service) -> None:
    """Keep hotline's account registered so he can call it. Never fatal.

    `HOTLINE_IOS_INBOUND=0` switches it off. Anything that goes wrong here --
    no credentials, no network, a refused REGISTER -- is logged loudly and put
    on /health, and the outbound doorbell carries on exactly as before: the
    registration runs on its own thread and its own connection, and retries.
    """
    if os.environ.get("HOTLINE_IOS_INBOUND", "1") == "0":
        log.info("inbound calls disabled by HOTLINE_IOS_INBOUND=0")
        return
    try:
        from .ring.inbound import SipInbound

        inbound = SipInbound(on_call=service.inbound_call)
        inbound.start()
    except Exception as exc:  # noqa: BLE001 - inbound must never cost outbound
        note = f"inbound calls unavailable: {exc}"
        log.error(note)
        service.degradations.append(note)
        return
    service.inbound = inbound
    log.info("inbound: registering %s@%s so he can call", inbound.user, inbound.domain)



def env_files() -> list[pathlib.Path]:
    """Where SIP_*, TELEGRAM_* and HOTLINE_API_KEY come from, first one wins.

    Not the checkout: an ownbox install lives in a directory `ownbox uninstall`
    deletes whole, and the SIP password must not go with it. `ownbox.sh setup`
    writes `~/.config/hotline-call/env`; a `.env` beside the code is still read
    after it, for a development checkout.
    """
    config = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    chosen = os.environ.get("HOTLINE_CALL_ENV", "")
    out = [pathlib.Path(chosen)] if chosen else [config / "hotline-call" / "env"]
    out.append(pathlib.Path(__file__).resolve().parents[2] / ".env")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hotline-calld")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOTLINE_CALL_HOST", DEFAULT_HOST),
        help="the address remote agents dial; loopback is always bound as well",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("HOTLINE_CALL_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--ring",
        default=os.environ.get("HOTLINE_IOS_RING", "sip"),
        help="comma-separated doorbells, tried in order (sip, telegram, loopback)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    from hotline.config import load_env

    load_env()
    # hotline's load_env() defaults to hotline's OWN .env, which has no SIP_* or
    # TELEGRAM_* in it. Without this the daemon starts, reports a sip doorbell,
    # and only says "sip is not configured" in a degradations field nobody was
    # reading. load_env never overwrites what is already set, so a key present
    # in both files keeps hotline's value -- the order hotline-iosd has always
    # used, kept so the two daemons agree on HOTLINE_API_KEY.
    for env_file in env_files():
        load_env(env_file)
    names = args.ring.split(",")
    # Build each doorbell ONCE and compose the default from those same objects,
    # so `--transport sip` selects the very transport the chain would use rather
    # than a second copy of it -- and so starting the chain starts them all.
    links = build_links(names)
    if not links:
        raise SystemExit("hotline-calld: no ring transport configured; set HOTLINE_IOS_RING")
    if len(links) == 1:
        transport: Any = next(iter(links.values()))
    else:
        from .ring.chain import RingChain

        transport = RingChain(list(links.values()))
    allow = {
        ip.strip()
        for ip in os.environ.get("HOTLINE_ALLOW_IPS", "").split(",")
        if ip.strip()
    }
    speaker, transcriber, fillers = build_voice()
    from .appmirror import AppMirror
    from .gpu_handoff import GpuHandoff

    mirror = None if os.environ.get("HOTLINE_CALL_APP_MIRROR", "1") == "0" else AppMirror()
    service = Service(
        transport,
        transcriber=transcriber,
        speaker=speaker,
        fillers=fillers,
        mirror=mirror,
        allow_ips=allow,
        api_key=os.environ.get("HOTLINE_API_KEY", ""),
        # Bogdan, 2026-10-16: "Hotline-call uses stock personaplex from now on." Calls
        # to him only; guests stay on the Serbian path. "" restores the old default.
        default_engine=os.environ.get("HOTLINE_IOS_DEFAULT_ENGINE", "personaplex").strip().lower(),
        gpu_handoff=lambda: GpuHandoff(health=speaker.health if speaker is not None else None),
    )
    if not service.can_talk:
        # On /health rather than only in the log. A doorbell that rings and then
        # gives him thirty seconds of silence is the exact failure this whole
        # path exists to end, and it must not be something you can only discover
        # by answering the phone.
        service.degradations.append(
            "answered calls carry no audio: this daemon can ring him but not talk"
        )

    service.set_links(links)

    async def run() -> None:
        service.loop = asyncio.get_running_loop()
        try:
            await transport.start()
            service.ring_ready = True
        except Exception as exc:  # noqa: BLE001 - see comment
            # Do NOT die. A ringer that cannot start is exactly the thing that
            # should be visible on /health rather than fatal at boot.
            log.error("ring transport did not start: %s", exc)
            service.degradations.append(str(exc))
        server = build_server(service, args.host, args.port)
        await server.start()
        # `asyncio.start_server` over a list binds what it can and raises only
        # when *nothing* binds -- and loopback always binds. At boot the tailnet
        # address may not exist yet; serving loopback alone would look healthy
        # to every local curl while remote agents cannot reach it. Exiting hands
        # the retry to the unit's Restart=always.
        missing = [h for h in server.hosts if h != LOOPBACK and h not in server.bound]
        if missing:
            raise SystemExit(
                f"bound {server.bound or 'nothing'} but not {', '.join(missing)} "
                f"on port {args.port}. Is tailscaled up? Exiting so the service "
                "manager retries."
            )
        start_inbound(service)
        # NOT warmed here. His rule, 2026-09-11: "nothing should be loaded
        # prematurely ... after the call is done then everything unloaded."
        # The warm happens in `_voice_leg`, while the phone is ringing.
        log.info(
            "hotline-calld on %s:%d, ring=%s, rings_when_closed=%s, app mirror=%s",
            ",".join(server.hosts), args.port, getattr(transport, "name", "?"),
            getattr(transport, "rings_when_closed", False),
            mirror.url if mirror is not None else "off",
        )
        if getattr(transport, "is_fake", False):
            log.error(
                "THIS DOORBELL RINGS NOTHING (%s). Callers will be told so and "
                "hotline-call will fall back to hotline-page.",
                getattr(transport, "name", "?"),
            )
            service.degradations.append(
                f"doorbell {getattr(transport, 'name', '?')} is fake; nobody is being rung"
            )
        await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    finally:
        if service.inbound is not None:
            service.inbound.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
