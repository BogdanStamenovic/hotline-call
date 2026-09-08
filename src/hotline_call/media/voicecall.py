"""Holding an answered SIP call open and carrying audio both ways.

`ring/sip.py` deliberately hangs up the instant he answers -- the ring *is* the
message there, and an answered call it cannot talk on is worse than none. This
is the other half: what to do when the point of the call is to talk.

**The asymmetry that is easy to get wrong.** SDES gives each side its own master
key. The key in OUR offer encrypts what WE send; the key in HIS answer decrypts
what HE sends. Two SrtpSessions, not one, and using the offer's key for both
directions produces a call that authenticates its own packets perfectly and
cannot read a single one of his.

**Why 8 kHz G.711 and not Opus.** Measured on this box on 2026-09-08: Whisper
large-v3 scores 12.5% WER on Serbian over a real G.711 mu-law roundtrip and
12.5% on the 16 kHz original -- the narrowband penalty is nil at that model
size. G.711 is also the one codec that cannot be negotiated away. Opus would
save bandwidth on a relay path that has bandwidth to spare, in exchange for a
binding we would have to carry.

**Pacing is not optional.** Audio must leave at wall-clock speed: a 20 ms frame
every 20 ms. Writing a whole utterance into the socket as fast as the loop can
run delivers several seconds of audio in a few milliseconds, which every jitter
buffer on the far end discards as a flood.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import time
from dataclasses import dataclass

import numpy as np

from . import pcm, rtp, srtp

log = logging.getLogger(__name__)

WIRE_RATE = 8000        # G.711 is 8 kHz, always
FRAME_MS = 20
FRAME_SAMPLES = WIRE_RATE * FRAME_MS // 1000   # 160
PT_PCMU = 0             # payload type 0 is mu-law and is never negotiated away


class SdpError(Exception):
    pass


@dataclass
class SdpAnswer:
    """The bits of his 200 OK's SDP that decide where audio goes and how."""

    host: str
    port: int
    srtp_key: bytes
    srtp_salt: bytes
    payload_types: list[int]

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)


def parse_sdp_answer(message: str) -> SdpAnswer:
    """Pull the media destination and SRTP key out of a 200 OK.

    Takes the whole SIP message rather than just the body: splitting the body
    off is the caller's job to get wrong, and the SDP is unambiguous enough to
    find in situ.
    """
    host = ""
    port = 0
    payload_types: list[int] = []
    key = salt = b""

    for raw in message.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("c=IN IP4 "):
            # A session-level c= may be overridden by a media-level one; last wins,
            # which is the same rule as taking the media section's own value.
            host = line[len("c=IN IP4 "):].split("/")[0].strip()
        elif line.startswith("m=audio "):
            parts = line.split()
            if len(parts) < 4:
                raise SdpError(f"malformed m=audio line: {line!r}")
            port = int(parts[1])
            payload_types = [int(p) for p in parts[3:] if p.isdigit()]
        elif line.startswith("a=crypto:"):
            if not key:  # first acceptable crypto line wins, per RFC 4568
                try:
                    _tag, key, salt = srtp.parse_crypto_line(line)
                except srtp.SrtpError:
                    continue  # a profile we do not implement; keep looking

    if not host:
        raise SdpError("no c=IN IP4 line: nowhere to send audio")
    if not port:
        raise SdpError("no m=audio port: the far end declined media")
    if not key:
        raise SdpError("no usable a=crypto line: cannot key SRTP for this call")
    if PT_PCMU not in payload_types:
        raise SdpError(f"far end did not offer PCMU; payload types were {payload_types}")
    return SdpAnswer(host, port, key, salt, payload_types)


