"""Put a live hotline agent on the phone call: "spoji me sa <agent>".

**What this is.** While he is on a Haiku call, he can ask for one of the Claude
Code sessions hotline runs ("spoji me sa manager-5", "add tells to the call").
This module finds that agent, cancels whatever turn it is in (one Escape into
its tmux pane, `hotline.tmuxen.interrupt`), and tells it that it is on a live
call with him until he hangs up. When the call ends it tells the agent the call
is over and to go back to what it was doing. His request, 2026-10-31.

**Two modes, because which one is better was the open question.**

- `relay`: every turn of his goes to the agent; the agent's answer comes back
  and Haiku turns it into speech (shorter, Serbian, no markdown). Haiku stays
  the voice, so a sloppy written answer is cleaned up, at the cost of one more
  model round (~0.6-1 s) per turn.
- `direct`: Haiku drops out after connecting. Each turn goes straight to the
  agent and its reply text is spoken as-is (after `speakable`, which only strips
  markup). One model fewer per turn; the agent's own phone manners are all
  there is.

Neither mode streams the agent's tokens: a session in a tmux pane is only read
back from its transcript when its turn ends (`Router.collect`), so the first
word cannot come before the last one is written. The line is kept alive with
the holding clips from disk (`AnsweredCall.reply_streaming`) and, past
`KEEPALIVE_SECONDS`, a short spoken "još radi" from here.

**Why the routing is deterministic rather than a Haiku tool per turn.** Once an
agent is connected, his words go to it without asking Haiku whether to. A
`ask_agent` tool would cost Haiku a full round before the agent even sees the
question, and on a turn where Haiku decides to answer itself he gets Haiku's
guess presented as the agent's answer. Connecting is the one step that is a
Haiku tool, because that is where names have to be understood.

**Safety.**

- Never the operator (`hotline-80`, or anything holding a privileged role) and
  never the agents in `HOTLINE_IOS_BRIDGE_DENY` (default: `manager-6`, the
  agent that deploys this). Never a session that is not live right now; a name
  not in the live list is answered out loud, never guessed at.
- Every message carries provenance: `kind=voice` (Whisper's words, spoken by
  him on a phone call) plus, when `HOTLINE_IOS_BRIDGE_WARRANT` names one, his
  Discord message asking for this feature as a verified warrant. The agent's own
  rules still decide what it may do; nothing here widens them, and the opening
  message says so.
- The call voice never claims the agent did something: relay mode hands Haiku
  the agent's words verbatim and tells it to add nothing.
- Speculation (answering at his first pause) is off while an agent is
  connected and for any turn that mentions connecting: a speculative turn that
  is then dropped would still have reached the agent, and there is no unsend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("hotline-ios.agentbridge")

MODES = ("relay", "direct")
DEFAULT_MODE = os.environ.get("HOTLINE_IOS_BRIDGE_MODE", "direct").strip().lower()
DEFAULT_DENY = ("manager-6", "hotline-80")

CONNECT_SECONDS = 40.0
"""How long connecting may wait for the agent's first words. Below cvoiced's 60 s
websocket receive timeout, because during a Haiku tool call nothing is fed to
the voice stream and it would otherwise break under us."""

ANSWER_SECONDS = 180.0
"""How long one turn may wait for the agent. A session doing a real lookup can
take a minute; the line is kept alive meanwhile, so the cap only stops a turn
that is never coming back."""

KEEPALIVE_SECONDS = 9.0
"""After this long with no answer, say something. The holding clips from disk
cover the first 3 s; past that the line goes quiet until the agent finishes,
and cvoiced's stream times out after 60 s of nothing."""

SETTLE_SECONDS = 0.6
"""Between the Escape and the message. The CLI has to finish cancelling before
a new message lands as a new turn rather than in the queue that Escape just
cleared (same pacing as `daemon.COMPACT_SETTLE`)."""

KEEPALIVES = ("Još radi, samo trenutak.", "I dalje gleda.", "Još malo.")

CONNECT_WORDS = re.compile(
    r"\b(spoj\w*|povež\w*|povez\w*|poveż\w*|dodaj\w*|ubaci\w*|prebaci\w*|add|connect|"
    r"patch|agent\w*)\b", re.IGNORECASE)
"""Words that make a turn possibly a connect request. Used only to switch
speculation off for that turn, so a false positive costs a few hundred ms and a
miss is the case to avoid."""

