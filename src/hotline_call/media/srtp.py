"""SRTP: AES_CM_128_HMAC_SHA1_80, the profile his phone actually insists on.

**Why this exists at all.** `ring/sip.py` offers `RTP/SAVP` with an SDES crypto
line because a plain `RTP/AVP` offer is answered `488 Not acceptable here` --
and, because the push has already fired by then, the visible symptom is his
phone lighting up and dying about a second later. He described that as "a call
for a split second". Offering SAVP makes it ring properly, but an offer we
cannot actually honour only moves the failure to the first audio packet. This is
the half that honours it.

**Why written rather than installed.** The alternative was `baresip`, which is
present and does SRTP well -- but it is a softphone, not a library: adopting it
for the media leg means a subprocess, its own audio device model, its control
protocol, and its opinions about what a call is. What we need is 200 lines of
RFC 3711 sitting between a UDP socket and a numpy array, feeding Whisper and
cvoice rather than a speaker. The scope of the thing to write is smaller than
the scope of the thing to integrate.

**Why this is safe to hand-roll, which crypto usually is not.** Everything here
is fully specified with published test vectors, and `test_srtp.py` checks
against RFC 3711's own Appendix B rather than against itself. The primitives
(AES-CTR, HMAC-SHA1) come from `cryptography`; what is written here is the
key-derivation and IV-construction around them, which is where SRTP
implementations actually differ and where a self-consistent implementation can
be confidently wrong. A test that only proves protect/unprotect round-trip would
pass with the IV assembled backwards -- so that is deliberately not the test.

**What is NOT implemented, and matters.**

- **No replay protection.** RFC 3711 s3.3.2 wants a replay window; there is none.
  On a tailnet call to one known peer the exposure is low, but this is a real
  omission and not an oversight -- write the window before this ever faces a
  network he does not control.
- **No MKI, no <=64-bit tags, no NULL cipher, no AES-f8.** One profile only.
- **Rekeying is not handled.** `key_derivation_rate` is accepted and honoured in
  the KDF, but nothing re-derives mid-session; in practice kdr=0 (derive once)
  is what SDES peers negotiate and what Linphone sends.
- **ROC is inferred from sequence wrap, not carried.** Standard for SRTP, but it
  means a receiver that misses 2^15 consecutive packets desynchronises
  permanently. Fine for a phone call, fatal for a lossy one-way feed.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
from hashlib import sha1

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# RFC 3711 s4.3.1. These label the three keys the KDF pulls off one master key.
LABEL_RTP_ENCR = 0x00
LABEL_RTP_AUTH = 0x01
LABEL_RTP_SALT = 0x02

MASTER_KEY_LEN = 16   # AES-128
MASTER_SALT_LEN = 14  # 112 bits, per the profile
SESSION_AUTH_LEN = 20  # HMAC-SHA1 wants a full 160-bit key
AUTH_TAG_LEN = 10      # ..._HMAC_SHA1_80 -> 80 bits on the wire
SRTP_PROFILE = "AES_CM_128_HMAC_SHA1_80"


class SrtpError(Exception):
    pass


class AuthenticationFailure(SrtpError):
    """The tag did not verify. Drop the packet; never process it anyway."""


def aes_cm_keystream(key: bytes, iv: bytes, length: int) -> bytes:
    """AES in counter mode, RFC 3711 s4.1.1.

    The counter is the low 16 bits of the block, so a single keystream run is
    limited to 2^16 blocks (1 MiB) -- far beyond any RTP packet, and the reason
    this can lean on CTR mode directly instead of stepping ECB by hand.
    """
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return encryptor.update(b"\x00" * length) + encryptor.finalize()


def _srtp_iv(salt: bytes, ssrc: int, index: int) -> bytes:
    """IV = (salt * 2^16) XOR (SSRC * 2^64) XOR (index * 2^16), RFC 3711 s4.1.1.

    Written as byte-offset XORs rather than a 128-bit integer because the offsets
    are the specification: the shifts exist to land SSRC on bytes 4..7 and the
    48-bit packet index on bytes 8..13 of a 16-byte block.
    """
    iv = bytearray(16)
    iv[0:MASTER_SALT_LEN] = salt
    ssrc_bytes = struct.pack("!I", ssrc & 0xFFFFFFFF)
    for i in range(4):
        iv[4 + i] ^= ssrc_bytes[i]
    index_bytes = (index & 0xFFFFFFFFFFFF).to_bytes(6, "big")
    for i in range(6):
        iv[8 + i] ^= index_bytes[i]
    return bytes(iv)


def derive_key(master_key: bytes, master_salt: bytes, label: int,
               length: int, index: int = 0, kdr: int = 0) -> bytes:
    """One session key off the master pair, RFC 3711 s4.3.1.

    x = (label || index DIV kdr) XOR master_salt, right-aligned, then used as the
    IV for an AES-CM keystream under the master key. kdr=0 means "derive once",
    which is what SDES peers negotiate in practice.
    """
    r = 0 if kdr == 0 else index // kdr
    key_id = bytes([label]) + r.to_bytes(6, "big")  # 7 bytes
    x = bytearray(master_salt)
    # Right-aligned XOR: key_id lands in the LOW 7 bytes of the 14-byte salt.
    offset = len(x) - len(key_id)
    for i, b in enumerate(key_id):
        x[offset + i] ^= b
    iv = bytes(x) + b"\x00\x00"  # x * 2^16
    return aes_cm_keystream(master_key, iv, length)


def payload_offset(packet: bytes) -> int:
    """Where the RTP header stops, accounting for CSRCs and a header extension."""
    if len(packet) < 12:
        raise SrtpError("packet shorter than an RTP header")
    cc = packet[0] & 0x0F
    offset = 12 + 4 * cc
    if packet[0] & 0x10:  # X bit: a header extension follows the CSRC list
        if len(packet) < offset + 4:
            raise SrtpError("truncated header extension")
        ext_words = struct.unpack("!H", packet[offset + 2:offset + 4])[0]
        offset += 4 + 4 * ext_words
    if len(packet) < offset:
        raise SrtpError("header longer than the packet")
    return offset


class SrtpSession:
    """One direction of one call.

    Sender and receiver each need their own instance: SDES gives each side its
    own master key, and the rollover counters are independent.
    """

    def __init__(self, master_key: bytes, master_salt: bytes, kdr: int = 0):
        if len(master_key) != MASTER_KEY_LEN:
            raise SrtpError(f"master key must be {MASTER_KEY_LEN} bytes")
        if len(master_salt) != MASTER_SALT_LEN:
            raise SrtpError(f"master salt must be {MASTER_SALT_LEN} bytes")
        self.encr_key = derive_key(master_key, master_salt, LABEL_RTP_ENCR,
                                   MASTER_KEY_LEN, kdr=kdr)
        self.auth_key = derive_key(master_key, master_salt, LABEL_RTP_AUTH,
                                   SESSION_AUTH_LEN, kdr=kdr)
        self.salt = derive_key(master_key, master_salt, LABEL_RTP_SALT,
                               MASTER_SALT_LEN, kdr=kdr)
        self.roc = 0
        self._last_seq: int | None = None

    # -- packet index ----------------------------------------------------

    def _advance_roc(self, seq: int) -> int:
        """Track the rollover counter. RFC 3711 s3.3.1.

        The ROC is never transmitted, so both ends infer it from sequence
        wrapping. The 2^15 threshold is what makes a wrap distinguishable from
        ordinary reordering; a gap larger than that is indistinguishable from a
        wrap and this will guess wrong, which is inherent to SRTP rather than
        specific to here.
        """
        if self._last_seq is not None:
            if self._last_seq > 0x8000 and seq < (self._last_seq - 0x8000):
                self.roc = (self.roc + 1) & 0xFFFFFFFF
            elif self._last_seq < 0x8000 and seq > (self._last_seq + 0x8000):
                # A late packet from before the wrap: index it under the old ROC.
                self._last_seq = seq
                return (self.roc - 1) & 0xFFFFFFFF
        self._last_seq = seq
        return self.roc

    # -- the two operations ----------------------------------------------

    def protect(self, packet: bytes) -> bytes:
        """Plain RTP in, SRTP out."""
        offset = payload_offset(packet)
        header, payload = packet[:offset], packet[offset:]
        ssrc = struct.unpack("!I", packet[8:12])[0]
        seq = struct.unpack("!H", packet[2:4])[0]
        roc = self._advance_roc(seq)
        index = (roc << 16) | seq

        keystream = aes_cm_keystream(self.encr_key, _srtp_iv(self.salt, ssrc, index),
                                     len(payload))
        encrypted = bytes(a ^ b for a, b in zip(payload, keystream))
        body = header + encrypted
        # The tag covers the ROC too, though the ROC is not sent -- that is what
        # stops an attacker replaying a packet into a different rollover epoch.
        tag = hmac.new(self.auth_key, body + struct.pack("!I", roc), sha1).digest()
        return body + tag[:AUTH_TAG_LEN]

    def unprotect(self, packet: bytes) -> bytes:
        """SRTP in, plain RTP out. Raises rather than returning bad plaintext."""
        if len(packet) < 12 + AUTH_TAG_LEN:
            raise SrtpError("packet too short to carry an auth tag")
        body, tag = packet[:-AUTH_TAG_LEN], packet[-AUTH_TAG_LEN:]
        ssrc = struct.unpack("!I", body[8:12])[0]
        seq = struct.unpack("!H", body[2:4])[0]
        roc = self._advance_roc(seq)

        expected = hmac.new(self.auth_key, body + struct.pack("!I", roc),
                            sha1).digest()[:AUTH_TAG_LEN]
        # compare_digest, not ==, so a wrong tag cannot be found byte by byte.
        if not hmac.compare_digest(expected, tag):
            raise AuthenticationFailure("SRTP auth tag did not verify")

        offset = payload_offset(body)
        index = (roc << 16) | seq
        keystream = aes_cm_keystream(self.encr_key, _srtp_iv(self.salt, ssrc, index),
                                     len(body) - offset)
        return body[:offset] + bytes(a ^ b for a, b in zip(body[offset:], keystream))


# -- SDES, the keying half that goes in the SDP ---------------------------


def new_key_salt() -> tuple[bytes, bytes]:
    """A fresh master key and salt from the system CSPRNG."""
    return secrets.token_bytes(MASTER_KEY_LEN), secrets.token_bytes(MASTER_SALT_LEN)


def crypto_line(master_key: bytes, master_salt: bytes, tag: int = 1) -> str:
    """The `a=crypto:` attribute for an SDP offer, RFC 4568.

    The key travels in the SDP in the clear, which is SDES's well-known weakness
    and the reason `ring/sip.py` defaults to SIP over TLS: without it the media
    key is readable by anything on the path.
    """
    inline = base64.b64encode(master_key + master_salt).decode()
    return f"a=crypto:{tag} {SRTP_PROFILE} inline:{inline}"


def parse_crypto_line(line: str) -> tuple[int, bytes, bytes]:
    """Read a peer's `a=crypto:` answer back into (tag, master_key, master_salt)."""
    line = line.strip()
    if line.startswith("a="):
        line = line[2:]
    if not line.startswith("crypto:"):
        raise SrtpError("not a crypto attribute")
    parts = line[len("crypto:"):].split()
    if len(parts) < 3:
        raise SrtpError("malformed crypto attribute")
    tag_s, profile = parts[0], parts[1]
    if profile != SRTP_PROFILE:
        raise SrtpError(f"unsupported profile {profile!r}; only {SRTP_PROFILE}")
    for param in parts[2:]:
        if not param.startswith("inline:"):
            continue
        # Anything after a '|' is lifetime / MKI, neither of which is honoured here.
        material = base64.b64decode(param[len("inline:"):].split("|")[0])
        if len(material) != MASTER_KEY_LEN + MASTER_SALT_LEN:
            raise SrtpError("inline key material is the wrong length")
        return int(tag_s), material[:MASTER_KEY_LEN], material[MASTER_KEY_LEN:]
    raise SrtpError("crypto attribute carried no inline key")
