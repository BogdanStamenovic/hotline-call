"""Both duplex engines at the line's real rate, with a fake model process.

The fake speaks the engines' pipe protocol (READY on stderr, float32 frames in on
stdin, float32 audio out on stdout) for a given EngineSpec: it records every
input frame and echoes it back resampled to the engine's output rate, in uneven
chunks the way Nemotron writes codec blocks. That measures, without a GPU, what
the MODEL is fed and what reaches his phone. `_transcribe` and `free_gpu` are
stubbed: the real ones load Whisper and talk to ollama.
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import numpy as np
import pytest

from hotline_ios import duplexcall
from hotline_ios.media import codec, duplex, pcm, rtp, srtp

needs_opus = pytest.mark.skipif(codec.opus_library() is None, reason="no libopus")

FAKE_ENGINE = r'''
import sys, numpy as np, soxr
out_path, in_frame, in_rate, out_rate = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
rec = open(out_path, "wb")
back = soxr.ResampleStream(in_rate, out_rate, 1, dtype="float32")
sys.stderr.write("READY\n"); sys.stderr.flush()
need, held = in_frame * 4, []
while True:
    buf = b""
    while len(buf) < need:
        chunk = sys.stdin.buffer.read(need - len(buf))
        if not chunk:
            sys.exit(0)
        buf += chunk
    rec.write(buf); rec.flush()
    held.append(back.resample_chunk(np.frombuffer(buf, dtype="<f4")))
    if len(held) >= 3:       # ~240 ms blocks, like a codec chunk
        sys.stdout.buffer.write(np.concatenate(held).astype("<f4").tobytes())
        sys.stdout.buffer.flush()
        held = []
'''


def band_db(audio, rate, freq, ref=1000.0):
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    freqs = np.fft.rfftfreq(audio.size, 1 / rate)

    def at(f):
        return float(spectrum[(freqs > f - 60) & (freqs < f + 60)].sum()) + 1e-20

    return 10 * np.log10(at(freq) / at(ref))


def tones(seconds, rate, freqs=(1000, 6000, 10000)):
    t = np.arange(int(seconds * rate)) / rate
    return sum(0.12 * np.sin(2 * np.pi * f * t) for f in freqs if f < rate / 2).astype(np.float32)


def run_call(tmp_path, monkeypatch, choice: codec.Choice, engine: str):
    script = tmp_path / "fake_engine.py"
    script.write_text(FAKE_ENGINE)
    heard_by_model = tmp_path / "model_in.f32"
    real = duplexcall.DuplexEngine

    def make(cmd, spec=duplex.PERSONAPLEX, **kw):
        return real([sys.executable, str(script), str(heard_by_model), str(spec.in_frame),
                     str(spec.in_rate), str(spec.out_rate)], spec=spec, **kw)

    monkeypatch.setattr(duplexcall, "DuplexEngine", make)
    monkeypatch.setattr(duplexcall, "free_gpu", lambda: None)
    monkeypatch.setattr(duplexcall.DuplexCall, "_transcribe", lambda self, st, rate=8000: [])
    began = time.monotonic()
    dc = duplexcall.DuplexCall(note=lambda *a: None, deliver=lambda s: None,
                               hung_up=lambda: time.monotonic() - began > 4.5,
                               record_dir=tmp_path, engine=engine)
    assert dc.wait_ready(20)

    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ours.bind(("127.0.0.1", 0))
    pkey, psalt = srtp.new_key_salt()
    okey, osalt = srtp.new_key_salt()
    port = phone.getsockname()[1]
    attrs = "a=rtpmap:96 opus/48000/2\r\n" if choice.name == "opus" else ""
    reply = ("SIP/2.0 200 OK\r\n\r\nv=0\r\nc=IN IP4 127.0.0.1\r\n"
             f"m=audio {port} RTP/SAVP {choice.pt}\r\n{srtp.crypto_line(pkey, psalt)}\r\n{attrs}")
    peer = codec.open_codec(choice)
    received: list[bytes] = []

    def phone_side():
        send = srtp.SrtpSession(pkey, psalt)
        recv = srtp.SrtpSession(okey, osalt)
        wire = pcm.from_model(tones(3.0, 48000), 48000, peer.rate)
        n = peer.frame_samples * 2
        phone.settimeout(0.001)
        for seq, i in enumerate(range(0, len(wire) - n + 1, n)):
            pkt = rtp.build_packet(seq, seq * peer.ts_step, 0xFEED, peer.encode(wire[i:i + n]), peer.pt)
            phone.sendto(send.protect(pkt), ours.getsockname())
            deadline = time.monotonic() + 0.02
            while time.monotonic() < deadline:
                try:
                    data, _ = phone.recvfrom(4096)
                except (TimeoutError, BlockingIOError):
                    continue
                parsed = rtp.parse_packet(srtp_unprotect(recv, data))
                if parsed is not None:
                    received.append(peer.decode(parsed[3]))

    talker = threading.Thread(target=phone_side, daemon=True)
    talker.start()
    dc(reply, ours, okey, osalt)
    talker.join(5)
    model_in = np.fromfile(heard_by_model, dtype="<f4")
    back = np.frombuffer(b"".join(received), dtype="<i2").astype(np.float32) / 32768.0
    return dc, model_in, back, peer.rate


def srtp_unprotect(session, data):
    try:
        return session.unprotect(data)
    except srtp.SrtpError:
        return b""


ENGINES = [("personaplex", duplex.PERSONAPLEX), ("nemotron", duplex.NEMOTRON)]


@needs_opus
@pytest.mark.parametrize("engine,spec", ENGINES)
def test_on_opus_the_model_hears_above_4khz(tmp_path, monkeypatch, engine, spec):
    dc, model_in, back, rate = run_call(tmp_path, monkeypatch, codec.Choice("opus", 96, 48000),
                                        engine)
    assert dc.engine.wire_rate == 48000
    assert dc.stats["codec"].startswith("Opus")
    tail = model_in[spec.in_rate:]          # past the first second
    assert tail.size > spec.in_rate
    assert band_db(tail, spec.in_rate, 6000) > -6
    # And the echo comes back to his phone with its top end. The fake echoes its
    # INPUT, so the highest tone it can return is set by the lower of the two
    # rates -- Nemotron's 16 kHz input caps it at 8 kHz, not its 22.05 kHz output.
    top = 10000 if min(spec.in_rate, spec.out_rate) > 20000 else 6000
    echoed = back[rate:]
    assert echoed.size > rate // 2
    assert band_db(echoed, rate, top) > -6


@pytest.mark.parametrize("engine,spec", ENGINES)
def test_on_g711_the_model_gets_the_narrow_line_it_always_did(tmp_path, monkeypatch, engine, spec):
    dc, model_in, _back, _ = run_call(tmp_path, monkeypatch, codec.PCMU, engine)
    assert dc.engine.wire_rate == 8000
    assert band_db(model_in[spec.in_rate:], spec.in_rate, 6000) < -30
