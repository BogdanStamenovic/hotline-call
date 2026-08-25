"""The second doorbell: ring him through his own Linphone account.

He asked for **both** — "Okay we will do both" — because Telegram and Linphone
fail for unrelated reasons. This is the Linphone half.

## Why this is small enough to be written rather than installed

Because **it never carries audio.** The whole job is:

    REGISTER to sip.linphone.org  ->  INVITE his account  ->  see 180 Ringing
                                                          ->  CANCEL

His phone rings on the INVITE, linphone.org's own push infrastructure wakes the
app, and he hangs up on it and opens ours. There is no SDP negotiation worth the
name, no RTP, no codec, no jitter buffer, no media at all. What is left is a few
hundred lines of a text protocol and one MD5 digest — which is why this does not
need `baresip`, and therefore does not need a system package he has not approved.

## Why the evidence here is better than Telegram's

`ConfirmedRing` needs proof the phone is alerting. Telegram gives us "the server
accepted the request", which is an inference. **SIP gives us `180 Ringing`,
which is the far end saying, literally, that it is ringing.** That is the
strongest confirmation any transport in this project has.

`183 Session Progress` is accepted too — some proxies send it in place of 180 —
and `200 OK` obviously counts, though nobody is meant to answer.

## What is not built, deliberately

- **No media.** See above. If this ever needs to carry audio, the RTP and G.711
  work is in `parked/` and was written against a measured 172 ms jitter path.
- **TLS is the default, and my first reason for that was wrong.** One UDP
  INVITE to `sip.linphone.org` got no response of any kind — no 100, no 407,
  nothing — while REGISTER over the same socket worked. I wrote that up as
  "linphone.org ignores INVITEs over UDP". **A later run over UDP rang his
  phone**, which refutes it.

  The likelier explanation is duller and is a defect here rather than there:
  **SIP over UDP requires the client to retransmit an INVITE** (RFC 3261's
  timer A, 500 ms doubling), and this does not. One lost datagram is therefore
  indistinguishable from a server that never answers — which is exactly what
  was observed and exactly what was mis-diagnosed.

  So TLS is the default for a correct reason instead of an invented one: it
  runs over TCP, which retransmits for us. UDP is left available and is known
  to work; it is simply less reliable until timer A exists here.

- **His client requires encrypted media.** A plain `RTP/AVP` offer is answered
  `488 Not acceptable here`, and — this is the part that made it hard to read —
  the push has already fired by then, so his phone lights up and the call dies
  about a second later. He described it as "a call for a split second", which is
  exactly what a 488 after a `110 Push sent` looks like from the outside.
  Offering `RTP/SAVP` with an SDES crypto line makes it ring properly.

- **UDP reachability was verified without an account.** An
  unauthenticated REGISTER to `sip.linphone.org` (176.31.149.179) on UDP 5060
  from archserver comes back as:

      SIP/2.0 401
      realm=sip.linphone.org  algorithm=MD5  qop=auth
      nonce=...  opaque=...

  So the host is reachable, it speaks UDP, and `parse_challenge` handles their
  actual challenge — including `opaque`, which they send and many servers do
  not, and which has to be echoed back or the second REGISTER is rejected.
  What is still unverified is everything *after* authentication succeeds, which
  needs an account.

- **TLS is implemented and unverified.** UDP is the default precisely because it
  is the one that could be tested without credentials, and it worked.
- **No re-registration timer.** A ring registers immediately before inviting,
  which costs one round trip and removes an entire class of "the registration
  silently expired three hours ago" failure. Given a ring happens rarely, that
  is the right trade.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import random
import re
import secrets
import socket
import ssl
import string
import time
import uuid

from .base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable

log = logging.getLogger("hotline-ios.ring.sip")

DEFAULT_REALM = "sip.linphone.org"
RING_CODES = (180, 183)
"""What counts as 'his phone is ringing'. 183 because some proxies send session
progress instead of 180, and treating that as silence would report a working
doorbell as broken."""


def _tag(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _digest(
    user: str, password: str, realm: str, nonce: str, method: str, uri: str,
    *, qop: str = "", nc: str = "00000001", cnonce: str = "", algorithm: str = "MD5",
) -> str:
    """RFC 2617 digest. MD5 here is the protocol's choice, not ours."""
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    if algorithm.upper() == "MD5-SESS":
        ha1 = hashlib.md5(f"{ha1}:{nonce}:{cnonce}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        raw = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
    else:
        raw = f"{ha1}:{nonce}:{ha2}"
    return hashlib.md5(raw.encode()).hexdigest()


def parse_challenge(header: str) -> dict[str, str]:
    """Pull the parameters out of a WWW-Authenticate / Proxy-Authenticate line."""
    found: dict[str, str] = {}
    for key, quoted, bare in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', header):
        found[key.lower()] = quoted or bare
    return found


def status_of(message: str) -> int:
    match = re.match(r"SIP/2\.0\s+(\d{3})", message)
    return int(match.group(1)) if match else 0


def header_of(message: str, name: str) -> str:
    for line in message.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


class SipTransport:
    """Ring him by placing a SIP call to his Linphone account."""

    name = "sip"
    rings_when_closed = True
    """True, and it is the reason this is worth having alongside Telegram:
    linphone.org's own push gateway wakes the app. Nothing of ours stays alive."""

    def __init__(
        self,
        *,
        user: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        peer: str | None = None,
        transport: str | None = None,
        port: int | None = None,
    ) -> None:
        self.user = user or os.environ.get("SIP_USER", "")
        self.password = password or os.environ.get("SIP_PASSWORD", "")
        self.domain = domain or os.environ.get("SIP_DOMAIN", DEFAULT_REALM)
        # His Linphone address -- the one he creates and sends over. Not ours.
        self.peer = peer or os.environ.get("SIP_PEER", "")
        # TLS by default: UDP cannot place a call against linphone.org at all,
        # which was established by watching an INVITE get no response whatsoever
        # while REGISTER over the same socket succeeded.
        self.transport = (transport or os.environ.get("SIP_TRANSPORT", "tls")).lower()
        default_port = "5061" if self.transport == "tls" else "5060"
        self.port = port or int(os.environ.get("SIP_PORT", default_port))
        # Bound lazily per call: the offer has to name a port that exists, and
        # port 9 (discard) is one of the things a client rejects with 488.
        self._media: socket.socket | None = None
        self._crypto_key = ""
        self.ringing = asyncio.Event()
        self._sock: socket.socket | None = None
        self._local: tuple[str, int] = ("0.0.0.0", 0)

    async def start(self) -> None:
        if not (self.user and self.password and self.peer):
            # At startup, not at ring time. A doorbell that only reveals it was
            # never configured when someone needs it is worse than one that says
            # so immediately.
            raise CallUnreachable(
                "sip is not configured: needs SIP_USER, SIP_PASSWORD and SIP_PEER"
            )

    async def stop(self) -> None:
        self._close()

    # ---- the wire --------------------------------------------------------

    def _connect(self) -> socket.socket:
        if self.transport == "tls":
            raw = socket.create_connection((self.domain, self.port or 5061), timeout=10)
            context = ssl.create_default_context()
            sock: socket.socket = context.wrap_socket(raw, server_hostname=self.domain)
        elif self.transport == "tcp":
            sock = socket.create_connection((self.domain, self.port), timeout=10)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(10)
            sock.connect((self.domain, self.port))
        self._local = sock.getsockname()[:2]
        self._sock = sock
        return sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, sock: socket.socket, message: str) -> None:
        sock.sendall(message.encode())

    def _recv(self, sock: socket.socket, timeout: float) -> str:
        sock.settimeout(timeout)
        try:
            return sock.recv(65535).decode("utf-8", errors="replace")
        except TimeoutError:
            return ""

    # ---- messages --------------------------------------------------------

    def _via(self, branch: str) -> str:
        host, port = self._local
        proto = "TLS" if self.transport == "tls" else self.transport.upper()
        return f"Via: SIP/2.0/{proto} {host}:{port};branch=z9hG4bK{branch};rport"

    def _register(self, call_id: str, cseq: int, auth: str = "") -> str:
        uri = f"sip:{self.domain}"
        me = f"sip:{self.user}@{self.domain}"
        host, port = self._local
        lines = [
            f"REGISTER {uri} SIP/2.0",
            self._via(_tag()),
            f"From: <{me}>;tag={_tag()}",
            f"To: <{me}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: <sip:{self.user}@{host}:{port};transport={self.transport}>",
            "Max-Forwards: 70",
            "Expires: 300",
            "User-Agent: hotline-ios",
        ]
        if auth:
            lines.append(auth)
        lines.append("Content-Length: 0")
        return "\r\n".join(lines) + "\r\n\r\n"

    def _open_media(self) -> int:
        """Bind a real UDP port to advertise, and return it.

        Nothing is ever sent on it. But the offer has to name a port the far end
        finds plausible -- port 9, the discard port, is one of the things that
        earns a 488 -- and binding one costs nothing.
        """
        if self._media is None:
            self._media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._media.bind(("0.0.0.0", 0))
        return int(self._media.getsockname()[1])

    def _close_media(self) -> None:
        if self._media is not None:
            with contextlib.suppress(OSError):
                self._media.close()
            self._media = None

    def _invite(self, call_id: str, cseq: int, from_tag: str, auth: str = "") -> str:
        me = f"sip:{self.user}@{self.domain}"
        them = self.peer if self.peer.startswith("sip:") else f"sip:{self.peer}"
        host, port = self._local
        media_port = self._open_media()
        if not self._crypto_key:
            self._crypto_key = base64.b64encode(secrets.token_bytes(30)).decode()
        # RTP/SAVP with an SDES key. His client refuses plain RTP/AVP with 488,
        # and because the push has already fired by then the phone lights up and
        # dies a second later -- which looks like a notification bug rather than
        # a negotiation failure. The key is real but never used: no media is sent.
        sdp = (
            "v=0\r\n"
            f"o=- {random.randint(1, 2**31)} 1 IN IP4 {host}\r\n"
            "s=hotline\r\n"
            f"c=IN IP4 {host}\r\n"
            "t=0 0\r\n"
            f"m=audio {media_port} RTP/SAVP 0 8 101\r\n"
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{self._crypto_key}\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=sendrecv\r\n"
        )
        lines = [
            f"INVITE {them} SIP/2.0",
            self._via(_tag()),
            f"From: <{me}>;tag={from_tag}",
            f"To: <{them}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} INVITE",
            f"Contact: <sip:{self.user}@{host}:{port};transport={self.transport}>",
            "Max-Forwards: 70",
            "User-Agent: hotline-ios",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
            "Content-Type: application/sdp",
        ]
        if auth:
            lines.append(auth)
        lines.append(f"Content-Length: {len(sdp)}")
        return "\r\n".join(lines) + "\r\n\r\n" + sdp

    def _authorisation(self, challenge: str, method: str, uri: str, *, proxy: bool) -> str:
        params = parse_challenge(challenge)
        realm = params.get("realm", self.domain)
        nonce = params.get("nonce", "")
        qop = params.get("qop", "").split(",")[0].strip()
        cnonce = _tag(16)
        response = _digest(
            self.user, self.password, realm, nonce, method, uri,
            qop=qop, cnonce=cnonce, algorithm=params.get("algorithm", "MD5"),
        )
        parts = [
            f'Digest username="{self.user}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
        ]
        if params.get("opaque"):
            parts.append(f'opaque="{params["opaque"]}"')
        if qop:
            parts += [f"qop={qop}", "nc=00000001", f'cnonce="{cnonce}"']
        if params.get("algorithm"):
            parts.append(f"algorithm={params['algorithm']}")
        name = "Proxy-Authorization" if proxy else "Authorization"
        return f"{name}: " + ", ".join(parts)

    # ---- ringing ---------------------------------------------------------

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> None:
        self.ringing.clear()
        if not (self.user and self.password and self.peer):
            raise CallUnreachable("sip is not configured")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._ring_blocking, timeout)
        finally:
            self._close()
            self._close_media()

    def _ring_blocking(self, timeout: float) -> None:
        """The whole exchange, synchronously, off the event loop.

        Written blocking and pushed to an executor rather than as an asyncio
        protocol: it is a short strictly-ordered request/response conversation,
        and expressing that as a state machine would be more code for no gain.
        """
        try:
            sock = self._connect()
        except OSError as exc:
            raise CallUnreachable(f"cannot reach {self.domain}:{self.port}: {exc}") from exc

        call_id = uuid.uuid4().hex
        self._authenticate_register(sock, call_id)
        self._invite_and_watch(sock, call_id, timeout)

    def _authenticate_register(self, sock: socket.socket, call_id: str) -> None:
        self._send(sock, self._register(call_id, 1))
        reply = self._recv(sock, 10)
        if not reply:
            raise CallUnreachable(f"{self.domain} did not answer a REGISTER")
        code = status_of(reply)
        if code in (401, 407):
            challenge = header_of(reply, "WWW-Authenticate") or header_of(
                reply, "Proxy-Authenticate"
            )
            auth = self._authorisation(
                challenge, "REGISTER", f"sip:{self.domain}", proxy=(code == 407)
            )
            self._send(sock, self._register(call_id, 2, auth))
            reply = self._recv(sock, 10)
            code = status_of(reply)
        if code == 403:
            raise CallUnreachable("sip credentials rejected (403) -- check user and password")
        if code != 200:
            raise CallUnreachable(f"REGISTER failed with {code or 'no response'}")

    def _invite_and_watch(self, sock: socket.socket, call_id: str, timeout: float) -> None:
        from_tag = _tag()
        invite_id = uuid.uuid4().hex
        them = self.peer if self.peer.startswith("sip:") else f"sip:{self.peer}"
        self._send(sock, self._invite(invite_id, 1, from_tag))

        # A real deadline, not a counter decremented by the poll interval --
        # the first version subtracted a fixed 5s per read regardless of how
        # long the read actually took, so a 180 followed immediately by a 200
        # ended the loop before the 200 was ever read.
        deadline = time.monotonic() + timeout
        answered = False
        cseq = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reply = self._recv(sock, min(5.0, remaining))
            if not reply:
                continue
            code = status_of(reply)
            if code in (401, 407):
                cseq += 1
                auth = self._authorisation(
                    header_of(reply, "WWW-Authenticate")
                    or header_of(reply, "Proxy-Authenticate"),
                    "INVITE", them, proxy=(code == 407),
                )
                self._send(sock, self._invite(invite_id, cseq, from_tag, auth))
                continue
            if code in RING_CODES:
                # The far end saying, in the protocol's own words, that it is
                # ringing. The strongest confirmation any transport here has.
                log.info("sip: %s is ringing (%d)", self.peer, code)
                self.ringing.set()
                continue
            if code == 200:
                answered = True
                # RFC 3261: a 200 to an INVITE must be ACKed, and a call that
                # has been answered is ended with BYE, not CANCEL. This only
                # started mattering when he actually picked one up -- before
                # that the code never reached a 200 and CANCEL was always right.
                to_tag = header_of(reply, "To")
                self._finish_answered(sock, invite_id, from_tag, to_tag, cseq)
                break
            if code in (486, 600, 603):
                self._cancel(sock, invite_id, from_tag, cseq)
                raise CallDeclined(f"he declined the sip call ({code})")
            if code == 404:
                raise CallUnreachable(f"sip: {self.peer} not found (404)")
            if code >= 400:
                raise CallUnreachable(f"sip call failed with {code}")

        if not answered:
            self._cancel(sock, invite_id, from_tag, cseq)
        if not self.ringing.is_set():
            raise CallUnreachable("sip: no 180 Ringing -- nothing confirmed his phone alerted")
        if not answered:
            raise CallUnanswered(f"sip rang for {timeout:.0f}s with no answer")

    def _finish_answered(
        self, sock: socket.socket, call_id: str, from_tag: str, to_header: str, cseq: int
    ) -> None:
        """ACK the 200, then hang up.

        We never wanted the call -- the ring IS the message, and he reads the
        question in the app. But an unACKed 200 makes the far end retransmit it
        for half a minute, and a call left up keeps his phone occupied.
        """
        me = f"sip:{self.user}@{self.domain}"
        them = self.peer if self.peer.startswith("sip:") else f"sip:{self.peer}"
        to_value = to_header or f"<{them}>"
        for method, seq in (("ACK", cseq), ("BYE", cseq + 1)):
            message = "\r\n".join([
                f"{method} {them} SIP/2.0",
                self._via(_tag()),
                f"From: <{me}>;tag={from_tag}",
                f"To: {to_value}",
                f"Call-ID: {call_id}",
                f"CSeq: {seq} {method}",
                "Max-Forwards: 70",
                "Content-Length: 0",
            ]) + "\r\n\r\n"
            with contextlib.suppress(OSError):
                self._send(sock, message)

    def _cancel(self, sock: socket.socket, call_id: str, from_tag: str, cseq: int) -> None:
        """Stop it ringing. He is already reading the question in the app."""
        me = f"sip:{self.user}@{self.domain}"
        them = self.peer if self.peer.startswith("sip:") else f"sip:{self.peer}"
        message = "\r\n".join([
            f"CANCEL {them} SIP/2.0",
            self._via(_tag()),
            f"From: <{me}>;tag={from_tag}",
            f"To: <{them}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} CANCEL",
            "Max-Forwards: 70",
            "Content-Length: 0",
        ]) + "\r\n\r\n"
        try:
            self._send(sock, message)
        except OSError:
            pass
