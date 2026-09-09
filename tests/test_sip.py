"""The Linphone doorbell, against a real SIP server on a real UDP socket.

The server here is a stand-in for sip.linphone.org, but nothing between it and
the transport is faked: real datagrams, real digest authentication, real
response parsing. What it cannot prove is that linphone.org behaves this way --
that needs his account.
"""

import asyncio
import socket
import threading

import pytest

from hotline_ios.ring.base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable
from hotline_ios.ring.sip import (
    SipTransport,
    _digest,
    header_of,
    parse_challenge,
    status_of,
)


async def eventually(predicate, *, within: float = 2.0) -> bool:
    """Wait for something the registrar THREAD observes, up to a deadline.

    `ring()` returning does not mean the fake registrar has read what was sent
    to it -- CANCEL goes out on a socket and is seen by another thread, so
    asserting on it immediately is a race. It failed about one run in three
    under full-suite load and passed alone every time, which is the signature.
    Polling for the observation keeps the assertion meaningful; deleting it
    would have "fixed" the flake by no longer checking that he stops being rung.
    """
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


WHO = CallTarget(device="phone", reason="the build is stuck", caller_id="the ios build")
NONCE = "dcd98b7102dd2f0e8b11d0f600bfb0c093"


def test_the_digest_matches_rfc_2617s_own_worked_example():
    # The spec's example, verbatim. If this ever breaks, authentication against
    # a real registrar breaks with it and the error will be far less obvious.
    assert _digest(
        "Mufasa", "Circle Of Life", "testrealm@host.com", NONCE,
        "GET", "/dir/index.html", qop="auth", nc="00000001", cnonce="0a4f113b",
    ) == "6629fae49393a05397450978507c4ef1"


def test_a_challenge_is_parsed_including_unquoted_values():
    got = parse_challenge('Digest realm="sip.linphone.org", nonce="abc", qop="auth", algorithm=MD5')
    assert got["realm"] == "sip.linphone.org"
    assert got["nonce"] == "abc"
    # algorithm arrives unquoted; missing it silently would change the hash.
    assert got["algorithm"] == "MD5"


def test_status_and_header_parsing():
    assert status_of("SIP/2.0 180 Ringing") == 180
    assert status_of("nonsense") == 0
    assert header_of("SIP/2.0 200 OK\r\nCall-ID: xyz\r\n\r\n", "call-id") == "xyz"


class FakeRegistrar(threading.Thread):
    """Enough of sip.linphone.org to exercise the whole exchange.

    Challenges the REGISTER, accepts the authenticated one, then answers the
    INVITE with whatever script the test asked for.
    """

    daemon = True

    def __init__(self, invite_script=(180, 200), *, reject_register=False):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.4)
        self.port = self.sock.getsockname()[1]
        self.invite_script = list(invite_script)
        self.reject_register = reject_register
        self.stop_flag = threading.Event()
        self.saw_authenticated_register = False
        self.saw_invite = False
        self.saw_cancel = False
        self.seen: list[str] = []

    def _reply(self, request, addr, status, extra=()):
        lines = [f"SIP/2.0 {status}"]
        for name in ("Via", "From", "To", "Call-ID", "CSeq"):
            value = header_of(request, name)
            if value:
                if name == "To" and ";tag=" not in value:
                    value += ";tag=farend"
                lines.append(f"{name}: {value}")
        lines.extend(extra)
        lines.append("Content-Length: 0")
        self.sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), addr)

    def run(self):
        while not self.stop_flag.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            request = data.decode(errors="replace")
            method = request.split(" ", 1)[0].upper()

            if method == "REGISTER":
                if self.reject_register:
                    self._reply(request, addr, "403 Forbidden")
                elif "Authorization:" in request:
                    self.saw_authenticated_register = True
                    self._reply(request, addr, "200 OK")
                else:
                    challenge = (
                        f'WWW-Authenticate: Digest realm="test", nonce="{NONCE}", '
                        'qop="auth", algorithm=MD5'
                    )
                    self._reply(request, addr, "401 Unauthorized", [challenge])
            elif method == "INVITE":
                self.saw_invite = True
                for status in self.invite_script:
                    text = {180: "180 Ringing", 183: "183 Session Progress",
                            200: "200 OK", 486: "486 Busy Here",
                            404: "404 Not Found"}[status]
                    self._reply(request, addr, text)
            elif method == "CANCEL":
                self.saw_cancel = True
                self._reply(request, addr, "200 OK")
            elif method in ("ACK", "BYE"):
                self.seen.append(method)
                if method == "BYE":
                    self._reply(request, addr, "200 OK")

    def stop(self):
        self.stop_flag.set()
        self.sock.close()



@pytest.fixture
def registrar():
    server = FakeRegistrar()
    server.start()
    yield server
    server.stop()