class VoiceCall:
    """One answered call's audio, both directions.

    Synchronous on purpose. The whole exchange is a strictly ordered
    speak-then-listen conversation running in an executor thread; an asyncio
    protocol here would be more machinery for the same behaviour.
    """

    def __init__(
        self,
        media_sock: socket.socket,
        answer: SdpAnswer,
        our_key: bytes,
        our_salt: bytes,
        ssrc: int | None = None,
    ) -> None:
        self.sock = media_sock
        self.remote = answer.address
        # Ours encrypts outbound; his decrypts inbound. See the module docstring.
        self.tx = srtp.SrtpSession(our_key, our_salt)
        self.rx = srtp.SrtpSession(answer.srtp_key, answer.srtp_salt)
        self.ssrc = ssrc if ssrc is not None else int.from_bytes(struct.pack("!I", id(self) & 0xFFFFFFFF), "big")
        self._seq = 0
        self._ts = 0
        self.frames_sent = 0
        # Frames that arrived while WE were speaking. On a barge-in these are
        # the opening of his sentence, so they are kept and handed to the next
        # receive_turn rather than dropped -- discarding them loses the first
        # syllable of every interruption, which reads as him mumbling.
        self._pending_rx: list[bytes] = []
        self.interrupted = False
        # Measured during priming silence, when neither of us is talking.
        # None means never measured; the absolute threshold is used instead.
        self.line_floor: float | None = None
        # Learned from the first thing he actually says. Until then only the
        # noise floor is available, which is why the first turn is the one most
        # likely to be interrupted by the room.
        self.his_level: float | None = None
        self.his_voice: np.ndarray | None = None
        self.frames_received = 0
        self.auth_failures = 0

    # -- outbound ---------------------------------------------------------

    # Consecutive loud frames before we accept that he has started talking.
    #
    # This was 8 (160 ms) against the fixed MAX_THRESHOLD, and on the first live
    # conversation it cut off every single utterance about a sixth of a second
    # in -- he heard "nothing for four seconds, then it started talking and just
    # stopped". A real phone line is never as quiet as a synthesised test clip:
    # room noise, comfort noise and our own audio echoing back through the relay
    # all sit above a threshold picked for clean audio, so the barge-in
    # triggered on us talking to ourselves.
    #
    # Two changes. Half a second, because a real interruption lasts longer than
    # that and an echo burst usually does not. And the threshold is calibrated
    # against the line's own measured noise rather than assumed -- see
    # `line_floor`.
    BARGE_IN_FRAMES = 25
    # How far above the measured line noise counts as him talking.
    BARGE_IN_OVER_FLOOR = 4.0
    # ...and, once we have heard him, what fraction of HIS OWN speaking level a
    # sound must reach. This is the fix for other people in the room: background
    # conversation clears a noise-floor threshold easily, because it IS speech,
    # just not his. It does not clear a threshold set relative to a voice
    # speaking directly into the handset, because distance costs it an order of
    # magnitude. His idea, 2026-09-08, after a call where the room kept
    # interrupting him.
    # Measured on a live call: his speaking level came out at 0.0413, so 0.45
    # of it is 0.0186 -- BELOW the noise-floor threshold, which meant the
    # profile was not actually raising the bar at all. 0.6 puts it clear of
    # the floor while still well under a normal speaking voice.
    BARGE_IN_OF_HIS_LEVEL = 0.6
    # Spectral bands used to tell his voice from someone across the room. Coarse
    # on purpose: this is a cheap similarity check on 8 kHz telephony audio, not
    # speaker identification, and it is a SECOND opinion that only ever makes
    # the barge-in harder to trigger.
    VOICE_BANDS = 8
    VOICE_SIMILARITY = 0.82
    # Frames of inbound audio to retain while we speak. A barge-in needs its
    # onset kept, not the whole utterance: without a cap this grew for the
    # length of every reply and handed the next turn 19 s of mostly our own
    # echo, which Whisper duly transcribed.
    PENDING_RX_CAP = 60

    def send_audio(self, audio: np.ndarray, rate: int, *, interruptible: bool = False) -> float:
        """Speak. Float32 mono in [-1, 1] at `rate`, paced to real time.

        With `interruptible`, stop the moment he starts talking over us and set
        `self.interrupted`. Talking over someone who has started answering is
        the single rudest thing a voice agent does, and on a phone it is also
        useless -- he has stopped listening either way.

        Returns the wall-clock seconds spent, which should track the audio's own
        duration closely unless it was cut short -- a large gap on an
        uninterrupted send means the pacing is broken.
        """
        wire = pcm.from_model(audio, rate=rate, out_rate=WIRE_RATE)
        ulaw = pcm.ulaw_encode(wire)
        began = time.monotonic()
        self.interrupted = False
        loud_run = 0
        started = self.frames_sent
        if interruptible:
            # Judge the interruption on audio from now, not on whatever queued
            # while we were quiet. Stale frames are why a reply could be cut off
            # by something he said before it started.
            self._flush_inbound()
        for i in range(0, len(ulaw) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            self._send_frame(ulaw[i:i + FRAME_SAMPLES])
            # Absolute schedule, not `sleep(0.02)`: sleeping a fixed amount per
            # frame accumulates every scheduling overshoot and the stream drifts
            # progressively later than the clock it is meant to track.
            due = began + ((self.frames_sent - started) * FRAME_MS / 1000.0)
            slack = due - time.monotonic()
            if interruptible:
                loud_run = self._drain_inbound(slack, loud_run)
                if loud_run >= self.BARGE_IN_FRAMES:
                    self.interrupted = True
                    log.info("barge-in: he started talking, stopping mid-utterance")
                    break
            elif slack > 0:
                time.sleep(slack)
        return time.monotonic() - began

    def _flush_inbound(self) -> int:
        """Discard anything already queued, and say how much there was."""
        dropped = 0
        original = self.sock.gettimeout()
        try:
            self.sock.setblocking(False)
            while True:
                try:
                    self.sock.recvfrom(4096)
                    dropped += 1
                except (BlockingIOError, socket.timeout, TimeoutError):
                    break
                except OSError:
                    break
        finally:
            self.sock.settimeout(original)
        if dropped:
            log.debug("flushed %d stale inbound frames before speaking", dropped)
        return dropped

    def _drain_inbound(self, budget: float, loud_run: int) -> int:
        """Read whatever has arrived, inside the pacing slack we already owe.

        Runs in the gap between frames rather than on a thread: there is 20 ms
        of scheduled idle per frame and this needs a fraction of it, so a thread
        would add a lock around the SRTP receive state for no gain.
        """
        deadline = time.monotonic() + max(0.0, budget)
        original = self.sock.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return loud_run
                self.sock.settimeout(remaining)
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except (socket.timeout, TimeoutError):
                    return loud_run
                except OSError:
                    return loud_run
                try:
                    plain = self.rx.unprotect(data)
                except srtp.AuthenticationFailure:
                    self.auth_failures += 1
                    continue
                except srtp.SrtpError:
                    continue
                parsed = rtp.parse_packet(plain)
                if parsed is None:
                    continue
                payload = parsed[3]
                self._pending_rx.append(payload)
                if len(self._pending_rx) > self.PENDING_RX_CAP:
                    del self._pending_rx[:-self.PENDING_RX_CAP]
                self.frames_received += 1
                samples = np.frombuffer(pcm.ulaw_decode(payload), dtype="<i2")
                level = float(np.sqrt(np.mean((samples / 32768.0) ** 2))) if samples.size else 0.0
                bar = self._barge_threshold()
                is_him = (bar is not None and level >= bar
                          and self._sounds_like_him(samples))
                loud_run = loud_run + 1 if is_him else 0
                if loud_run >= self.BARGE_IN_FRAMES:
                    return loud_run
        finally:
            self.sock.settimeout(original)

    def _send_frame(self, payload: bytes) -> None:
        packet = rtp.build_packet(self._seq & 0xFFFF, self._ts & 0xFFFFFFFF,
                                  self.ssrc, payload)
        self.sock.sendto(self.tx.protect(packet), self.remote)
        self._seq += 1
        self._ts += FRAME_SAMPLES
        self.frames_sent += 1

    # How much silence to send before the first real audio of a call. His client
    # cannot play anything until its jitter buffer has filled, so whatever
    # arrives during that window is swallowed -- on the first live call he heard
    # the greeting's opening syllable clipped and described it as "malo glitch
    # na pocetku". 0.4 s was not enough; 1.2 s covers a buffer sized for the
    # 172 ms jitter measured on this path with room to spare, and costs only a
    # second of silence he is not listening to yet anyway.
    PRIMING_SECONDS = 1.2

    def send_silence(self, seconds: float, *, calibrate: bool = False) -> None:
        """Keep the stream alive while nothing is being said.

        Some far ends tear down a call whose media stops; more practically, a
        gap in the RTP timestamps is what makes his client's jitter buffer
        decide the network died.
        """
        quiet = b"\xff" * FRAME_SAMPLES  # mu-law silence is 0xFF, not 0x00
        began = time.monotonic()
        heard: list[float] = []
        while time.monotonic() - began < seconds:
            self._send_frame(quiet)
            if calibrate:
                # Listen while we are deliberately silent: this is the only
                # moment in a call when whatever arrives is definitionally the
                # line's own noise, which is what the threshold has to clear.
                heard.extend(self._sample_levels(FRAME_MS / 1000.0))
            else:
                # Keepalive silence still has to DRAIN, even though it discards.
                # This runs for seconds while the agent thinks, and his phone
                # streams at us the whole time; leaving that in the socket
                # buffer means the next utterance opens by reading a pile of
                # audio from several seconds ago and scoring it as an
                # interruption. Discarded rather than kept because he is
                # listening to a filler here, not being asked anything.
                self._sample_levels(FRAME_MS / 1000.0)
        if not calibrate:
            return
        if len(heard) >= 25:
            self.line_floor = float(np.percentile(heard, 75))
            log.info("line noise floor %.4f -> barge-in above %.4f",
                     self.line_floor, self._barge_threshold() or -1)
        else:
            # Too little to judge. Leaving line_floor None disables barge-in,
            # which is the safe direction -- see _barge_threshold.
            log.info("only %d frames during priming; barge-in stays off", len(heard))

    @staticmethod
    def _envelope(samples: np.ndarray, bands: int) -> np.ndarray:
        """A coarse normalised spectrum: what this sound is made of, not how loud.

        Normalised so it describes timbre rather than level -- the whole point
        is to compare a quiet sound against a loud reference and still tell
        whether it is the same voice.
        """
        if samples.size < 32:
            return np.zeros(bands, dtype=np.float32)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        chunks = np.array_split(spectrum[1:], bands)
        env = np.array([float(c.mean()) for c in chunks], dtype=np.float32)
        total = float(np.linalg.norm(env))
        return env / total if total > 0 else env

    def enrol_voice(self, audio: np.ndarray, rate: int = 16000) -> None:
        """Learn his voice from a turn we already know was him.

        Called with the first turn he speaks: he is the one who answered the
        phone, so whatever endpointed as speech there is definitionally him.
        Anything quieter or spectrally unlike this afterwards is the room.
        """
        if audio.size < rate // 2:
            return
        frame = max(64, rate * FRAME_MS // 1000)
        frames = [audio[i:i + frame] for i in range(0, audio.size - frame + 1, frame)]
        levels = np.array([float(np.sqrt(np.mean(f ** 2))) for f in frames])
        if not levels.size:
            return
        # The loud half is his voice; the quiet half is the gaps between words.
        speaking = levels[levels >= np.percentile(levels, 60)]
        if not speaking.size:
            return
        self.his_level = float(np.median(speaking))
        loud = [f for f, lv in zip(frames, levels) if lv >= np.percentile(levels, 60)]
        envs = [self._envelope(f, self.VOICE_BANDS) for f in loud]
        if envs:
            mean = np.mean(envs, axis=0)
            norm = float(np.linalg.norm(mean))
            self.his_voice = (mean / norm) if norm > 0 else None
        log.info("enrolled his voice: level %.4f -> interrupts must reach %.4f",
                 self.his_level, self._barge_threshold() or -1)

    def _sounds_like_him(self, samples: np.ndarray) -> bool:
        """Cheap timbre check. True when unknown, so it can only ever add caution."""
        if self.his_voice is None:
            return True
        env = self._envelope(samples.astype(np.float32) / 32768.0, self.VOICE_BANDS)
        if not env.any():
            return True
        return float(np.dot(env, self.his_voice)) >= self.VOICE_SIMILARITY

    def _barge_threshold(self) -> float | None:
        """The level that counts as him interrupting, or None for "do not".

        None rather than a guess when the line was never measured. The guess is
        what broke the first conversation: an assumed threshold that a real line
        clears on its own noise turns every utterance into a barge-in. Refusing
        to interrupt is a mild failure -- we talk over him occasionally -- where
        a wrong threshold is a total one, and he hears nothing at all.
        """
        if self.line_floor is None:
            return None
        bar = max(self.line_floor * self.BARGE_IN_OVER_FLOOR, self.MAX_THRESHOLD)
        if self.his_level is not None:
            bar = max(bar, self.his_level * self.BARGE_IN_OF_HIS_LEVEL)
        return bar

    def _sample_levels(self, budget: float) -> list[float]:
        """Read for `budget` seconds and return the frame energies seen."""
        levels: list[float] = []
        deadline = time.monotonic() + max(0.0, budget)
        original = self.sock.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return levels
                self.sock.settimeout(remaining)
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except (socket.timeout, TimeoutError, OSError):
                    return levels
                try:
                    plain = self.rx.unprotect(data)
                except srtp.AuthenticationFailure:
                    self.auth_failures += 1
                    continue
                except srtp.SrtpError:
                    continue
                parsed = rtp.parse_packet(plain)
                if parsed is None:
                    continue
                samples = np.frombuffer(pcm.ulaw_decode(parsed[3]), dtype="<i2")
                levels.append(float(np.sqrt(np.mean((samples / 32768.0) ** 2)))
                              if samples.size else 0.0)
        finally:
            self.sock.settimeout(original)

    # -- inbound ----------------------------------------------------------

    def receive_audio(self, seconds: float, out_rate: int = 16000) -> np.ndarray:
        """Listen for `seconds` and return what arrived, at `out_rate`.

        Missing packets are not concealed -- a gap comes back as a gap. This
        feeds an ASR model, and inventing audio to paper over loss is exactly
        the kind of helpfulness that produces a confident wrong transcript.
        """
        collected = bytearray()
        deadline = time.monotonic() + seconds
        original = self.sock.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sock.settimeout(min(0.5, remaining))
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                try:
                    plain = self.rx.unprotect(data)
                except srtp.AuthenticationFailure:
                    self.auth_failures += 1
                    continue
                except srtp.SrtpError:
                    continue
                parsed = rtp.parse_packet(plain)
                if parsed is None:
                    continue
                collected += parsed[3]
                self.frames_received += 1
        finally:
            self.sock.settimeout(original)

        if not collected:
            return np.zeros(0, dtype=np.float32)
        return pcm.to_model(pcm.ulaw_decode(bytes(collected)), rate=WIRE_RATE) \
            if out_rate == 16000 else pcm.resample(
                pcm.to_model(pcm.ulaw_decode(bytes(collected)), rate=WIRE_RATE),
                16000, out_rate)


    # -- turn taking ------------------------------------------------------

    # Speech is this many times louder than the noise floor. A ratio rather than
    # an absolute level because the floor on a relayed mobile call is nothing
    # like the floor in a quiet room, and a fixed threshold tuned on one is deaf
    # or permanently triggered on the other.
    SPEECH_OVER_FLOOR = 3.0
    # Below this, the "floor" is really digital silence and multiplying it gives
    # a threshold that any comfort noise trips.
    MIN_THRESHOLD = 0.004
    # And above this, it is speech whatever the floor says. Needed because a
    # relative threshold has nothing to measure against when the stream is
    # speech end to end with no gaps: the percentile lands on his voice, three
    # times that is above anything he can produce, and a caller talking without
    # pause reads as silence. Telephony speech sits around 0.02-0.15 RMS and
    # comfort noise below 0.01, so this separates them with room on both sides.
    MAX_THRESHOLD = 0.02

    def receive_turn(
        self,
        *,
        max_seconds: float = 20.0,
        silence_ms: int = 800,
        min_speech_ms: int = 300,
        calibrate_ms: int = 300,
        chunk_at_ms: int = 350,
        on_chunk=None,
    ) -> tuple[np.ndarray, str]:
        """Listen until he stops talking, then return what he said.

        Endpointing by frame energy rather than a model: silero-vad needs torch,
        and torch is 2.5 GB on a root partition at 80% for a decision an RMS
        comparison makes well enough on 8 kHz telephony audio. That is a real
        quality trade -- this will end a turn on a long mid-sentence pause, and
        it cannot tell his voice from a television -- but it degrades in an
        obvious direction and costs nothing.

        Returns (audio at 16 kHz, why it stopped) so the caller can tell "he
        finished" from "he never started" from "he is still going". Those need
        different replies and collapsing them into an empty array loses that.

        `on_chunk` is handed each phrase as he finishes it, at any pause of
        `chunk_at_ms` too short to end the turn. His idea: transcribing the whole
        utterance only after he stops means the ASR cost lands entirely in the
        silence he is waiting through, and on a 10 s turn that was 4.4 s of dead
        air. Feeding phrases out as they complete moves nearly all of it under
        his own speech, leaving only the final phrase to transcribe at the end.

        Chunking happens at pauses rather than on a timer because a fixed
        boundary lands mid-word, and Whisper given half a word transcribes half
        a word.
        """
        # Whatever arrived while we were speaking IS the start of his turn.
        frames: list[bytes] = list(self._pending_rx)
        self._pending_rx = []
        levels: list[float] = []
        speech_frames = 0
        trailing_silence = 0
        chunk_start = 0          # index into `frames` where the current phrase began
        emitted_to = 0
        threshold: float | None = None
        frames_for_silence = max(1, silence_ms // FRAME_MS)
        frames_for_chunk = max(1, chunk_at_ms // FRAME_MS)
        frames_for_speech = max(1, min_speech_ms // FRAME_MS)
        frames_to_calibrate = max(1, calibrate_ms // FRAME_MS)

        for payload in frames:
            samples = np.frombuffer(pcm.ulaw_decode(payload), dtype="<i2")
            levels.append(float(np.sqrt(np.mean((samples / 32768.0) ** 2))) if samples.size else 0.0)
        if levels:
            # Those frames were captured because they were loud, so credit them
            # as speech: a barge-in that then falls below min_speech_ms would
            # otherwise be reported as silence.
            bar = self._barge_threshold() or self.MAX_THRESHOLD
            speech_frames = sum(1 for lv in levels if lv >= bar)

        deadline = time.monotonic() + max_seconds
        original = self.sock.gettimeout()
        reason = "timeout"
        try:
            while time.monotonic() < deadline:
                self.sock.settimeout(min(0.5, max(0.01, deadline - time.monotonic())))
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                try:
                    plain = self.rx.unprotect(data)
                except srtp.AuthenticationFailure:
                    self.auth_failures += 1
                    continue
                except srtp.SrtpError:
                    continue
                parsed = rtp.parse_packet(plain)
                if parsed is None:
                    continue
                payload = parsed[3]
                frames.append(payload)
                self.frames_received += 1

                samples = np.frombuffer(pcm.ulaw_decode(payload), dtype="<i2")
                level = float(np.sqrt(np.mean((samples / 32768.0) ** 2))) if samples.size else 0.0
                levels.append(level)

                if len(levels) < frames_to_calibrate:
                    continue
                # The floor is a low percentile of everything heard so far, not
                # the average of the first 300 ms. Calibrating on a fixed opening
                # window assumes he is quiet when the turn starts; when he is
                # not, the floor is measured on his voice, the threshold lands
                # three times above it, and the endpointer is deaf for the whole
                # turn -- which is how a 4 s answer came back as "silence".
                # Recomputed periodically rather than per frame: sorting every
                # 20 ms is pointless when the floor moves this slowly.
                if threshold is None or len(levels) % 25 == 0:
                    floor = float(np.percentile(levels, 20))
                    threshold = min(
                        max(floor * self.SPEECH_OVER_FLOOR, self.MIN_THRESHOLD),
                        self.MAX_THRESHOLD,
                    )

                if level >= threshold:
                    speech_frames += 1
                    trailing_silence = 0
                else:
                    trailing_silence += 1
                    if speech_frames >= frames_for_speech and trailing_silence >= frames_for_silence:
                        reason = "endpointed"
                        break
                    # A shorter pause is a phrase boundary, not the end of his
                    # turn: hand what he has said so far to the transcriber and
                    # keep listening.
                    if (on_chunk is not None and trailing_silence == frames_for_chunk
                            and len(frames) - emitted_to > frames_for_speech):
                        phrase = pcm.to_model(
                            pcm.ulaw_decode(b"".join(frames[emitted_to:])), rate=WIRE_RATE)
                        emitted_to = len(frames)
                        chunk_start = emitted_to
                        try:
                            on_chunk(phrase)
                        except Exception:
                            log.exception("chunk handler raised; continuing to listen")
        finally:
            self.sock.settimeout(original)

        # "timeout" means he was still going when the cap hit; "silence" means
        # audio arrived and none of it was speech. Collapsing the two loses the
        # difference between "keep listening" and "he is not there".
        if reason == "timeout" and speech_frames < frames_for_speech:
            reason = "silence"
        if not frames:
            return np.zeros(0, dtype=np.float32), "no-audio"
        # Only the tail: everything before `emitted_to` already went to on_chunk
        # and transcribing it twice would duplicate half his sentence.
        tail = frames[emitted_to:] if on_chunk is not None else frames
        audio = (pcm.to_model(pcm.ulaw_decode(b"".join(tail)), rate=WIRE_RATE)
                 if tail else np.zeros(0, dtype=np.float32))
        return audio, reason

    def stats(self) -> dict[str, int]:
        return {
            "frames_sent": self.frames_sent,
            "frames_received": self.frames_received,
            "auth_failures": self.auth_failures,
        }
