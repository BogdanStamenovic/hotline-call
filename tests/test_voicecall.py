"""The answered-call media path.

The bug this file exists to catch is the SDES key asymmetry: our offer's key
encrypts what we send, his answer's key decrypts what he sends. Wiring both
directions to the same key produces a call that authenticates its own packets
perfectly and cannot read one of his -- and every single-ended test passes.
"""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
import pytest

from hotline_ios.media import pcm, rtp, srtp, voicecall


def sdp_200(host="192.168.1.50", port=7078, key=None, salt=None, pts="0 8 101"):
    if key is None:
        key, salt = srtp.new_key_salt()
    crypto = srtp.crypto_line(key, salt)
    return (
        "SIP/2.0 200 OK\r\n"
        "To: <sip:b0g13a@sip.linphone.org>;tag=abc\r\n"
        "Content-Type: application/sdp\r\n"
        "\r\n"
        "v=0\r\n"
        "o=- 1 1 IN IP4 " + host + "\r\n"
        "s=Talk\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/SAVP {pts}\r\n"
        f"{crypto}\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    ), key, salt


# -- SDP parsing -----------------------------------------------------------


def test_parses_destination_and_key_from_a_200():
    message, key, salt = sdp_200()
    a = voicecall.parse_sdp_answer(message)
    assert a.address == ("192.168.1.50", 7078)
    assert (a.srtp_key, a.srtp_salt) == (key, salt)
    assert voicecall.PT_PCMU in a.payload_types


def test_media_level_connection_line_wins():
    message, _, _ = sdp_200()
    message = message.replace("c=IN IP4 192.168.1.50",
                              "c=IN IP4 10.0.0.1", 1) + "c=IN IP4 172.16.0.9\r\n"
    assert voicecall.parse_sdp_answer(message).host == "172.16.0.9"


def test_a_zero_port_is_a_declined_media_stream_not_a_destination():
    message, _, _ = sdp_200(port=0)
    with pytest.raises(voicecall.SdpError, match="declined media"):
        voicecall.parse_sdp_answer(message)


def test_no_crypto_line_is_refused_rather_than_falling_back_to_plain_rtp():
    message, _, _ = sdp_200()
    message = "\r\n".join(l for l in message.split("\r\n") if not l.startswith("a=crypto"))
    with pytest.raises(voicecall.SdpError, match="crypto"):
        voicecall.parse_sdp_answer(message)


def test_an_unimplemented_crypto_profile_is_skipped_for_one_we_have():
    key, salt = srtp.new_key_salt()
    message, _, _ = sdp_200(key=key, salt=salt)
    message = message.replace("a=crypto:1",
                              "a=crypto:1 AES_256_CM_HMAC_SHA1_80 inline:QUJD\r\na=crypto:2", 1)
    a = voicecall.parse_sdp_answer(message)
    assert (a.srtp_key, a.srtp_salt) == (key, salt)


def test_a_far_end_without_pcmu_is_refused():
    message, _, _ = sdp_200(pts="9 101")
    with pytest.raises(voicecall.SdpError, match="PCMU"):
        voicecall.parse_sdp_answer(message)


# -- the media path over real sockets --------------------------------------


def call_pair():
    """Us and a stand-in for his phone, on real UDP sockets."""
    ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); ours.bind(("127.0.0.1", 0))
    theirs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); theirs.bind(("127.0.0.1", 0))
    our_key, our_salt = srtp.new_key_salt()
    their_key, their_salt = srtp.new_key_salt()
    answer = voicecall.SdpAnswer(*theirs.getsockname(), their_key, their_salt, [0])
    call = voicecall.VoiceCall(ours, answer, our_key, our_salt)
    return call, theirs, (our_key, our_salt), (their_key, their_salt), ours.getsockname()


def test_what_we_send_is_readable_with_OUR_key_and_not_his():
    call, theirs, ours_ks, theirs_ks, _ = call_pair()
    tone = (np.sin(np.arange(1600) * 0.05) * 0.5).astype(np.float32)
    call.send_audio(tone, rate=8000)
    theirs.settimeout(1.0)
    data, _ = theirs.recvfrom(4096)

    correct = srtp.SrtpSession(*ours_ks)
    assert rtp.parse_packet(correct.unprotect(data)) is not None

    wrong = srtp.SrtpSession(*theirs_ks)
    with pytest.raises(srtp.AuthenticationFailure):
        wrong.unprotect(data)