def transport_for(server, **kw):
    return SipTransport(
        user="bogdan", password="hotline", domain="127.0.0.1",
        peer="sip:him@127.0.0.1", port=server.port, transport="udp", **kw,
    )


async def test_a_ring_is_confirmed_by_180_ringing(registrar):
    # The strongest confirmation any transport here has: the far end saying, in
    # the protocol's own words, that it is ringing.
    registrar.invite_script = [180]
    t = transport_for(registrar)
    with pytest.raises(CallUnanswered):
        await t.ring(WHO, timeout=1.5)
    assert t.ringing.is_set()
    assert registrar.saw_authenticated_register
    assert registrar.saw_invite
    # And it stops ringing afterwards rather than buzzing him indefinitely.
    assert await eventually(lambda: registrar.saw_cancel)


async def test_183_counts_as_ringing_too(registrar):
    # Some proxies send session progress instead of 180, and treating that as
    # silence would report a working doorbell as broken.
    registrar.invite_script = [183]
    t = transport_for(registrar)
    with pytest.raises(CallUnanswered):
        await t.ring(WHO, timeout=1.5)
    assert t.ringing.is_set()


async def test_an_answered_call_is_not_an_error(registrar):
    registrar.invite_script = [180, 200]
    t = transport_for(registrar)
    await t.ring(WHO, timeout=3)
    assert t.ringing.is_set()


async def test_busy_is_declined_not_unreachable(registrar):
    # A decline is an answer. The chain must not fall through to another
    # doorbell on it.
    registrar.invite_script = [486]
    t = transport_for(registrar)
    with pytest.raises(CallDeclined):
        await t.ring(WHO, timeout=2)


async def test_an_unknown_address_is_unreachable(registrar):
    registrar.invite_script = [404]
    t = transport_for(registrar)
    with pytest.raises(CallUnreachable) as exc:
        await t.ring(WHO, timeout=2)
    assert "not found" in str(exc.value)


async def test_bad_credentials_say_so_rather_than_timing_out():
    server = FakeRegistrar(reject_register=True)
    server.start()
    try:
        t = transport_for(server)
        with pytest.raises(CallUnreachable) as exc:
            await t.ring(WHO, timeout=2)
        assert "credentials" in str(exc.value)
    finally:
        server.stop()


async def test_silence_from_the_far_end_never_claims_a_ring(registrar):
    # The property that makes this safe inside ConfirmedRing: no 180 means no
    # claim, even though the INVITE was sent and nothing errored.
    registrar.invite_script = []
    t = transport_for(registrar)
    with pytest.raises(CallUnreachable) as exc:
        await t.ring(WHO, timeout=1.2)
    assert "nothing confirmed" in str(exc.value)
    assert not t.ringing.is_set()


async def test_being_unconfigured_fails_at_startup():
    t = SipTransport(user="", password="", peer="")
    with pytest.raises(CallUnreachable) as exc:
        await t.start()
    assert "not configured" in str(exc.value)


def test_the_real_linphone_challenge_format_parses():
    """Their actual challenge, captured from sip.linphone.org over UDP.

    Kept as a fixture rather than a live call so the suite stays offline. The
    live probe that produced it needed no account: an unauthenticated REGISTER
    is answered with a 401 and this header.

    `opaque` is the reason this test exists. linphone.org sends it, many servers
    do not, and it has to be echoed back verbatim or the authenticated REGISTER
    is rejected -- a failure that presents as "it just does not ring".
    """
    header = (
        'Digest realm="sip.linphone.org", nonce="HFE47gAAAADyW23QAAD253sZEAsAAAAA", '
        'opaque="+GNywA==", algorithm=MD5, qop="auth"'
    )
    got = parse_challenge(header)
    assert got["realm"] == "sip.linphone.org"
    assert got["nonce"] == "HFE47gAAAADyW23QAAD253sZEAsAAAAA"
    assert got["opaque"] == "+GNywA=="
    assert got["algorithm"] == "MD5"
    assert got["qop"] == "auth"


def test_the_authorisation_echoes_opaque_and_uses_qop():
    # Both are required by linphone.org's challenge above. Dropping either is a
    # silent authentication failure rather than an error.
    t = SipTransport(user="bogdan", password="secret", domain="sip.linphone.org",
                     peer="sip:him@sip.linphone.org")
    header = t._authorisation(
        'Digest realm="sip.linphone.org", nonce="abc", opaque="+GNywA==", '
        'algorithm=MD5, qop="auth"',
        "REGISTER", "sip:sip.linphone.org", proxy=False,
    )
    assert header.startswith("Authorization: Digest ")
    assert 'opaque="+GNywA=="' in header
    assert "qop=auth" in header and "nc=00000001" in header and "cnonce=" in header
    assert 'username="bogdan"' in header


