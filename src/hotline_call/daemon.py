"""`hotline-iosd` -- the service a Claude session calls to ring Bogdan's phone.

Runs beside `hotlined` rather than inside it. Three reasons, in order of how
much they matter:

1. **`hotlined` must keep working when this does not.** It carries the Discord
   bridge and the iPhone Shortcut path, which are the fallbacks this thing
   degrades to. Putting the experimental ringer in the same process as its own
   safety net is how you lose both at once.
2. **Model instances must not be shared.** hotline's `Transcriber`/`Speaker`
   `load()`/`unload()` are not reference-counted, so a Discord call hanging up
   would unload the model out from under a live phone call. `bot.py` already
   gives each call its own; this follows that, and pays ~1.5 GB of VRAM for it.
3. `HotlineBot.call` is a single-slot attribute and refuses a second join. A
   separate process sidesteps that entirely rather than reworking it.

It reuses hotline's `httpd.Server` -- roughly a hundred lines Bogdan has already
read -- rather than adding a web framework, and gates on the same source-IP
allowlist that `hotlined` uses.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

from . import ingest, vitals
from .endpoint import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOOPBACK,
    bind_hosts,
    local_url,
    unreachable,
)
from .events import Entry, EventLog, Waker
from .ingest import Ingested
from .ring.base import (
    CallDeclined,
    CallError,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
)
from .store import UNATTRIBUTED, Store

log = logging.getLogger("hotline-iosd")

MAX_WAIT = 30.0
TURN_TIMEOUT = 900.0
"""Ceiling on a long-poll. Under most proxy and NAT idle timeouts."""

HYDRATE_WINDOW = 3600.0
"""How far back a restart reads conversations into its in-memory index.

The same hour `reap()` keeps them for, so a restart lands in the state the
process would have been in anyway. Anything older is still in the database and
still reachable through `/api/v1/agents/history`; this is only about what is
warm."""

PING_PATH = "/api/v1/ping"
"""A route that answers and does nothing else, so `/health` can verify local
reachability without recursing into itself or moving any counter."""

HOOK_PATH = "/api/v1/hook"

ROSTER_POLL = 3.0
"""How often a parked `roster-events` waiter recomputes the roster.

There is no background heartbeat -- liveness is a per-request check, which is
stronger than an interval. But a change in liveness is only *observed* when
somebody computes the roster, so the long-poll does that computation itself
while it waits. The cost is one `discover()` per interval per waiting client and
exactly nothing when nobody is listening."""

STALL_AFTER = 600.0
"""Busy with no tool call for this long reads as stalled.

A guess, and labelled as one: it is a `stalled` flag, not a liveness claim --
liveness is already checked properly. Tune it once the transcript hook is
feeding `last_tool_at` from something other than the daemon's own turns."""

HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionStart",
               "StatusLine")
"""What `/api/v1/hook` will act on. Anything else is accepted and counted, not
refused: a hook installed by a future version must never fail a real turn
because this daemon has not been taught the name yet."""

TURN_ENDING_EVENTS = ("Stop", "SubagentStop")
"""The nudges that close a phase. The transcript's own end-of-turn markers
(`system/turn_duration`, `system/stop_hook_summary`) are written *after* Stop
fires, so waiting for them would leave every phase open until the next prompt."""

SAFETY_POLL = 30.0
"""How often the safety poll sweeps live sessions.

Not the primary path -- the hook is. This catches the cases the hook cannot: a
session started before the hook was installed, a nudge dropped while the daemon
was restarting, a hook that hit its own 30 s backoff. Cheap because it reads
only what the stored offset says is new."""

STOP_DEBOUNCE = 2.0
"""A repeat `stop` on the same agent inside this many seconds is refused.

§4: sub-200 ms double-Escape behaviour is unverified, and designing around a
guess is how you get a cancel that sometimes cancels twice. It also makes a
client retry-after-timeout safe -- the second request is told the first one
landed rather than firing a second keystroke into a session that has already
been cancelled."""

PENDING_DURATIONS = 64
"""How many not-yet-placed tool durations to hold per agent.

One per tool call in flight plus slack. Past this the oldest is dropped and
counted on `/health` -- a bound that says so beats an unbounded dict that grows
quietly on an agent whose transcript has stopped being readable."""

SPAWN_TIMEOUT = 45.0
"""How long `resume` and `new` wait for a fresh session to register itself.

Half `tmuxen.spawn`'s own default: this is a request from a phone, and a minute
and a half of a spinner is worse than an honest failure that can be retried."""

COMPACT_SETTLE = 0.6
"""Between the Escape and typing `/compact`.

Pacing a terminal, not waiting for work: the CLI has to finish cancelling before
it will accept a new line. The actual completion of compaction is watched for
and is never a timer."""

COMPACT_POLL = 0.5
COMPACT_TIMEOUT = 180.0
"""How long to watch for the compaction boundary before giving up and saying so.

The spike measured a real compaction at 71 s. This is a bound on the wait, not a
substitute for the signal -- expiring means `compacted: false` with the reason,
never a success declared by the clock."""

CONTINUE_AFTER_COMPACT = (
    "Your context was just compacted from the hotline app. Pick up exactly where "
    "you left off -- read the compaction summary above for what you were doing, "
    "and carry on without starting over."
)
"""The default continuation `compact` injects when no `then` was supplied."""

ROSTER_FIELDS = ("task", "live", "busy", "state", "stalled", "blocked",
                 "retired", "historyGeneration", "authority")
"""What counts as a roster change worth waking a phone for.

Deliberately excludes `blockedSince` and `cwd`: a timestamp that only moves
because the thing it describes moved is not independently newsworthy, and
ticking on it would make the invalidation stream fire on its own output.

`authority` is in here despite almost never moving. When it does move it is
because he granted or revoked a standing role, which is exactly the kind of
change a phone holding the old row would otherwise show wrong until the next
poll -- and it costs one comparison per row against a value that is `None` for
nearly all of them."""


