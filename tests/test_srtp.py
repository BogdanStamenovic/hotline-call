"""SRTP against RFC 3711's own Appendix B vectors.

The point of testing this way is that a self-consistent SRTP implementation can
be confidently wrong: assemble the IV with SSRC and index swapped and every
protect/unprotect round-trip still passes, while nothing on earth can decrypt
the result. The published vectors are the only check that catches that, so they
come first and the round-trip tests are the afterthought rather than the point.
"""

from __future__ import annotations

import struct

import pytest

from hotline_ios.media import srtp


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# -- RFC 3711 B.1.1: the AES-CM keystream ---------------------------------


def test_aes_cm_keystream_matches_rfc3711_b11():
    key = h("2B7E151628AED2A6ABF7158809CF4F3C")
    iv = h("F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000")
    out = srtp.aes_cm_keystream(key, iv, 48)
    assert out[0:16] == h("E03EAD0935C95E80E166B16DD92B4EB4")
    assert out[16:32] == h("D23513162B02D0F72A43A2FE4A5F97AB")
    assert out[32:48] == h("41E95B3BB0A2E8DD477901E4FCA894C0")


def test_iv_construction_matches_that_vector():
    """The B.1.1 IV is salt||0000 precisely because SSRC and index are zero."""
    salt = h("F0F1F2F3F4F5F6F7F8F9FAFBFCFD")
    assert srtp._srtp_iv(salt, ssrc=0, index=0) == h("F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000")


def test_iv_places_ssrc_and_index_at_the_right_offsets():
    """SSRC * 2^64 lands on bytes 4..7; index * 2^16 on bytes 8..13."""
    iv = srtp._srtp_iv(bytes(14), ssrc=0xDEADBEEF, index=0x0000AABBCCDD)
    assert iv[4:8] == h("DEADBEEF")
    assert iv[8:14] == h("0000AABBCCDD")
    assert iv[0:4] == bytes(4) and iv[14:16] == bytes(2)


# -- RFC 3711 B.2: key derivation -----------------------------------------

MASTER_KEY = h("E1F97A0D3E018BE0D64FA32C06DE4139")
MASTER_SALT = h("0EC675AD498AFEEBB6960B3AABE6")


def test_kdf_cipher_key_matches_rfc3711_b2():
    got = srtp.derive_key(MASTER_KEY, MASTER_SALT, srtp.LABEL_RTP_ENCR, 16)
    assert got == h("C61E7A93744F39EE10734AFE3FF7A087")


def test_kdf_cipher_salt_matches_rfc3711_b2():
    got = srtp.derive_key(MASTER_KEY, MASTER_SALT, srtp.LABEL_RTP_SALT, 14)
    assert got == h("30CBBC08863D8C85D49DB34A9AE1")


def test_kdf_auth_key_matches_rfc3711_b2():
    got = srtp.derive_key(MASTER_KEY, MASTER_SALT, srtp.LABEL_RTP_AUTH, 20)
    assert got == h("CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4")


def test_session_keys_are_all_three_derivations():
    s = srtp.SrtpSession(MASTER_KEY, MASTER_SALT)
    assert s.encr_key == h("C61E7A93744F39EE10734AFE3FF7A087")
    assert s.salt == h("30CBBC08863D8C85D49DB34A9AE1")
    assert s.auth_key == h("CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4")


# -- header parsing --------------------------------------------------------


def rtp(seq: int, payload: bytes, ssrc: int = 0x12345678, cc: int = 0,
        ext: bytes = b"") -> bytes:
    first = 0x80 | cc | (0x10 if ext else 0)
    header = struct.pack("!BBHII", first, 0x00, seq, 0, ssrc)
    return header + b"\x00" * (4 * cc) + ext + payload


def test_payload_offset_plain_header():
    assert srtp.payload_offset(rtp(1, b"x")) == 12


def test_payload_offset_counts_csrcs():
    assert srtp.payload_offset(rtp(1, b"x", cc=3)) == 12 + 12


def test_payload_offset_counts_header_extension():
    ext = struct.pack("!HH", 0xBEDE, 2) + b"\x00" * 8
    assert srtp.payload_offset(rtp(1, b"x", ext=ext)) == 12 + 12


def test_payload_offset_rejects_a_runt():
    with pytest.raises(srtp.SrtpError):
        srtp.payload_offset(b"\x80\x00\x00")


# -- protect / unprotect ---------------------------------------------------


def session_pair():
    key, salt = srtp.new_key_salt()
    return srtp.SrtpSession(key, salt), srtp.SrtpSession(key, salt)