def test_the_offer_is_encrypted_and_names_a_real_port():
    """What made it actually ring his phone.

    A plain RTP/AVP offer is answered 488 Not acceptable here -- and because
    linphone.org has already sent the push by then, the phone lights up and dies
    about a second later, which reads as a notification bug rather than a
    negotiation failure. He described exactly that before this was fixed.
    """
    t = SipTransport(user="bogdan", password="x", peer="sip:him@sip.linphone.org")
    t._local = ("10.0.0.1", 5060)
    try:
        body = t._invite("cid", 1, "tag").split("\r\n\r\n", 1)[1]
        media = next(line for line in body.split("\r\n") if line.startswith("m=audio"))
        assert "RTP/SAVP" in media
        # Port 9 is the discard port and is one of the things that earns a 488.
        assert int(media.split()[1]) > 1024
        assert any(line.startswith("a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:")
                   for line in body.split("\r\n"))
        assert "a=sendrecv" in body
    finally:
        t._close_media()


def test_tls_is_the_default_because_udp_has_no_retransmission_here():
    # NOT because linphone.org ignores UDP INVITEs -- that was my first reading
    # of one silent attempt, and a later UDP run rang his phone, refuting it.
    # The real gap is that SIP over UDP requires the client to retransmit an
    # INVITE (RFC 3261 timer A) and this does not, so a single lost datagram is
    # indistinguishable from silence. TLS runs over TCP, which retransmits.
    t = SipTransport(user="u", password="p", peer="sip:x@sip.linphone.org")
    assert t.transport == "tls"
    assert t.port == 5061
    # Asking for udp still works, for probing.
    assert SipTransport(user="u", password="p", peer="sip:x@y", transport="udp").port == 5060


async def test_an_answered_call_is_acked_and_byed_rather_than_cancelled(registrar):
    """RFC 3261: CANCEL is invalid once a final response has arrived.

    This only started mattering the moment he actually picked one up -- until
    then the code never reached a 200 and CANCEL was always the right thing.
    An unACKed 200 makes the far end retransmit it for half a minute, and a call
    left up keeps his phone occupied for a ring nobody wanted to answer.
    """
    registrar.invite_script = [180, 200]
    t = transport_for(registrar)
    await t.ring(WHO, timeout=3)
    # The registrar polls its socket, so give it a moment to see the last
    # datagram rather than racing it. Bounded, and it fails if BYE never comes.
    for _ in range(50):
        if "BYE" in registrar.seen:
            break
        await asyncio.sleep(0.05)
    assert "ACK" in registrar.seen, registrar.seen
    assert "BYE" in registrar.seen, registrar.seen
    assert not registrar.saw_cancel


# -- message framing over the TCP/TLS byte stream --------------------------


