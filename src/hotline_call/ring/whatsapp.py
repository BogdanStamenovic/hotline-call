"""The third doorbell: a WhatsApp call, placed and carried by wa-call.

Bogdan's request, 2026-11-06 (Discord 1568267763036856402): calls routed by the
registry, so a person who prefers WhatsApp is rung on WhatsApp. Unlike SIP this
module implements none of the protocol. `wa-call daemon` (Rust, on
whatsapp-rust, his repo `wa-call`) is a linked companion device of a WhatsApp
account; this talks to its control socket (JSON lines on
`$XDG_RUNTIME_DIR/wa-call.sock`) and asks it to send the call's audio to a Unix
socket this module listens on. `media/pcmlink.py` turns that socket into the
same `VoiceCall` a SIP call has, so the voice on the line is the same voice.

```
place() ─▶ WhatsAppTransport.ring ─dial {to, spec.audio.socket}─▶ wa-call daemon ─▶ WhatsApp
               │      ▲  events: peer-ringing, accepted, ended …       │
               │      └────────────────────────────────────────────────┤
               └─ listens on wa-<id>.sock ◀─ s16le 16 kHz both ways ───┘
```

**Evidence, as everywhere in `ring/`.** `peer-ringing` is the callee's device
sending `<preaccept>`: it is alerting. That is what sets `ringing`, so
`ConfirmedRing` gets the same proof a SIP `180` gives. wa-call's `connected`
event is NOT used: it fires when the relay is allocated, which upstream
documents as happening while the peer may still be ringing -- or not ringing at
all, if their phone is offline.

**Degrades by raising `CallUnreachable`.** No daemon, a daemon that is not
linked or not online, a dial wa-call refuses: each is `CallUnreachable`, which a
`RingChain` falls through on, so `via=auto` rings SIP instead.

**Inbound is opt-in and narrow.** The linked account may well be his personal
one, where every call from anybody would also reach this daemon. Only numbers
in `HOTLINE_CALL_WA_ANSWER` are answered; every other call is left alone and
rings his phone exactly as it would without wa-call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import select
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from .base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable

log = logging.getLogger("hotline-call.ring.whatsapp")

ADDRESS_PREFIX = "whatsapp:"
"""How a WhatsApp-only guest is written in `CallTarget.address`. `address` is
what tells `_answered_call` the callee is a guest and not him, so it must not be
empty for a guest who has no SIP account."""


def runtime_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def default_socket() -> pathlib.Path:
    return pathlib.Path(os.environ.get("WA_CALL_SOCKET") or runtime_dir() / "wa-call.sock")


def normalise_number(raw: str) -> str:
    """E.164 out of what a person or an agent typed: `+381 60 000-0000` -> `+381600000000`.

    Returns "" for blank or for anything that is not a phone number, so a typo
    reads as "not on WhatsApp" rather than as a call to some other number.
    """
    raw = (raw or "").strip()
    raw = raw.removeprefix(ADDRESS_PREFIX)
    raw = raw.split("@", 1)[0].split(":", 1)[0]
    digits = re.sub(r"[\s().\-]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits if re.fullmatch(r"\+[1-9]\d{6,14}", digits) else ""


def number_of(target: CallTarget) -> str:
    """The WhatsApp number a target is rung on, or ""."""
    return normalise_number(target.metadata.get("whatsapp", "")) or (
        normalise_number(target.address) if target.address.startswith(ADDRESS_PREFIX) else "")


def peer_number(event: dict[str, Any]) -> str:
    """The caller's phone number from a wa-call `ringing` event.

    `peer` may be a LID (`…@lid`), WhatsApp's privacy identifier, which is not a
    phone number; `caller_pn`, when wa-call reports it, is."""
    for key in ("caller_pn", "peer"):
        value = str(event.get(key) or "")
        if value and not value.endswith("@lid"):
            number = normalise_number(value)
            if number:
                return number
    return ""


class WaCallError(Exception):
    pass


class WaCallClient:
    """wa-call's control socket: one JSON request per line, one JSON reply per line."""

    def __init__(self, path: pathlib.Path | str | None = None, *, timeout: float = 5.0) -> None:
        self.path = pathlib.Path(path) if path else default_socket()
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise WaCallError(f"no wa-call daemon at {self.path} ({exc.strerror or exc})") from exc
        return sock

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        with self._connect() as sock:
            sock.sendall((json.dumps({"op": op, **fields}) + "\n").encode())
            line = _readline(sock)
        if not line:
            raise WaCallError("wa-call closed the connection without answering")
        reply = json.loads(line)
        if reply.get("ok") is not True:
            raise WaCallError(str(reply.get("error") or "request failed"))
        return reply

    def events(self) -> EventStream:
        sock = self._connect()
        sock.sendall(b'{"op":"events"}\n')
        _readline(sock)            # the {"ok":true} that opens the stream
        return EventStream(sock)


