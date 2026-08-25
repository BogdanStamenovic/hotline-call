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

from .events import Entry, EventLog, Waker
from .ring.base import (
    CallDeclined,
    CallError,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
)
from .store import UNATTRIBUTED, Store

log = logging.getLogger("hotline-iosd")

DEFAULT_PORT = 8789
"""One past hotlined's 8788, so the two are obviously siblings."""

MAX_WAIT = 30.0
TURN_TIMEOUT = 900.0
"""Ceiling on a long-poll. Under most proxy and NAT idle timeouts."""

HYDRATE_WINDOW = 3600.0
"""How far back a restart reads conversations into its in-memory index.

The same hour `reap()` keeps them for, so a restart lands in the state the
process would have been in anyway. Anything older is still in the database and
still reachable through `/api/v1/agents/history`; this is only about what is
warm."""

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

ROSTER_FIELDS = ("task", "live", "busy", "state", "stalled", "blocked",
                 "retired", "historyGeneration")
"""What counts as a roster change worth waking a phone for.

Deliberately excludes `blockedSince` and `cwd`: a timestamp that only moves
because the thing it describes moved is not independently newsworthy, and
ticking on it would make the invalidation stream fire on its own output."""


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
        self.store = store if store is not None else self._open_store()
        # One wake per agent for the agent-scoped feed, and one for the roster.
        # Same broadcast primitive `EventLog` uses; see `events.Waker`.
        self._agent_wakers: dict[str, Waker] = {}
        self._roster_waker = Waker()
        # None until the first roster computation. A restart must not tick every
        # agent as "changed" just because it has nothing to compare against.
        self._roster_snapshot: dict[str, dict[str, Any]] | None = None
        self._hydrate()

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
        except Exception as exc:  # noqa: BLE001 - see docstring
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
        except Exception:  # noqa: BLE001 - a cold index beats a dead daemon
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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
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
                agent, kind, text, conversation_id=conversation, tool=tool, at=at
            )
            entry = Entry(seq=stored.seq, kind=kind, text=text, tool=tool, at=at)
        except Exception as exc:  # noqa: BLE001 - a full disk must not drop his question
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
        except Exception as exc:  # noqa: BLE001 - the ring matters more than the record
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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001 - hotline may not be installed at all
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
        except Exception:  # noqa: BLE001 - annotation is not worth losing a call over
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
        return {"call_id": call_id, "ended": events is not None}

    # ---- delegation: what the app is actually for -------------------------

    def agents(self) -> dict[str, Any]:
        """Who is alive, what each is working on, and which are busy.

        Reads hotline's registry and cross-references live sessions, because a
        registry record outlives the process it describes -- a name in the file
        is not evidence anything is running, and showing him a dead agent to
        talk to is worse than showing him none.
        """
        try:
            from hotline.agents import Registry
            from hotline.ccsocks import discover
        except Exception:
            log.exception("registry unavailable")
            return {"agents": []}

        live: dict[str, Any] = {}  # session_id -> whatever discover() yields
        try:
            for session in discover():
                live[str(getattr(session, "session_id", ""))] = session
        except Exception:
            log.exception("could not enumerate live sessions")

        out: list[dict[str, Any]] = []
        for agent in Registry().working():
            session = live.get(agent.session_id)
            out.append({
                "name": agent.name,
                "task": agent.task,
                "cwd": str(getattr(session, "cwd", "") or ""),
                "live": session is not None,
                "busy": bool(getattr(session, "status", "") == "busy"),
            })
        # Live sessions that never declared themselves are still worth talking
        # to -- his own shells, mostly -- so they are listed under their derived
        # name rather than hidden because they skipped a registration step.
        declared = {a.session_id for a in Registry().working()}
        for session_id, session in live.items():
            if session_id in declared:
                continue
            out.append({
                "name": str(getattr(session, "name", "") or session_id[:8]),
                "task": "",
                "cwd": str(getattr(session, "cwd", "") or ""),
                "live": True,
                "busy": bool(getattr(session, "status", "") == "busy"),
            })
        return {"agents": out}

    async def say(self, text: str, agent: str | None) -> dict[str, Any]:
        """Send him a turn's worth of instruction and follow the reply.

        Returns immediately with a conversation key. The answer arrives on the
        event feed rather than on this response, because a task can take
        minutes and an HTTP request that long is a request that dies on a
        network handover.
        """
        conversation, _events = self._open_conversation(agent, "say")
        name = self.call_agent[conversation]
        self._append(conversation, "you", text)

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
                    except Exception:  # noqa: BLE001
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
        return {"conversation": conversation}

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

    async def reply(self, conversation: str, text: str) -> dict[str, Any]:
        """His answer to a question a ring opened.

        Distinct from `say`, which starts a new conversation with an agent. This
        one lands in an existing conversation and is what unblocks the agent
        waiting on `hotline-call`.
        """
        from hotline.httpd import HttpError

        events = self._channel(conversation)
        if events is None:
            raise HttpError(404, f"no conversation {conversation}")
        self._append(conversation, "you", text)
        try:
            # He answered, so nothing is blocked on him here any more --
            # whatever else happens to the conversation afterwards.
            self.store.mark_answered(conversation)
        except Exception:  # noqa: BLE001 - the answer is delivered either way
            log.exception("could not mark %s answered", conversation)
        self._roster_waker.wake()
        return {"conversation": conversation, "delivered": True}

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


def build_server(service: Service, host: str, port: int) -> Any:
    from hotline.httpd import HttpError, Server

    server = Server(host, port, log=lambda message: log.info("%s", message))

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
            "uptime_seconds": round(time.time() - service.started, 1),
            "transport": getattr(service.transport, "name", "?"),
            "rings_when_closed": bool(getattr(service.transport, "rings_when_closed", False)),
            "transports_available": sorted(service.links),
            "active_calls": service.open_calls(),
            "conversations_held": len(service.calls),
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
        return 200, service.agents()

    @server.route("POST", "/api/v1/say")
    async def say(request: Any) -> tuple[int, dict[str, Any]]:
        service.authorise(request)
        body = request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HttpError(400, "text is required")
        agent = body.get("agent")
        return 200, await service.say(text, str(agent) if agent else None)

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
        return 200, await service.reply(conversation, text)

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
    parser.add_argument("--host", default=os.environ.get("HOTLINE_IOS_HOST", "100.72.2.62"))
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