RELEASE = re.compile(
    r"\b(prekini vezu|raskini vezu|odspoj\w*|isključi (ga|je|agenta)|vrati (se|haiku)|"
    r"disconnect|hang up on (him|it|the agent)|drop the agent)\b", re.IGNORECASE)
"""In direct mode Haiku is not listening, so leaving the agent has to be
recognised here. Deliberately not a farewell: "ćao"/"prijatno" end the CALL,
which the conversation already handles, and that releases the agent too."""

MAX_SPOKEN = 700


OPENING = """THIS IS A LIVE CALL WITH BOGDAN. DROP WHATEVER YOU ARE DOING FOR NOW AND \
ANSWER DIRECTLY, AS FAST AS POSSIBLE, UNTIL HANGUP.

{why}Bogdan is on a live phone call with the hotline voice and asked to be put \
through to you; his words were: «{asked}». From now until a message saying \
CALL ENDED, every message you get through this bridge is his speech, \
transcribed by Whisper (it mis-hears names and numbers), and the text of your \
turn is read aloud to him by a text-to-speech voice. Only your FINAL text of \
each turn is spoken, and only once the turn ends: he hears nothing while you \
work, so the bridge plays holding phrases for you. Do not write "samo \
sekund" yourself.

Phone manners, because he is listening, not reading:
- Short spoken Serbian, Latin script with proper diacritics. One or two \
sentences.
- NO markdown, lists, code, file paths, URLs, hashes, or long numbers. Numbers \
as words.
- Answer him directly. Keep any lookup short (seconds, not minutes). Anything \
that needs real work: say you will do it after the call, and do it then.
- Never say something is done unless you did it in this turn.
- Your own rules still apply. This call does not widen what you may do: \
anything outward or irreversible still needs his verified yes, exactly as \
before.

First reply now, in ONE short Serbian sentence: say you are on the line and \
what you were working on."""

TURN = """LIVE CALL (agent bridge). Bogdan just said, out loud: «{said}»
Reply in one or two short spoken Serbian sentences; your text is read aloud \
to him as soon as this turn ends. No markdown."""

HANGUP = """CALL ENDED. Bogdan hung up the phone call ({why}). Nothing more from \
this call will be read aloud. {resume} If he asked you on the call for work \
you promised to do afterwards, do it now under your normal rules."""

RESUME_BUSY = ("Resume what you were doing before the call interrupted you, from "
               "where you stopped. The 'tool use was rejected, stop and wait' notice "
               "you got at the interrupt came from the bridge's Escape, which only made "
               "room for the call; it was not his verdict on your work, and it lapses "
               "now. (His warranted message asked for the interrupt to last until "
               "hangup.) Only if something he SAID on the call told "
               "you to stop or change course does that stand.")
RESUME_IDLE = ("You were idle when the call reached you, so there is nothing to "
               "resume unless you were waiting on something.")


class BridgeError(RuntimeError):
    """Something to say out loud, not a crash. `str()` is the spoken reason."""


# -- what the bridge needs from hotline -------------------------------------


@dataclass
class Agent:
    name: str           # what it is called: the registered name when it has one
    session: Any        # hotline.ccsocks.LiveSession
    task: str = ""
    pane: str = ""      # tmux target, "" when it runs headless
    denied: str = ""    # why it may not be connected, "" when it may


