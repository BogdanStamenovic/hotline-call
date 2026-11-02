"""Him calling hotline, against a fake linphone.org on a real TCP socket.

The proxy here plays sip.linphone.org's part: it takes our REGISTER with a
digest challenge, then hands us INVITEs, ACKs, BYEs and CANCELs down the same
connection, the way flexisip does for a client behind NAT. Media is real SRTP
over real UDP. What it cannot prove is linphone.org's own behaviour -- that was
exercised live on 2026-10-31 by calling our own account (see the commit).
"""

import base64
import queue
import re
import socket
import threading
import time

import pytest

from hotline_call.media import pcm, rtp, srtp
from hotline_call.media.voicecall import VoiceCall, parse_sdp_answer
from hotline_call.ring import inbound
from hotline_call.ring.inbound import OfferError, SipInbound, answer_offer
from hotline_call.ring.sip import header_of, status_of

NONCE = "HFE47gAAAADyW23QAAD253sZEAsAAAAA"
HIM = "sip:him@127.0.0.1"
INSTANCE = "00000000-0000-4000-8000-0000000000aa"


def offer(port: int, key: bytes, salt: bytes, *, profile="RTP/SAVP", pts="96 0 8 101",
          crypto=True, video=False) -> str:
    """A Linphone-shaped offer: Opus first, then G.711, with an SDES line."""
    sdp = (
        "v=0\r\n"
        "o=him 1 1 IN IP4 127.0.0.1\r\n"
        "s=Talk\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {port} {profile} {pts}\r\n"
        "a=rtpmap:96 opus/48000/2\r\n"
        "a=fmtp:96 useinbandfec=1\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=rtcp-mux\r\n"
    )
    if crypto:
        sdp += (
            "a=crypto:1 AES_256_CM_HMAC_SHA1_80 inline:"
            + base64.b64encode(b"x" * 46).decode() + "\r\n"
            + srtp.crypto_line(key, salt, tag=2) + "\r\n"
        )
    if video:
        sdp += "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 VP8/90000\r\n"
    return sdp


