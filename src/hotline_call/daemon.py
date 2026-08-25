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
import contextlib
import logging
import os
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .events import EventLog
from .ring.base import (
    CallDeclined,
    CallError,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
)

log = logging.getLogger("hotline-iosd")

DEFAULT_PORT = 8789
"""One past hotlined's 8788, so the two are obviously siblings."""

MAX_WAIT = 30.0
TURN_TIMEOUT = 900.0
"""Ceiling on a long-poll. Under most proxy and NAT idle timeouts."""


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
        self.calls: dict[str, EventLog] = {}
        # Live sessions, so the phone can end a call from its own UI rather
        # than only by the far end hanging up.
        self.sessions: dict[str, Any] = {}
        self.started = time.time()
        self.degradations: list[str] = []

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

        conversation = uuid.uuid4().hex[:12]
        events = EventLog()
        self.calls[conversation] = events
        # Put the question in the conversation before ringing, so that whenever
        # he opens the app -- during the ring, or an hour later -- it is already
        # there and he never answers a phone to silence.
        events.append("claude", f"{target.caller_id}: {reason}", at=time.time())
        if str(body.get("context", "")):
            events.append("summary", str(body["context"])[:1200], at=time.time())
        began = time.monotonic()

        try:
            await self.transport.ring(target, timeout=ring_timeout)
        except CallDeclined as exc:
            events.append("state", "declined", at=time.time())
            events.close()
            return self._outcome(conversation, "declined", began, str(exc))
        except CallUnanswered as exc:
            # It rang and he did not pick up. The conversation stays OPEN: he
            # may well open the app five minutes later, and closing it here
            # would throw away the question he is about to answer.
            events.append("state", "unanswered", at=time.time())
            return self._outcome(conversation, "unanswered", began, str(exc))
        except (CallUnreachable, CallError) as exc:
            self.degradations.append(str(exc))
            log.warning("call %s undeliverable: %s", conversation, exc)
            events.append("error", str(exc), at=time.time())
            events.close()
            return self._outcome(conversation, "unreachable", began, str(exc))

        if not wait:
            return self._outcome(conversation, "ringing", began, "not waiting")

        reply = await self._await_reply(events, reply_timeout)
        if not reply:
            return self._outcome(conversation, "unanswered", began,
                                 f"rang, but nothing came back within {reply_timeout:.0f}s")
        return self._outcome(conversation, "answered", began, "", reply)

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

    async def _bind(self, key: str, agent: str) -> None:
        try:
            bind = getattr(self.pool, "bind", None)
            if bind is None:
                return
            from hotline.agents import Registry

            record = Registry().by_name(agent)
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
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "call_id": call_id,
            "conversation": call_id,
            "state": state,
            "reply": reply,
            "detail": detail,
            "waited_seconds": round(time.monotonic() - began, 1),
            "transport": getattr(self.transport, "name", "?"),
            "rings_when_closed": bool(getattr(self.transport, "rings_when_closed", False)),
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
        events = self.calls.get(call_id)
        if events is not None and not events.closed:
            events.append("state", "ended", at=time.time())
            events.close()
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

        live: dict[str, Any] = {}
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
        conversation = uuid.uuid4().hex[:12]
        events = EventLog()
        self.calls[conversation] = events
        events.append("you", text, at=time.time())

        key = f"ios-{conversation}"
        if agent:
            await self._bind(key, agent)

        async def run() -> None:
            def narrate(event: Any) -> None:
                kind = getattr(event, "kind", "")
                if kind in ("tool", "summary"):
                    events.append(kind, getattr(event, "detail", ""),
                                  getattr(event, "tool", None), time.time())

            try:
                _route, reply = await self.pool.ask(
                    key, text, narrator=narrate, timeout=TURN_TIMEOUT,
                    origin=_typed(agent),
                )
                answer = reply.text
                if getattr(reply, "notice", ""):
                    answer = f"Heads up, {reply.notice}. {answer}"
                events.append("claude", answer, at=time.time())
            except Exception as exc:
                log.exception("delegation turn failed")
                events.append("error", f"{type(exc).__name__}: {exc}", at=time.time())
            finally:
                events.close()

        self.sessions[conversation] = asyncio.ensure_future(run())
        return {"conversation": conversation}

    # ---- the live feed ---------------------------------------------------

    async def feed(self, call_id: str, since: int, wait: float) -> dict[str, Any]:
        from hotline.httpd import HttpError

        events = self.calls.get(call_id)
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
        return 200, {
            "ok": True,
            "uptime_seconds": round(time.time() - service.started, 1),
            "transport": getattr(service.transport, "name", "?"),
            "rings_when_closed": bool(getattr(service.transport, "rings_when_closed", False)),
            "active_calls": len(service.calls),
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hotline-iosd")
    parser.add_argument("--host", default=os.environ.get("HOTLINE_IOS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOTLINE_IOS_PORT", DEFAULT_PORT)))
    parser.add_argument("--transport", default=os.environ.get("HOTLINE_IOS_TRANSPORT", "loopback"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    log.warning(
        "starting with transport=%s -- no ring transport is chosen yet; see docs/ARCHITECTURE.md",
        args.transport,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
