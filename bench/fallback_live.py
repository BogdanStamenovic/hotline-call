"""Call the fallback on pigion through linphone.org, from hotline's own account.

The live half of `tests/test_fallback.py`. Run on archserver while pigion runs
`hotline-call-fallback serve --test`:

    PYTHONPATH=src .venv/bin/python bench/fallback_live.py [--wav OUT.wav]

It REGISTERs and INVITEs hotline's own account (SIP_USER) with the same
`SipTransport` that rings him. linphone.org forks the INVITE to every binding
of that account: production calld refuses this caller with 403, and only the
test-mode fallback answers. Then it counts what arrives: SRTP packets that
decrypt with the key pigion's answer advertised, any that fail authentication,
the codec, and whether pigion ended the call with a BYE. Opus is decoded and the
duration and level reported; `--wav` keeps the decoded audio -- his cloned
voice, so put it somewhere gitignored, never in the repo.

Afterwards it drops its own REGISTER binding (Expires: 0 on that exact
Contact), so the test leaves nothing behind on the account.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import socket
import struct
import sys
import time
import uuid
import wave

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / "src")]

from hotline_call import fallback
from hotline_call.media import codec, srtp
from hotline_call.ring.base import CallTarget
from hotline_call.ring.sip import SipTransport, header_of, status_of

log = logging.getLogger("fallback-live")


class SelfCall(SipTransport):
    """SipTransport with an unregister after the call, on the same connection."""

    def _finish_answered(self, sock, call_id, from_tag, to_header, cseq, reply=""):
        try:
            super()._finish_answered(sock, call_id, from_tag, to_header, cseq, reply=reply)
        finally:
            self._unregister(sock)

    def _unregister(self, sock) -> None:
        reg_id = uuid.uuid4().hex
        message = self._register(reg_id, 1).replace("Expires: 300", "Expires: 0")
        self._send(sock, message)
        for _ in range(4):
            reply = self._recv(sock, 5)
            if not reply:
                break
            if not reply.startswith("SIP/2.0") or "REGISTER" not in header_of(reply, "CSeq"):
                continue
            code = status_of(reply)
            if code in (401, 407):
                auth = self._authorisation(header_of(reply, "WWW-Authenticate")
                                           or header_of(reply, "Proxy-Authenticate"),
                                           "REGISTER", f"sip:{self.domain}", proxy=code == 407)
                self._send(sock, self._register(reg_id, 2, auth).replace("Expires: 300",
                                                                         "Expires: 0"))
                continue
            log.info("unregistered the test client's binding: %d", code)
            RESULT["unregister"] = code
            return
        RESULT["unregister"] = "no answer"


RESULT: dict = {}


def on_answer(transport: SelfCall, wav_path: str):
    def handler(reply: str, media: socket.socket, key: bytes, salt: bytes) -> None:
        body = reply.split("\r\n\r\n", 1)[1]
        host, port, pt, crypto = fallback.parse_agreed(reply)
        _tag, their_key, their_salt = srtp.parse_crypto_line(crypto)
        rx = srtp.SrtpSession(their_key, their_salt)
        tx = srtp.SrtpSession(key, salt)
        choice = codec.negotiate(body)
        RESULT.update(answer_media=f"{host}:{port}", pt=pt,
                      codec=choice.name if choice else None,
                      user_agent=header_of(reply, "User-Agent"))
        log.info("answered by %s, media %s:%d, %s", RESULT["user_agent"], host, port,
                 RESULT["codec"])
        media.settimeout(0.05)
        ok = failed = foreign = 0
        payloads: list[bytes] = []
        sources = set()
        first = last = 0.0
        sent = 0
        seq, ts, ssrc = 1, 0, 0x1234
        deadline = time.monotonic() + 30
        bye = False
        while time.monotonic() < deadline:
            if sent < 10:   # a little audio of ours, so its rx/auth counters run too
                sent += 1
                silence = b"\xff" * 160 if (choice and choice.name == "PCMU") else b"\xf8\xff\xfe"
                media.sendto(tx.protect(struct.pack("!BBHII", 0x80, pt, seq, ts, ssrc) + silence),
                             (host, port))
                seq += 1
                ts += 960 if choice and choice.name == "opus" else 160
            try:
                packet, source = media.recvfrom(4096)
            except TimeoutError:
                packet = b""
            if packet:
                sources.add(f"{source[0]}:{source[1]}")
                try:
                    plain = rx.unprotect(packet)
                except srtp.SrtpError:
                    failed += 1
                else:
                    if plain[1] & 0x7F != pt:
                        foreign += 1
                    else:
                        ok += 1
                        payloads.append(plain[srtp.payload_offset(plain):])
                        now = time.monotonic()
                        first = first or now
                        last = now
            if transport.far_end_hung_up():
                bye = transport._dialog_over
                break
        RESULT.update(frames_ok=ok, auth_failures=failed, foreign=foreign,
                      media_from=sorted(sources), far_end_bye=bye,
                      span_s=round(last - first, 2) if first else 0)
        if choice and payloads:
            dec = codec.open_codec(choice)
            pcm16 = b"".join(dec.decode(p) for p in payloads)
            samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16)
            rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples))) / 32768
            RESULT.update(decoded_s=round(len(samples) / dec.rate, 2), rms=round(rms, 4),
                          decode_errors=dec.errors)
            if wav_path:
                with wave.open(wav_path, "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(dec.rate)
                    out.writeframes(pcm16)
    return handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", default="", help="keep the decoded audio here (NOT the repo)")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    fallback.load_env_file(fallback.config_dir() / "env")
    me = f"{os.environ['SIP_USER']}@{os.environ.get('SIP_DOMAIN', 'sip.linphone.org')}"
    transport = SelfCall(peer=me)
    transport.on_answer = on_answer(transport, args.wav)
    import asyncio

    started = time.monotonic()
    try:
        asyncio.run(transport.ring(CallTarget(device="fallback-test", address=me), timeout=args.timeout))
        RESULT["ring"] = "answered"
    except Exception as exc:
        RESULT["ring"] = f"{type(exc).__name__}: {exc}"
    RESULT["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(RESULT, indent=2))
    return 0 if RESULT.get("frames_ok") and not RESULT.get("auth_failures") else 1


if __name__ == "__main__":
    raise SystemExit(main())