class FakeProxy(threading.Thread):
    """Enough of flexisip to register us and deliver a call down our connection."""

    daemon = True

    def __init__(self, *, expires=600):
        super().__init__()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.expires = expires
        self.conn: socket.socket | None = None
        self.connections = 0
        self.registers: list[str] = []
        self.inbox: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self._buffer = b""

    def run(self):
        while not self.stop_flag.is_set():
            if self.conn is None:
                try:
                    self.conn, _ = self.listener.accept()
                except (TimeoutError, OSError):
                    continue
                self.conn.settimeout(0.2)
                self.connections += 1
                self._buffer = b""
            try:
                chunk = self.conn.recv(65535)
            except TimeoutError:
                continue
            except OSError:
                self.conn = None
                continue
            if not chunk:
                self.conn = None
                continue
            self._buffer += chunk
            while True:
                if self._buffer.startswith(b"\r\n\r\n"):
                    self._buffer = self._buffer[4:]
                    self.conn.sendall(b"\r\n")   # RFC 5626 pong
                    continue
                message = self._take()
                if message is None:
                    break
                self._got(message)

    def _take(self):
        split = self._buffer.find(b"\r\n\r\n")
        if split < 0:
            return None
        head = self._buffer[:split].decode()
        match = re.search(r"Content-Length:\s*(\d+)", head, re.IGNORECASE)
        total = split + 4 + (int(match.group(1)) if match else 0)
        if len(self._buffer) < total:
            return None
        message = self._buffer[:total].decode()
        self._buffer = self._buffer[total:]
        return message

    def _got(self, message):
        if message.startswith("REGISTER "):
            self.registers.append(message)
            if "Authorization:" not in message:
                self.reply(message, "401 Unauthorized", [(
                    f'WWW-Authenticate: Digest realm="127.0.0.1", nonce="{NONCE}", '
                    'opaque="+GNywA==", algorithm=MD5, qop="auth"')])
            else:
                contact = header_of(message, "Contact")
                self.reply(message, "200 Registration successful",
                           [f"Contact: {contact};expires={self.expires}"])
            return
        self.inbox.put(message)

    def reply(self, request, status, extra=()):
        lines = [f"SIP/2.0 {status}"]
        for name in ("Via", "From", "To", "Call-ID", "CSeq"):
            lines.append(f"{name}: {header_of(request, name)}")
        lines += list(extra)
        lines.append("Content-Length: 0")
        self.send("\r\n".join(lines) + "\r\n\r\n")

    def send(self, message: str):
        assert self.conn is not None, "nobody is connected"
        self.conn.sendall(message.encode())

    def expect(self, predicate, within=5.0) -> str:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                message = self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if predicate(message):
                return message
        raise AssertionError("the expected message never arrived")

    def drop(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def stop(self):
        self.stop_flag.set()
        self.drop()
        self.listener.close()


def status(code):
    return lambda m: status_of(m) == code


def request(method):
    return lambda m: m.startswith(f"{method} ")


class Call:
    """One call he places through the proxy: builds the requests, tracks the dialog."""

    def __init__(self, proxy, sdp, *, caller=HIM):
        self.proxy = proxy
        self.sdp = sdp
        self.caller = caller
        self.call_id = f"call-{time.monotonic_ns()}"
        self.branch = f"z9hG4bK.{time.monotonic_ns()}"
        self.to = "<sip:hotline@127.0.0.1>"

    def _headers(self, method, cseq, branch=None):
        return [
            f"Via: SIP/2.0/TCP 127.0.0.1:5061;branch={branch or self.branch}",
            "Via: SIP/2.0/TLS 10.0.0.2:5061;branch=z9hG4bKphone;received=1.2.3.4",
            f"From: <{self.caller}>;tag=histag",
            f"To: {self.to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
        ]

    def invite(self, branch=None):
        lines = [
            "INVITE sip:hotline@127.0.0.1:5061;ob;transport=tcp SIP/2.0",
            *self._headers("INVITE", 20, branch),
            "Record-Route: <sips:sip11.example:5061;lr>",
            "Record-Route: <sip:edge.example;lr>",
            "Contact: <sip:him@10.0.0.2:5061;transport=tls>",
            "Content-Type: application/sdp",
            f"Content-Length: {len(self.sdp)}",
        ]
        self.proxy.send("\r\n".join(lines) + "\r\n\r\n" + self.sdp)

    def learn_tag(self, response):
        self.to = header_of(response, "To")

    def simple(self, method, cseq, *, to=None):
        lines = [f"{method} sip:hotline@127.0.0.1:5061;ob;transport=tcp SIP/2.0",
                 *self._headers(method, cseq), "Content-Length: 0"]
        if to is not None:
            lines = [line if not line.startswith("To:") else f"To: {to}" for line in lines]
        self.proxy.send("\r\n".join(lines) + "\r\n\r\n")

    def ack(self):
        self.simple("ACK", 20)

    def bye(self):
        self.simple("BYE", 21)

    def cancel(self):
        # Same branch, same CSeq number, To without the tag (RFC 3261 9.1).
        self.simple("CANCEL", 20, to="<sip:hotline@127.0.0.1>")


class Handler:
    """Stands in for `AnsweredCall`: records how it was called, holds the line."""

    def __init__(self, call, *, hold=10.0):
        self.call = call
        self.hold = hold
        self.args = None
        self.ran = threading.Event()
        self.abandoned = ""

    def __call__(self, reply, sock, key, salt):
        self.args = (reply, sock, key, salt)
        self.ran.set()
        deadline = time.monotonic() + self.hold
        while time.monotonic() < deadline and not self.call.far_end_hung_up():
            time.sleep(0.02)

    def abandon(self, why):
        self.abandoned = why


@pytest.fixture
def proxy():
    server = FakeProxy()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def listener(proxy):
    handlers: list[Handler] = []
    hold = {"seconds": 10.0}

    def on_call(call):
        handler = Handler(call, hold=hold["seconds"])
        handlers.append(handler)
        return handler

    s = SipInbound(user="hotline", password="pw", domain="127.0.0.1", port=proxy.port,
                   transport="tcp", peer=HIM, media_host="127.0.0.1", instance=INSTANCE,
                   answer_after=0.1, on_call=on_call)
    s.handlers = handlers   # type: ignore[attr-defined]
    s.hold = hold           # type: ignore[attr-defined]
    s.start()
    assert s.registered.wait(5), s.status()
    yield s
    s.stop()


def phone_media():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    key, salt = srtp.new_key_salt()
    return sock, key, salt


def answered(proxy, listener, *, caller=HIM, sdp=None):
    """Place a call, see it through 100/180/200 and ACK it. Returns the pieces."""
    sock, key, salt = phone_media()
    call = Call(proxy, sdp or offer(sock.getsockname()[1], key, salt), caller=caller)
    call.invite()
    proxy.expect(status(100))
    ringing = proxy.expect(status(180))
    ok = proxy.expect(status(200))
    call.learn_tag(ok)
    call.ack()
    return call, ok, ringing, sock, key, salt


# -- the SDP answer ---------------------------------------------------------


def test_the_answer_is_encrypted_with_our_key_under_his_crypto_tag():
    key, salt = srtp.new_key_salt()
    ours, our_salt = srtp.new_key_salt()
    answer, agreed = answer_offer(offer(4000, key, salt), "100.72.2.62", 5000, ours, our_salt,
                                  env="opus,pcmu,pcma")
    assert "m=audio 5000 RTP/SAVP " in answer
    assert "c=IN IP4 100.72.2.62" in answer
    # His tag 2 is the suite we implement; tag 1 (AES-256) is skipped.
    tag, got_key, got_salt = srtp.parse_crypto_line(
        next(line for line in answer.split("\r\n") if line.startswith("a=crypto:")))
    assert (tag, got_key, got_salt) == (2, ours, our_salt)
    # And what the conversation is handed carries HIS key, his address and
    # exactly one codec -- the one we answered with.
    parsed = parse_sdp_answer(agreed)
    assert parsed.address == ("127.0.0.1", 4000)
    assert (parsed.srtp_key, parsed.srtp_salt) == (key, salt)
    assert parsed.payload_types == [int(answer.split("m=audio 5000 RTP/SAVP ")[1].split()[0])]


def test_a_second_stream_is_declined_with_port_zero_not_dropped():
    key, salt = srtp.new_key_salt()
    answer, _ = answer_offer(offer(4000, key, salt, video=True), "127.0.0.1", 5000, key, salt)
    assert "m=video 0 RTP/SAVP 96" in answer


def test_plain_rtp_and_missing_crypto_are_refused():
    key, salt = srtp.new_key_salt()
    with pytest.raises(OfferError, match="not encrypted"):
        answer_offer(offer(4000, key, salt, profile="RTP/AVP"), "127.0.0.1", 5000, key, salt)
    with pytest.raises(OfferError, match="crypto"):
        answer_offer(offer(4000, key, salt, crypto=False), "127.0.0.1", 5000, key, salt)


def test_an_offer_with_no_codec_in_common_is_refused():
    key, salt = srtp.new_key_salt()
    with pytest.raises(OfferError, match="codec"):
        answer_offer(offer(4000, key, salt, pts="18 101"), "127.0.0.1", 5000, key, salt)


# -- the registration --------------------------------------------------------


def test_it_registers_with_digest_and_a_stable_instance(proxy, listener):
    authed = proxy.registers[-1]
    assert "Authorization: Digest" in authed
    assert 'opaque="+GNywA=="' in authed
    contact = header_of(authed, "Contact")
    assert f'+sip.instance="<urn:uuid:{INSTANCE}>"' in contact
    assert ";ob" in contact and "transport=tcp" in contact
    assert listener.status()["registered"]


def test_a_dropped_connection_is_reconnected_and_re_registered(proxy, listener, monkeypatch):
    monkeypatch.setattr(inbound, "BACKOFF", (0.1,))
    before = len(proxy.registers)
    proxy.drop()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(proxy.registers) < before + 2:
        time.sleep(0.05)
    assert proxy.connections >= 2
    assert len(proxy.registers) >= before + 2
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not listener.registered.is_set():
        time.sleep(0.05)
    assert listener.registered.is_set()


def test_no_credentials_is_refused_at_start_not_at_ring_time():
    with pytest.raises(RuntimeError, match="SIP_USER"):
        SipInbound(user="", password="", peer=HIM).start()
    with pytest.raises(RuntimeError, match="SIP_PEER"):
        SipInbound(user="u", password="p", peer="").start()


# -- a call ------------------------------------------------------------------


def test_his_call_is_answered_and_the_handler_gets_his_media(proxy, listener):
    call, ok, ringing, sock, key, salt = answered(proxy, listener)
    assert "Contact: <sip:hotline@" in ringing
    # The 200 carries the dialog: our tag, the route set as received, every Via.
    assert ";tag=" in header_of(ok, "To")
    assert ok.count("Record-Route:") == 2
    assert ok.count("Via:") == 2
    assert "m=audio" in ok and "RTP/SAVP" in ok

    handler = listener.handlers[0]
    assert handler.ran.wait(3)
    reply, media, our_key, our_salt = handler.args
    parsed = parse_sdp_answer(reply)
    assert parsed.address == sock.getsockname()
    assert (parsed.srtp_key, parsed.srtp_salt) == (key, salt)
    assert media is not None
    # The key handed to the handler is the one our answer advertised.
    line = next(x for x in ok.split("\r\n") if x.startswith("a=crypto:"))
    assert srtp.parse_crypto_line(line)[1:] == (our_key, our_salt)
    sock.close()
    call.bye()


def test_his_bye_ends_it_and_frees_the_line(proxy, listener):
    call, *_rest, sock, _k, _s = answered(proxy, listener)
    handler = listener.handlers[0]
    assert handler.ran.wait(3)
    call.bye()
    assert status_of(proxy.expect(status(200))) == 200
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and listener.call is not None:
        time.sleep(0.02)
    assert handler.call.far_end_hung_up()
    assert listener.call is None, "the slot was never freed for the next call"
    sock.close()


def test_when_we_finish_first_the_bye_goes_to_his_handset_by_the_route_set(proxy, listener):
    listener.hold["seconds"] = 0.3
    _call, ok, _ringing, sock, _k, _s = answered(proxy, listener)
    bye = proxy.expect(request("BYE"))
    assert bye.startswith("BYE sip:him@10.0.0.2:5061;transport=tls SIP/2.0")
    # A UAS uses Record-Route in the order it arrived, not reversed.
    routes = [line for line in bye.split("\r\n") if line.startswith("Route:")]
    assert routes == ["Route: <sips:sip11.example:5061;lr>", "Route: <sip:edge.example;lr>"]
    assert header_of(bye, "From") == header_of(ok, "To")
    assert header_of(bye, "To").endswith("tag=histag")
    sock.close()


def test_a_cancel_before_answer_is_487_and_releases_what_was_set_up(proxy, listener):
    listener.answer_after = 5.0
    sock, key, salt = phone_media()
    call = Call(proxy, offer(sock.getsockname()[1], key, salt))
    call.invite()
    proxy.expect(status(180))
    call.cancel()
    proxy.expect(lambda m: status_of(m) == 200 and "CANCEL" in header_of(m, "CSeq"))
    proxy.expect(lambda m: status_of(m) == 487 and "INVITE" in header_of(m, "CSeq"))
    handler = listener.handlers[0]
    assert handler.abandoned
    assert not handler.ran.is_set(), "it answered a call he had already hung up"
    assert listener.call is None
    sock.close()


def test_a_stranger_is_refused_and_never_reaches_the_daemon(proxy, listener):
    sock, key, salt = phone_media()
    call = Call(proxy, offer(sock.getsockname()[1], key, salt), caller="sip:someone@127.0.0.1")
    call.invite()
    proxy.expect(status(403))
    assert listener.handlers == []
    assert listener.status()["rejected"] == 1
    sock.close()


def test_a_second_call_while_one_is_up_is_busy(proxy, listener):
    first, *_rest, sock, _k, _s = answered(proxy, listener)
    other, key, salt = phone_media()
    second = Call(proxy, offer(other.getsockname()[1], key, salt))
    second.invite()
    proxy.expect(lambda m: status_of(m) == 486 and header_of(m, "Call-ID") == second.call_id)
    first.bye()
    sock.close()
    other.close()


def test_plain_rtp_from_him_is_488_with_the_reason(proxy, listener):
    sock, key, salt = phone_media()
    Call(proxy, offer(sock.getsockname()[1], key, salt, profile="RTP/AVP")).invite()
    refusal = proxy.expect(status(488))
    assert "not encrypted" in header_of(refusal, "Warning")
    sock.close()


def test_the_same_call_down_a_second_path_is_a_merged_request(proxy, listener):
    call, *_rest, sock, _k, _s = answered(proxy, listener)
    call.invite(branch="z9hG4bK.another-path")
    proxy.expect(status(482))
    call.bye()
    sock.close()


def test_an_unacknowledged_200_is_retransmitted_then_given_up_with_a_bye(proxy, listener,
                                                                          monkeypatch):
    monkeypatch.setattr(inbound, "T1", 0.05)
    monkeypatch.setattr(inbound, "ACK_GIVE_UP", 0.8)
    sock, key, salt = phone_media()
    call = Call(proxy, offer(sock.getsockname()[1], key, salt))
    call.invite()
    proxy.expect(status(200))
    proxy.expect(status(200), within=2)      # a retransmission
    proxy.expect(request("BYE"), within=3)
    handler = listener.handlers[0]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and listener.call is not None:
        time.sleep(0.02)
    assert handler.call.far_end_hung_up()
    assert listener.call is None
    sock.close()


def test_options_is_answered(proxy, listener):
    Call(proxy, "").simple("OPTIONS", 1)
    proxy.expect(status(200))


def test_audio_flows_both_ways_through_the_answered_call(proxy, listener):
    """The real media path: our VoiceCall on the socket the listener bound, his
    phone on the other end, SRTP keyed from the two crypto lines."""
    stats = {}

    def on_call(call):
        def run(reply, media, key, salt):
            voice = VoiceCall(media, parse_sdp_answer(reply), key, salt)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and not call.far_end_hung_up():
                time.sleep(0.02)
            voice.close()
            voice.pump.join(1.0)
            stats.update(voice.stats())
        return run

    listener.on_call = on_call
    sock, key, salt = phone_media()
    # PCMU only, so the fake phone needs no Opus encoder.
    sdp = offer(sock.getsockname()[1], key, salt, pts="0 101")
    call = Call(proxy, sdp)
    call.invite()
    ok = proxy.expect(status(200))
    call.learn_tag(ok)
    call.ack()
    ours = parse_sdp_answer(ok)
    tx = srtp.SrtpSession(key, salt)
    rx = srtp.SrtpSession(ours.srtp_key, ours.srtp_salt)
    frame = pcm.ulaw_encode(b"\x00\x10" * 160)
    heard = 0
    sock.settimeout(0.02)
    for seq in range(60):
        sock.sendto(tx.protect(rtp.build_packet(seq, seq * 160, 0xBEEF, frame)),
                    ("127.0.0.1", ours.port))
        try:
            data, _ = sock.recvfrom(4096)
            if rtp.parse_packet(rx.unprotect(data)) is not None:
                heard += 1
        except TimeoutError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not stats:
        time.sleep(0.05)
    assert heard > 20, "his phone heard nothing from us"
    assert stats["frames_received"] > 20, stats
    assert stats["auth_failures"] == 0
    sock.close()


def test_a_redial_right_after_his_hangup_is_answered_not_busy(proxy, listener):
    """The conversation only checks for a hangup between turns, so its thread
    can outlive the BYE by most of a turn. The line must be free anyway."""
    stubborn = threading.Event()

    def on_call(call):
        def run(reply, media, key, salt):
            stubborn.wait(5)      # ignores the hangup, like a turn in progress
        return run

    listener.on_call = on_call
    first, *_rest, sock, _k, _s = answered(proxy, listener)
    held = listener.call
    first.bye()
    proxy.expect(status(200))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and listener.call is not None:
        time.sleep(0.02)
    assert listener.call is None
    assert held.media is not None, "the media socket was closed under a running pump"

    second, ok, *_more = answered(proxy, listener)
    assert header_of(ok, "Call-ID") == second.call_id
    stubborn.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and held.media is not None:
        time.sleep(0.02)
    assert held.media is None
    second.bye()
    sock.close()
