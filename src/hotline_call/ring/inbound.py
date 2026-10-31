"""The other direction: he rings hotline, and hotline picks up.

`sip.py` registers only for the length of one ring, which is right for a
doorbell and useless for being called: an INVITE to our account needs a binding
that is up when it arrives. This keeps one up all the time, on its own TLS
connection, and answers what arrives on it.

## What it does

- **One persistent registration**, refreshed at half its granted lifetime, with
  a CRLF keepalive on the connection (RFC 5626 4.4.1) and TCP keepalives
  underneath. A drop -- EOF, a socket error, a REGISTER that goes unanswered --
  closes the connection and starts over with backoff. Nothing else in the
  daemon waits on this; a registration that cannot be made is logged and
  retried, and outbound rings never touch this socket.
- **A stable `+sip.instance`.** Re-registrations replace the binding instead of
  piling up a new one per reconnect, each of which linphone.org would otherwise
  fork every INVITE to until it expired. `HOTLINE_IOS_INBOUND_INSTANCE`
  overrides it -- a test registration MUST use a different one, or it replaces
  the production binding.
- **Answering**: 100 Trying, 180 Ringing, then 200 OK with an SDP answer after
  `answer_after` seconds -- long enough that a CANCEL has somewhere to land, and
  that the models the daemon starts warming on the INVITE get a head start.
  The 200 is retransmitted (RFC 3261 13.3.1.4, which applies over TLS too) until
  his ACK arrives, and a call whose ACK never comes is ended with a BYE after
  64*T1 rather than held silently.
- **Encrypted media only.** His client offers `RTP/SAVP` with SDES and refuses
  plain RTP (see `sip.py`). An offer with no `AES_CM_128_HMAC_SHA1_80` line, no
  shared codec, or no audio is answered `488` with a Warning that says which.
  The answer echoes the offer's crypto tag with OUR key: SDES keys are per
  direction, ours encrypts what we send, his decrypts what he sends.
- **Only him.** The From URI has to be `SIP_PEER`. Anything else is `403` and a
  warning in the log naming the address that tried.
- **One call at a time.** A second INVITE while one is up -- or while the daemon
  says it is busy, e.g. PersonaPlex holding the GPU -- is `486 Busy Here`.
- **BYE both ways, CANCEL before answer** (`200` to the CANCEL, `487` to the
  INVITE), in-dialog re-INVITE/UPDATE answered with the same SDP, OPTIONS 200.

## The thread model

One thread owns the SIP socket and does all of the I/O on it, including the BYE
at the end of a call. The conversation runs on a second thread, exactly as it
does for an outbound call, and only ever reads `InboundCall.far_end_hung_up()`
-- a flag the SIP thread sets. Two threads writing one TLS socket would need a
lock around every send, and SSL objects are not safe to share for reads anyway.

## Limitations

- **From is trusted as linphone.org delivers it.** linphone.org authenticates
  its own users' From, so a call from his account is him; a call relayed from
  another domain claiming his address is not something this can tell apart.
- **No ICE, no late offer.** An INVITE without SDP is answered 488. ICE
  attributes in the offer are ignored, and symmetric RTP latching in
  `media/voicecall.py` is what makes NAT work, the same as on outbound calls.
- **A dropped SIP connection mid-call loses its signalling.** The audio carries
  on; a BYE sent to the dead connection is lost, and the conversation ends on
  its own dead-line timer (90 s of no RTP) instead.
- **UDP is not supported here.** Inbound needs the server to be able to reach
  us at any moment, which over UDP behind NAT means keepalive tuning against
  binding timeouts nobody has measured. TLS (default) and TCP only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..media import codec, srtp
from .sip import (
    DEFAULT_REALM,
    _digest,
    _tag,
    header_of,
    headers_of,
    parse_challenge,
    status_of,
    uri_of,
)

log = logging.getLogger("hotline-ios.ring.inbound")

T1 = 0.5
T2 = 4.0
ACK_GIVE_UP = 64 * T1
KEEPALIVE_EVERY = 25.0
"""Seconds between CRLF pings. Under the 30-60 s idle timeouts typical of
consumer NAT and of proxies that reap quiet TLS flows."""
REGISTER_TIMEOUT = 15.0
BACKOFF = (2.0, 5.0, 15.0, 30.0, 60.0)
SUPPORTED_PROFILES = ("RTP/SAVP", "RTP/SAVPF")


class Busy(Exception):
    """Raised by `on_call` to have the INVITE answered 486."""


def _same_account(a: str, b: str) -> bool:
    """Whether two SIP addresses name the same account: user exact, host any case."""
    def norm(value: str) -> tuple[str, str]:
        value = uri_of(value) if "<" in value else value.split(";")[0].strip()
        value = re.sub(r"^sips?:", "", value.strip(), flags=re.IGNORECASE)
        user, _, host = value.partition("@")
        return user, host.split(":")[0].lower()
    ua, ha = norm(a)
    ub, hb = norm(b)
    return bool(ua) and ua == ub and ha == hb


def _with_tag(header: str, tag: str) -> str:
    return header if ";tag=" in header else f"{header};tag={tag}"


def _tag_of(header: str) -> str:
    match = re.search(r";\s*tag=([^;>\s]+)", header)
    return match.group(1) if match else ""


def _branch(message: str) -> str:
    match = re.search(r";\s*branch=([^;,\s]+)", header_of(message, "Via") or header_of(message, "v"))
    return match.group(1) if match else ""


class OfferError(Exception):
    """An offer we cannot carry audio over. The message goes in the 488's Warning."""