def test_round_trip_returns_the_original_packet():
    send, recv = session_pair()
    packet = rtp(100, b"\xd5" * 160)  # a G.711 frame is 160 bytes of mu-law
    assert recv.unprotect(send.protect(packet)) == packet


def test_protect_actually_encrypts_the_payload_and_leaves_the_header():
    send, _ = session_pair()
    packet = rtp(100, b"\xd5" * 160)
    out = send.protect(packet)
    assert out[:12] == packet[:12], "header must stay in the clear for routing"
    assert out[12:172] != packet[12:], "payload must not survive as plaintext"
    assert len(out) == len(packet) + srtp.AUTH_TAG_LEN


def test_a_tampered_payload_is_rejected():
    send, recv = session_pair()
    out = bytearray(send.protect(rtp(100, b"\xd5" * 160)))
    out[50] ^= 0x01
    with pytest.raises(srtp.AuthenticationFailure):
        recv.unprotect(bytes(out))


def test_a_tampered_header_is_rejected():
    send, recv = session_pair()
    out = bytearray(send.protect(rtp(100, b"\xd5" * 160)))
    out[8] ^= 0x01  # SSRC
    with pytest.raises(srtp.AuthenticationFailure):
        recv.unprotect(bytes(out))


def test_a_foreign_key_is_rejected_rather_than_returning_garbage():
    send, _ = session_pair()
    stranger = srtp.SrtpSession(*srtp.new_key_salt())
    with pytest.raises(srtp.AuthenticationFailure):
        stranger.unprotect(send.protect(rtp(100, b"\xd5" * 160)))


def test_a_short_packet_is_rejected():
    _, recv = session_pair()
    with pytest.raises(srtp.SrtpError):
        recv.unprotect(b"\x80\x00\x00\x01" + b"\x00" * 10)


def test_each_packet_gets_a_different_keystream():
    """Same payload, different sequence -> different ciphertext, or the whole
    construction is a two-time pad."""
    send, _ = session_pair()
    a = send.protect(rtp(100, b"\xd5" * 160))
    b = send.protect(rtp(101, b"\xd5" * 160))
    assert a[12:172] != b[12:172]


def test_a_stream_of_packets_round_trips_in_order():
    send, recv = session_pair()
    for seq in range(200, 260):
        packet = rtp(seq, bytes([seq & 0xFF]) * 160)
        assert recv.unprotect(send.protect(packet)) == packet


def test_rollover_counter_advances_across_a_sequence_wrap():
    send, recv = session_pair()
    for seq in (0xFFFE, 0xFFFF, 0x0000, 0x0001):
        packet = rtp(seq, b"\xd5" * 160)
        assert recv.unprotect(send.protect(packet)) == packet
    assert send.roc == 1, "ROC must tick on the wrap or the index desynchronises"
    assert recv.roc == 1


# -- SDES ------------------------------------------------------------------


def test_crypto_line_round_trips_through_its_own_parser():
    key, salt = srtp.new_key_salt()
    tag, k2, s2 = srtp.parse_crypto_line(srtp.crypto_line(key, salt, tag=1))
    assert (tag, k2, s2) == (1, key, salt)


def test_crypto_line_shape_is_what_rfc4568_specifies():
    line = srtp.crypto_line(bytes(16), bytes(14), tag=1)
    assert line.startswith("a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:")


def test_parser_accepts_a_line_with_a_lifetime_suffix():
    """Linphone appends |2^20 and this must not choke on it."""
    key, salt = srtp.new_key_salt()
    line = srtp.crypto_line(key, salt) + "|2^20|1:4"
    _, k2, s2 = srtp.parse_crypto_line(line)
    assert (k2, s2) == (key, salt)


def test_parser_rejects_an_unsupported_profile():
    with pytest.raises(srtp.SrtpError):
        srtp.parse_crypto_line("a=crypto:1 AES_CM_128_HMAC_SHA1_32 inline:AAAA")


def test_parser_rejects_wrong_length_key_material():
    import base64
    bad = base64.b64encode(b"\x00" * 20).decode()
    with pytest.raises(srtp.SrtpError):
        srtp.parse_crypto_line(f"a=crypto:1 {srtp.SRTP_PROFILE} inline:{bad}")


def test_session_rejects_wrong_sized_master_material():
    with pytest.raises(srtp.SrtpError):
        srtp.SrtpSession(bytes(15), bytes(14))
    with pytest.raises(srtp.SrtpError):
        srtp.SrtpSession(bytes(16), bytes(13))