def test_what_he_sends_is_read_with_HIS_key():
    call, theirs, _, theirs_ks, our_addr = call_pair()
    his = srtp.SrtpSession(*theirs_ks)
    payload = pcm.ulaw_encode(b"\x00\x10" * voicecall.FRAME_SAMPLES)
    for n in range(5):
        packet = rtp.build_packet(n, n * voicecall.FRAME_SAMPLES, 0xCAFE, payload)
        theirs.sendto(his.protect(packet), our_addr)
    got = call.receive_audio(0.6)
    assert call.frames_received == 5
    assert call.auth_failures == 0
    assert got.size > 0


def test_a_packet_encrypted_with_the_wrong_key_is_counted_not_decoded():
    call, theirs, ours_ks, _, our_addr = call_pair()
    impostor = srtp.SrtpSession(*srtp.new_key_salt())
    packet = rtp.build_packet(1, 160, 0xCAFE, b"\xff" * voicecall.FRAME_SAMPLES)
    theirs.sendto(impostor.protect(packet), our_addr)
    got = call.receive_audio(0.4)
    assert call.auth_failures == 1
    assert call.frames_received == 0
    assert got.size == 0


def test_audio_survives_the_round_trip_recognisably():
    """A tone in must come back as the same tone, not noise."""
    call, theirs, ours_ks, _, _ = call_pair()
    freq, rate, n = 440.0, 8000, 8000
    tone = (np.sin(2 * np.pi * freq * np.arange(n) / rate) * 0.6).astype(np.float32)
    call.send_audio(tone, rate=rate)

    theirs.settimeout(1.0)
    reader = srtp.SrtpSession(*ours_ks)
    body = bytearray()
    while True:
        try:
            body += rtp.parse_packet(reader.unprotect(theirs.recvfrom(4096)[0]))[3]
        except (socket.timeout, TimeoutError):
            break
    back = pcm.to_model(pcm.ulaw_decode(bytes(body)), rate=8000)
    spectrum = np.abs(np.fft.rfft(back))
    peak = np.fft.rfftfreq(back.size, 1 / 16000)[int(np.argmax(spectrum))]
    assert abs(peak - freq) < 15, f"440 Hz came back as {peak:.0f} Hz"


def test_sending_is_paced_to_real_time_rather_than_flooded():
    """Half a second of audio must take about half a second to send.

    Without pacing the whole utterance leaves in milliseconds and the far end's
    jitter buffer discards it as a flood -- which looks like silence on his
    phone, not like an error anywhere here.
    """
    call, _theirs, _, _, _ = call_pair()
    audio = np.zeros(4000, dtype=np.float32)  # 0.5 s at 8 kHz
    elapsed = call.send_audio(audio, rate=8000)
    assert 0.40 < elapsed < 0.75, f"0.5 s of audio took {elapsed:.3f} s"


def test_frame_accounting_matches_the_audio_length():
    call, _theirs, _, _, _ = call_pair()
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000)  # 1.0 s
    assert call.frames_sent == 50, f"1 s at 20 ms/frame is 50 frames, got {call.frames_sent}"


def test_receive_returns_empty_rather_than_hanging_when_nothing_arrives():
    call, _theirs, _, _, _ = call_pair()
    began = time.monotonic()
    got = call.receive_audio(0.3)
    assert got.size == 0
    assert time.monotonic() - began < 1.5


# -- turn taking -----------------------------------------------------------


def feed(theirs_sock, their_keys, our_addr, audio, rate=8000):
    """Push float32 audio at us as SRTP frames, as his phone would."""
    session = srtp.SrtpSession(*their_keys)
    wire = pcm.from_model(audio.astype(np.float32), rate=rate, out_rate=8000)
    ulaw = pcm.ulaw_encode(wire)
    n = voicecall.FRAME_SAMPLES
    for i in range(0, len(ulaw) - n + 1, n):
        seq = i // n
        packet = rtp.build_packet(seq & 0xFFFF, seq * n, 0xFEED, ulaw[i:i + n])
        theirs_sock.sendto(session.protect(packet), our_addr)