def answer_offer(offer: str, media_host: str, media_port: int, key: bytes, salt: bytes,
                 env: str | None = None) -> tuple[str, str]:
    """Our SDP answer to his offer, and the same offer reduced to what was agreed.

    The second string is what `AnsweredCall` is handed in place of a 200 OK: his
    address and port, his crypto line, and ONLY the codec we answered with.
    `parse_sdp_answer` takes the answerer's order, which on an offer would be
    his preference rather than our decision, so it must not see the rest.
    """
    body = offer.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in offer else offer
    lines = [line.strip() for line in body.replace("\r\n", "\n").split("\n") if line.strip()]
    session_c = ""
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("m="):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        elif line.startswith("c=IN IP4 "):
            session_c = line
    if not sections:
        raise OfferError("no media in the offer")

    audio_index = -1
    for index, section in enumerate(sections):
        parts = section[0].split()
        if parts[0] == "m=audio" and len(parts) >= 4 and parts[1] != "0":
            audio_index = index
            break
    if audio_index < 0:
        raise OfferError("no audio stream offered")

    audio = sections[audio_index]
    m_parts = audio[0].split()
    profile = m_parts[2]
    if profile not in SUPPORTED_PROFILES:
        raise OfferError(f"media profile {profile} is not encrypted (SRTP/SDES only)")
    crypto = ""
    tag = 0
    for line in audio:
        if line.startswith("a=crypto:"):
            try:
                tag, _k, _s = srtp.parse_crypto_line(line)
            except (srtp.SrtpError, ValueError):
                continue
            crypto = line
            break
    if not crypto:
        raise OfferError(f"no {srtp.SRTP_PROFILE} crypto line in the offer")
    c_line = next((line for line in audio if line.startswith("c=IN IP4 ")), session_c)
    if not c_line:
        raise OfferError("no c=IN IP4 line: nowhere to send audio")

    agreed = codec.answer_lines("\r\n".join(audio), env)
    if agreed is None:
        raise OfferError("no codec in common (not even PCMU)")
    choice, pt, attrs = agreed

    answer_sections = []
    for index, section in enumerate(sections):
        if index == audio_index:
            answer_sections.append(
                f"m=audio {media_port} {profile} {pt}\r\n"
                f"{srtp.crypto_line(key, salt, tag)}\r\n"
                + "".join(f"{line}\r\n" for line in attrs)
                + "a=sendrecv\r\n"
            )
        else:
            # Every offered stream gets a line in the answer, declined with port
            # 0, or the answer is malformed (RFC 3264 6).
            parts = section[0].split()
            answer_sections.append(f"{parts[0]} 0 {' '.join(parts[2:])}\r\n")
    answer = (
        "v=0\r\n"
        f"o=- {random.randint(1, 2**31)} 1 IN IP4 {media_host}\r\n"
        "s=hotline\r\n"
        f"c=IN IP4 {media_host}\r\n"
        "t=0 0\r\n"
        + "".join(answer_sections)
    )
    keep = [line for line in audio
            if line.startswith((f"a=rtpmap:{pt} ", f"a=fmtp:{pt} "))]
    agreed_offer = (
        "SIP/2.0 200 OK\r\n\r\n"
        "v=0\r\n"
        f"{c_line}\r\n"
        f"m=audio {m_parts[1]} {profile} {pt}\r\n"
        f"{crypto}\r\n"
        + "".join(f"{line}\r\n" for line in keep)
    )
    log.info("inbound: answering with %s/%d (pt %s)", choice.name, choice.clock, pt)
    return answer, agreed_offer


