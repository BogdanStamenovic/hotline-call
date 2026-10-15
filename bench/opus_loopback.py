"""Opus vs G.711 end to end, without a phone: a fake Linphone answers our INVITE.

The whole path that runs on a real call, minus linphone.org: `SipTransport`
registers and invites over UDP, a stand-in phone answers the actual offer with
`codec.answer_lines` (as Linphone would, its own preference first), and the
answered-call handler builds a `VoiceCall` from the 200 OK exactly the way
`conversation.py` does. Audio then goes both ways over SRTP.

Measured: which codec was negotiated, how much of a 1/6/10/14 kHz test signal
survives each direction (dB relative to 1 kHz), and what the codec costs per
20 ms frame in the pump.

    python bench/opus_loopback.py              # phone that speaks Opus
    python bench/opus_loopback.py --g711-phone # phone that only has G.711

Exit 0 when the result matches what that phone should get.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
import time

import numpy as np

from hotline_ios.media import codec, pcm, rtp, srtp, voicecall
from hotline_ios.ring.base import CallTarget
from hotline_ios.ring.sip import SipTransport, header_of

TONES = [1000, 6000, 10000, 14000]


def tones(seconds: float, rate: int) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return sum(0.12 * np.sin(2 * np.pi * f * t) for f in TONES if f < rate / 2).astype(np.float32)


def band_db(audio: np.ndarray, rate: int, freq: float) -> float:
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    freqs = np.fft.rfftfreq(audio.size, 1 / rate)
    if freq >= rate / 2:
        return float("nan")

    def at(f):
        return float(spectrum[(freqs > f - 60) & (freqs < f + 60)].sum()) + 1e-20

    return 10 * np.log10(at(freq) / at(1000))


class FakeLinphone(threading.Thread):
    """REGISTER 401/200, then INVITE -> 180 -> 200 with an SDP answer, then media."""

    daemon = True

    def __init__(self, codecs: str) -> None:
        super().__init__()
        self.codecs = codecs
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media.bind(("127.0.0.1", 0))
        self.key, self.salt = srtp.new_key_salt()
        self.offer = ""
        self.choice: codec.Choice | None = None
        self.their_key = self.their_salt = b""
        self.their_media: tuple[str, int] | None = None
        self.answered = threading.Event()
        self.stopping = threading.Event()

    def reply(self, request, addr, status, extra=(), body=""):
        lines = [f"SIP/2.0 {status}"]
        for name in ("Via", "From", "To", "Call-ID", "CSeq"):
            value = header_of(request, name)
            if value:
                if name == "To" and ";tag=" not in value:
                    value += ";tag=phone"
                lines.append(f"{name}: {value}")
        lines.extend(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        self.sock.sendto(("\r\n".join(lines) + "\r\n\r\n" + body).encode(), addr)

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            request = data.decode(errors="replace")
            method = request.split(" ", 1)[0].upper()
            if method == "REGISTER":
                if "Authorization:" in request:
                    self.reply(request, addr, "200 OK")
                else:
                    challenge = ('WWW-Authenticate: Digest realm="t", nonce="abc", '
                                 'qop="auth", algorithm=MD5')
                    self.reply(request, addr, "401 Unauthorized", [challenge])
            elif method == "INVITE":
                self.offer = request.split("\r\n\r\n", 1)[1]
                self.reply(request, addr, "180 Ringing")
                self.reply(request, addr, "200 OK", ["Contact: <sip:phone@127.0.0.1>"],
                           self.answer())
            elif method == "BYE":
                self.reply(request, addr, "200 OK")

    def answer(self) -> str:
        made = codec.answer_lines(self.offer, self.codecs)
        assert made is not None, "the offer had nothing this phone can carry"
        self.choice, pts, attrs = made
        for line in self.offer.split("\r\n"):
            if line.startswith("a=crypto:"):
                _tag, self.their_key, self.their_salt = srtp.parse_crypto_line(line)
            elif line.startswith("c=IN IP4 "):
                host = line.split()[2]
            elif line.startswith("m=audio "):
                port = int(line.split()[1])
        self.their_media = (host, port)
        self.answered.set()
        return (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Talk\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {self.media.getsockname()[1]} RTP/SAVP {pts} 101\r\n"
            f"{srtp.crypto_line(self.key, self.salt)}\r\n"
            + "".join(f"{a}\r\n" for a in attrs)
            + "a=rtpmap:101 telephone-event/8000\r\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--g711-phone", action="store_true",
                        help="the phone has only PCMU/PCMA")
    args = parser.parse_args(argv)
    phone = FakeLinphone("pcmu,pcma" if args.g711_phone else "opus,pcmu,pcma")
    phone.start()
    result: dict = {}

    def on_answer(reply, media_sock, key, salt):
        answer = voicecall.parse_sdp_answer(reply)
        call = voicecall.VoiceCall(media_sock, answer, key, salt)
        result["codec"] = call.codec.describe()
        result["rate"] = call.rate
        try:
            peer = codec.open_codec(phone.choice)
            # Phone -> us: decoded at the line rate, and as Whisper gets it.
            call.record()
            send = srtp.SrtpSession(phone.key, phone.salt)
            wire = pcm.from_model(tones(1.5, 48000), 48000, peer.rate)
            n = peer.frame_samples * 2
            for seq, i in enumerate(range(0, len(wire) - n + 1, n)):
                packet = rtp.build_packet(seq, seq * peer.ts_step, 0xFEED,
                                          peer.encode(wire[i:i + n]), peer.pt)
                phone.media.sendto(send.protect(packet), phone.their_media)
                time.sleep(0.02)
            model = call.receive_audio(0.3)
            line = np.frombuffer(call.recorded(), dtype="<i2").astype(np.float32) / 32768.0
            result["in_line"] = {f: band_db(line[call.rate // 5:], call.rate, f) for f in TONES}
            whole = pcm.to_model(call.recorded(), call.rate)
            result["in_model"] = {f: band_db(whole[3200:], 16000, f) for f in TONES}
            del model

            # Us -> phone: TTS-like 24 kHz in, his decoder out.
            phone.media.settimeout(0.005)
            while True:   # drop the idle frames already queued at his end
                try:
                    phone.media.recvfrom(4096)
                except TimeoutError:
                    break
            phone.media.settimeout(1.0)
            heard: list[bytes] = []
            recv = srtp.SrtpSession(phone.their_key, phone.their_salt)
            reader = threading.Thread(target=lambda: [
                heard.append(peer.decode(rtp.parse_packet(recv.unprotect(
                    phone.media.recvfrom(4096)[0]))[3])) for _ in range(75)])
            reader.start()
            began = time.perf_counter()
            call.send_audio(tones(1.5, 24000), rate=24000)
            result["send_wall_s"] = time.perf_counter() - began
            reader.join(3)
            out = np.frombuffer(b"".join(heard), dtype="<i2").astype(np.float32) / 32768.0
            skip = peer.rate // 5
            result["out_phone"] = {f: band_db(out[skip:], peer.rate, f) for f in TONES}
            result["stats"] = call.stats()
        finally:
            call.close()
            call.pump.join(1.0)

    transport = SipTransport(user="bogdan", password="pw", domain="127.0.0.1",
                             peer="sip:him@127.0.0.1", port=phone.port, transport="udp",
                             on_answer=on_answer)
    transport.media_host = "127.0.0.1"
    try:
        asyncio.run(transport.ring(CallTarget(device="phone", reason="bench",
                                              caller_id="bench"), timeout=10))
    except Exception as exc:  # an answered ring is reported however ring() likes
        result.setdefault("ring", f"{type(exc).__name__}: {exc}")
    phone.stopping.set()

    offer_m = next(line for line in phone.offer.split("\r\n") if line.startswith("m=audio"))
    print(f"offer      : {offer_m}")
    print(f"negotiated : {phone.choice}")
    print(f"call codec : {result.get('codec')} at {result.get('rate')} Hz")
    for key, label in (("in_line", "phone->us, line"), ("in_model", "phone->us, whisper 16k"),
                       ("out_phone", "us->phone, decoded")):
        row = result.get(key, {})
        print(f"{label:24s}" + "  ".join(f"{f // 1000:>2d}k {row.get(f, float('nan')):6.1f} dB"
                                         for f in TONES))
    print(f"stats      : {result.get('stats')}")

    # Codec cost per frame, as the pump pays it.
    c = codec.open_codec(phone.choice)
    frame = pcm.from_model(tones(0.02, c.rate), c.rate, c.rate)
    payload = c.encode(frame)
    t0 = time.perf_counter()
    for _ in range(500):
        c.encode(frame)
    t1 = time.perf_counter()
    for _ in range(500):
        c.decode(payload)
    t2 = time.perf_counter()
    print(f"per frame  : encode {(t1 - t0) / 500 * 1e3:.3f} ms, decode {(t2 - t1) / 500 * 1e3:.3f} ms,"
          f" payload {len(payload)} B")

    expected = "PCMU" if args.g711_phone else "opus"
    ok = phone.choice is not None and phone.choice.name == expected
    if not args.g711_phone:
        ok = ok and result.get("in_model", {}).get(6000, -99) > -6
        ok = ok and result.get("out_phone", {}).get(10000, -99) > -6
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