def speech(seconds, rate=8000, amp=0.35):
    t = np.arange(int(seconds * rate)) / rate
    # Voiced speech is not a pure tone; a couple of harmonics plus a wobble
    # keeps the RMS in a realistic place for the endpointer to judge.
    sig = (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 360 * t))
    return (sig * amp * (1 + 0.2 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)


def quiet(seconds, rate=8000, amp=0.0008):
    return (np.random.randn(int(seconds * rate)) * amp).astype(np.float32)



def feed_during(theirs_sock, their_keys, our_addr, audio, delay=0.15, rate=8000):
    """Start streaming at us shortly AFTER the send begins.

    Pre-feeding is not how an interruption happens, and it no longer works:
    send_audio flushes stale inbound audio before it starts speaking, precisely
    so a reply cannot be cut off by something that arrived while we were quiet.
    """
    def run():
        time.sleep(delay)
        feed(theirs_sock, their_keys, our_addr, audio, rate=rate)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

def test_a_turn_ends_after_he_stops_talking():
    call, theirs, _, their_keys, our_addr = call_pair()
    clip = np.concatenate([quiet(0.4), speech(1.2), quiet(1.4)])
    feed(theirs, their_keys, our_addr, clip)
    audio, reason = call.receive_turn(max_seconds=6.0, silence_ms=600)
    assert reason == "endpointed", f"expected endpointing, got {reason}"
    assert audio.size > 0


def test_a_turn_of_pure_silence_says_so_rather_than_pretending_he_spoke():
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(2.0))
    audio, reason = call.receive_turn(max_seconds=2.5, silence_ms=600)
    assert reason in ("silence", "timeout"), reason
    assert audio.size > 0, "the audio still arrived; it just was not speech"


def test_nothing_arriving_at_all_is_distinguished_from_silence():
    call, _theirs, _, _, _ = call_pair()
    audio, reason = call.receive_turn(max_seconds=0.6)
    assert reason == "no-audio"
    assert audio.size == 0


def test_a_short_pause_mid_sentence_does_not_end_the_turn():
    """The known weakness of energy endpointing, held to a documented bound."""
    call, theirs, _, their_keys, our_addr = call_pair()
    clip = np.concatenate([quiet(0.4), speech(0.8), quiet(0.3), speech(0.8), quiet(1.2)])
    feed(theirs, their_keys, our_addr, clip)
    audio, reason = call.receive_turn(max_seconds=8.0, silence_ms=800)
    assert reason == "endpointed"
    # The whole utterance, both halves, not just the first clause.
    assert audio.size / 16000 > 1.8, f"turn was cut short at {audio.size/16000:.2f}s"


def test_a_turn_is_capped_so_a_stuck_stream_cannot_hang_the_call():
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, speech(4.0))
    began = time.monotonic()
    _audio, reason = call.receive_turn(max_seconds=1.0, silence_ms=800)
    assert time.monotonic() - began < 2.5
    assert reason == "timeout"


# -- barge-in --------------------------------------------------------------


def test_speaking_uninterrupted_plays_the_whole_utterance():
    call, _theirs, _, _, _ = call_pair()
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False
    assert call.frames_sent == 50


def test_he_can_talk_over_us_and_we_stop():
    call, theirs, _, their_keys, our_addr = call_pair()
    # A real call primes with silence first, which is when the line gets
    # measured. Without that the barge-in is deliberately disabled.
    feed(theirs, their_keys, our_addr, quiet(1.0))
    call.send_silence(0.5, calibrate=True)
    # Two seconds of us talking, and he cuts in a moment after we start.
    feed_during(theirs, their_keys, our_addr, speech(1.5))
    spent = call.send_audio(np.zeros(16000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is True, "he talked over us and we kept going"
    assert spent < 1.5, f"took {spent:.2f}s to stop"


def test_quiet_line_noise_does_not_count_as_an_interruption():
    """One loud frame is a click. Only a sustained run is a person."""
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(1.2))
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False


def test_the_barge_in_audio_is_kept_and_becomes_his_next_turn():
    """The bug this guards: dropping what we consumed while detecting the
    interruption silently eats the first syllable of every interruption."""
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(1.0))
    call.send_silence(0.5, calibrate=True)
    feed_during(theirs, their_keys, our_addr, speech(1.5))
    call.send_audio(np.zeros(16000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted
    assert call._pending_rx, "the interrupting audio was thrown away"
    kept = len(call._pending_rx)

    audio, reason = call.receive_turn(max_seconds=2.0, silence_ms=400)
    assert audio.size >= kept * voicecall.FRAME_SAMPLES * 2, \
        "his opening words did not make it into the turn"
    assert reason in ("endpointed", "timeout")
    assert call._pending_rx == [], "pending audio must be consumed exactly once"


def test_a_non_interruptible_send_ignores_him_talking():
    """Fillers and goodbyes are sent non-interruptibly on purpose."""
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, speech(1.0))
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000)
    assert call.interrupted is False
    assert call.frames_sent == 50