class InboundCall:
    """One incoming call, from its INVITE until one side ends it."""

    def __init__(self, invite: str, local_tag: str) -> None:
        self.invite = invite
        self.call_id = header_of(invite, "Call-ID") or header_of(invite, "i")
        self.their_from = header_of(invite, "From") or header_of(invite, "f")
        self.their_tag = _tag_of(self.their_from)
        self.caller = uri_of(self.their_from)
        self.our_to = _with_tag(header_of(invite, "To") or header_of(invite, "t"), local_tag)
        self.cseq = header_of(invite, "CSeq").split()[0] if header_of(invite, "CSeq") else "1"
        self.target = uri_of(header_of(invite, "Contact") or header_of(invite, "m")) or self.caller
        # A UAS's route set is the INVITE's Record-Route in the order received
        # (RFC 3261 12.1.1) -- the reverse of what `sip.route_set` computes for
        # the UAC side, which is why that helper is not reused here.
        self.routes: list[str] = []
        for value in headers_of(invite, "Record-Route"):
            self.routes.extend(part.strip() for part in value.split(",") if part.strip())
        self.state = "ringing"     # -> answered -> over
        self.answer_sdp = ""
        self.agreed_offer = ""
        self.media: socket.socket | None = None
        self.key = b""
        self.salt = b""
        self.ok = ""               # the 200 OK, kept for retransmission
        self.answer_at = 0.0
        self.answered_at = 0.0
        self.next_resend = 0.0
        self.resend_gap = T1
        self.acked = threading.Event()
        self.over = threading.Event()
        self.handler_done = threading.Event()
        self.bye_sent = False
        self.handler: Any = None
        self.ended = ""

    def matches(self, message: str) -> bool:
        return (header_of(message, "Call-ID") or header_of(message, "i")) == self.call_id

    def far_end_hung_up(self) -> bool:
        """The `hung_up` hook `AnsweredCall` polls. Set only by the SIP thread."""
        return self.over.is_set()