class Hotline:
    """hotline, in-process. Everything the bridge does to an agent goes
    through here, so tests replace this one object and nothing else.

    In-process rather than `hotline --to` subprocesses: the CLI would re-import
    hotline and re-verify the warrant against Discord on every turn (~0.4 s
    measured per spawn on this box); here it is verified once per process."""

    def __init__(self, deny: tuple[str, ...] | None = None) -> None:
        raw = os.environ.get("HOTLINE_IOS_BRIDGE_DENY")
        self.deny = tuple(n.strip().lower() for n in raw.split(",") if n.strip()) \
            if raw is not None else (deny or DEFAULT_DENY)

    def agents(self) -> list[Agent]:
        from hotline.agents import Registry
        from hotline.ccsocks import discover

        registry = Registry()
        by_session = {a.session_id: a for a in registry.agents.values()}
        out = []
        for session in discover():
            record = by_session.get(session.session_id)
            name = record.name if record is not None else session.name
            denied = ""
            if name.lower() in self.deny or session.name.lower() in self.deny:
                denied = "that agent is off limits to the call bridge"
            elif record is not None and getattr(record, "privileged", False):
                denied = "that is the operator; it is never interrupted by a call"
            elif not session.tmux:
                denied = "it runs without a tmux pane, so its turn cannot be interrupted"
            out.append(Agent(name=name, session=session,
                             task=(record.task if record is not None else "")[:140],
                             pane=session.tmux or "", denied=denied))
        return out

    def busy(self, agent: Agent) -> bool:
        from hotline.router import mid_turn

        return bool(mid_turn(agent.session))

    def interrupt(self, agent: Agent) -> None:
        from hotline import tmuxen

        asyncio.run(tmuxen.interrupt(agent.pane))

    def ask(self, agent: Agent, text: str, origin: Any, timeout: float) -> str:
        from dataclasses import replace

        from hotline.router import Router

        async def go() -> str:
            router = Router()
            stamped = replace(origin, awaiting_reply=True) if origin is not None else None
            watch = await router.deliver(agent.session.session_id, text, stamped)
            reply = await router.collect(watch, timeout=timeout)
            return reply.text

        return asyncio.run(go())

    def tell(self, agent: Agent, text: str, origin: Any) -> None:
        from hotline.router import Router

        asyncio.run(Router().deliver(agent.session.session_id, text, origin))


_WARRANT: dict[str, Any] = {}
_WARRANT_LOCK = threading.Lock()


def warrant() -> dict | None:
    """His Discord message asking for this feature, as a checked receipt, or None.

    `HOTLINE_IOS_BRIDGE_WARRANT` is `channel_id/message_id` (or a message link).
    Verified against Discord once per process, the way `hotline --to --warrant`
    verifies before sending: a warrant that does not check out is not sent,
    because attaching a bad one is worse than attaching none."""
    ref = os.environ.get("HOTLINE_IOS_BRIDGE_WARRANT", "").strip()
    if not ref:
        return None
    with _WARRANT_LOCK:
        if ref in _WARRANT:
            return _WARRANT[ref]
        parts = [p for p in ref.replace("https://discord.com/channels/", "").split("/") if p]
        record: dict | None = None
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
            record = {"kind": "human", "channel_id": parts[-2], "message_id": parts[-1]}
            try:
                from hotline.config import load_env
                from hotline.provenance import verify

                env = load_env()
                gated = env.get("DISCORD_USER_ID") or os.environ.get("DISCORD_USER_ID")
                verdict = verify(record, token=env.get("HOTLINE_BOT_TOKEN")
                                 or os.environ.get("HOTLINE_BOT_TOKEN"), gated_user_id=gated)
                if not verdict.ok:
                    log.error("bridge warrant %s does not verify (%s); sending none",
                              ref, verdict.summary)
                    record = None
                elif gated:
                    record["author_id"] = str(gated)
            except Exception:  # noqa: BLE001 - no warrant beats no call
                log.exception("could not verify the bridge warrant; sending none")
                record = None
        else:
            log.error("HOTLINE_IOS_BRIDGE_WARRANT=%r is not channel_id/message_id", ref)
        _WARRANT[ref] = record
        return record


def origin(label: str, kind: str = "voice") -> Any:
    """kind=voice: these are Whisper's words of something he said out loud, and
    hotline tells the receiver what that does and does not establish. The
    hang-up notice is kind=service: nobody said it, the bridge observed it."""
    try:
        from hotline.provenance import Origin

        return Origin(kind=kind, label=label, warrant=warrant())
    except Exception:  # noqa: BLE001 - an unlabelled turn beats a dropped one
        log.exception("could not build provenance for the bridge")
        return None


# -- speech -------------------------------------------------------------------


_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"(?<![\w.])(?:~|\.{0,2})?/(?:[\w.@-]+/)+[\w.@-]*")