def test_a_noisy_line_does_not_trigger_a_barge_in():
    """The regression that made him hear nothing at all.

    A live line is never as quiet as a synthesised clip -- room noise, comfort
    noise, and our own audio echoing back through the relay all sit above a
    threshold chosen for clean audio. With a fixed bar this cut off every
    utterance about 160 ms in.
    """
    call, theirs, _, their_keys, our_addr = call_pair()
    # Line noise well above the old fixed MAX_THRESHOLD, but it is not speech.
    noisy = quiet(3.0, amp=0.03)
    feed(theirs, their_keys, our_addr, noisy)
    call.send_silence(0.6, calibrate=True)   # measure that noise
    assert call.line_floor is not None, "the floor was never measured"
    feed(theirs, their_keys, our_addr, noisy)
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False, "line noise was mistaken for him talking"
    assert call.frames_sent >= 50 + 30, "the utterance was cut short"


def test_real_speech_still_interrupts_over_that_same_noise():
    """The other half: calibrating must not make it deaf to an actual person."""
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(2.0, amp=0.03))
    call.send_silence(0.6, calibrate=True)
    feed_during(theirs, their_keys, our_addr, speech(2.0))
    call.send_audio(np.zeros(16000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is True, "he talked and we did not stop"


def test_an_unmeasured_line_refuses_to_interrupt_rather_than_guessing():
    call, theirs, _, their_keys, our_addr = call_pair()
    assert call.line_floor is None
    assert call._barge_threshold() is None
    feed(theirs, their_keys, our_addr, speech(1.5))
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False
    assert call.frames_sent == 50, "the whole utterance must still go out"


def test_retained_audio_is_capped_so_a_long_reply_does_not_hoard_his_echo():
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(4.0, amp=0.03))
    call.send_audio(np.zeros(24000, dtype=np.float32), rate=8000, interruptible=True)
    assert len(call._pending_rx) <= voicecall.VoiceCall.PENDING_RX_CAP


def test_audio_that_arrived_before_we_spoke_cannot_interrupt_us():
    """He said something, the agent thought about it, and then we answered.
    What he said must not cut off the answer to it."""
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(1.0))
    call.send_silence(0.5, calibrate=True)
    feed(theirs, their_keys, our_addr, speech(2.0))   # queued while we were quiet
    time.sleep(0.1)
    call.send_audio(np.zeros(8000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False, "stale audio cut off the reply to it"
    assert call.frames_sent >= 50 + 25


# -- voice profiling: telling him from the room ----------------------------


def test_background_speech_does_not_interrupt_once_he_is_enrolled():
    """His diagnosis, 2026-09-08: people talking in the room kept cutting him off.

    Background conversation IS speech, so it clears a noise-floor threshold
    easily. What it does not clear is a threshold set relative to a voice
    speaking into the handset.
    """
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(1.0))
    call.send_silence(0.5, calibrate=True)
    call.enrol_voice(speech(2.0, rate=16000, amp=0.35), rate=16000)
    assert call.his_level is not None

    # Someone across the room: same kind of sound, a fraction of the level.
    feed_during(theirs, their_keys, our_addr, speech(1.5, amp=0.05))
    call.send_audio(np.zeros(16000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is False, "the room interrupted him"


def test_he_still_interrupts_after_enrolment():
    call, theirs, _, their_keys, our_addr = call_pair()
    feed(theirs, their_keys, our_addr, quiet(1.0))
    call.send_silence(0.5, calibrate=True)
    call.enrol_voice(speech(2.0, rate=16000, amp=0.35), rate=16000)
    feed_during(theirs, their_keys, our_addr, speech(2.0, amp=0.35))
    call.send_audio(np.zeros(16000, dtype=np.float32), rate=8000, interruptible=True)
    assert call.interrupted is True, "he talked over us and we did not stop"


def test_enrolment_ignores_a_clip_too_short_to_learn_from():
    call, _theirs, _, _, _ = call_pair()
    call.enrol_voice(speech(0.2, rate=16000), rate=16000)
    assert call.his_level is None


def test_the_envelope_describes_timbre_not_loudness():
    """Same sound at two volumes must look the same, or the check is just
    another energy threshold wearing a hat."""
    loud = speech(1.0, rate=16000, amp=0.4)
    soft = speech(1.0, rate=16000, amp=0.02)
    a = voicecall.VoiceCall._envelope(loud, 8)
    b = voicecall.VoiceCall._envelope(soft, 8)
    assert float(np.dot(a, b)) > 0.99


# -- chunked transcription -------------------------------------------------


def test_phrases_are_handed_over_at_pauses_while_he_is_still_talking():
    call, theirs, _, their_keys, our_addr = call_pair()
    got: list = []
    clip = np.concatenate([quiet(0.3), speech(0.9), quiet(0.5), speech(0.9), quiet(1.2)])
    feed(theirs, their_keys, our_addr, clip)
    audio, reason = call.receive_turn(max_seconds=8.0, silence_ms=900,
                                      chunk_at_ms=300, on_chunk=got.append)
    assert reason == "endpointed"
    assert got, "no phrase was emitted before the end of the turn"


def test_a_chunked_turn_returns_only_the_untranscribed_tail():
    """Returning everything would transcribe the first half of his sentence
    twice and paste it in front of itself."""
    call, theirs, _, their_keys, our_addr = call_pair()
    got: list = []
    clip = np.concatenate([quiet(0.3), speech(0.9), quiet(0.5), speech(0.9), quiet(1.2)])
    feed(theirs, their_keys, our_addr, clip)
    tail, _ = call.receive_turn(max_seconds=8.0, silence_ms=900,
                                chunk_at_ms=300, on_chunk=got.append)
    emitted = sum(c.size for c in got)
    assert emitted > 0
    assert tail.size < emitted + 16000, "the tail looks like the whole utterance again"


def test_a_raising_chunk_handler_does_not_kill_the_turn():
    call, theirs, _, their_keys, our_addr = call_pair()
    def boom(_):
        raise RuntimeError("transcriber fell over")
    clip = np.concatenate([quiet(0.3), speech(0.9), quiet(0.5), speech(0.9), quiet(1.2)])
    feed(theirs, their_keys, our_addr, clip)
    _audio, reason = call.receive_turn(max_seconds=8.0, silence_ms=900,
                                       chunk_at_ms=300, on_chunk=boom)
    assert reason == "endpointed", "a broken transcriber must not end the call"


def test_the_outbound_stream_never_stops_while_we_listen():
    """The cause of every "it degrades after the first answer" report.

    An RTP stream that stops is not a quiet stream, it is a dead one. His
    Linphone showed the quality meter dropping and recovering on every turn, and
    each restart resets the far end's jitter buffer -- heard as the start of our
    next sentence being chopped.
    """
    call, theirs, ours_ks, their_keys, our_addr = call_pair()
    feed_during(theirs, their_keys, our_addr, quiet(1.5), delay=0.05)
    before = call.frames_sent
    call.receive_turn(max_seconds=1.0, silence_ms=800)
    sent = call.frames_sent - before
    # 1 s at 20 ms a frame is 50; allow slack for scheduling either way.
    assert 35 <= sent <= 65, f"sent {sent} frames while listening for 1 s"


def test_the_keepalive_frames_are_real_srtp_his_phone_can_read():
    call, theirs, ours_ks, _their, _addr = call_pair()
    call.receive_turn(max_seconds=0.4, silence_ms=800)
    theirs.settimeout(0.5)
    reader = srtp.SrtpSession(*ours_ks)
    data, _ = theirs.recvfrom(4096)
    parsed = rtp.parse_packet(reader.unprotect(data))
    assert parsed is not None, "keepalive frames must be valid SRTP, not padding"


def test_rtp_timestamps_stay_continuous_across_a_listening_gap():
    """Contiguous timestamps with a wall-clock hole make the far end treat the
    next utterance as arriving from the past."""
    call, theirs, ours_ks, _their, _addr = call_pair()
    call.send_audio(np.zeros(1600, dtype=np.float32), rate=8000)   # 0.2 s
    call.receive_turn(max_seconds=0.6, silence_ms=800)
    call.send_audio(np.zeros(1600, dtype=np.float32), rate=8000)
    theirs.settimeout(0.5)
    reader = srtp.SrtpSession(*ours_ks)
    stamps, seqs = [], []
    while True:
        try:
            # parse_packet returns (payload_type, seq, timestamp, payload)
            _pt, seq, ts, _payload = rtp.parse_packet(
                reader.unprotect(theirs.recvfrom(4096)[0]))
            seqs.append(seq); stamps.append(ts)
        except Exception:
            break
    assert len(stamps) > 20, f"only {len(stamps)} packets crossed the wire"
    assert {b - a for a, b in zip(seqs, seqs[1:])} == {1}, "sequence numbers skipped"
    gaps = {b - a for a, b in zip(stamps, stamps[1:])}
    assert gaps == {voicecall.FRAME_SAMPLES}, f"timestamp discontinuity: {sorted(gaps)}"