class EventStream:
    """wa-call's NDJSON event stream, read with a timeout so the reader can give up."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = b""

    def fileno(self) -> int:
        return self.sock.fileno()

    def next(self, timeout: float) -> dict[str, Any] | None:
        """One event, or None if none arrived within `timeout`. Raises on EOF."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], left)
            if not ready:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WaCallError("wa-call's event stream ended")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return {"event": "unparsed", "raw": line.decode(errors="replace")}

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def _readline(sock: socket.socket) -> str:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace").strip()


class _AudioListener:
    """The socket wa-call connects to for one call's audio, removed however it ends."""

    def __init__(self) -> None:
        directory = runtime_dir() / "hotline-call"
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / f"wa-{uuid.uuid4().hex[:12]}.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.sock.listen(1)
        self.conn: socket.socket | None = None

    def accept(self, timeout: float) -> socket.socket | None:
        if self.conn is None:
            ready, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
            if ready:
                self.conn, _ = self.sock.accept()
        return self.conn

    def drain(self) -> None:
        """Throw away audio that arrives before anyone is talking."""
        if self.conn is None:
            return
        with contextlib.suppress(BlockingIOError, InterruptedError, OSError):
            self.conn.setblocking(False)
            while self.conn.recv(65536):
                pass

    def close(self) -> None:
        for sock in (self.conn, self.sock):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