class Service:
    def __init__(
        self,
        transport: Any,
        pool: Any,
        *,
        transcriber: Any = None,
        speaker: Any = None,
        allow_ips: set[str] | None = None,
        api_key: str = "",
        page_fallback: Any = None,
        segmenter_factory: Any = None,
        store: Store | None = None,
    ) -> None:
        self.transport = transport
        self.pool = pool
        self.transcriber = transcriber
        self.speaker = speaker
        self.allow_ips = allow_ips or set()
        self.api_key = api_key
        self.page_fallback = page_fallback
        # Injectable so a test can be deterministic, and so a transport with an
        # unusual rate can tune the VAD. None means hotline's Segmenter.
        self.segmenter_factory = segmenter_factory
        # The in-memory INDEX over persisted rows, not the record itself. The
        # store is the record. This holds the conversations that are warm --
        # recent or still open -- because that is what `EventLog`'s long-poll
        # wake and its `gap`/`dropped` accounting need to work against, and
        # those already work. `reap()` evicts from here and deletes nothing:
        # deletion from the database is `purge` and nothing else (§3).
        self.calls: dict[str, EventLog] = {}
        # When each conversation was opened, so closed ones can be reaped.
        # EventLog has no timestamp of its own and adding one there would put
        # wall-clock into a structure whose tests are all deterministic.
        self.call_opened: dict[str, float] = {}
        # conversation -> the agent it belongs to, so an append does not have to
        # go back to the database to find out where to file itself.
        self.call_agent: dict[str, str] = {}
        # name -> transport, so a caller's --transport can be honoured or
        # refused rather than silently ignored. Populated by set_links(); a
        # Service built directly in a test just has the one default.
        self.links: dict[str, Any] = {}
        # Live sessions, so the phone can end a call from its own UI rather
        # than only by the far end hanging up.
        self.sessions: dict[str, Any] = {}
        self.started = time.time()
        # False until the doorbell's start() has succeeded. A transport that
        # could not start cannot ring, and /health reporting ok:true in that
        # state is the same lie as reporting ok:true on a loopback -- it was
        # doing exactly that with "sip is not configured" sitting in
        # degradations right beside it.
        self.ring_ready = False
        self.degradations: list[str] = []
        # Hook accounting, all reported on /health. Counters rather than a
        # boolean because "the map is empty" and "the map is empty and 4000
        # nudges were dropped" are very different situations and used to look
        # identical.
        self.hook_events = 0
        self.unattributed_hook_events = 0
        self.hook_parse_failures = 0
        # Durations that arrived for a tool call this daemon has no row for --
        # a dropped PreToolUse nudge, or a read that is behind. Counted rather
        # than ignored: the app renders no duration bar for those rows, and a
        # rising number here is the only way to tell that from "nothing ran".
        self.tool_durations_unmatched = 0
        # agent -> {tool_use_id: duration_ms} that arrived before the row they
        # belong to existed. Measured on a real session: `PostToolUse` landed
        # 244 ms after `PreToolUse` for a trivial Bash call, which is less than
        # it takes the nudge in front of it to read the transcript and commit.
        # Dropping those would mean fast tools -- most of them -- never getting
        # a duration bar, which would look like the feature working badly rather
        # than a race.
        self._pending_durations: dict[str, dict[str, float]] = {}
        # agent -> why its offset stopped advancing. Non-empty means the map is
        # knowingly behind for that agent rather than knowingly complete.
        self.ingest_stalled: dict[str, str] = {}
        # One lock per agent. Two nudges for the same session can arrive while
        # the first read is still in flight, and both would read from the same
        # stored offset and write every event twice.
        self._ingest_locks: dict[str, asyncio.Lock] = {}
        self.store = store if store is not None else self._open_store()
        # One wake per agent for the agent-scoped feed, and one for the roster.
        # Same broadcast primitive `EventLog` uses; see `events.Waker`.
        self._agent_wakers: dict[str, Waker] = {}
        self._roster_waker = Waker()
        # None until the first roster computation. A restart must not tick every
        # agent as "changed" just because it has nothing to compare against.
        self._roster_snapshot: dict[str, dict[str, Any]] | None = None
        # Where this process is actually listening, filled in by `build_server`.
        # `/health` verifies the local URL rather than remembering that it was
        # configured, because the two disagreed once and nothing noticed.
        self.listen_hosts: list[str] = []
        self.listen_port = DEFAULT_PORT
        # agent -> when it was last interrupted, on the monotonic clock. Wall
        # time would let an NTP step turn a debounce into a permanent refusal.
        self._last_stop_at: dict[str, float] = {}
        # Assistant output samples, in memory on purpose -- see `vitals.py`.
        self.rates = vitals.Rates()
        self._hydrate()

    # ---- where this daemon can be reached --------------------------------

    def bound_to(self, hosts: Sequence[str], port: int) -> None:
        self.listen_hosts = list(hosts)
        self.listen_port = int(port)

    @property
    def hook_url(self) -> str:
        """The URL local tooling -- the hook, the statusline wrapper -- must use.

        Read off the port actually being served rather than off a constant, so
        this cannot report an address the daemon is not on.
        """
        return local_url(HOOK_PATH, self.listen_port)

    async def reachable_locally(self, timeout: float = 2.0) -> tuple[bool, str]:
        """Can something on this box actually reach us on the hook's URL?

        A real request over TCP, made now. Not a boot-time memory and not a
        reading of the bind list -- the failure this exists to catch is exactly
        the one where the configuration says one thing and the socket says
        another.

        It targets `/api/v1/ping` rather than `/health` so that health checking
        itself does not recurse, and so the probe stays cheap enough to run on
        every poll.
        """
        host, port = LOOPBACK, self.listen_port
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except (TimeoutError, OSError) as exc:
            return False, f"{host}:{port} is not accepting connections ({exc})"
        try:
            headers = f"GET {PING_PATH} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if self.api_key:
                headers += f"X-Hotline-Key: {self.api_key}\r\n"
            writer.write((headers + "Connection: close\r\n\r\n").encode())
            await asyncio.wait_for(writer.drain(), timeout)
            status = await asyncio.wait_for(reader.readline(), timeout)
        except (TimeoutError, OSError) as exc:
            return False, f"{host}:{port} accepted a connection but did not answer ({exc})"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        line = status.decode("latin-1", "replace").strip()
        if " 200 " not in line:
            return False, f"{host}:{port}{PING_PATH} answered {line!r}"
        return True, ""

    # ---- the store -------------------------------------------------------

    def _open_store(self) -> Store:
        """Open the durable store, or run without one rather than not run.

        A daemon that refuses to boot because its database will not open is
        worse than one that rings his phone and forgets afterwards -- ringing is
        the job. So a failure here degrades to an in-memory database, which
        keeps every code path identical, and says so in the two places that are
        actually read: the log and `/health`.
        """
        try:
            return Store()
        except Exception as exc:  # see docstring
            log.exception("could not open the store; running without persistence")
            self.degradations.append(
                f"store unavailable ({type(exc).__name__}: {exc}); "
                "nothing is being persisted and history will be empty"
            )
            return Store(":memory:")

    def _hydrate(self) -> None:
        """Warm the in-memory index from the database at boot.

        This is the whole point of the store from his side: an unanswered
        question used to die with the process, so a daemon restart threw away
        the thing he was about to answer.
        """
        try:
            rows = self.store.conversations(limit=200)
        except Exception:  # a cold index beats a dead daemon
            log.exception("could not hydrate conversations from the store")
            return
        cutoff = time.time() - HYDRATE_WINDOW
        for row in rows:
            if row["closed_at"] is not None and float(row["opened_at"]) < cutoff:
                continue
            self._load_channel(row)

    def _load_channel(self, row: dict[str, Any]) -> EventLog:
        events = EventLog()
        try:
            for stored in self.store.conversation_tail(str(row["id"])):
                events.adopt(Entry(seq=stored.seq, kind=stored.kind, text=stored.text,
                                   tool=stored.tool, at=stored.at))
        except Exception:
            log.exception("could not read conversation %s back", row["id"])
        if row["closed_at"] is not None:
            events.closed = True
        conversation = str(row["id"])
        self.calls[conversation] = events
        self.call_opened[conversation] = float(row["opened_at"])
        self.call_agent[conversation] = str(row["agent_name"])
        return events

    def _channel(self, conversation: str) -> EventLog | None:
        """The in-memory log for a conversation, reading it back in if it is cold.

        A conversation reaped out of the index -- or opened by a previous
        process -- is still answerable, which it was not before. A conversation
        that never existed is still None, so `/api/v1/events` and `/api/v1/reply`
        keep their 404.
        """
        existing = self.calls.get(conversation)
        if existing is not None:
            return existing
        try:
            row = self.store.conversation(conversation)
        except Exception:
            log.exception("could not look up conversation %s", conversation)
            return None
        return self._load_channel(row) if row is not None else None

    def _waker(self, agent: str) -> Waker:
        waker = self._agent_wakers.get(agent)
        if waker is None:
            waker = self._agent_wakers[agent] = Waker()
        return waker

    def _append(
        self,
        conversation: str,
        kind: str,
        text: str,
        tool: str | None = None,
        at: float | None = None,
        client_token: str | None = None,
    ) -> Entry:
        """Persist one event, then index it. The store assigns the sequence.

        Order matters: the database is what hands out `seq`, so it has to be
        written first. If it cannot be, the conversation continues on a number
        one past whatever the index already holds -- monotonic within the
        conversation, which is all a client's `since` cursor requires -- and the
        failure is recorded rather than swallowed.
        """
        at = time.time() if at is None else at
        agent = self.call_agent.get(conversation, UNATTRIBUTED)
        events = self._channel(conversation)
        try:
            stored = self.store.append_event(
                agent, kind, text, conversation_id=conversation, tool=tool, at=at,
                client_token=client_token,
            )
            entry = Entry(seq=stored.seq, kind=kind, text=text, tool=tool, at=at)
        except Exception as exc:  # a full disk must not drop his question
            log.exception("could not persist %s event on %s", kind, conversation)
            self.degradations.append(f"event not persisted ({type(exc).__name__}: {exc})")
            seq = (events.latest + 1) if events is not None else 1
            entry = Entry(seq=seq, kind=kind, text=text, tool=tool, at=at)
        if events is not None:
            events.adopt(entry)
        self._waker(agent).wake()
        return entry

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
        """Conversations still awaiting an answer.

        `len(self.calls)` counted every conversation ever opened, so three
        --no-wait smoke tests read as three live calls forever. A number that
        only goes up is not a signal.
        """
        return sum(1 for events in self.calls.values() if not events.closed)

    def reap(self, *, older_than: float = 3600.0) -> int:
        """Evict closed conversations from the in-memory index after an hour.

        Only closed ones: an unanswered call stays, because he may open the app
        later and answer it, and dropping it would throw away the question he
        is about to answer.

        **This deletes nothing.** It used to be the only thing bounding memory
        and it still is, but the rows survive in the store and `_channel()`
        reads a reaped conversation straight back in. Real deletion is `purge`
        and only `purge` -- an automatic retention policy is exactly what §3
        decided against.
        """
        now = time.time()
        stale = [
            call_id
            for call_id, events in self.calls.items()
            if events.closed and now - self.call_opened.get(call_id, now) > older_than
        ]
        for call_id in stale:
            self.calls.pop(call_id, None)
            self.call_opened.pop(call_id, None)
            self.call_agent.pop(call_id, None)
        return len(stale)

    def _open_conversation(self, agent: str | None, kind: str) -> tuple[str, EventLog]:
        """Mint a conversation, persist it, and index it. One place, two callers.

        `kind` is `"ring"` or `"say"` and it is what decides whether the agent
        counts as blocked on him: a ring is an agent waiting for an answer, a
        `say` is him giving one an instruction. Getting that backwards would put
        a blocked pin on every agent he ever talked to.
        """
        conversation = uuid.uuid4().hex[:12]
        name = self.resolve_agent(agent)
        opened = time.time()
        events = EventLog()
        self.calls[conversation] = events
        self.call_opened[conversation] = opened
        self.call_agent[conversation] = name
        try:
            self.store.open_conversation(
                conversation, name, kind, opened_at=opened,
                waiting_since=opened if kind == "ring" else None,
            )
        except Exception as exc:  # the ring matters more than the record
            log.exception("could not persist conversation %s", conversation)
            self.degradations.append(
                f"conversation not persisted ({type(exc).__name__}: {exc})"
            )
        # No explicit roster tick here. A ring changes `blocked`, which the
        # roster diff already watches; waking the parked long-poll makes it
        # recompute now rather than at the next interval, and keeping one source
        # of ticks is worth more than saving it three seconds.
        self._roster_waker.wake()
        return conversation, events

    def _close_conversation(self, conversation: str) -> None:
        """Close it in the index and in the store, and tell the roster."""
        events = self._channel(conversation)
        if events is not None and not events.closed:
            events.close()
        try:
            if self.store.close_conversation(conversation):
                agent = self.call_agent.get(conversation)
                if agent:
                    self._waker(agent).wake()
                    self._roster_waker.wake()
        except Exception:
            log.exception("could not close conversation %s in the store", conversation)

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
        target = CallTarget(
            device=str(body.get("device", "phone")),
            agent=str(agent) if agent else None,
            reason=reason,
            caller_id=str(body.get("source", "Claude")),
        )
        ring_timeout = float(body.get("ring_timeout", 45.0))
        reply_timeout = float(body.get("timeout", 900.0))
        wait = bool(body.get("wait", True))

        doorbell = self._resolve_transport(str(body.get("transport", "")))

        conversation, events = self._open_conversation(agent, "ring")
        # Put the question in the conversation before ringing, so that whenever
        # he opens the app -- during the ring, or an hour later -- it is already
        # there and he never answers a phone to silence.
        self._append(conversation, "claude", f"{target.caller_id}: {reason}")
        if str(body.get("context", "")):
            self._append(conversation, "summary", str(body["context"])[:1200])
        began = time.monotonic()

        try:
            await doorbell.ring(target, timeout=ring_timeout)
        except CallDeclined as exc:
            self._append(conversation, "state", "declined")
            self._close_conversation(conversation)
            return self._outcome(conversation, "declined", began, str(exc), transport=doorbell)
        except CallUnanswered as exc:
            # It rang and he did not pick up. The conversation stays OPEN: he
            # may well open the app five minutes later, and closing it here
            # would throw away the question he is about to answer.
            self._append(conversation, "state", "unanswered")
            return self._outcome(conversation, "unanswered", began, str(exc), transport=doorbell)
        except (CallUnreachable, CallError) as exc:
            self.degradations.append(str(exc))
            log.warning("call %s undeliverable: %s", conversation, exc)
            self._append(conversation, "error", str(exc))
            self._close_conversation(conversation)
            return self._outcome(conversation, "unreachable", began, str(exc), transport=doorbell)

        if not wait:
            return self._outcome(conversation, "ringing", began, "not waiting", transport=doorbell)

        reply = await self._await_reply(events, reply_timeout)
        if not reply:
            return self._outcome(conversation, "unanswered", began,
                                 f"rang, but nothing came back within {reply_timeout:.0f}s",
                                 transport=doorbell)
        return self._outcome(conversation, "answered", began, "", reply, transport=doorbell)

    async def _await_reply(self, events: EventLog, timeout: float) -> str:
        """Block until he types something in the app, or time runs out."""
        deadline = time.monotonic() + timeout
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

    def _registry_record(self, agent: str) -> Any:
        """hotline's registry entry for a name, or None. The one lookup.

        Was inlined in `_bind`; conversations now need the same answer at open
        time, and two copies of "ask the registry what this name really is"
        would be two chances to disagree about what `hotline-80` means.
        """
        try:
            from hotline.agents import Registry

            return Registry().by_name(agent)
        except Exception:  # hotline may not be installed at all
            log.exception("could not look %s up in the registry", agent)
            return None

    def resolve_agent(self, agent: str | None) -> str:
        """The name a conversation is filed under, canonicalised.

        Canonical because the registry's name is the identity Bogdan types --
        `connect hotline-80` -- and filing under whatever casing or alias the
        caller happened to use would split one agent's channel in two.

        Unnamed callers go to the `UNATTRIBUTED` bucket rather than nowhere.
        `hotline-call --agent` defaults to None, so a plain ring naming no agent
        is the common case, not an edge case, and the question he is about to
        answer has to be filed somewhere.
        """
        if not agent:
            return UNATTRIBUTED
        record = self._registry_record(str(agent))
        name = str(record.name) if record is not None else str(agent)
        try:
            if record is not None:
                self.store.ensure_agent(
                    name,
                    session_id=record.session_id,
                    task=record.task,
                    declared_at=record.declared_at,
                    completed_at=record.completed_at,
                )
            else:
                self.store.ensure_agent(name)
        except Exception:  # annotation is not worth losing a call over
            log.exception("could not record agent %s", name)
        return name

    async def _bind(self, key: str, agent: str) -> None:
        try:
            bind = getattr(self.pool, "bind", None)
            if bind is None:
                return
            record = self._registry_record(agent)
            if record is not None:
                bind(key, record.name, record.session_id)
        except Exception:
            # A failed bind means the call lands on the newest session instead
            # of the named one. Worth logging, never worth dropping the call.
            log.exception("could not bind %s to %s", key, agent)

    def _outcome(
        self,
        call_id: str,
        state: str,
        began: float,
        detail: str = "",
        reply: str = "",
        transport: Any = None,
    ) -> dict[str, Any]:
        used = transport if transport is not None else self.transport
        out: dict[str, Any] = {
            "call_id": call_id,
            "conversation": call_id,
            "state": state,
            "reply": reply,
            "detail": detail,
            "waited_seconds": round(time.monotonic() - began, 1),
            "transport": getattr(used, "name", "?"),
            "rings_when_closed": bool(getattr(used, "rings_when_closed", False)),
            # Callers cannot otherwise tell a ring from a no-op: "answered via
            # loopback+confirmed" and "answered via sip+confirmed" are the same
            # sentence. This is the field that distinguishes them, and
            # hotline-call refuses to report success when it is true.
            "fake": bool(getattr(used, "is_fake", False)),
        }
        return out

    async def hang_up(self, call_id: str) -> dict[str, Any]:
        """Dismiss a conversation from the phone's side.

        Idempotent, and a missing call is not an error: the app can send this
        after the far end has already ended, and turning that race into a 404
        would make the phone show a failure for something that worked.
        """
        pending = self.sessions.pop(call_id, None)
        if pending is not None:
            pending.cancel()
        events = self._channel(call_id)
        if events is not None and not events.closed:
            self._append(call_id, "state", "ended")
            self._close_conversation(call_id)
        return {
            "call_id": call_id,
            "ended": events is not None,
            # Always false, and it is not a placeholder. This endpoint ends the
            # daemon's own wait -- it cancels the asyncio task awaiting a reply
            # and closes the conversation. The agent on the other end keeps
            # running, untouched, and never learns this happened. Reporting
            # anything else here would be the same lie as a /health that says ok
            # while the pager rings nothing. Stopping the process is `kill`, and
            # cancelling its turn is `stop`; both are separate endpoints because
            # they are separate acts.
            "process_stopped": False,
        }

    # ---- delegation: what the app is actually for -------------------------

    def _roster_rows(self) -> list[dict[str, Any]]:
        """Every agent hotline knows about, annotated with what this daemon knows.

        Reads hotline's registry and cross-references live sessions, because a
        registry record outlives the process it describes -- a name in the file
        is not evidence anything is running, and showing him a dead agent to
        talk to is worse than showing him none.

        Liveness is computed here, per request, and is not cached anywhere. That
        is stronger than a heartbeat interval, not weaker: `discover()`
        re-validates the pid's start time and the control socket every time, so
        a crashed process reads as gone immediately rather than after a poll.
        """
        try:
            from hotline.agents import Registry
            from hotline.ccsocks import discover
        except Exception:
            log.exception("registry unavailable")
            return []

        live: dict[str, Any] = {}  # session_id -> whatever discover() yields
        try:
            for session in discover():
                live[str(getattr(session, "session_id", ""))] = session
        except Exception:
            log.exception("could not enumerate live sessions")

        try:
            registry = Registry()
            records = list(registry.agents.values())
        except Exception:
            log.exception("could not read the registry")
            records = []

        now = time.time()
        try:
            annotations = {row["name"]: row for row in self.store.agents()}
            blocked = self.store.blocked_since()
            # One grouped query for the whole roster rather than one per row:
            # this is also the body of a long-poll, which recomputes on every
            # pass while a phone is listening.
            tool_counts = self.store.tools_since(now - vitals.WINDOW)
        except Exception:
            log.exception("could not read agent annotations")
            annotations, blocked, tool_counts = {}, {}, {}

        # One `list-sessions` for the whole roster rather than one `has-session`
        # per agent: this runs on every request AND on every pass of a parked
        # long-poll, so per-agent shelling out would be a subprocess storm. It is
        # still computed now, per request, never cached -- which is what §4 asks
        # for. The question is how many processes it costs, not how fresh it is.
        panes = self._panes()

        out: list[dict[str, Any]] = []
        declared: set[str] = set()
        for record in records:
            declared.add(str(record.session_id))
            out.append(self._roster_row(
                str(record.name), str(record.task or ""), live.get(record.session_id),
                completed_at=record.completed_at, annotations=annotations,
                blocked=blocked, now=now, panes=panes, record=record,
                tool_counts=tool_counts,
            ))
        # Live sessions that never declared themselves are still worth talking
        # to -- his own shells, mostly -- so they are listed under their derived
        # name rather than hidden because they skipped a registration step.
        for session_id, session in live.items():
            if session_id in declared:
                continue
            name = str(getattr(session, "name", "") or session_id[:8])
            out.append(self._roster_row(
                name, "", session, completed_at=None, annotations=annotations,
                blocked=blocked, now=now, panes=panes, record=None,
                tool_counts=tool_counts,
            ))
        return out

    def _roster_row(
        self,
        name: str,
        task: str,
        session: Any,
        *,
        completed_at: float | None,
        annotations: dict[str, Any],
        blocked: dict[str, float],
        now: float,
        panes: set[str] | None = None,
        record: Any = None,
        tool_counts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        annotation = annotations.get(name) or {}
        busy = bool(getattr(session, "status", "") == "busy")
        live = session is not None
        last_tool_at = annotation.get("last_tool_at")

        # Four states, and the two that used to be indistinguishable are the
        # point: a clean finish and a crash both just vanished identically.
        # `completed_at` is set by `done` and only by `done`, so it outranks
        # liveness -- an agent that said it was finished and is still winding
        # down is finished, not idle.
        if completed_at is not None:
            state, dead_reason = "done", "finished and said so"
        elif live and busy:
            state, dead_reason = "working", None
        elif live:
            state, dead_reason = "idle", None
        else:
            state, dead_reason = "dead", "its process is gone and it never said done"

        # Never claimed without evidence. `last_tool_at` is None until something
        # has actually been observed doing a tool call, and an unobserved agent
        # is not a stalled one -- `busy` is the session's own word for itself
        # and is exactly what goes stale when a process hangs.
        stalled = bool(
            state == "working"
            and last_tool_at is not None
            and now - float(last_tool_at) > STALL_AFTER
        )

        blocked_since = blocked.get(name)
        return {
            "name": name,
            "task": task,
            "cwd": str(getattr(session, "cwd", "") or annotation.get("cwd") or ""),
            "live": live,
            "busy": busy,
            # Everything below is additive. The app on his phone decodes the
            # five fields above and ignores the rest, which is what makes this
            # shippable without a reinstall on a 7-day provisioning profile.
            "state": state,
            "deadReason": dead_reason,
            "stalled": stalled,
            "lastToolAt": last_tool_at,
            "blocked": blocked_since is not None,
            "blockedSince": blocked_since,
            "retired": annotation.get("retired_at") is not None,
            "historyGeneration": int(annotation.get("history_generation") or 0),
            # hotline's own `Registry.Agent.authority`: "sys-admin" or None, a
            # standing role granted by Bogdan. Passed through as the string
            # rather than as a boolean, because hotline's field is `str | None`
            # and a second role must not decode as "not sys-admin".
            #
            # None for a live session that never declared itself: there is no
            # registry record to read a role off, and an undeclared shell has
            # not been granted anything. Absent is the honest answer, and the
            # app renders no badge for it.
            "authority": (
                getattr(record, "authority", None) if record is not None else None
            ),
            # §11's first ask: the strip's ELAPSED cell.
            #
            # The registry's own value first, because that is when the agent
            # declared itself and it survives this database being purged. Then
            # the session's `startedAt` -- a live session that never declared
            # itself still started at a knowable moment, and the annotation
            # below it records only when this store first noticed the name,
            # which for one of his own shells can be hours late and would draw
            # an ELAPSED of a few seconds on something that has been running all
            # day.
            "declaredAt": _declared_at(record, session, annotation),
            "controls": self._controls(
                name, session, live=live, panes=panes, record=record
            ),
            # §11's second ask, and the reason it is a separate boolean from
            # `vitals.contextUsed`: "no first turn yet" and "no statusline
            # wrapper for this session" both arrive as a missing number, render
            # identically, and mean opposite things. This one is observed --
            # the wrapper has reported for this agent at least once -- rather
            # than read out of a settings file the session may predate.
            "contextAvailable": annotation.get("statusline_at") is not None,
            "vitals": vitals.project(
                agent=name,
                annotation=dict(annotation),
                tools_in_window=int((tool_counts or {}).get(name, 0)),
                blocked_since=blocked_since,
                rates=self.rates,
                now=now,
            ),
        }

    def _note_roster_changes(self, rows: list[dict[str, Any]]) -> None:
        """Record what changed since the last time anyone looked.

        A tick is an invalidation, not a fact: it tells a client its cached row
        is stale and nothing more. So a duplicate tick costs one refetch and is
        not worth machinery to prevent -- which is why `retire` and `purge` can
        emit their own without coordinating with this.
        """
        snapshot = {row["name"]: _signature(row) for row in rows}
        previous = self._roster_snapshot
        self._roster_snapshot = snapshot
        if previous is None:
            # First computation of this process. There is nothing to compare
            # against, and ticking every agent as "changed" on every restart
            # would make a restart look like the whole box changed at once.
            return
        for name, current in snapshot.items():
            was = previous.get(name)
            if was is None:
                self._tick(name, "appeared")
            elif was != current:
                self._tick(name, ", ".join(
                    f"{field}: {was.get(field)} -> {current[field]}"
                    for field in current if was.get(field) != current[field]
                ))
        for name in previous:
            if name not in snapshot:
                self._tick(name, "gone")

    def _tick(self, agent: str, what: str) -> None:
        try:
            self.store.append_roster_event(agent, what)
        except Exception:  # a missed tick costs a stale row, not data
            log.exception("could not record a roster change for %s", agent)
            return
        self._roster_waker.wake()

    def agents(self, *, include_done: bool = False, include_retired: bool = False
               ) -> dict[str, Any]:
        """Who is alive, what each is working on, and which are busy.

        The default list is exactly what it has always been -- hotline's working
        agents plus undeclared live sessions -- because the app installed on his
        phone renders whatever this returns. Agents that finished, and agents he
        has retired, are new and are therefore opt-in: they are extra ROWS, and
        "the response is unchanged, the fields are additive" does not cover
        quietly filling his list with corpses.

        A done agent whose process is somehow still alive is never hidden. That
        is the one case worth seeing without asking.
        """
        rows = self._roster_rows()
        self._note_roster_changes(rows)
        return {"agents": [
            row for row in rows
            if (include_done or row["state"] != "done" or row["live"])
            and (include_retired or not row["retired"])
        ]}

    def _is_live(self, agent: str) -> bool:
        return any(row["name"] == agent and row["live"] for row in self._roster_rows())

    # ---- control: what he can do to an agent from his phone ---------------
    #
    # Everything here is subject to two rules that are easy to state and easy to
    # get wrong.
    #
    # **`enabled` is computed per agent, at request time, and never cached.** A
    # capability is a claim about right now, and a cached one is a claim about
    # whenever the cache was filled. The pty cancel needs a pty, and a pane can
    # die while the process it was running lives on.
    #
    # **The endpoint enforces independently of what the roster said.** A phone
    # that has been asleep is holding a roster from ten minutes ago; every
    # refusal below is re-derived when the request arrives, so a stale client
    # showing a control as enabled gets a 409 rather than an action.

    def _panes(self) -> set[str]:
        try:
            from hotline import tmuxen

            return tmuxen.sessions()
        except Exception:
            log.exception("could not list tmux sessions")
            return set()

    def _pty_of(self, session: Any, panes: set[str] | None = None) -> tuple[str, str]:
        """`(tmux target, why there is none)`. Exactly one of the two is empty.

        The reason strings are the ones the phone renders verbatim (§7 rule 4),
        so they say what is actually wrong rather than "unavailable". "Running
        headless" and "its pane is gone" call for different responses from him
        and used to be the same sentence.
        """
        if session is None:
            return "", "not running"
        target = str(getattr(session, "tmux", "") or "")
        if not target:
            return "", "running headless — its turn can't be interrupted, only killed"
        name = target.split(":", 1)[0]
        alive = name in (self._panes() if panes is None else panes)
        if not alive:
            return "", f"its tmux session {name} is gone, so there is no pane to type into"
        return target, ""

    def _resumable(self, record: Any) -> str:
        """"" when the agent could be resumed, or why it could not."""
        if record is None:
            return "it never declared itself, so there is no record to resume from"
        try:
            from hotline.revive import brief_for

            return "" if brief_for(record) is not None else (
                "it left no handoff and its transcript is gone"
            )
        except Exception:
            log.exception("could not work out whether %s is resumable", record)
            return "could not read its handoff or transcript"

    def _controls(
        self,
        name: str,
        session: Any,
        *,
        live: bool,
        panes: set[str] | None = None,
        record: Any = None,
    ) -> list[dict[str, Any]]:
        """The capability list for one agent, computed now.

        Order is the order the phone renders them in, and it is deliberate:
        the two that keep the session first, the two that change what it is
        doing next, the destructive one last.
        """
        _target, no_pty = self._pty_of(session, panes)
        # Only asked for an agent that is not running. `brief_for` globs the
        # projects directory and reads the handoff file, and this is recomputed
        # on every request and on every pass of a parked long-poll -- paying
        # that for every live agent to answer a question whose answer is "it is
        # still running" would make the roster cost scale with his disk.
        cannot_resume = "" if live else self._resumable(record)
        return [
            _capability("stop", "Stop", not no_pty, no_pty),
            _capability("compact", "Compact", not no_pty, no_pty),
            _capability("retask", "Retask", live, "" if live else "not running"),
            _capability(
                "resume", "Resume", not live and not cannot_resume,
                "it is still running — retask it instead" if live else cannot_resume,
            ),
            _capability("kill", "Kill", live, "" if live else "not running"),
        ]

    def global_controls(self) -> list[dict[str, Any]]:
        """`new`, which belongs to the box rather than to any agent.

        Verified rather than assumed: spawning needs a tmux to spawn into and a
        `claude` to run, and reporting it enabled on a box missing either is the
        same lie /health exists to stop telling about the doorbell.
        """
        import shutil

        why = ""
        if shutil.which("tmux") is None:
            why = "tmux is not installed on this box, so there is no pane to start one in"
        else:
            try:
                from hotline.config import CLAUDE_BIN

                if not (pathlib.Path(CLAUDE_BIN).exists() or shutil.which(CLAUDE_BIN)):
                    why = f"{CLAUDE_BIN} is not on this box"
            except Exception:  # noqa: BLE001 - hotline is a PYTHONPATH dependency
                why = "hotline is not importable here, so nothing can be spawned"
        return [_capability("new", "New agent", not why, why)]

    def _session_of(self, name: str) -> Any:
        """The live session behind a roster name, or None. Resolved fresh.

        Not taken from a roster row the caller happened to have: every
        destructive path re-resolves immediately before acting, because the
        window between computing a roster and acting on it is exactly where a
        pid gets recycled.
        """
        live = self._live_sessions()
        record = self._registry_record(name)
        if record is not None:
            found = live.get(str(getattr(record, "session_id", "")))
            if found is not None:
                return found
        for session_id, session in live.items():
            if str(getattr(session, "name", "") or session_id[:8]) == name:
                return session
        return None

    def _note(self, agent: str, kind: str, text: str, tool: str | None = None) -> None:
        """File a fact about the session into its own channel.

        A stop, a kill and a compaction are things that happened to the agent,
        not toasts about a button press, so they belong in the record he scrolls
        rather than in a banner that disappears.
        """
        try:
            self.store.append_event(agent, kind, text, tool=tool)
        except Exception:
            log.exception("could not record %s for %s", kind, agent)
            return
        self._waker(agent).wake()

    def _refuse(self, status: int, message: str) -> Any:
        from hotline.httpd import HttpError

        return HttpError(status, message)

    async def _interrupt(self, name: str, session: Any) -> str:
        """The one cancel path, with its debounce. Returns "" or why it refused.

        `tmuxen.interrupt` is the only thing that knows a cancel is a keystroke.
        Everything above this line asks for it by name, so the day `ccsocks`
        grows a real cancel message this is where it is swapped and nowhere
        else.
        """
        from hotline.errors import HotlineError

        target, why = self._pty_of(session)
        if not target:
            return why
        now = time.monotonic()
        last = self._last_stop_at.get(name)
        if last is not None and now - last < STOP_DEBOUNCE:
            return "already interrupting"
        self._last_stop_at[name] = now
        try:
            from hotline import tmuxen

            await tmuxen.interrupt(target)
        except HotlineError as exc:
            # Nothing landed, so the debounce must not stand: it exists to stop
            # a second Escape reaching a session that already got one, and this
            # session got none.
            self._last_stop_at.pop(name, None)
            return str(exc)
        return ""

    async def stop(self, agent: str) -> dict[str, Any]:
        """Cancel the current turn. The session survives and can take new work."""
        name = self._known_agent(agent)
        session = self._session_of(name)
        why = await self._interrupt(name, session)
        if why:
            raise self._refuse(409, why)
        self._note(name, "state", "interrupted from the phone; the session is still running")
        self._roster_waker.wake()
        return {"agent": name, "interrupted": True}

    async def kill(self, agent: str) -> dict[str, Any]:
        """End the session. It only comes back through `resume`.

        `ccsocks.terminate()` directly rather than `router.kill_session()`: the
        router resolves a fuzzy spec, and this has already resolved the exact
        session. Handing a name back to a fuzzy resolver so it can find it again
        is a chance to find a different one, on the one endpoint where that
        would be unrecoverable. `terminate()` carries the self-guard either way.
        """
        from hotline.errors import HotlineError

        name = self._known_agent(agent)
        session = self._session_of(name)
        if session is None:
            raise self._refuse(409, "not running")
        try:
            from hotline.ccsocks import terminate

            outcome = await terminate(session)
        except HotlineError as exc:
            raise self._refuse(409, str(exc)) from exc
        self._note(name, "state", f"killed from the phone: {outcome}")
        self._roster_waker.wake()
        return {"agent": name, "outcome": outcome}

    async def retask(
        self,
        agent: str,
        text: str,
        *,
        stop_first: bool = False,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        """Give a running agent new work, optionally cancelling what it is on.

        Composed here rather than as two calls from the phone: if the interrupt
        lands and the network drops the second request, the agent is cancelled
        with nothing queued to replace it. One request makes it atomic from the
        phone's side.

        `stop_first` is refused when that agent's `stop` is not available, never
        silently downgraded -- "it stopped and started the new work" and "it will
        get to this after the current turn" are different outcomes and he acts
        on them differently.
        """
        from hotline.errors import HotlineError

        name = self._known_agent(agent)
        session = self._session_of(name)
        if session is None:
            raise self._refuse(409, "not running")

        interrupted = False
        if stop_first:
            why = await self._interrupt(name, session)
            if why:
                raise self._refuse(409, f"stop_first was asked for, but {why}")
            interrupted = True

        try:
            from hotline.ccsocks import inject

            await inject(session, text)
        except HotlineError as exc:
            raise self._refuse(409, str(exc)) from exc

        # Observed after the fact rather than predicted: whether this lands now
        # or waits its turn depends on what the session is doing at this instant,
        # and the descriptor is the session's own word for that.
        queued = self._is_busy(session)
        entry = self._note_retask(name, text, client_token)
        self._roster_waker.wake()
        return {
            "agent": name,
            "delivered": True,
            "queued": queued,
            "interrupted": interrupted,
            "client_token": client_token,
            "seq": entry,
        }

    def _is_busy(self, session: Any) -> bool:
        try:
            from hotline.ccsocks import status_of

            return status_of(int(getattr(session, "pid", 0))) == "busy"
        except Exception:
            log.exception("could not re-read the status of %s", getattr(session, "name", "?"))
            return bool(getattr(session, "status", "") == "busy")

    def _note_retask(self, name: str, text: str, client_token: str | None) -> int | None:
        try:
            stored = self.store.append_event(name, "you", text, client_token=client_token)
        except Exception:
            log.exception("could not record a retask for %s", name)
            return None
        self._waker(name).wake()
        return stored.seq

    # ---- compact -----------------------------------------------------------

    async def compact(self, agent: str, then: str | None = None) -> dict[str, Any]:
        """Interrupt, run `/compact`, wait for it to really finish, then continue.

        Composed here for the same reason `retask {stop_first}` is: three calls
        from a phone means a dropped connection can leave the agent interrupted
        and idle, or compacted and never restarted.

        Two things about the mechanism are not obvious and were both established
        by direct observation rather than reasoning:

        * **`/compact` has to go through the pty.** `ccsocks.inject("/compact")`
          delivers it as literal text -- the transcript shows it landing as an
          ordinary peer user turn, and the model answered that it had no tool
          that could compact anything. That is the exact "types a string into a
          prompt" failure this endpoint exists to avoid, and it is what the
          obvious implementation would have shipped.
        * **The continuation is the opposite.** `inject()` is right for it,
          because a continuation genuinely is a user message. So the two steps
          use two different channels on purpose.

        Every field of the response reports what actually happened. `resumed:
        false` is a valid outcome, not a failure to smooth over.
        """
        from hotline.errors import HotlineError

        name = self._known_agent(agent)
        session = self._session_of(name)
        began = time.monotonic()
        result: dict[str, Any] = {
            "agent": name, "interrupted": False, "compacted": False, "resumed": False,
            "preTokens": None, "postTokens": None, "durationMs": None, "detail": "",
        }

        target, why = self._pty_of(session)
        if not target:
            raise self._refuse(409, why)

        stopped = await self._interrupt(name, session)
        if stopped:
            raise self._refuse(409, stopped)
        result["interrupted"] = True

        # The CLI has to finish cancelling before it will take a new line. This
        # paces a terminal, which is the one thing a short wait is honest for --
        # it is not standing in for the completion signal, which is watched for
        # and never timed.
        await asyncio.sleep(COMPACT_SETTLE)
        try:
            from hotline import tmuxen

            await tmuxen.send_command(target, "/compact")
        except HotlineError as exc:
            result["detail"] = f"interrupted, but /compact could not be typed: {exc}"
            return self._finish_compact(result, began)

        session_id = str(getattr(session, "session_id", ""))
        found, detail = await self._await_compaction(name, session_id, session)
        result["detail"] = detail
        if found is None:
            return self._finish_compact(result, began)

        result["compacted"] = True
        result["preTokens"] = _as_int(found.get("pre_tokens"))
        result["postTokens"] = _as_int(found.get("post_tokens"))
        result["durationMs"] = _as_int(found.get("duration_ms"))

        try:
            from hotline.ccsocks import inject

            await inject(session, then or CONTINUE_AFTER_COMPACT)
            result["resumed"] = True
        except HotlineError as exc:
            # Reported, not smoothed over: he is looking at a compacted session
            # that is sitting there doing nothing, and the app renders that as
            # its own state with a Continue button rather than as success.
            result["detail"] = (
                f"compacted, but the continuation could not be delivered: {exc}"
            )
        self._roster_waker.wake()
        return self._finish_compact(result, began)

    def _finish_compact(self, result: dict[str, Any], began: float) -> dict[str, Any]:
        result["elapsedMs"] = int((time.monotonic() - began) * 1000)
        summary = (
            f"compacted {result['preTokens']} → {result['postTokens']} tokens"
            if result["compacted"] and result["preTokens"] is not None
            else ("compacted" if result["compacted"] else "compaction did not complete")
        )
        if not result["resumed"]:
            summary += "; not resumed"
        if result["detail"]:
            summary += f" ({result['detail']})"
        self._note(result["agent"], "state", summary)
        return result

    async def _await_compaction(
        self, agent: str, session_id: str, session: Any
    ) -> tuple[dict[str, Any] | None, str]:
        """Wait for the `compact_boundary` record. Observed, never timed.

        The transcript marker is the only thing counted as success, because it
        is the only signal that says compaction *happened* -- it carries the
        CLI's own `preTokens`, `postTokens` and `durationMs`, which is what lets
        the button report "48k → 4.1k in 71s" instead of a green tick.

        The descriptor's `busy` → `idle` flip is watched too, but only as a
        reason to stop waiting. On its own it is ambiguous: a session that never
        ran the command is also idle, and treating that as a success is exactly
        the check-that-measures-nothing this project has been burned by. So
        going idle without a marker ends the wait and is reported as a failure
        with the reason, not as a compaction.
        """
        deadline = time.monotonic() + COMPACT_TIMEOUT
        saw_busy = False
        while time.monotonic() < deadline:
            await asyncio.sleep(COMPACT_POLL)
            ingested = await self.ingest_session(agent, session_id)
            if ingested is not None and ingested.last_compaction:
                return dict(ingested.last_compaction), ""
            status = self._status_of(session)
            if status == "busy":
                saw_busy = True
            elif saw_busy and status == "idle":
                # One more read: the descriptor can flip before the record has
                # been flushed, and calling it a failure on that race would be
                # wrong about a compaction that did happen.
                await asyncio.sleep(COMPACT_POLL)
                ingested = await self.ingest_session(agent, session_id)
                if ingested is not None and ingested.last_compaction:
                    return dict(ingested.last_compaction), ""
                return None, (
                    "the session went idle without writing a compaction boundary, "
                    "so /compact did not run"
                )
        return None, (
            f"no compaction boundary appeared within {COMPACT_TIMEOUT:.0f}s; "
            "nothing was resumed rather than firing into a session that may still be busy"
        )

    def _status_of(self, session: Any) -> str | None:
        try:
            from hotline.ccsocks import status_of

            return status_of(int(getattr(session, "pid", 0)))
        except Exception:
            log.exception("could not read the status of %s", getattr(session, "name", "?"))
            return None

    async def resume(self, agent: str, cwd: str | None = None) -> dict[str, Any]:
        """Bring a dead agent back on a new session, seeded with what survives.

        The revive itself is `hotline.revive.resume`, shared with the CLI's
        `--resume`, because two implementations of "spawn, rehome, keep the
        channel" would be two chances to disagree about what resuming means.

        The brief is injected and not waited on. The CLI waits five minutes for
        the first answer and prints it; this cannot, and `seeded` says whether
        the message was accepted rather than whether it was understood.
        """
        from hotline.errors import HotlineError

        name = str(agent or "").strip()
        if not name:
            raise self._refuse(400, "agent is required")
        try:
            from hotline.agents import Registry
            from hotline.channels import from_env as channels_from_env
            from hotline.revive import NoSuchAgent, NothingToResumeFrom
            from hotline.revive import resume as revive_resume
        except Exception as exc:
            raise self._refuse(503, f"hotline is not importable here: {exc}") from exc

        try:
            resumed = await revive_resume(
                name, Registry(), cwd=cwd or None, channels=channels_from_env()
            )
        except NoSuchAgent as exc:
            raise self._refuse(404, str(exc)) from exc
        except NothingToResumeFrom as exc:
            raise self._refuse(409, str(exc)) from exc
        except HotlineError as exc:
            raise self._refuse(409, f"could not start a session: {exc}") from exc

        seeded = True
        try:
            from hotline.ccsocks import inject

            await inject(resumed.session, resumed.brief.seed)
        except HotlineError:
            log.exception("resumed %s but could not hand it its brief", name)
            seeded = False

        self.store.ensure_agent(
            resumed.agent.name,
            session_id=resumed.session.session_id,
            task=resumed.agent.task,
        )
        self._note(
            resumed.agent.name, "state",
            "resumed from the phone" + (
                " on its handoff" if resumed.from_handoff else " on its transcript"
            ),
        )
        self._roster_waker.wake()
        return {
            "agent": resumed.agent.name,
            "session": resumed.session.session_id,
            "from_handoff": resumed.from_handoff,
            "seeded": seeded,
            "tmux": resumed.tmux,
            "keptChannel": resumed.kept_channel,
            "channelError": resumed.channel_error,
        }

    async def new_agent(
        self, task: str, cwd: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        """Start a fresh session and hand it its task."""
        from hotline.errors import HotlineError

        task = str(task or "").strip()
        if not task:
            raise self._refuse(400, "task is required")
        blocked = self.global_controls()[0]
        if not blocked["enabled"]:
            raise self._refuse(409, str(blocked["reason"]))
        try:
            from hotline import tmuxen
            from hotline.agents import Registry
        except Exception as exc:
            raise self._refuse(503, f"hotline is not importable here: {exc}") from exc

        wanted = str(name or "").strip() or None
        try:
            session = await tmuxen.spawn(
                wanted or f"ios-{uuid.uuid4().hex[:6]}",
                cwd=cwd or None, name=wanted, timeout=SPAWN_TIMEOUT,
            )
        except HotlineError as exc:
            raise self._refuse(409, f"could not start a session: {exc}") from exc

        agent_name = wanted or str(getattr(session, "name", "") or session.session_id[:8])
        try:
            Registry().declare(session.session_id, agent_name, task)
        except Exception:  # a running session beats a tidy registry
            log.exception("started %s but could not declare it", agent_name)

        delivered = True
        try:
            from hotline.ccsocks import inject

            await inject(session, task)
        except HotlineError:
            log.exception("started %s but could not hand it its task", agent_name)
            delivered = False

        self.store.ensure_agent(agent_name, session_id=session.session_id, task=task)
        self._note(agent_name, "state", "started from the phone")
        self._roster_waker.wake()
        return {
            "agent": agent_name,
            "session": session.session_id,
            "delivered": delivered,
            "tmux": getattr(session, "tmux", None),
        }

    # ---- the map: hooks nudge, this tails the transcript ------------------

    def _live_sessions(self) -> dict[str, Any]:
        """session_id -> live session. Same `discover()` the roster uses."""
        try:
            from hotline.ccsocks import discover

            return {str(getattr(s, "session_id", "")): s for s in discover() if
                    getattr(s, "session_id", "")}
        except Exception:
            log.exception("could not enumerate live sessions")
            return {}

    def agent_for_session(self, session_id: str) -> str | None:
        """Which channel a session's transcript belongs to, or None.

        §9.7 settled the correlation by direct observation: `hook.session_id` ==
        the descriptor's `sessionId` == `Registry.Agent.session_id`. So this is
        two exact lookups, not a heuristic, and it returns None rather than
        guessing when neither matches.

        The second lookup is the same fallback the roster already applies: a
        live session that never declared itself is still listed, under a name
        derived from its descriptor. Filing its events under that same derived
        name is not a phantom bucket -- it is the row he is already looking at.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        try:
            from hotline.agents import Registry

            for record in Registry().agents.values():
                if str(record.session_id) == session_id:
                    return str(record.name)
        except Exception:
            log.exception("could not read the registry while attributing a hook")
        session = self._live_sessions().get(session_id)
        if session is not None:
            return str(getattr(session, "name", "") or session_id[:8])
        return None

    def _degrade(self, note: str) -> None:
        """Append to the list `/health` actually shows, deduped against the last.

        Deduped because these come from a hook that fires on every tool call,
        and an unbounded list of the same sentence would push everything else
        off the end of the five that get reported."""
        if not self.degradations or self.degradations[-1] != note:
            self.degradations.append(note)

    def _ingest(self, agent: str, session_id: str, *, turn_ended: bool) -> Ingested | None:
        """Read this session's transcript forward and file what is new.

        Synchronous: it is a bounded file read plus a handful of indexed inserts
        against a local SQLite, which is the same reasoning `Store` is
        synchronous for. The caller holds a per-agent lock.

        Returns None when the read could not be trusted, and in that case the
        stored offset is **not** advanced -- §2 is explicit that a read which
        produces nothing recognisable must be retried loudly rather than skipped
        past. A silently-empty map is the loopback-doorbell failure shape.
        """
        from hotline import transcript

        # Passed the session so a position belonging to a different transcript
        # is discarded rather than applied to this one. An agent outlives its
        # sessions -- `resume` hands it a new one -- and a byte offset into the
        # wrong file makes the channel go silent for good.
        offset, sidechains = self.store.read_position(agent, session_id)
        size = transcript.size_of(session_id)
        if offset > size:
            # A position past the end of the file it names. `_read_slice` seeks
            # there, reads nothing, and leaves the offset where it was, so this
            # is not a slow channel -- it is a channel that has stopped forever
            # and says nothing about it.
            #
            # Two ways to get here. A transcript that was rotated or truncated,
            # and -- the one that actually happened -- a position written before
            # positions recorded which session they belonged to, carried onto
            # the shorter transcript of a respawn under the same name.
            self._degrade(
                f"transcript position for {agent} ({offset}) was past the end of "
                f"{session_id[:12]}'s transcript ({size}); restarted its read"
            )
            offset, sidechains = 0, {}
        if offset == 0 and not sidechains:
            offset = _first_offset(session_id)
        found = transcript.events_since(session_id, offset, sidechains=sidechains)

        if not found.trustworthy:
            self.hook_parse_failures += 1
            note = (
                f"transcript for {agent} unreadable: {found.unparseable} of "
                f"{found.lines} lines in this slice did not parse and nothing was "
                f"recognised; its offset is held at {offset} and will be re-read"
            )
            log.error("%s", note)
            self.ingest_stalled[agent] = note
            self._degrade(note)
            return None
        if found.overlong:
            # Advancing loses one record; not advancing stops this agent's map
            # for good. Say which was chosen rather than doing it quietly.
            self._degrade(
                f"transcript for {agent} contained a record larger than one read "
                f"({transcript.MAX_SLICE_BYTES} bytes); it was skipped rather than "
                "stalling the channel"
            )
        self.ingest_stalled.pop(agent, None)

        result = ingest.absorb(self.store, agent, found.events, turn_ended=turn_ended)
        # The only place an output rate can come from: the prose itself is not
        # stored, so it is counted as it goes past. See `vitals.py`.
        self.rates.observe(agent, result.text_samples)
        self.store.set_read_position(agent, found.offset, found.sidechains, session_id)
        self._flush_durations(agent)
        if result.last_tool_at is not None:
            # An honestly-observed tool call, which is what makes `stalled` mean
            # something. Never set from "the session says it is busy".
            try:
                self.store.set_last_tool_at(agent, result.last_tool_at)
            except Exception:
                log.exception("could not record a tool call for %s", agent)
        if result.events:
            self._waker(agent).wake()
        return result

    async def ingest_session(
        self, agent: str, session_id: str, *, turn_ended: bool = False
    ) -> Ingested | None:
        """`_ingest` under this agent's lock, off the event loop's critical path.

        Two nudges for one session overlap constantly -- a `PreToolUse` and the
        `Stop` behind it -- and without the lock both would read from the same
        stored offset and write every event twice.
        """
        lock = self._ingest_locks.setdefault(agent, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(
                self._ingest, agent, session_id, turn_ended=turn_ended
            )

    async def hook(self, body: dict[str, Any]) -> dict[str, Any]:
        """The nudge. `{session_id, cwd, transcript_path, event}` and nothing else.

        Deliberately tiny: no model output, no tool arguments, no secrets on the
        wire. Everything it reports is read out of the file afterwards, which is
        also why a daemon that was down for an hour catches up rather than
        losing that hour.

        Always 200 for a well-formed body, including for a session it cannot
        attribute. The caller is a hook attached to a real turn and a 4xx there
        buys nothing -- the drop is recorded on `/health` instead, where it can
        actually be seen.
        """
        self.hook_events += 1
        session_id = str(body.get("session_id", "") or "")
        event = str(body.get("event", "") or "")
        agent = self.agent_for_session(session_id)
        if agent is None:
            self.unattributed_hook_events += 1
            log.warning(
                "hook %r for session %s matches no agent (cwd %s); dropped",
                event, session_id[:12] or "?", body.get("cwd", "?"),
            )
            return {"ok": False, "reason": "no agent for that session", "event": event}

        used = body.get("context_used_percentage")
        if isinstance(used, int | float):
            # §9.7: the statusLine payload is the only honest source, and it is
            # `null` before a session's first turn. Nothing is stored for null,
            # because "unknown" and "none used" are different states.
            try:
                self.store.set_context_used(agent, max(0.0, min(1.0, float(used) / 100.0)))
            except Exception:
                log.exception("could not record a context sample for %s", agent)

        if event == "StatusLine":
            # Recorded even when the payload carried no usage figure. This is
            # what `contextAvailable` is: the wrapper reported, so a missing
            # number means "no first turn yet" rather than "no wrapper here",
            # and the app renders those two differently on purpose.
            try:
                self.store.set_statusline_seen(agent)
            except Exception:
                log.exception("could not record a statusline report for %s", agent)
            # Reports context, not transcript progress, and fires several times
            # per turn. Tailing on it would triple the reads for nothing.
            return {"ok": True, "agent": agent, "event": event, "events": 0}

        if event == "PostToolUse":
            # The only place a per-tool duration exists. The transcript's own
            # `durationMs` is a whole turn's, and there is nothing per-call in
            # the file at all -- checked, not assumed. No tail here either: the
            # `tool_use` record was already read on the PreToolUse nudge, and
            # re-reading would double the work for a column update.
            return {"ok": True, "agent": agent, "event": event,
                    **self._record_duration(agent, body)}

        result = await self.ingest_session(
            agent, session_id, turn_ended=event in TURN_ENDING_EVENTS
        )
        if result is None:
            return {"ok": False, "reason": "transcript unreadable", "agent": agent,
                    "event": event}
        return {
            "ok": True, "agent": agent, "event": event,
            "events": result.events, "tools": result.tools,
            "phases_opened": result.phases_opened, "phases_closed": result.phases_closed,
        }

    def _flush_durations(self, agent: str) -> None:
        """Apply durations that arrived before their row did. Called after a read.

        Anything still unmatched stays parked until the map is bounded out from
        under it, at which point it is counted rather than forgotten: the app
        draws no duration bar for those rows, and a rising number is the only
        way to tell that from "nothing ran".
        """
        parked = self._pending_durations.get(agent)
        if not parked:
            return
        for tool_use_id in list(parked):
            try:
                if self.store.set_duration(tool_use_id, parked[tool_use_id]):
                    del parked[tool_use_id]
            except Exception:
                log.exception("could not apply a parked duration for %s", agent)
                return
        while len(parked) > PENDING_DURATIONS:
            parked.pop(next(iter(parked)))
            self.tool_durations_unmatched += 1
        if not parked:
            self._pending_durations.pop(agent, None)

    def _record_duration(self, agent: str, body: dict[str, Any]) -> dict[str, Any]:
        """Attach `PostToolUse.duration_ms` to the row the transcript already wrote.

        A miss is reported rather than swallowed: it means the row for that
        `tool_use_id` was never written, and the app then draws no duration bar
        at all instead of a guessed one -- which is the correct behaviour and is
        worth being able to see the rate of.
        """
        tool_use_id = str(body.get("tool_use_id") or "")
        duration = body.get("duration_ms")
        if not tool_use_id or not isinstance(duration, int | float):
            return {"timed": False, "reason": "no tool_use_id or duration_ms in the nudge"}
        try:
            matched = self.store.set_duration(tool_use_id, float(duration))
        except Exception:
            log.exception("could not record a tool duration for %s", agent)
            return {"timed": False, "reason": "the store refused it"}
        if not matched:
            # Not a failure yet: the row is usually written by the nudge in
            # front of this one, which may still be mid-read. Parked and
            # applied after the next read of this agent's transcript.
            self._pending_durations.setdefault(agent, {})[tool_use_id] = float(duration)
            return {"timed": False, "parked": True}
        return {"timed": True}

    async def poll_once(self) -> dict[str, Any]:
        """One sweep of every live session that maps to a known agent.

        The safety net §2 asks for. It exists because the hook can miss: a
        session that predates the install, a nudge dropped over a daemon
        restart, a hook sitting in its own 30 s backoff. It never closes a phase
        -- only a Stop nudge knows a turn ended -- so a poll can add tool calls
        to an open phase but cannot invent an ending for it.
        """
        seen = 0
        events = 0
        for session_id in list(self._live_sessions()):
            agent = self.agent_for_session(session_id)
            if agent is None:
                continue
            seen += 1
            result = await self.ingest_session(agent, session_id)
            if result is not None:
                events += result.events
        return {"sessions": seen, "events": events}

    async def safety_poll(self, interval: float = SAFETY_POLL) -> None:
        """Run `poll_once` forever. Started by `main()`, never by a test."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed sweep must not end the sweeping
                log.exception("safety poll failed")

    # ---- the agent-scoped channel ----------------------------------------

    def _known_agent(self, agent: str) -> str:
        """Resolve a name, or 404. Same reasoning as `/api/v1/events`' bad call_id.

        An empty feed for a name that does not exist reads to a client exactly
        like an agent that has said nothing, and the first is a bug in the
        caller while the second is normal.
        """
        from hotline.httpd import HttpError

        name = str(agent or "").strip()
        if not name:
            raise HttpError(400, "agent is required")
        if self._registry_record(name) is not None:
            return self.resolve_agent(name)
        # A live session that never declared itself is a row on his roster under
        # its derived name, so it has to be addressable by that name too --
        # otherwise every control on one of his own shells 404s while the app is
        # showing it as available.
        if self._session_of(name) is not None:
            return name
        try:
            if self.store.agent(name) is not None:
                return name
        except Exception:
            log.exception("could not look up agent %s", name)
        raise HttpError(404, f"no agent {name}")

    async def agent_feed(self, agent: str, since: int, wait: float) -> dict[str, Any]:
        """The live channel for one agent, over the one global sequence.

        Same cursor and long-poll semantics as `/api/v1/events`, which is kept
        untouched beside it. The difference is what it is scoped to: a ring's
        Q&A, a delegated `say` and eventually hook-reported tool events all
        arrive here interleaved and ordered by `seq`, because conversation has
        stopped being a display concept.
        """
        name = self._known_agent(agent)
        waker = self._waker(name)
        deadline = time.monotonic() + min(wait, MAX_WAIT)
        found = self.store.since(name, since)
        while not found and time.monotonic() < deadline:
            await waker.sleep(deadline - time.monotonic())
            found = self.store.since(name, since)
        return {
            "agent": name,
            "events": [event.as_agent_json() for event in found],
            "cursor": found[-1].seq if found else since,
            # A channel is closed when its agent is not running. There is no
            # per-conversation close here to inherit -- the channel outlives
            # every conversation in it -- so this is the honest end-of-stream
            # signal, and it is recomputed rather than remembered.
            "closed": not self._is_live(name),
            "historyGeneration": self.store.history_generation(name),
        }

    def history(self, agent: str, before: int | None, limit: int) -> dict[str, Any]:
        """A page of an agent's past, walking backwards from `before`.

        `before` is exclusive and is the `oldest_seq` of the page you already
        have, so paging has no off-by-one at either end and meets the live feed
        exactly once at the other.
        """
        name = self._known_agent(agent)
        events, has_more = self.store.history(name, before=before, limit=limit)
        return {
            "agent": name,
            "events": [event.as_agent_json() for event in events],
            "oldest_seq": events[0].seq if events else None,
            "newest_seq": events[-1].seq if events else None,
            "has_more": has_more,
            "historyGeneration": self.store.history_generation(name),
        }

    async def roster_events(self, since: int, wait: float) -> dict[str, Any]:
        """Wake the phone when the agent list stops being true.

        The stale-list fix. `ContentView` refreshes from `.task` and
        `.refreshable` only, and `.task` runs once per `Store` identity -- so a
        foregrounded app shows whatever was true when it launched.

        This long-poll recomputes the roster itself on each pass rather than
        reading a cache some background loop maintains. There is no such loop by
        design: liveness is a per-request check, and making the waiting client
        be the thing that performs it means the cost exists only while somebody
        is actually listening.
        """
        deadline = time.monotonic() + min(wait, MAX_WAIT)
        while True:
            self._note_roster_changes(self._roster_rows())
            found = self.store.roster_since(since)
            left = deadline - time.monotonic()
            if found or left <= 0:
                break
            await self._roster_waker.sleep(min(ROSTER_POLL, left))
        return {
            "events": [
                {"seq": event.seq, "agent": event.agent_name,
                 "text": event.text, "at": event.at}
                for event in found
            ],
            "cursor": found[-1].seq if found else since,
        }

    # ---- retention: a curated deletion surface, not a policy --------------

    def retire(self, agent: str, retired: bool) -> dict[str, Any]:
        """Visibility only. Reversible, destroys nothing, orthogonal to liveness.

        An agent can be live and retired at once -- retiring is him saying he
        does not want to look at it, not him saying it is finished.
        """
        name = self._known_agent(agent)
        at = self.store.set_retired(name, retired)
        self._tick(name, f"retired: {at is not None}")
        return {"agent": name, "retired": at is not None, "retiredAt": at}

    def purge(
        self,
        agent: str,
        *,
        scope: str = "history",
        conversation_id: str | None = None,
        before_seq: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Real `DELETE`. See `store.Store.purge` for what each filter means.

        `dry_run` returns the same counts without touching anything, so the
        confirmation sheet can read "hotline-80 -- 340 events since Aug 12"
        rather than a generic warning about data loss.

        One thing to know about `scope="everything"`: hotline's `Registry`, not
        this store, owns agent identity. Dropping the agents row removes every
        event, phase and conversation of that agent's and resets its history
        generation, but if hotline still has the record the next roster
        computation will recreate an empty annotation row under the same name.
        What `everything` guarantees is that nothing of the history survives,
        which is what "delete their fields" asks for.
        """
        from hotline.httpd import HttpError

        name = self._known_agent(agent)
        try:
            result = self.store.purge(
                name, scope=scope, conversation_id=conversation_id,
                before_seq=before_seq, dry_run=dry_run,
            )
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        if not dry_run:
            # The in-memory output samples are history too. Leaving them would
            # mean a purged agent still animated a rate derived from prose that
            # is no longer anywhere.
            self.rates.forget(name)
            for conversation in list(self.calls):
                if self.store.conversation(conversation) is None:
                    self.calls.pop(conversation, None)
                    self.call_opened.pop(conversation, None)
                    self.call_agent.pop(conversation, None)
            # The client's only signal that its whole local cache for this agent
            # is now a lie. A purge reaches the phone within one roster wake.
            self._tick(name, f"historyGeneration: {result['history_generation']}")
        return result

    async def say(
        self, text: str, agent: str | None, client_token: str | None = None
    ) -> dict[str, Any]:
        """Send him a turn's worth of instruction and follow the reply.

        Returns immediately with a conversation key. The answer arrives on the
        event feed rather than on this response, because a task can take
        minutes and an HTTP request that long is a request that dies on a
        network handover.
        """
        conversation, _events = self._open_conversation(agent, "say")
        name = self.call_agent[conversation]
        # §11: the phone's own id for this message, echoed back and carried on
        # the row. It turns reconciling a local echo against the feed from
        # "sound because this phone is the only writer" into an equality test,
        # and makes a retry after a timeout detectable as a duplicate rather
        # than delivered twice.
        #
        # Spelled `client_token` on the echo as well as on the row, matching
        # §11. The roster's own additions stay camelCase because that is what
        # the roster has always been -- `deadReason`, `blockedSince` -- and one
        # field appearing under two spellings on two endpoints is worse than
        # either convention.
        self._append(conversation, "you", text, client_token=client_token)

        key = f"ios-{conversation}"
        if agent:
            await self._bind(key, agent)

        async def run() -> None:
            def narrate(event: Any) -> None:
                kind = getattr(event, "kind", "")
                if kind in ("tool", "summary"):
                    self._append(conversation, kind, getattr(event, "detail", ""),
                                 getattr(event, "tool", None))
                if kind == "tool":
                    # The only honestly-observed `last_tool_at` there is until
                    # the transcript hook lands: this daemon watched this tool
                    # call happen. It is only ever set from observation, never
                    # from "the session says it is busy", which is what makes
                    # `stalled` mean something.
                    try:
                        self.store.set_last_tool_at(name, time.time())
                    except Exception:
                        log.exception("could not record a tool call for %s", name)

            try:
                _route, reply = await self.pool.ask(
                    key, text, narrator=narrate, timeout=TURN_TIMEOUT,
                    origin=_typed(agent),
                )
                answer = reply.text
                if getattr(reply, "notice", ""):
                    answer = f"Heads up, {reply.notice}. {answer}"
                self._append(conversation, "claude", answer)
            except Exception as exc:
                log.exception("delegation turn failed")
                self._append(conversation, "error", f"{type(exc).__name__}: {exc}")
            finally:
                self._close_conversation(conversation)

        self.sessions[conversation] = asyncio.ensure_future(run())
        return {"conversation": conversation, "client_token": client_token}

    def conversations(self) -> dict[str, Any]:
        """Everything open, newest first.

        The app needs this because a ring opens a conversation on the SERVER --
        the phone was not involved and has no id for it. Without this the
        question is sitting there and the app cannot find it, which is exactly
        what happened the first time this was run end to end.
        """
        out: list[dict[str, Any]] = []
        for conversation, events in self.calls.items():
            entries = list(events.entries)
            if not entries:
                continue
            asked = next((e for e in entries if e.kind == "claude"), entries[0])
            answered = any(e.kind == "you" for e in entries)
            out.append({
                "conversation": conversation,
                "opened": entries[0].at,
                "asked": asked.text,
                "answered": answered,
                "closed": events.closed,
                "waiting": not answered and not events.closed,
                # Additive: which channel this belongs to, so the redesigned app
                # can group by agent without a second round trip.
                "agent": self.call_agent.get(conversation, UNATTRIBUTED),
            })
        out.sort(key=lambda row: row["opened"], reverse=True)
        return {"conversations": out}

    async def reply(
        self, conversation: str, text: str, client_token: str | None = None
    ) -> dict[str, Any]:
        """His answer to a question a ring opened.

        Distinct from `say`, which starts a new conversation with an agent. This
        one lands in an existing conversation and is what unblocks the agent
        waiting on `hotline-call`.
        """
        from hotline.httpd import HttpError

        events = self._channel(conversation)
        if events is None:
            raise HttpError(404, f"no conversation {conversation}")
        self._append(conversation, "you", text, client_token=client_token)
        try:
            # He answered, so nothing is blocked on him here any more --
            # whatever else happens to the conversation afterwards.
            self.store.mark_answered(conversation)
        except Exception:  # the answer is delivered either way
            log.exception("could not mark %s answered", conversation)
        self._roster_waker.wake()
        return {"conversation": conversation, "delivered": True, "client_token": client_token}

    # ---- the live feed ---------------------------------------------------

    async def feed(self, call_id: str, since: int, wait: float) -> dict[str, Any]:
        from hotline.httpd import HttpError

        events = self._channel(call_id)
        if events is None:
            raise HttpError(404, f"no call {call_id}")
        found = await events.wait(since, min(wait, MAX_WAIT))
        return {
            "call_id": call_id,
            "events": [entry.as_json() for entry in found],
            "cursor": found[-1].seq if found else since,
            "closed": events.closed,
            # Say so rather than letting a client believe it has everything.
            "gap": events.gap(since),
            "dropped": events.dropped,
        }


def _declared_at(record: Any, session: Any, annotation: dict[str, Any]) -> float | None:
    declared = getattr(record, "declared_at", None)
    if isinstance(declared, int | float) and declared:
        return float(declared)
    started = getattr(session, "started_at", None)
    if isinstance(started, int | float) and started:
        # The descriptor writes milliseconds; everything else on this wire is
        # epoch seconds, and mixing the two would put the ELAPSED cell fifty
        # thousand years out rather than visibly wrong.
        return float(started) / 1000.0
    noticed = annotation.get("declared_at")
    return float(noticed) if noticed else None


def _as_int(value: Any) -> int | None:
    """A real number or nothing. Never a zero standing in for "not reported"."""
    return int(value) if isinstance(value, int | float) else None


def _capability(id_: str, label: str, enabled: bool, reason: str) -> dict[str, Any]:
    """One `Capability`, in the shape §4 declares.

    `reason` is dropped when the thing is enabled -- a reason for a control that
    works is noise -- and is never empty when it is not. A disabled control with
    no reason is indistinguishable on the phone from a broken one, which is the
    exact failure §7 rule 4 exists to prevent.
    """
    return {
        "id": id_,
        "label": label,
        "enabled": bool(enabled),
        "reason": None if enabled else (reason or "unavailable"),
    }


def _signature(row: dict[str, Any]) -> dict[str, Any]:
    """What counts as a roster change worth waking a phone for.

    `controls` is in here as the set of enabled ids rather than the whole list,
    because a pane can die while the process that owned it lives: `stop` and
    `compact` go from available to refused and nothing else on the row moves. A
    phone holding the old row would show him two buttons that now 409.
    """
    out: dict[str, Any] = {field: row[field] for field in ROSTER_FIELDS}
    out["controls"] = ",".join(
        capability["id"] for capability in row.get("controls", ()) if capability["enabled"]
    )
    return out


def _first_offset(session_id: str) -> int:
    """Where to start reading a session nobody has watched before.

    Not zero, for a session whose transcript is already large. Replaying a
    day-old transcript would write tens of thousands of rows into a channel he
    has never opened, all stamped with timestamps from hours ago, and would do
    it for every session on the box the moment the hook is installed. The map
    starts when the daemon starts watching; what came before is still on disk in
    the transcript, which §2 keeps as the authoritative record anyway.

    A line boundary is not needed here: `events_since` trims to one, and a first
    read that starts mid-record loses that one record rather than corrupting the
    offset.
    """
    try:
        from hotline import transcript

        size = transcript.size_of(session_id)
    except Exception:
        log.exception("could not size the transcript for %s", session_id[:12])
        return 0
    return max(0, size - ingest.FIRST_SLICE_BYTES)


def _speakable():
    try:
        from hotline.voice import speakable

        return speakable
    except Exception:  # noqa: BLE001 - unpolished speech beats no answer
        return lambda text: text


def _typed(agent: str | None):
    """Label a delegated instruction as typed from his phone.

    Deliberately NOT kind="voice": there is no speech recognition in this path
    any more, so claiming a mis-hearing risk that does not exist would make the
    label useless where it does.
    """
    try:
        from hotline.provenance import Origin

        return Origin(kind="phone", label="typed in the hotline app on his phone")
    except Exception:  # noqa: BLE001 - an unlabelled turn beats a dropped one
        return None


def _origin(target: CallTarget):
    """Label the turn as spoken, on a phone call.

    Not cosmetic. A mis-transcription reaching a `bypassPermissions` session has
    no undo, and the session cannot exercise judgement about that if it cannot
    tell speech from typing.
    """
    try:
        from hotline.provenance import Origin

        return Origin(
            kind="voice",
            label=f"spoken on a phone call to {target.device}",
            author_id=None,
            channel_id=None,
        )
    except Exception:  # noqa: BLE001 - an unlabelled turn beats a dropped one
        return None


def _last_from_him(session: Any) -> str:
    for who, text in reversed(getattr(session, "transcript", [])):
        if who == "you":
            return str(text)
    return ""


def build_server(service: Service, host: str | Sequence[str], port: int) -> Any:
    from hotline.httpd import HttpError, Server

    # Loopback is added here rather than at the call site so no caller can build
    # a server this box cannot reach. See `endpoint.py`: the hook fired into a
    # closed port for as long as that was possible, and said nothing.
    hosts = bind_hosts(host) if isinstance(host, str) else list(host)
    server = Server(hosts, port, log=lambda message: log.info("%s", message))
    service.bound_to(hosts, port)

    @server.route("GET", PING_PATH)
    async def ping(request: Any) -> tuple[int, dict[str, Any]]:
        """Answer, and nothing else.

        Deliberately not behind `authorise`: its entire job is to prove that a
        local caller can reach this port, and a probe that could fail for a
        second reason would not answer the question it exists to answer.
        """
        return 200, {"ok": True}

    @server.route("GET", "/health")
    async def health(request: Any) -> tuple[int, dict[str, Any]]:
        fake = bool(getattr(service.transport, "is_fake", False))
        service.reap()
        # Checked here, now, not remembered from boot. `db_ok` reports a query
        # that just ran; a database that has since gone read-only or had its
        # disk fill has to read as not-ok or this endpoint is lying in exactly
        # the way it exists to stop.
        stats = service.store.stats()
        # Verified now, over a real connection. The hook is built to fail
        # silently -- blanket except, always exit 0, a backoff marker -- so an
        # unreachable URL produces no error anywhere and the map just quietly
        # stops filling. This is the only place that can notice.
        hook_ok, hook_why = await service.reachable_locally()
        if not hook_ok:
            note = f"local callers cannot reach {service.hook_url}: {hook_why}"
            if not service.degradations or service.degradations[-1] != note:
                service.degradations.append(note)
        if not stats["db_ok"]:
            # Into the field that is actually read, not just a boolean nobody
            # has written a client for yet. Deduped against the last entry
            # because /health is polled and this list is unbounded.
            note = f"store not readable: {stats.get('db_error', 'unknown')}"
            if not service.degradations or service.degradations[-1] != note:
                service.degradations.append(note)
        return 200, {
            **stats,
            # NOT ok when the doorbell is fake. A health check that reports ok
            # while the pager rings nothing is the exact failure this endpoint
            # exists to catch, and it reported ok:true for 2.5 hours.
            "ok": (not fake) and service.ring_ready,
            "fake": fake,
            "ring_ready": service.ring_ready,
            "uptime_seconds": round(time.time() - service.started, 1),
            "transport": getattr(service.transport, "name", "?"),
            "rings_when_closed": bool(getattr(service.transport, "rings_when_closed", False)),
            "transports_available": sorted(service.links),
            "active_calls": service.open_calls(),
            "conversations_held": len(service.calls),
            "listening_on": [f"{h}:{service.listen_port}" for h in service.listen_hosts],
            # The URL the hook and the statusline wrapper are pointed at, and
            # whether a request to it just now actually worked.
            "hook_url": service.hook_url,
            "hook_reachable": hook_ok,
            # §4: the app renders whatever is here and hardcodes none of it.
            # `new` belongs to the box rather than to any agent, so it rides
            # here rather than on a roster row.
            "globalControls": service.global_controls(),
            # The map's own honesty fields. `unattributed_hook_events` counts
            # nudges dropped because nothing matched the session -- never filed
            # under a guess -- and `ingest_stalled` names the agents whose
            # offset is deliberately held because their transcript stopped
            # parsing. Both are zero/empty on a healthy box, and both used to be
            # indistinguishable from "nothing is happening".
            "hook_events": service.hook_events,
            "unattributed_hook_events": service.unattributed_hook_events,
            "hook_parse_failures": service.hook_parse_failures,
            "tool_durations_unmatched": service.tool_durations_unmatched,
            "ingest_stalled": sorted(service.ingest_stalled),
            # Surfaced on the health endpoint on purpose: a ringer that has been
            # silently degrading is exactly what a health check is for.
            "degradations": service.degradations[-5:],
        }

    @server.route("POST", "/api/v1/call")
    async def place(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        return 200, await service.place(request.json())

    # POST rather than GET, and the cursor rides in the body.
    #
    # Not a REST preference -- a constraint found by running it. hotline's
    # httpd does `path = target.split("?", 1)[0]` and keeps nothing, so a query
    # string cannot reach a handler at all, and it routes on exact path strings
    # so `/events/<call_id>/<since>` is not available either. The choice was
    # between forking a server Bogdan has already read and putting two integers
    # in a JSON body. The body wins easily.
    @server.route("POST", "/api/v1/agents")
    async def agents(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json() or {}
        return 200, service.agents(
            include_done=bool(body.get("include_done", False)),
            include_retired=bool(body.get("include_retired", False)),
        )

    @server.route("POST", "/api/v1/agents/feed")
    async def agent_feed(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        return 200, await service.agent_feed(
            str(body.get("agent", "")), int(body.get("since", 0)),
            float(body.get("wait", 25)),
        )

    @server.route("POST", "/api/v1/agents/history")
    async def agent_history(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        before = body.get("before")
        return 200, service.history(
            str(body.get("agent", "")),
            int(before) if before is not None else None,
            int(body.get("limit", 100)),
        )

    @server.route("POST", "/api/v1/agents/roster-events")
    async def roster_events(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json() or {}
        return 200, await service.roster_events(
            int(body.get("since", 0)), float(body.get("wait", 25))
        )

    # ---- control ---------------------------------------------------------
    #
    # Every one of these re-derives its own refusal rather than trusting the
    # roster the client is holding. That is not belt-and-braces: his phone
    # renders a roster that can be minutes old, and the capability list is a
    # description of the moment it was computed.

    @server.route("POST", "/api/v1/agents/stop")
    async def stop(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        return 200, await service.stop(str(body.get("agent", "")))

    @server.route("POST", "/api/v1/agents/kill")
    async def kill(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        return 200, await service.kill(str(body.get("agent", "")))

    @server.route("POST", "/api/v1/agents/retask")
    async def retask(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HttpError(400, "text is required")
        token = body.get("client_token")
        return 200, await service.retask(
            str(body.get("agent", "")), text,
            stop_first=bool(body.get("stop_first", False)),
            client_token=str(token) if token else None,
        )

    @server.route("POST", "/api/v1/agents/resume")
    async def resume(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        cwd = body.get("cwd")
        return 200, await service.resume(
            str(body.get("agent", "")), str(cwd) if cwd else None
        )

    @server.route("POST", "/api/v1/agents/new")
    async def new_agent(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        cwd, name = body.get("cwd"), body.get("name")
        return 200, await service.new_agent(
            str(body.get("task", "")),
            str(cwd) if cwd else None,
            str(name) if name else None,
        )

    @server.route("POST", "/api/v1/agents/compact")
    async def compact(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        then = body.get("then")
        return 200, await service.compact(
            str(body.get("agent", "")), str(then) if then else None
        )

    @server.route("POST", "/api/v1/agents/retire")
    async def retire(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        if "retired" not in body:
            raise HttpError(400, "retired is required; send it explicitly rather than toggling")
        return 200, service.retire(str(body.get("agent", "")), bool(body["retired"]))

    @server.route("POST", "/api/v1/agents/purge")
    async def purge(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        before = body.get("before_seq")
        return 200, service.purge(
            str(body.get("agent", "")),
            scope=str(body.get("scope", "history")),
            conversation_id=str(body["conversation_id"]) if body.get("conversation_id") else None,
            before_seq=int(before) if before is not None else None,
            dry_run=bool(body.get("dry_run", False)),
        )

    @server.route("POST", "/api/v1/hook")
    async def hook(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        return 200, await service.hook(request.json() or {})

    @server.route("POST", "/api/v1/say")
    async def say(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HttpError(400, "text is required")
        agent = body.get("agent")
        token = body.get("client_token")
        return 200, await service.say(
            text, str(agent) if agent else None, str(token) if token else None
        )

    @server.route("POST", "/api/v1/conversations")
    async def conversations(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        return 200, service.conversations()

    @server.route("POST", "/api/v1/reply")
    async def reply(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        conversation = str(body.get("conversation", ""))
        text = str(body.get("text", "")).strip()
        if not conversation or not text:
            raise HttpError(400, "conversation and text are required")
        token = body.get("client_token")
        return 200, await service.reply(
            conversation, text, str(token) if token else None
        )

    @server.route("POST", "/api/v1/hangup")
    async def hangup(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        call_id = str(body.get("call_id", ""))
        if not call_id:
            raise HttpError(400, "call_id is required")
        return 200, await service.hang_up(call_id)

    @server.route("POST", "/api/v1/events")
    async def feed(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        call_id = str(body.get("call_id", ""))
        if not call_id:
            raise HttpError(400, "call_id is required")
        return 200, await service.feed(
            call_id, int(body.get("since", 0)), float(body.get("wait", 25))
        )

    return server


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
                    "hotline-iosd: refusing to start with the 'loopback' doorbell, "
                    "which rings nothing. It was left running for 2.5 hours on "
                    "2026-08-25 and every caller got exit 0 while he was never "
                    "contacted. If that is genuinely what you want, set "
                    "HOTLINE_IOS_ALLOW_FAKE=1 -- saying it twice is the point."
                )
            links.append(LoopbackTransport())
        else:
            raise SystemExit(
                f"hotline-iosd: unknown ring transport {name!r}; "
                "known: telegram, sip, loopback"
            )
    if not links:
        raise SystemExit("hotline-iosd: no ring transport configured; set HOTLINE_IOS_RING")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hotline-iosd")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOTLINE_IOS_HOST", DEFAULT_HOST),
        help="the address his phone dials; loopback is always bound as well",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("HOTLINE_IOS_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--ring",
        default=os.environ.get("HOTLINE_IOS_RING", "telegram"),
        help="comma-separated doorbells, tried in order (telegram, loopback)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    from hotline.config import load_env
    from hotline.pool import SessionPool

    load_env()
    # hotline's load_env() defaults to hotline's OWN .env, which has no SIP_* or
    # TELEGRAM_* in it. Without this the daemon starts, reports a sip doorbell,
    # and only says "sip is not configured" in a degradations field nobody was
    # reading. Loaded second so hotline-ios's values win: load_env never
    # overwrites what is already set.
    load_env(pathlib.Path(__file__).resolve().parents[3] / ".env")
    names = args.ring.split(",")
    # Build each doorbell ONCE and compose the default from those same objects,
    # so `--transport sip` selects the very transport the chain would use rather
    # than a second copy of it -- and so starting the chain starts them all.
    links = build_links(names)
    if not links:
        raise SystemExit("hotline-iosd: no ring transport configured; set HOTLINE_IOS_RING")
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
    service = Service(
        transport,
        SessionPool(),
        allow_ips=allow,
        api_key=os.environ.get("HOTLINE_API_KEY", ""),
    )

    service.set_links(links)

    async def run() -> None:
        try:
            await transport.start()
            service.ring_ready = True
        except Exception as exc:  # noqa: BLE001 - see comment
            # Do NOT die. The daemon still serves the app, the agent list and the
            # transcript feed without a working doorbell -- and a ringer that
            # cannot start is exactly the thing that should be visible on
            # /health rather than fatal at boot.
            log.error("ring transport did not start: %s", exc)
            service.degradations.append(str(exc))
        server = build_server(service, args.host, args.port)
        await server.start()
        # The phone is the entire point, so losing its address is fatal, not a
        # degradation. `asyncio.start_server` over a list binds what it can and
        # raises only when *nothing* binds -- and loopback always binds, so it
        # can never raise here. At boot that is a real race: the bind address is
        # the tailnet IP, which does not exist until tailscaled is up. Silently
        # serving loopback alone would mean a daemon that looks healthy in every
        # log line and every local curl while the phone cannot reach it at all.
        #
        # Dying instead hands the problem to the restart policy in
        # ~/.config/systemd/user/hotline-ios.service (Restart=always,
        # RestartSec=5, no start limit), which retries until tailscale is up.
        missing = unreachable(server.hosts, server.bound)
        if missing:
            raise SystemExit(
                f"bound {server.bound or 'nothing'} but not {', '.join(missing)} "
                f"on port {args.port}; the phone dials that address. "
                "Is tailscaled up? Exiting so the service manager retries."
            )
        # Started here rather than in Service.__init__ so a test can construct a
        # Service without a running loop, and so nothing sweeps the box during
        # the suite.
        poll = asyncio.ensure_future(service.safety_poll())
        poll.add_done_callback(lambda task: task.cancelled() or task.exception())
        log.info(
            "hotline-iosd on %s:%d, ring=%s, rings_when_closed=%s",
            args.host, args.port, getattr(transport, "name", "?"),
            getattr(transport, "rings_when_closed", False),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