class SipInbound:
    """Keep hotline's account registered and answer his calls on it."""

    def __init__(
        self,
        *,
        user: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        peer: str | None = None,
        transport: str | None = None,
        port: int | None = None,
        media_host: str | None = None,
        instance: str | None = None,
        expires: int = 600,
        answer_after: float | None = None,
        on_call: Callable[[InboundCall], Any] | None = None,
        keepalive: float = KEEPALIVE_EVERY,
        server_hostname: str | None = None,
        verify_tls: bool = True,
    ) -> None:
        self.user = user or os.environ.get("SIP_USER", "")
        self.password = password or os.environ.get("SIP_PASSWORD", "")
        self.domain = domain or os.environ.get("SIP_DOMAIN", DEFAULT_REALM)
        self.peer = peer if peer is not None else os.environ.get("SIP_PEER", "")
        self.transport = (transport or os.environ.get("SIP_TRANSPORT", "tls")).lower()
        if self.transport not in ("tls", "tcp"):
            # See the module docstring: UDP is not supported for staying reachable.
            log.warning("inbound: SIP_TRANSPORT=%s is not supported for inbound; using tls",
                        self.transport)
            self.transport = "tls"
        default_port = 5061 if self.transport == "tls" else 5060
        if port is None:
            env_port = os.environ.get("SIP_PORT", "")
            port = int(env_port) if env_port and (os.environ.get("SIP_TRANSPORT", "tls").lower()
                                                   == self.transport) else default_port
        self.port = port
        self.media_host = media_host if media_host is not None else os.environ.get("SIP_MEDIA_HOST", "")
        self.instance = instance or os.environ.get("HOTLINE_IOS_INBOUND_INSTANCE", "") or str(
            uuid.uuid5(uuid.NAMESPACE_URL,
                       f"hotline-ios-inbound:{self.user}@{self.domain}:{socket.gethostname()}"))
        self.expires = expires
        self.answer_after = (answer_after if answer_after is not None
                             else float(os.environ.get("HOTLINE_IOS_INBOUND_ANSWER_AFTER", "1.5")))
        self.on_call = on_call
        self.keepalive = keepalive
        self.server_hostname = server_hostname or self.domain
        self.verify_tls = verify_tls

        self.registered = threading.Event()
        self.registered_until = 0.0
        self.last_error = ""
        self.counts = {"registrations": 0, "connects": 0, "answered": 0, "rejected": 0,
                       "busy": 0, "cancelled": 0}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._local: tuple[str, int] = ("0.0.0.0", 0)
        self._reg_call_id = uuid.uuid4().hex
        self._reg_from_tag = _tag()
        self._reg_cseq = 0
        self._reg_pending = 0.0      # when the outstanding REGISTER was sent, 0 if none
        self._reg_authed = False
        self._next_register = 0.0
        self._next_ping = 0.0
        self._local_cseq = 0
        self._last_rx = 0.0
        self._pongs = False
        self._attempt = 0
        self.call: InboundCall | None = None
        self._draining: list[InboundCall] = []

    # ---- lifecycle ---------------------------------------------------------

    def configured(self) -> str:
        """Empty when it can run, else why not."""
        if not (self.user and self.password):
            return "inbound needs SIP_USER and SIP_PASSWORD"
        if not self.peer:
            return "inbound needs SIP_PEER: it only answers his account"
        return ""

    def start(self) -> None:
        why = self.configured()
        if why:
            raise RuntimeError(why)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sip-inbound", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict[str, Any]:
        return {
            "registered": self.registered.is_set(),
            "registeredFor": max(0, int(self.registered_until - time.monotonic()))
            if self.registered.is_set() else 0,
            "inCall": self.call is not None,
            "lastError": self.last_error,
            **self.counts,
        }

    def _run(self) -> None:
        self._attempt = 0
        while not self._stop.is_set():
            try:
                self._connect()
                self.counts["connects"] += 1
                self._register()
                self._serve()
            except Exception as exc:  # noqa: BLE001 - the loop is the recovery
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("inbound: %s; reconnecting", self.last_error)
            self.registered.clear()
            if self._stop.is_set():
                break
            self._close()
            delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
            self._attempt += 1
            self._stop.wait(delay)
        # Leaving: drop the binding so linphone.org stops forking calls to a
        # process that is gone, then let go of the connection.
        with contextlib.suppress(Exception):
            if self._sock is not None and self.registered.is_set():
                self._send(self._register_message(expires=0))
        self.registered.clear()
        self._close()

    # ---- the wire ----------------------------------------------------------

    def _connect(self) -> None:
        self._close()
        raw = socket.create_connection((self.domain, self.port), timeout=10)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                with contextlib.suppress(OSError):
                    raw.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        if self.transport == "tls":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            sock: socket.socket = context.wrap_socket(raw, server_hostname=self.server_hostname)
        else:
            sock = raw
        self._local = sock.getsockname()[:2]
        self._sock = sock
        self._buffer = b""
        self._reg_pending = 0.0
        self._reg_authed = False
        self._last_rx = time.monotonic()
        self._pongs = False
        log.info("inbound: connected to %s:%d over %s from %s:%d",
                 self.domain, self.port, self.transport, *self._local)

    def _close(self) -> None:
        self._buffer = b""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _send(self, message: str) -> None:
        assert self._sock is not None
        self._sock.sendall(message.encode())

    def _read(self, timeout: float) -> str | None:
        """One message, "" on timeout. Raises ConnectionError on EOF."""
        assert self._sock is not None
        deadline = time.monotonic() + timeout
        while True:
            message = self._take_message()
            if message is not None:
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ""
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65535)
            except TimeoutError:
                return ""
            except ssl.SSLWantReadError:
                return ""
            if not chunk:
                raise ConnectionError("the server closed the connection")
            self._last_rx = time.monotonic()
            self._buffer += chunk

    def _take_message(self) -> str | None:
        # Keepalive pongs are bare CRLFs between messages; they carry nothing
        # except that the flow is alive.
        if self._buffer.startswith(b"\r\n"):
            self._pongs = True
            self._buffer = self._buffer.lstrip(b"\r\n")
        split = self._buffer.find(b"\r\n\r\n")
        if split < 0:
            return None
        head = self._buffer[:split].decode("utf-8", errors="replace")
        length = 0
        for line in head.split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() in ("content-length", "l"):
                with contextlib.suppress(ValueError):
                    length = int(value.strip())
        total = split + 4 + length
        if len(self._buffer) < total:
            return None
        message = self._buffer[:total].decode("utf-8", errors="replace")
        self._buffer = self._buffer[total:]
        return message

    # ---- messages ----------------------------------------------------------

    def _via(self) -> str:
        host, port = self._local
        return (f"Via: SIP/2.0/{self.transport.upper()} {host}:{port};"
                f"branch=z9hG4bK{_tag(16)};rport")

    def _contact_uri(self) -> str:
        host, port = self._local
        return f"sip:{self.user}@{host}:{port};transport={self.transport};ob"

    def _contact(self) -> str:
        return f"Contact: <{self._contact_uri()}>"

    def _register_message(self, *, auth: str = "", expires: int | None = None) -> str:
        self._reg_cseq += 1
        me = f"sip:{self.user}@{self.domain}"
        lines = [
            f"REGISTER sip:{self.domain} SIP/2.0",
            self._via(),
            f"From: <{me}>;tag={self._reg_from_tag}",
            f"To: <{me}>",
            f"Call-ID: {self._reg_call_id}",
            f"CSeq: {self._reg_cseq} REGISTER",
            f'Contact: <{self._contact_uri()}>;+sip.instance="<urn:uuid:{self.instance}>";reg-id=1',
            f"Expires: {self.expires if expires is None else expires}",
            "Supported: outbound, path",
            "Max-Forwards: 70",
            "User-Agent: hotline-ios inbound",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE, INFO, NOTIFY",
        ]
        if auth:
            lines.append(auth)
        lines.append("Content-Length: 0")
        return "\r\n".join(lines) + "\r\n\r\n"

    def _authorisation(self, challenge: str, method: str, uri: str, *, proxy: bool) -> str:
        params = parse_challenge(challenge)
        realm = params.get("realm", self.domain)
        nonce = params.get("nonce", "")
        qop = params.get("qop", "").split(",")[0].strip()
        cnonce = _tag(16)
        response = _digest(self.user, self.password, realm, nonce, method, uri,
                           qop=qop, cnonce=cnonce, algorithm=params.get("algorithm", "MD5"))
        parts = [f'Digest username="{self.user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', f'response="{response}"']
        if params.get("opaque"):
            parts.append(f'opaque="{params["opaque"]}"')
        if qop:
            parts += [f"qop={qop}", "nc=00000001", f'cnonce="{cnonce}"']
        if params.get("algorithm"):
            parts.append(f"algorithm={params['algorithm']}")
        return f"{'Proxy-Authorization' if proxy else 'Authorization'}: " + ", ".join(parts)

    def _register(self, auth: str = "") -> None:
        self._send(self._register_message(auth=auth))
        self._reg_pending = time.monotonic()

    def _response(self, request: str, status: str, *, local_tag: str = "",
                  extra: tuple[str, ...] = (), body: str = "",
                  record_route: bool = False) -> str:
        """A response built by echoing the request's transaction headers.

        Every Via, in order -- a proxy that forked the INVITE put its own on top
        and routes the response by it. Record-Route only on a dialog-creating
        response, where the far end learns the route set from it.
        """
        lines = [f"SIP/2.0 {status}"]
        for line in request.split("\r\n")[1:]:
            if not line:
                break
            name = line.split(":", 1)[0].strip().lower()
            if name in ("via", "v", "from", "f", "call-id", "i", "cseq"):
                lines.append(line)
            elif name in ("to", "t"):
                value = line.split(":", 1)[1].strip()
                lines.append(f"To: {_with_tag(value, local_tag) if local_tag else value}")
            elif record_route and name == "record-route":
                lines.append(line)
        lines.extend(extra)
        lines.append("User-Agent: hotline-ios inbound")
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def _reply(self, request: str, status: str, **kw: Any) -> None:
        with contextlib.suppress(OSError):
            self._send(self._response(request, status, **kw))

    def _in_dialog(self, method: str, call: InboundCall) -> str:
        self._local_cseq += 1
        return "\r\n".join([
            f"{method} {call.target} SIP/2.0",
            self._via(),
            *[f"Route: {route}" for route in call.routes],
            f"From: {call.our_to}",
            f"To: {call.their_from}",
            f"Call-ID: {call.call_id}",
            f"CSeq: {self._local_cseq} {method}",
            self._contact(),
            "Max-Forwards: 70",
            "User-Agent: hotline-ios inbound",
            "Content-Length: 0",
        ]) + "\r\n\r\n"

    # ---- the loop ----------------------------------------------------------

    def _serve(self) -> None:
        self._next_ping = time.monotonic() + self.keepalive
        while not self._stop.is_set():
            now = time.monotonic()
            if self._reg_pending and now - self._reg_pending > REGISTER_TIMEOUT:
                raise ConnectionError("REGISTER went unanswered")
            # Only once this server has shown it answers pings (linphone.org
            # does, measured 2026-10-31): a server that never pongs would
            # otherwise be reconnected to forever. Catches the half-open TCP
            # flow -- a NAT that forgot us -- minutes before TCP keepalive would.
            if self._pongs and now - self._last_rx > 2 * self.keepalive + 10:
                raise ConnectionError("keepalives went unanswered")
            if not self._reg_pending and self._next_register and now >= self._next_register:
                self._register()
            if now >= self._next_ping:
                self._sock.sendall(b"\r\n\r\n")  # type: ignore[union-attr]
                self._next_ping = now + self.keepalive
            self._tick_call(now)
            message = self._read(0.2)
            if message:
                self._handle(message)

    def _handle(self, message: str) -> str:
        first = message.split("\r\n", 1)[0]
        if first.startswith("SIP/2.0"):
            return self._handle_response(message)
        method = first.split(" ", 1)[0].upper()
        call = self.call if self.call is not None and self.call.matches(message) else None
        if method == "INVITE":
            if call is None:
                self._new_invite(message)
            else:
                self._in_dialog_invite(message, call)
        elif method == "ACK":
            if call is not None and not call.acked.is_set():
                call.acked.set()
                log.info("inbound: ACK after %.2fs", time.monotonic() - call.answered_at)
        elif method == "CANCEL":
            self._cancel(message, call)
        elif method == "BYE":
            if call is None:
                self._reply(message, "481 Call/Transaction Does Not Exist")
            else:
                self._reply(message, "200 OK")
                log.info("inbound: he hung up (BYE) after %.1fs",
                         time.monotonic() - (call.answered_at or time.monotonic()))
                self._end(call, "he hung up", bye=False)
        elif method in ("OPTIONS", "INFO", "NOTIFY"):
            self._reply(message, "200 OK", extra=(
                "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE, INFO, NOTIFY",))
        elif method == "UPDATE":
            if call is None:
                self._reply(message, "481 Call/Transaction Does Not Exist")
            else:
                has_body = "\r\n\r\n" in message and message.split("\r\n\r\n", 1)[1].strip()
                self._reply(message, "200 OK", extra=(self._contact(),),
                            body=call.answer_sdp if has_body else "")
        else:
            self._reply(message, "501 Not Implemented")
        return method

    def _handle_response(self, message: str) -> str:
        code = status_of(message)
        cseq = header_of(message, "CSeq")
        method = cseq.split()[-1].upper() if cseq else ""
        if method != "REGISTER":
            if method == "BYE":
                log.info("inbound: our BYE answered %d", code)
            return "response"
        if code < 200:
            return "response"
        self._reg_pending = 0.0
        if code in (401, 407) and not self._reg_authed:
            self._reg_authed = True
            challenge = header_of(message, "WWW-Authenticate") or header_of(message, "Proxy-Authenticate")
            self._register(self._authorisation(challenge, "REGISTER", f"sip:{self.domain}",
                                               proxy=code == 407))
            return "challenged"
        self._reg_authed = False
        if code == 423:
            minimum = header_of(message, "Min-Expires")
            if minimum.isdigit():
                self.expires = int(minimum)
            self._register()
            return "challenged"
        if code != 200:
            self.last_error = f"REGISTER answered {message.split(chr(13), 1)[0]}"
            raise ConnectionError(self.last_error)
        granted = self._granted(message)
        self.registered_until = time.monotonic() + granted
        # Half the granted time, never under 20 s: a server that grants 30 s
        # would otherwise have this re-registering in a tight loop.
        self._next_register = time.monotonic() + max(20.0, granted / 2)
        first = not self.registered.is_set()
        self.registered.set()
        self._attempt = 0
        self.counts["registrations"] += 1
        self.last_error = ""
        (log.info if first else log.debug)(
            "inbound: registered %s@%s for %ds (instance %s); flow %s",
            self.user, self.domain, granted, self.instance[:8],
            header_of(message, "Flow-Timer") or "no Flow-Timer")
        return "registered"

    def _granted(self, message: str) -> int:
        """Our binding's lifetime from a 200 to REGISTER, matched by instance."""
        for value in headers_of(message, "Contact"):
            for contact in value.split(","):
                if self.instance in contact or self._local[0] in contact:
                    match = re.search(r"expires=(\d+)", contact)
                    if match:
                        return int(match.group(1))
        expires = header_of(message, "Expires")
        return int(expires) if expires.isdigit() else self.expires

    # ---- calls -------------------------------------------------------------

    def _new_invite(self, invite: str) -> None:
        self._reply(invite, "100 Trying")
        local_tag = _tag()
        call = InboundCall(invite, local_tag)
        if not _same_account(call.caller, self.peer):
            self.counts["rejected"] += 1
            log.warning("inbound: refused a call from %s (only %s may call)", call.caller,
                        uri_of(self.peer) if "<" in self.peer else self.peer)
            self._reply(invite, "403 Forbidden", local_tag=local_tag)
            return
        if self.call is not None:
            self.counts["busy"] += 1
            log.warning("inbound: a second call from %s while one is up: 486", call.caller)
            self._reply(invite, "486 Busy Here", local_tag=local_tag)
            return

        call.key, call.salt = srtp.new_key_salt()
        media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        media.bind(("0.0.0.0", 0))
        try:
            call.answer_sdp, call.agreed_offer = answer_offer(
                invite, self.media_host or self._local[0], media.getsockname()[1],
                call.key, call.salt)
        except OfferError as exc:
            media.close()
            log.error("inbound: cannot carry his offer: %s", exc)
            self._reply(invite, "488 Not Acceptable Here", local_tag=local_tag,
                        extra=(f'Warning: 399 hotline "{exc}"',))
            return
        call.media = media

        handler = None
        try:
            handler = self.on_call(call) if self.on_call is not None else None
        except Busy as exc:
            media.close()
            self.counts["busy"] += 1
            log.warning("inbound: busy (%s): 486", exc)
            self._reply(invite, "486 Busy Here", local_tag=local_tag)
            return
        except Exception:
            log.exception("inbound: building the call handler raised")
        if handler is None:
            media.close()
            log.error("inbound: nothing can take the call: 480")
            self._reply(invite, "480 Temporarily Unavailable", local_tag=local_tag)
            return
        call.handler = handler
        call.local_tag = local_tag  # type: ignore[attr-defined]
        self.call = call
        self._reply(invite, "180 Ringing", local_tag=local_tag,
                    extra=(self._contact(),), record_route=True)
        call.answer_at = time.monotonic() + self.answer_after
        log.info("inbound: %s is calling; answering in %.1fs", call.caller, self.answer_after)

    def _in_dialog_invite(self, message: str, call: InboundCall) -> None:
        cseq = header_of(message, "CSeq").split()[0]
        if cseq == call.cseq:
            if _branch(message) != _branch(call.invite):
                # The same call reaching us down a second path -- two bindings
                # that both lead here. RFC 3261 8.2.2.2: a merged request.
                self._reply(message, "482 Loop Detected", local_tag=_tag())
                return
            # A retransmission of the INVITE we are already handling. Over TLS
            # it should not happen; if it does, repeat where we are.
            if call.state == "answered" and call.ok:
                with contextlib.suppress(OSError):
                    self._send(call.ok)
            return
        # A re-INVITE (session refresh, ICE completion). Same media, same answer.
        self._reply(message, "200 OK", local_tag=_tag_of(call.our_to),
                    extra=(self._contact(),), body=call.answer_sdp, record_route=True)

    def _cancel(self, message: str, call: InboundCall | None) -> None:
        if call is None:
            self._reply(message, "481 Call/Transaction Does Not Exist")
            return
        self._reply(message, "200 OK")
        if call.state != "ringing":
            return  # too late; the call is up and only a BYE ends it
        self.counts["cancelled"] += 1
        log.info("inbound: he hung up before we answered (CANCEL)")
        self._reply(call.invite, "487 Request Terminated", local_tag=_tag_of(call.our_to))
        self._end(call, "cancelled before answer", bye=False)

    def _answer(self, call: InboundCall) -> None:
        call.ok = self._response(
            call.invite, "200 OK", local_tag=_tag_of(call.our_to), record_route=True,
            extra=(self._contact(),
                   "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE, INFO, NOTIFY",
                   "Supported: outbound"),
            body=call.answer_sdp)
        self._send(call.ok)
        call.state = "answered"
        call.answered_at = time.monotonic()
        call.next_resend = call.answered_at + T1
        self.counts["answered"] += 1
        log.info("inbound: answered %s", call.caller)
        threading.Thread(target=self._converse, args=(call,), name="inbound-call",
                         daemon=True).start()

    def _converse(self, call: InboundCall) -> None:
        try:
            call.handler(call.agreed_offer, call.media, call.key, call.salt)
        except Exception:
            log.exception("inbound: the call handler raised; hanging up")
        finally:
            call.handler_done.set()

    def _tick_call(self, now: float) -> None:
        for gone in [c for c in self._draining if c.handler_done.is_set()]:
            self._draining.remove(gone)
            self._close_media(gone)
            log.info("inbound: call wound down (%s)", gone.ended)
        call = self.call
        if call is None:
            return
        if call.state == "ringing" and now >= call.answer_at:
            self._answer(call)
            return
        if call.state != "answered":
            return
        if not call.acked.is_set() and not call.over.is_set():
            if now - call.answered_at > ACK_GIVE_UP:
                log.warning("inbound: no ACK for %.0fs; ending the call", ACK_GIVE_UP)
                self._end(call, "never acknowledged", bye=True)
                return
            if now >= call.next_resend:
                with contextlib.suppress(OSError):
                    self._send(call.ok)
                call.resend_gap = min(call.resend_gap * 2, T2)
                call.next_resend = now + call.resend_gap
        if call.handler_done.is_set():
            self._end(call, "we hung up", bye=not call.over.is_set())

    def _end(self, call: InboundCall, why: str, *, bye: bool) -> None:
        if bye and not call.bye_sent and call.state == "answered":
            call.bye_sent = True
            with contextlib.suppress(OSError):
                self._send(self._in_dialog("BYE", call))
        call.over.set()
        if not call.ended:
            call.ended = why
        if call.state == "ringing":
            # Never answered, so the handler never ran: tell it to let go of
            # whatever it set up for a call that is not happening.
            abandon = getattr(call.handler, "abandon", None)
            if abandon is not None:
                with contextlib.suppress(Exception):
                    abandon(why)
            if call.media is not None:
                with contextlib.suppress(OSError):
                    call.media.close()
            call.handler_done.set()
        call.state = "over"
        if self.call is call:
            self.call = None
            log.info("inbound: call over (%s)", call.ended)
        if call.handler_done.is_set():
            self._close_media(call)
        else:
            # The conversation only notices `far_end_hung_up` between turns,
            # which can be most of `TURN_SECONDS` away. The line is free now --
            # a redial must not be told 486 for half a minute -- but the media
            # socket stays open until the pump on it has stopped: closed under
            # a running pump, every read fails at once and the thread spins.
            # Models are refcounted by the daemon, so a short overlap is safe.
            self._draining.append(call)

    def _close_media(self, call: InboundCall) -> None:
        if call.media is not None:
            with contextlib.suppress(OSError):
                call.media.close()
            call.media = None
