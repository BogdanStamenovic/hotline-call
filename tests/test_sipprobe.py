"""The probe has to be right before it is trusted, because it will be run once,
against a phone, and its output will decide an architecture.

The failure that matters is a false negative: a parser that misses a push
parameter that WAS on the wire would kill outcome C on a bug. So the tests use
real Linphone-shaped Contact headers, both spellings, and check the absence case
explicitly.
"""

import asyncio
from pathlib import Path

import pytest

from hotline_ios.sipprobe import SipProbe, header, parse_push_params, serve

# A real-shaped Linphone iOS Contact, RFC 8599 spelling.
MODERN = (
    '<sip:bogdan@100.72.2.62;transport=tls>;+sip.instance="<urn:uuid:aa-bb>";'
    "pn-provider=apns;pn-prid=9f8e7d6c5b4a3f2e1d0c;"
    "pn-param=ABCD1234.org.linphone.phone.voip&remote"
)
# Linphone's older proprietary spelling, still emitted by some builds.
LEGACY = (
    "<sip:bogdan@100.72.2.62>;app-id=org.linphone.phone.prod;"
    "pn-type=apple;pn-tok=deadbeefcafe"
)
BARE = "<sip:bogdan@100.72.2.62;transport=udp>"


def test_modern_push_params_are_found():
    got = parse_push_params(MODERN)
    assert got["pn-provider"] == "apns"
    assert got["pn-prid"] == "9f8e7d6c5b4a3f2e1d0c"
    # The & must terminate the value: pn-param feeds a push API verbatim and a
    # trailing "&remote" would be sent as part of the bundle id.
    assert got["pn-param"] == "ABCD1234.org.linphone.phone.voip"


def test_legacy_spelling_is_found_too():
    # Looking only for the modern names would turn "the app used the old
    # spelling" into "the app sent nothing" -- the exact wrong conclusion.
    got = parse_push_params(LEGACY)
    assert got["pn-tok"] == "deadbeefcafe"
    assert got["pn-type"] == "apple"


def test_absence_is_reported_as_absence():
    assert parse_push_params(BARE) == {}


def test_pushable_requires_an_actual_token():
    from hotline_ios.sipprobe import Registration

    assert Registration("a", MODERN, parse_push_params(MODERN)).pushable
    assert not Registration("a", BARE, parse_push_params(BARE)).pushable
    # Provider alone is not a token. Reporting C viable on this would be a lie.
    assert not Registration("a", "x;pn-provider=apns", {"pn-provider": "apns"}).pushable


def test_header_lookup_is_case_insensitive_and_exact():
    msg = "REGISTER sip:x SIP/2.0\r\nCall-ID: abc\r\nContact: <sip:y>\r\n\r\n"
    assert header(msg, "call-id") == "abc"
    assert header(msg, "Contact") == "<sip:y>"
    assert header(msg, "Missing") == ""


async def test_a_real_register_over_a_real_socket(tmp_path):
    """End to end over UDP -- no mocks. This is the thing that will run."""
    capture = tmp_path / "cap.txt"
    probe = await serve("127.0.0.1", 15060, capture)

    loop = asyncio.get_running_loop()
    got: asyncio.Queue[bytes] = asyncio.Queue()

    class Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(
        Client, remote_addr=("127.0.0.1", 15060)
    )
    register = (
        "REGISTER sip:100.72.2.62 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 100.108.255.28:5060;branch=z9hG4bK1\r\n"
        "From: <sip:bogdan@100.72.2.62>;tag=abc\r\n"
        "To: <sip:bogdan@100.72.2.62>\r\n"
        "Call-ID: probe-test-1\r\n"
        "CSeq: 1 REGISTER\r\n"
        f"Contact: {MODERN}\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport.sendto(register)

    reply = (await asyncio.wait_for(got.get(), timeout=3)).decode()
    transport.close()

    assert reply.startswith("SIP/2.0 200 OK")
    # A registrar must tag the To header or some clients never treat the
    # registration as confirmed and retry forever.
    assert ";tag=" in [ln for ln in reply.split("\r\n") if ln.startswith("To:")][0]
    assert "Call-ID: probe-test-1" in reply

    assert probe.packets == 1
    record = list(probe.registrations.values())[0]
    assert record.pushable
    assert record.push["pn-prid"] == "9f8e7d6c5b4a3f2e1d0c"

    # The verbatim capture is the primary evidence, not the parse.
    raw = capture.read_text()
    assert "pn-prid=9f8e7d6c5b4a3f2e1d0c" in raw
    assert "REGISTER sip:100.72.2.62" in raw


async def test_invite_is_declined_rather_than_half_accepted(tmp_path):
    probe = await serve("127.0.0.1", 15061, tmp_path / "c.txt")
    loop = asyncio.get_running_loop()
    got: asyncio.Queue[bytes] = asyncio.Queue()

    class Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(Client, remote_addr=("127.0.0.1", 15061))
    transport.sendto(
        b"INVITE sip:x SIP/2.0\r\nVia: SIP/2.0/UDP a;branch=z\r\nFrom: <sip:a>;tag=1\r\n"
        b"To: <sip:b>\r\nCall-ID: c\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n"
    )
    reply = (await asyncio.wait_for(got.get(), timeout=3)).decode()
    transport.close()
    # 200 here would commit to an RTP session this instrument cannot hold up.
    assert reply.startswith("SIP/2.0 405")