class WhatsAppTransport:
    """Ring a WhatsApp number through wa-call, and hold the call if answered."""

    name = "whatsapp"
    rings_when_closed = True
    """A WhatsApp call rings a locked phone with the app closed, like any call."""

    def __init__(
        self,
        *,
        client: WaCallClient | None = None,
        bogdan: str | None = None,
        answer_from: str | None = None,
        on_answer: Any = None,
        accept_within: float = 5.0,
    ) -> None:
        self.client = client or WaCallClient()
        # His own WhatsApp number, for a call to him with via=whatsapp. Empty
        # by default: if the linked account IS his, there is nobody to ring.
        self.bogdan = normalise_number(
            bogdan if bogdan is not None else os.environ.get("HOTLINE_CALL_WA_BOGDAN", ""))
        raw = answer_from if answer_from is not None else os.environ.get("HOTLINE_CALL_WA_ANSWER", "")
        self.answer_from = {n for n in (normalise_number(x) for x in raw.split(",")) if n}
        self.on_answer = on_answer
        # Set by the daemon: `(peer number, hung_up) -> handler` for an inbound
        # call (an object with `hold(call)` and `abandon(why)`), or None.
        self.on_call: Callable[[str, Callable[[], bool]], Any] | None = None
        self.accept_within = accept_within
        self.ringing = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gone = threading.Event()
        self._abort = threading.Event()
        self._stop = threading.Event()
        self._inbound: threading.Thread | None = None
        self.last_error = ""

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.answer_from and self._inbound is None:
            self._stop.clear()
            self._inbound = threading.Thread(target=self._watch_inbound, daemon=True,
                                             name="wa-inbound")
            self._inbound.start()
            log.info("whatsapp: answering calls from %d number(s)", len(self.answer_from))

    async def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        """For /health: is there a daemon, is it linked and online. Never raises."""
        out: dict[str, Any] = {"socket": str(self.client.path), "daemon": False,
                               "logged_in": False, "answers_inbound": len(self.answer_from)}
        try:
            reply = self.client.request("status")
        except (WaCallError, OSError, ValueError) as exc:
            out["error"] = str(exc)
            return out
        out["daemon"] = True
        out["logged_in"] = bool(reply.get("logged_in"))
        out["live_calls"] = len(reply.get("live") or [])
        if not out["logged_in"]:
            out["error"] = "wa-call is running but not linked or not online"
        return out

    # ---- ringing ------------------------------------------------------------

    def far_end_hung_up(self) -> bool:
        return self._gone.is_set()

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> None:
        self.ringing.clear()
        number = number_of(target) or ("" if target.address else self.bogdan)
        if not number:
            raise CallUnreachable(
                "whatsapp: no number to ring" + ("" if target.address else
                                                 " (HOTLINE_CALL_WA_BOGDAN is not set)"))
        self._loop = asyncio.get_running_loop()
        self._abort.clear()
        self._gone.clear()
        work = self._loop.run_in_executor(None, self._ring_blocking, number, timeout)
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # ConfirmedRing gave up on us (no ring evidence in time) or the chain
            # is being torn down. The thread must hang up, or the peer's phone
            # keeps ringing while the chain rings them again over SIP.
            self._abort.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(work, 10.0)
            raise

    def _ringing_now(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.ringing.set)

    def _ring_blocking(self, number: str, timeout: float) -> None:
        try:
            state = self.client.request("status")
        except WaCallError as exc:
            self.last_error = str(exc)
            raise CallUnreachable(f"whatsapp: {exc}") from exc
        if not state.get("logged_in"):
            self.last_error = "not linked or not online"
            raise CallUnreachable("whatsapp: wa-call is not linked or not online")
        listener = _AudioListener()
        events: EventStream | None = None
        call_id = ""
        try:
            try:
                events = self.client.events()
                reply = self.client.request(
                    "dial", to=number, spec={"audio": {"socket": str(listener.path)}})
            except WaCallError as exc:
                self.last_error = str(exc)
                raise CallUnreachable(f"whatsapp: dial refused: {exc}") from exc
            call_id = str(reply.get("call_id") or "")
            log.info("whatsapp: dialing %s (call %s)", _masked(number), call_id)
            self._await_answer(events, listener, call_id, timeout)
            self._hold(events, listener, call_id)
        finally:
            if call_id and not self._gone.is_set():
                with contextlib.suppress(WaCallError, OSError):
                    self.client.request("hangup", call_id=call_id)
            if events is not None:
                events.close()
            listener.close()

    def _await_answer(self, events: EventStream, listener: _AudioListener, call_id: str,
                      timeout: float) -> None:
        deadline = time.monotonic() + timeout
        rang = False
        while True:
            if self._abort.is_set():
                raise CallUnreachable("whatsapp: abandoned before it was answered")
            left = deadline - time.monotonic()
            if left <= 0:
                break
            listener.accept(0)
            listener.drain()
            event = events.next(min(0.25, left))
            if event is None or event.get("call_id") != call_id:
                continue
            kind = event.get("event")
            if kind == "peer-ringing" and not rang:
                rang = True
                log.info("whatsapp: %s is ringing", call_id)
                self._ringing_now()
            elif kind in ("accepted", "media-connected"):
                # An accept is also proof it rang, if the preaccept went missing.
                self._ringing_now()
                log.info("whatsapp: %s answered", call_id)
                return
            elif kind == "rejected" and event.get("declined"):
                self._gone.set()
                raise CallDeclined("they declined the whatsapp call")
            elif kind == "failed":
                self._gone.set()
                raise CallUnreachable(f"whatsapp: call failed: {event.get('error', '')}")
            elif kind in ("ended", "media-ended"):
                self._gone.set()
                if event.get("rejected"):
                    raise CallDeclined("they declined the whatsapp call")
                if rang:
                    raise CallUnanswered("whatsapp rang out")
                raise CallUnreachable(f"whatsapp: call ended before it rang ({event})")
        if not rang:
            raise CallUnreachable("whatsapp: no sign the phone rang")
        raise CallUnanswered(f"whatsapp rang for {timeout:.0f}s with no answer")

    def _hold(self, events: EventStream, listener: _AudioListener, call_id: str) -> None:
        handler = self.on_answer
        hold = getattr(handler, "hold", None)
        if hold is None:
            # Nothing can talk on it -- hang up, the way SIP ACKs and BYEs.
            log.info("whatsapp: answered with nothing to say; hanging up")
            return
        conn = listener.accept(self.accept_within)
        if conn is None:
            log.error("whatsapp: wa-call never connected the audio socket; hanging up")
            return
        watcher = threading.Thread(target=self._watch_end, args=(events, call_id),
                                   daemon=True, name="wa-call-end")
        watcher.start()
        hold(self._voice_call(conn))

    def _voice_call(self, conn: socket.socket) -> Any:
        from ..media.pcmlink import PcmPump
        from ..media.voicecall import VoiceCall

        pump = PcmPump(conn)
        call = VoiceCall.over(pump)
        # The stream closing is as good a hangup signal as the event.
        gone = self._gone

        def eof_watch() -> None:
            while not gone.is_set() and pump.is_alive():
                if pump.eof:
                    gone.set()
                    return
                time.sleep(0.2)

        threading.Thread(target=eof_watch, daemon=True, name="wa-call-eof").start()
        return call

    def _watch_end(self, events: EventStream, call_id: str) -> None:
        with contextlib.suppress(WaCallError, OSError, ValueError):
            while not self._gone.is_set():
                event = events.next(0.5)
                if event and event.get("call_id") == call_id and \
                        event.get("event") in ("ended", "media-ended", "failed"):
                    log.info("whatsapp: %s ended (%s)", call_id, event.get("by", event.get("event")))
                    self._gone.set()
                    return
        self._gone.set()

    # ---- him (or an allowed number) calling us -------------------------------

    def _watch_inbound(self) -> None:
        """Answer calls from `answer_from`, reconnecting to wa-call whenever it restarts."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                stream = self.client.events()
            except (WaCallError, OSError):
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            backoff = 1.0
            try:
                for event in _iterate(stream, self._stop):
                    if event.get("event") == "ringing" and event.get("direction", "incoming") == "incoming":
                        number = peer_number(event)
                        if number in self.answer_from:
                            threading.Thread(target=self._answer_inbound, daemon=True,
                                             args=(str(event.get("call_id", "")), number),
                                             name="wa-inbound-call").start()
                        else:
                            log.info("whatsapp: call %s is not for us; leaving it to the phone",
                                     event.get("call_id"))
            except (WaCallError, OSError, ValueError):
                pass
            finally:
                stream.close()

    def _answer_inbound(self, call_id: str, number: str) -> None:
        if self.on_call is None:
            return
        gone = threading.Event()
        try:
            handler = self.on_call(number, gone.is_set)
        except Exception:
            log.exception("whatsapp: could not prepare an inbound call")
            return
        if handler is None:
            return
        listener = _AudioListener()
        events: EventStream | None = None
        try:
            events = self.client.events()
            self.client.request("answer", call_id=call_id,
                                spec={"audio": {"socket": str(listener.path)}})
            conn = listener.accept(self.accept_within)
            if conn is None:
                raise WaCallError("wa-call never connected the audio socket")
        except (WaCallError, OSError) as exc:
            log.error("whatsapp: could not answer %s: %s", call_id, exc)
            with contextlib.suppress(Exception):
                handler.abandon(str(exc))
            if events is not None:
                events.close()
            listener.close()
            return
        log.info("whatsapp: answered inbound call %s from %s", call_id, _masked(number))
        from ..media.pcmlink import PcmPump
        from ..media.voicecall import VoiceCall

        pump = PcmPump(conn)

        def watch() -> None:
            with contextlib.suppress(WaCallError, OSError, ValueError):
                while not gone.is_set():
                    if pump.eof:
                        break
                    event = events.next(0.5)
                    if event and event.get("call_id") == call_id and \
                            event.get("event") in ("ended", "media-ended", "failed"):
                        break
            gone.set()

        threading.Thread(target=watch, daemon=True, name="wa-inbound-end").start()
        try:
            handler.hold(VoiceCall.over(pump))
        finally:
            if not gone.is_set():
                with contextlib.suppress(WaCallError, OSError):
                    self.client.request("hangup", call_id=call_id)
            gone.set()
            events.close()
            listener.close()


def _iterate(stream: EventStream, stop: threading.Event) -> Iterator[dict[str, Any]]:
    while not stop.is_set():
        event = stream.next(1.0)
        if event is not None:
            yield event


def _masked(number: str) -> str:
    """Logs are not the place for other people's phone numbers."""
    return number[:4] + "…" + number[-2:] if len(number) > 6 else "…"
