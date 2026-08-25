"""A SIP registrar that exists to answer one question with a socket.

**The question.** Outcome C depends on the stock Linphone iOS app, registered to
a SIP domain that is *not* linphone.org, still putting RFC 8599 push parameters
in its REGISTER `Contact` header -- `pn-provider`, `pn-prid`, `pn-param`. If it
does, we can hand those to Belledonne's push relay and his locked phone rings.
If it does not, C-TAILNET is dead and he needs to know before he picks it.

Everyone so far has argued this from documentation, and the documentation
disagrees with itself: Linphone's FAQ says "third-party SIP accounts do not
receive push notifications", while a source-grep of `liblinphone` finds the push
params gated on two booleans and no domain check anywhere. **The header is
directly observable.** Twenty minutes with a socket beats any amount of reading,
and it also yields the live `pn_prid` needed to test the push itself.

**So this deliberately does almost nothing.** It accepts a REGISTER, writes the
datagram to disk *verbatim*, and says 200 OK. It is not the real SIP server; it
is an instrument. In particular it does not authenticate, because an
unauthenticated registrar on a private tailnet for one experiment is the right
amount of machinery, and anything more would be a second thing to debug when the
measurement fails.

**Absence is a result.** If `pn-prid` is not in the header, that is the finding,
which is why the raw bytes go to the log rather than a parsed summary. A parser
that finds nothing and a phone that sent nothing look identical in a summary and
completely different in a capture.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("hotline-ios.sipprobe")

PUSH_PARAMS = ("pn-provider", "pn-prid", "pn-param", "pn-tok", "pn-type", "app-id")
"""Both the RFC 8599 names and Linphone's older proprietary ones. Looking for
only the modern set would turn "the app used the legacy spelling" into "the app
sent nothing", which is exactly the wrong conclusion to reach by accident."""


@dataclass
class Registration:
    user: str
    contact: str
    push: dict[str, str] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    source: str = ""

    @property
    def pushable(self) -> bool:
        """Enough to ask Belledonne's relay to ring this device."""
        return bool(self.push.get("pn-prid") or self.push.get("pn-tok"))


def parse_push_params(contact: str) -> dict[str, str]:
    """Pull push parameters out of a Contact header.

    They can arrive as URI parameters (`sip:x@y;pn-prid=abc`) or, per RFC 8599,
    inside the URI. Both spellings are matched rather than assuming one.
    """
    found: dict[str, str] = {}
    for name in PUSH_PARAMS:
        match = re.search(rf"[;?&]{re.escape(name)}=([^;>&\s]+)", contact, re.IGNORECASE)
        if match:
            found[name.lower()] = match.group(1)
    return found


def header(message: str, name: str) -> str:
    for line in message.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _response(request: str, status: str, extra: list[str] | None = None) -> bytes:
    """A minimally correct SIP response: echo the dialog-identifying headers.

    Via, From, To, Call-ID and CSeq must come back or the client discards the
    response and retries forever, which looks exactly like the server being
    down.
    """
    lines = [f"SIP/2.0 {status}"]
    for name in ("Via", "From", "To", "Call-ID", "CSeq"):
        value = header(request, name)
        if not value:
            continue
        if name == "To" and ";tag=" not in value:
            # A registrar must add a tag to the To header of a 200. Without it
            # some clients treat the registration as never confirmed.
            value += ";tag=hotlineprobe"
        lines.append(f"{name}: {value}")
    lines += extra or []
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class SipProbe(asyncio.DatagramProtocol):
    def __init__(self, capture: Path, expires: int = 600) -> None:
        self.capture = capture
        self.expires = expires
        self.registrations: dict[str, Registration] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.packets = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets += 1
        source = f"{addr[0]}:{addr[1]}"
        # Verbatim, before any parsing. If the parse is wrong this is the only
        # thing that can prove it.
        with self.capture.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n===== {time.strftime('%H:%M:%S')} from {source} =====\n")
            handle.write(data.decode("utf-8", errors="replace"))

        try:
            message = data.decode("utf-8", errors="replace")
            method = message.split(" ", 1)[0].upper()
        except Exception:
            return

        if method == "REGISTER":
            self._on_register(message, source)
            self._send(_response(message, "200 OK", [f"Expires: {self.expires}"]), addr)
        elif method == "OPTIONS":
            self._send(_response(message, "200 OK"), addr)
        elif method in ("INVITE", "ACK", "BYE", "CANCEL", "SUBSCRIBE", "PUBLISH", "INFO"):
            # Politely decline anything that is not the measurement. A 200 to an
            # INVITE would commit us to an RTP session this instrument cannot
            # hold up, and the client would then tear it down and log an error
            # that looks like our bug.
            if method != "ACK":
                self._send(_response(message, "405 Method Not Allowed"), addr)
        else:
            self._send(_response(message, "200 OK"), addr)

    def _send(self, payload: bytes, addr: tuple[str, int]) -> None:
        if self.transport is not None:
            self.transport.sendto(payload, addr)

    def _on_register(self, message: str, source: str) -> None:
        contact = header(message, "Contact")
        user = header(message, "From")
        push = parse_push_params(contact)
        record = Registration(user=user, contact=contact, push=push, source=source)
        key = re.sub(r"[^\w@.-]", "", user)[:120] or source
        self.registrations[key] = record

        log.info("REGISTER from %s", source)
        log.info("  From:    %s", user)
        log.info("  Contact: %s", contact)
        if push:
            log.info("  PUSH PARAMS FOUND: %s", push)
            if record.pushable:
                log.info(
                    "  >>> THIS IS THE ANSWER: the app DOES emit a push token for a "
                    "third-party domain. Outcome C-TAILNET is viable."
                )
        else:
            log.warning(
                "  >>> NO PUSH PARAMS IN THIS REGISTER. If this persists across a "
                "re-register with the app backgrounded, C-TAILNET is dead. Check the "
                "raw capture before concluding -- absence in the parse is not the "
                "same as absence on the wire."
            )


async def serve(host: str, port: int, capture: Path, expires: int = 600) -> SipProbe:
    loop = asyncio.get_running_loop()
    probe = SipProbe(capture, expires)
    await loop.create_datagram_endpoint(
        lambda: probe,
        local_addr=(host, port),
        family=socket.AF_INET,
    )
    log.info("SIP probe listening on %s:%d (UDP), capturing to %s", host, port, capture)
    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hotline-sipprobe",
        description="Log what a SIP client puts in its REGISTER Contact header.",
    )
    # Tailscale-only by default. A SIP port answering on the LAN is not
    # something to leave open casually, even for an experiment.
    parser.add_argument("--host", default="100.72.2.62", help="bind address (default: tailnet)")
    parser.add_argument("--port", type=int, default=5060)
    parser.add_argument(
        "--capture",
        default="/home/bodas/data/hotline-ios/sip-capture.txt",
        help="verbatim datagram log",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    async def run() -> None:
        await serve(args.host, args.port, Path(args.capture))
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