def speakable(text: str, limit: int = MAX_SPOKEN) -> str:
    """The agent's written reply, made sayable. Strips markup and drops what a
    voice cannot carry (code blocks, URLs, paths); never adds a claim. This is
    the last line of defence behind the phone manners, not a substitute."""
    lines, in_code = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or set(line) <= set("-*_=|: "):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = _URL.sub("link", line)
        line = _PATH.sub("", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = " ".join(line.split())
        if line:
            lines.append(line if line[-1] in ".!?:;," else line + ".")
    spoken = " ".join(lines)
    if len(spoken) <= limit:
        return spoken
    cut = max(spoken.rfind(". ", 0, limit), spoken.rfind("? ", 0, limit))
    cut = cut + 1 if cut > limit // 3 else limit
    return spoken[:cut].rstrip() + " Odgovor je duži, ovo je početak."


def markdown_leaks(text: str) -> list[str]:
    """What in a raw reply would have been read aloud as junk. For the bench."""
    found = []
    for name, pattern in (("code", r"```|`"), ("bold", r"\*\*"), ("heading", r"(?m)^#"),
                          ("list", r"(?m)^\s*(?:[-*+]|\d+[.)])\s"), ("url", r"https?://"),
                          ("path", r"(?:~|/home)/\S+")):
        if re.search(pattern, text):
            found.append(name)
    return found


# -- the bridge -------------------------------------------------------------


@dataclass
class Turn:
    said: str
    reply: str = ""
    seconds: float = 0.0
    error: str = ""


@dataclass
class AgentBridge:
    """One call's link to at most one agent at a time."""

    mode: str = DEFAULT_MODE
    hotline: Any = field(default_factory=Hotline)
    connect_seconds: float = CONNECT_SECONDS
    answer_seconds: float = ANSWER_SECONDS
    keepalive_seconds: float = KEEPALIVE_SECONDS
    settle_seconds: float = SETTLE_SECONDS
    agent: Agent | None = None
    was_busy: bool = False
    turns: list[Turn] = field(default_factory=list)
    events: list[tuple[float, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            log.error("unknown bridge mode %r; using direct", self.mode)
            self.mode = "direct"
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.agent is not None

    def _event(self, kind: str, text: str) -> None:
        self.events.append((time.time(), kind, text))
        log.info("bridge %s: %s", kind, text[:200])

    # -- names ----------------------------------------------------------

    def roster(self) -> str:
        """The live agents, one line each, for Haiku to pick a name from."""
        try:
            agents = self.hotline.agents()
        except Exception as exc:  # noqa: BLE001 - said out loud, not raised
            log.exception("could not list agents")
            return f"could not list agents: {exc}"
        lines = [f"{a.name}: {a.task or '(no task declared)'}"
                 + (f" [NOT CONNECTABLE: {a.denied}]" if a.denied else "")
                 for a in agents]
        return "\n".join(lines) or "no live agents"

    def find(self, spoken: str) -> Agent:
        """The one live agent `spoken` names, or BridgeError saying why not.

        Exact first (spaces for hyphens, as speech recognition writes them),
        then a unique prefix or substring. Ambiguity is never resolved by
        picking one: the wrong agent interrupted is the failure that costs."""
        want = " ".join(spoken.lower().strip(" .,!?\"'«»").split())
        if not want:
            raise BridgeError("nije rečeno koji agent")
        agents = self.hotline.agents()
        forms = {want, want.replace(" ", "-"), want.replace(" ", "")}
        hits = [a for a in agents if a.name.lower() in forms
                or getattr(a.session, "name", "").lower() in forms]
        if not hits:
            for test in (lambda n: n.startswith(want.replace(" ", "-")),
                         lambda n: want.replace(" ", "-") in n,
                         lambda n: want.replace(" ", "") in n.replace("-", "")):
                hits = [a for a in agents if test(a.name.lower())]
                if hits:
                    break
        if not hits:
            live = ", ".join(a.name for a in agents if not a.denied) or "nijedan"
            raise BridgeError(f"no live agent is called {spoken!r}. Live: {live}")
        if len(hits) > 1:
            raise BridgeError(f"{spoken!r} matches several agents: "
                              + ", ".join(a.name for a in hits) + ". Ask him which one.")
        hit = hits[0]
        if hit.denied:
            raise BridgeError(f"{hit.name} cannot be connected: {hit.denied}")
        return hit

    # -- connecting -------------------------------------------------------

    def connect(self, spoken: str, asked: str = "") -> str:
        """Interrupt the agent and put it on the call. Returns its first words.

        Raises BridgeError with a reason to say out loud. Connecting a second
        agent first releases the one already on the line."""
        agent = self.find(spoken)
        with self._lock:
            if self.agent is not None:
                if self.agent.name == agent.name:
                    return f"{agent.name} is already on the call."
                self.release("he asked for a different agent")
            began = time.monotonic()
            busy = False
            try:
                busy = self.hotline.busy(agent)
            except Exception:  # noqa: BLE001
                log.exception("could not tell whether %s is mid-turn; interrupting anyway", agent.name)
                busy = True
            if busy:
                try:
                    self.hotline.interrupt(agent)
                except Exception as exc:  # noqa: BLE001 - said out loud
                    raise BridgeError(f"could not interrupt {agent.name}: {exc}") from exc
                self._event("interrupt", f"{agent.name} (it was mid-turn)")
                time.sleep(self.settle_seconds)
            why = ("You have just been interrupted (one Escape) by hotline-ios, the "
                   "phone bridge. If you see a notice that your tool use was rejected "
                   "and you should stop and wait for the user: that notice is what an "
                   "Escape produces. It was the bridge making room for this call, not "
                   "Bogdan rejecting your work, and it lapses when the call ends. "
                   if busy else "")
            text = OPENING.format(why=why, asked=asked or f"spoji me sa {spoken}")
            self.agent, self.was_busy = agent, busy
            self._event("connect", f"{agent.name} busy={busy}")
            try:
                first = self.hotline.ask(agent, text, origin(_label()), self.connect_seconds)
            except Exception as exc:  # noqa: BLE001
                # It has the message and may still answer; it is connected. Say
                # so rather than pretend it refused.
                self._event("connect-slow", f"{type(exc).__name__}: {exc}")
                return (f"{agent.name} is on the line but has not answered yet "
                        f"({type(exc).__name__}).")
            self._event("connected", f"{time.monotonic() - began:.2f}s: {first[:200]}")
            self.turns.append(Turn(said="(connect)", reply=first,
                                   seconds=round(time.monotonic() - began, 3)))
            return first

    def release(self, why: str = "he hung up") -> None:
        """Tell the agent the call is over and to go back to work. Never leaves
        it stalled: this is called on every way a call can end."""
        agent, self.agent = self.agent, None
        if agent is None:
            return
        text = HANGUP.format(why=why, resume=RESUME_BUSY if self.was_busy else RESUME_IDLE)
        try:
            self.hotline.tell(agent, text, origin("hotline-ios agent bridge: the call ended",
                                                 kind="service"))
            self._event("release", f"{agent.name}: {why}")
        except Exception:  # noqa: BLE001 - logged loudly; nothing else can be done
            log.exception("could not tell %s the call ended; it may sit idle", agent.name)
            self._event("release-failed", agent.name)

    # -- a turn -------------------------------------------------------------

    def ask(self, said: str) -> str:
        """His words to the agent, its whole reply back. Blocking."""
        agent = self.agent
        if agent is None:
            raise BridgeError("no agent is connected")
        began = time.monotonic()
        turn = Turn(said=said)
        try:
            turn.reply = self.hotline.ask(agent, TURN.format(said=said), origin(_label()),
                                          self.answer_seconds)
        except Exception as exc:  # noqa: BLE001
            turn.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            turn.seconds = round(time.monotonic() - began, 3)
            self.turns.append(turn)
        return turn.reply

    def _waiting(self, said: str) -> Iterator[str | None | tuple[str, str]]:
        """Keepalive text while the agent works, then ("reply", text) or ("err", why)."""
        box: dict[str, str] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["reply"] = self.ask(said)
            except Exception as exc:  # noqa: BLE001
                box["err"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name="bridge-ask").start()
        spoken = 0
        while not done.wait(self.keepalive_seconds):
            yield KEEPALIVES[spoken % len(KEEPALIVES)] + " "
            yield None
            spoken += 1
        if "err" in box:
            yield ("err", box["err"])
        else:
            yield ("reply", box.get("reply", ""))

    def direct_stream(self, said: str) -> Iterator[str | None]:
        """`ask_stream` for direct mode: the agent's reply, spoken as written."""
        for item in self._waiting(said):
            if isinstance(item, tuple):
                kind, text = item
                if kind == "err":
                    yield "Agent nije odgovorio na vreme. "
                else:
                    yield speakable(text) or "Agent nije ništa rekao."
                return
            yield item

    def relay_stream(self, said: str, haiku: Any) -> Iterator[str | None]:
        """`ask_stream` for relay mode: the agent answers, Haiku says it."""
        for item in self._waiting(said):
            if isinstance(item, tuple):
                kind, text = item
                if kind == "err":
                    note = (f"Bogdan je rekao: «{said}». Agent nije odgovorio na vreme "
                            f"({text}). Reci mu to kratko.")
                else:
                    note = relay_note(said, text)
                yield from haiku.reply_stream(note, lookups=False)
                return
            yield item

    def stream_for(self, haiku: Any) -> Callable[[str], Iterator[str | None]]:
        """The call's `ask_stream`: Haiku until an agent is connected, then the
        agent in the configured mode. Leaving the agent is recognised here in
        both modes, so it does not depend on Haiku noticing."""
        def ask_stream(said: str) -> Iterator[str | None]:
            if self.connected and RELEASE.search(said):
                name = self.agent.name if self.agent else ""
                self.release("he asked to leave the agent and stay on the call")
                return haiku.reply_stream(
                    f"{said}\n[Sistem: agent {name} je isključen iz poziva i vraćen na "
                    "posao. Ti si ponovo glas poziva; potvrdi mu to kratko.]")
            if not self.connected:
                return haiku.reply_stream(said)
            if self.mode == "relay":
                return self.relay_stream(said, haiku)
            return self.direct_stream(said)
        return ask_stream

    def may_speculate(self, text: str) -> bool:
        return not self.connected and not CONNECT_WORDS.search(text)

    # -- Haiku's tools --------------------------------------------------------

    def tools(self) -> dict[str, tuple[dict, Callable[..., str]]]:
        """name -> (schema, fn) for `HaikuAgent(extra_tools=...)`."""
        return {
            "list_agents": ({
                "name": "list_agents",
                "description": "The live hotline agents (Claude sessions) that could be "
                               "connected to this call, one per line: name, task.",
                "input_schema": {"type": "object", "properties": {}},
            }, lambda: self.roster()),
            "connect_agent": ({
                "name": "connect_agent",
                "description": (
                    "Put a live hotline agent on this call, when Bogdan asks for it "
                    "('spoji me sa X', 'poveži me sa X', 'dodaj X u poziv', 'add X to the "
                    "call'). Interrupts what it is doing. `name` is the agent's exact "
                    "name from list_agents. If the name he said is unclear or matches "
                    "several, ASK him instead of guessing. Returns the agent's first "
                    "words, or why it could not be connected."),
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"},
                                   "his_words": {"type": "string",
                                                 "description": "What he said, verbatim."}},
                    "required": ["name"],
                },
            }, self._connect_tool),
        }

    def _connect_tool(self, name: str, his_words: str = "") -> str:
        try:
            first = self.connect(name, his_words)
        except BridgeError as exc:
            return f"NOT CONNECTED: {exc}"
        agent = self.agent.name if self.agent else name
        if self.mode == "direct":
            return (f"CONNECTED to {agent}. Its first words: «{first}»\n"
                    "From his next sentence on he talks to the agent DIRECTLY and you "
                    "are out of the conversation. Now say only, in one short sentence, "
                    "that he is connected, and repeat the agent's first words.")
        return (f"CONNECTED to {agent}. Its first words: «{first}»\n"
                "From now on everything he says goes to the agent and you will be given "
                "its answers to speak. Now tell him briefly that he is connected and "
                "what the agent said. Add nothing of your own.")

    def manners(self) -> str:
        """Appended to Haiku's system prompt on a call where this is available."""
        return (
            "AGENTS: Bogdan can ask you to connect one of his hotline agents to this "
            "call ('spoji me sa manager pet', 'dodaj tells u poziv'). Say a few words "
            "first ('Samo sekund, povezujem.'), then call connect_agent with the exact "
            "name; use list_agents if you are not sure which one he means. Never "
            "claim an agent said or did something that its own words do not say.\n"
            f"Live agents when this call started:\n{self.roster()}")


def relay_note(said: str, reply: str) -> str:
    """What Haiku is given in relay mode: his words, the agent's, and the rule.

    First person and no name, because "Bridge-test-agent-cf kaže da..." before
    every answer was the first thing the relay bench produced: the name read
    aloud with its hyphens, six times in six turns."""
    return (f"Bogdan je rekao: «{said}».\nPovezani agent je odgovorio: «{reply}»\n"
            "Izgovori mu odgovor agenta, u prvom licu agenta, kratko i prirodno za "
            "telefon. Ne ponavljaj ime agenta, ne počinji sa 'agent kaže'. Ne dodaj "
            "ništa što agent nije rekao i ne odgovaraj umesto njega.")


def _label() -> str:
    return ("Bogdan, spoken on a live phone call (hotline-ios agent bridge; "
            "Whisper transcription of his voice)")