class _ChunkedSocket:
    """A socket that hands back a SIP message in pieces, as TCP actually does."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def settimeout(self, _t):
        pass

    def recv(self, _n):
        return self.chunks.pop(0) if self.chunks else b""


def test_a_body_split_across_segments_is_reassembled():
    """The bug that made an answered call report 'nowhere to send audio'.

    One recv() returned the 200's headers and none of its SDP, so the parse saw
    a message with no c= line and blamed the far end for a short read here.
    """
    from hotline_ios.ring.sip import SipTransport

    body = "v=0\r\nc=IN IP4 192.168.1.50\r\nm=audio 7078 RTP/SAVP 0\r\n"
    head = ("SIP/2.0 200 OK\r\n"
            f"Content-Length: {len(body)}\r\n\r\n")
    ring = SipTransport(user="u", password="p", peer="sip:x@y", domain="y")
    sock = _ChunkedSocket([head.encode(), body.encode()])
    got = ring._recv(sock, timeout=5)
    assert got.endswith(body)
    assert "c=IN IP4 192.168.1.50" in got


def test_two_messages_in_one_segment_are_returned_one_at_a_time():
    """100 Trying and 180 Ringing arrive back to back; neither may be dropped."""
    from hotline_ios.ring.sip import SipTransport

    both = (b"SIP/2.0 100 Trying\r\nContent-Length: 0\r\n\r\n"
            b"SIP/2.0 180 Ringing\r\nContent-Length: 0\r\n\r\n")
    ring = SipTransport(user="u", password="p", peer="sip:x@y", domain="y")
    sock = _ChunkedSocket([both])
    assert "100 Trying" in ring._recv(sock, timeout=5)
    assert "180 Ringing" in ring._recv(sock, timeout=5)


# -- routing an answered dialog --------------------------------------------
#
# He answered a real call on 2026-09-10 at 00:25:46Z, heard all of it, and was
# cut off 37.24s in while audio was still flowing perfectly both ways. Nothing
# had hung up: the ACK went to his address-of-record with no Route set, never
# reached his handset, and RFC 3261 13.3.1.4 had his phone retransmit its 200
# for 64*T1 and then send a BYE. It was invisible for as long as this hung up
# in the same breath as it ACKed.


ANSWER_200 = (
    "SIP/2.0 200 OK\r\n"
    "Via: SIP/2.0/TLS 192.168.1.139:44321;branch=z9hG4bKabc;rport=44321\r\n"
    "Record-Route: <sip:91.121.209.194:5061;transport=tls;lr>\r\n"
    "Record-Route: <sip:176.31.149.179;transport=tls;lr>, <sip:proxy2;lr>\r\n"
    "From: <sip:hotline-caller@sip.linphone.org>;tag=fromtag\r\n"
    "To: <sip:b0g13a@sip.linphone.org>;tag=totag\r\n"
    "Call-ID: callid\r\n"
    "CSeq: 2 INVITE\r\n"
    'Contact: <sip:b0g13a@10.44.2.9:41234;transport=tls>;+sip.instance="<urn:uuid:x>"\r\n'
    "Content-Length: 0\r\n"
    "\r\n"
)


class Wire:
    """Collects what we put on the socket, without being one."""

    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data.decode())

    def requests(self, method):
        return [m for m in self.sent if m.startswith(method + " ")]


def answered(transport, wire, reply=ANSWER_200):
    transport._finish_answered(wire, "callid", "fromtag",
                               "<sip:b0g13a@sip.linphone.org>;tag=totag", 2,
                               reply=reply)


def test_the_ack_goes_to_his_handset_not_to_his_address_of_record():
    wire = Wire()
    answered(SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org"), wire)
    ack = wire.requests("ACK")[0]
    assert ack.splitlines()[0] == "ACK sip:b0g13a@10.44.2.9:41234;transport=tls SIP/2.0"


def test_the_ack_carries_the_route_set_in_reverse():
    """RFC 3261 12.1.2. The wrong order sends it back out through the proxies in
    the sequence they were traversed on the way in, which is a different path
    and generally a dead one."""
    wire = Wire()
    answered(SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org"), wire)
    routes = [line.split(":", 1)[1].strip()
              for line in wire.requests("ACK")[0].splitlines()
              if line.lower().startswith("route:")]
    assert routes == [
        "<sip:proxy2;lr>",
        "<sip:176.31.149.179;transport=tls;lr>",
        "<sip:91.121.209.194:5061;transport=tls;lr>",
    ]


def test_the_ack_says_where_to_reach_us():
    wire = Wire()
    answered(SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org"), wire)
    assert any(line.lower().startswith("contact:")
               for line in wire.requests("ACK")[0].splitlines())


def test_the_bye_goes_the_same_way_as_the_ack():
    wire = Wire()
    answered(SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org"), wire)
    ack, bye = wire.requests("ACK")[0], wire.requests("BYE")[0]
    def routing(message):
        return [line for line in message.splitlines()
                if line.startswith(("ACK ", "BYE ", "Route:"))]
    assert routing(bye)[1:] == routing(ack)[1:]
    assert bye.splitlines()[0].endswith("sip:b0g13a@10.44.2.9:41234;transport=tls SIP/2.0")


def test_a_200_with_no_contact_still_gets_an_ack_at_the_address_of_record():
    """Degrading to what it did before is right: an AOR is a poor target and no
    ACK at all is a worse one."""
    wire = Wire()
    stripped = "\r\n".join(line for line in ANSWER_200.split("\r\n")
                           if not line.lower().startswith("contact:"))
    answered(SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org"),
             wire, reply=stripped)
    assert wire.requests("ACK")[0].startswith("ACK sip:b0g13a@sip.linphone.org SIP/2.0")


def test_a_retransmitted_200_is_counted_and_re_acked():
    """The instrument, not just the fix. Zero retransmissions means the ACK
    landed; anything else says so in one line instead of costing another call."""
    transport = SipTransport(user="u", password="p", peer="sip:b0g13a@sip.linphone.org")
    wire = Wire()
    seen = []

    def on_answer(reply, media, key, salt):
        # What the conversation loop does every time round its own loop.
        transport._sock = _Retransmitting(ANSWER_200)
        transport.far_end_hung_up()
        seen.append(transport.unacked)

    transport.on_answer = on_answer
    answered(transport, wire)
    assert seen == [1]
    assert transport.unacked == 1
    # Two ACKs: the original, and the one the retransmission asked for.
    assert len(wire.requests("ACK")) == 2


class _Retransmitting:
    """A socket that hands over one retransmitted 200 OK and then nothing."""

    def __init__(self, message):
        self.pending = [message.encode()]

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if self.pending:
            return self.pending.pop(0)
        raise BlockingIOError

    def sendall(self, data):
        pass
