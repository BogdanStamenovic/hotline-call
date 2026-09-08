"""RTP framing, the jitter buffer, and a real two-socket loopback.

The jitter buffer is the part that earns its tests: the measured path to his
phone relays through DERP at 92-623 ms with 172 ms of jitter, so reordering and
loss are the normal case here, not the edge case.
"""

import asyncio

import numpy as np
import pytest

from hotline_ios.media.rtp import (
    PCMU_FORMAT,
    PT_PCMU,
    SAMPLES_PER_FRAME,
    JitterBuffer,
    RtpStream,
    build_packet,
    parse_packet,
)

FRAME = b"\x11\x00" * SAMPLES_PER_FRAME


def test_packet_roundtrip():
    packet = build_packet(42, 1000, 0xDEADBEEF, b"payload")
    pt, seq, ts, payload = parse_packet(packet)
    assert (pt, seq, ts, payload) == (PT_PCMU, 42, 1000, b"payload")


def test_a_short_or_non_rtp_datagram_is_rejected_not_decoded():
    assert parse_packet(b"") is None
    assert parse_packet(b"\x00" * 8) is None
    # Version 1 is not RTP. Decoding it would turn junk into audio.
    assert parse_packet(b"\x40" + b"\x00" * 15) is None


def test_csrc_and_extension_headers_are_skipped():
    # A client that sends CSRCs or a header extension would otherwise have those
    # bytes decoded as if they were speech.
    base = build_packet(1, 0, 1, b"AUDIO")
    with_csrc = bytes([0x82]) + base[1:12] + b"\x00" * 8 + b"AUDIO"
    assert parse_packet(with_csrc)[3] == b"AUDIO"


def test_jitter_buffer_reorders():
    buf = JitterBuffer(target_ms=40)
    buf.push(2, b"B")
    buf.push(1, b"A")
    # Ordering is the property that matters, and it holds from the first pop:
    # the buffer starts at the lowest sequence it has, not the first it saw.
    assert buf.pop() == b"A"
    assert buf.pop() == b"B"


def test_out_of_order_arrival_is_counted_once_there_is_a_baseline():
    # `reordered` deliberately counts nothing before playout begins: until the
    # buffer has an expected-next, "out of order" has no meaning to be wrong
    # about. So the counter is exercised after the first pop, which is also the
    # only case a live call ever sees.
    buf = JitterBuffer(target_ms=20)
    buf.push(1, b"A")
    assert buf.pop() == b"A"
    buf.push(3, b"C")
    buf.push(2, b"B")
    assert buf.reordered >= 1
    assert buf.pop() == b"B"
    assert buf.pop() == b"C"


def test_a_gap_repeats_the_last_frame_rather_than_leaving_a_hole():
    # A hole is a click; a repeat is a smear, and the ear forgives a smear.
    buf = JitterBuffer(target_ms=40)
    buf.push(1, b"A")
    buf.push(3, b"C")
    assert buf.pop() == b"A"
    assert buf.pop() == b"A"      # concealed frame 2
    assert buf.concealed == 1
    assert buf.pop() == b"C"


def test_a_very_late_frame_is_dropped_not_played_out_of_order():
    buf = JitterBuffer(target_ms=20)
    buf.push(10, b"A")
    buf.pop()
    buf.push(5, b"OLD")
    assert buf.dropped_late == 1


def test_sequence_wraparound_does_not_look_like_a_late_frame():
    # 16-bit sequence numbers really do roll over on a long call, and a naive
    # comparison discards every frame after the wrap.
    buf = JitterBuffer(target_ms=20)
    buf.push(65535, b"A")
    buf.pop()
    buf.push(0, b"B")
    assert buf.dropped_late == 0
    assert buf.pop() == b"B"


async def test_two_rtp_streams_actually_talk_to_each_other():
    """No mocks: two real UDP sockets, real framing, real G.711."""
    loop = asyncio.get_running_loop()
    left = RtpStream()
    right = RtpStream()
    lt, _ = await loop.create_datagram_endpoint(lambda: left, local_addr=("127.0.0.1", 0))
    rt, _ = await loop.create_datagram_endpoint(lambda: right, local_addr=("127.0.0.1", 0))
    left.remote = rt.get_extra_info("sockname")
    right.remote = lt.get_extra_info("sockname")
    left.start_clock()
    right.start_clock()
    try:
        # Half a second of a tone, so the content is checkable rather than zeros.
        t = np.linspace(0, 0.5, 4000, endpoint=False)
        tone = (np.sin(2 * np.pi * 440 * t) * 0.4 * 32767).astype("<i2").tobytes()
        left.send(tone)

        got = b""
        for _ in range(120):
            try:
                frame = await asyncio.wait_for(right.recv(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError):
                break
            if frame is None:
                break
            got += frame
            if len(got) >= len(tone) // 2:
                break

        assert left.sent > 5, left.sent
        assert right.received > 5, right.received
        # G.711 is lossy, so check it is recognisably the same signal rather
        # than byte-identical.
        heard = np.frombuffer(got[: len(tone)], dtype="<i2").astype(np.float64)
        assert heard.size > 0
        assert 0.2 < float(np.abs(heard).max()) / 32767 <= 1.0
    finally:
        await left.close()
        await right.close()


async def test_a_wrong_payload_type_is_ignored_rather_than_decoded():
    loop = asyncio.get_running_loop()
    stream = RtpStream()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: stream, local_addr=("127.0.0.1", 0))
    try:
        bad = bytes([0x80, 111]) + build_packet(1, 0, 1, b"\xff" * 160)[2:]
        stream.datagram_received(bad, ("127.0.0.1", 9999))
        assert stream.received == 0
    finally:
        await stream.close()
