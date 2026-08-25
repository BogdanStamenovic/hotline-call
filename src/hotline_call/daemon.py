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

from .call import CallSession, TurnEvent
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

    # ---- placing a call --------------------------------------------------

    async def place(self, body: dict[str, Any]) -> dict[str, Any]:
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
        # A hard ceiling on the whole call, separate from the ring timeout.
        # Without it a call nobody hangs up blocks the HTTP request forever --
        # which is exactly what happened the first time this ran against a
        # transport that never closes its own stream.
        max_seconds = float(body.get("timeout", 900.0))
        wait = bool(body.get("wait", True))

        call_id = uuid.uuid4().hex[:12]
        events = EventLog()
        self.calls[call_id] = events
        events.append("state", "ringing", at=time.time())
        began = time.monotonic()

        try:
            stream = await self.transport.ring(target, timeout=ring_timeout)
        except CallDeclined as exc:
            events.append("state", "declined", at=time.time())
            events.close()
            return self._outcome(call_id, "declined", began, str(exc))
        except CallUnanswered as exc:
            events.append("state", "unanswered", at=time.time())
            events.close()
            return self._outcome(call_id, "unanswered", began, str(exc))
        except (CallUnreachable, CallError) as exc:
            # The loud part. A ring that never landed must be visible here, not
            # only in the caller's exit code.
            self.degradations.append(str(exc))
            log.warning("call %s undeliverable: %s", call_id, exc)
            events.append("error", str(exc), at=time.time())
            events.close()
            return self._outcome(call_id, "unreachable", began, str(exc))

        session = CallSession(
            pool=self.pool,
            transcriber=self.transcriber,
            speaker=self.speaker,
            stream=stream,
            target=target,
            key=f"ios-{call_id}",
            segmenter_factory=self.segmenter_factory,
            speakable=_speakable(),
            origin_factory=lambda: _origin(target),
            on_event=lambda event: self._record(events, event),
        )
        # Bind the conversation to a specific agent before the first turn, so
        # "call hotline-80" reaches hotline-80 rather than whatever is newest.
        if target.agent:
            await self._bind(f"ios-{call_id}", target.agent)

        runner = asyncio.ensure_future(session.run())
        # Announce why the phone rang. Without this he answers to silence and
        # has to ask, which on a spoken channel costs a whole turn.
        opening = f"{target.caller_id} here. {reason}"
        await session.say(opening)
        events.append("said", opening, at=time.time())

        if not wait:
            return self._outcome(call_id, "ringing", began, "not waiting")

        detail = ""
        try:
            await asyncio.wait_for(runner, timeout=max_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            # End it rather than leaking the call and the request together.
            detail = f"call exceeded {max_seconds:.0f}s and was ended"
            log.warning("call %s: %s", call_id, detail)
            events.append("error", detail, at=time.time())
            await session.hangup()
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
        reply = _last_from_him(session)
        events.close()
        state = "answered" if reply else "ended"
        return self._outcome(call_id, state, began, detail, reply, session)

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

    def _record(self, events: EventLog, event: TurnEvent) -> None:
        events.append(event.kind, event.text, event.tool, event.at)

    def _outcome(
        self,
        call_id: str,
        state: str,
        began: float,
        detail: str = "",
        reply: str = "",
        session: Any = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "call_id": call_id,
            "state": state,
            "reply": reply,
            "detail": detail,
            "waited_seconds": round(time.monotonic() - began, 1),
            "transport": getattr(self.transport, "name", "?"),
            "rings_when_closed": bool(getattr(self.transport, "rings_when_closed", False)),
        }
        if session is not None:
            out["transcript"] = [{"who": who, "text": text} for who, text in session.transcript]
        return out

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
