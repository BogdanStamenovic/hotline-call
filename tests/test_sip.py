"""The Linphone doorbell, against a real SIP server on a real UDP socket.

The server here is a stand-in for sip.linphone.org, but nothing between it and
the transport is faked: real datagrams, real digest authentication, real
response parsing. What it cannot prove is that linphone.org behaves this way --
that needs his account.
"""

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
    assert registrar.saw_cancel


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
