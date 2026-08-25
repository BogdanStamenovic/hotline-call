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
