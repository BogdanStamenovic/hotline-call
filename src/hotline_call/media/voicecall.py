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
import queue
import re
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from . import pcm, rtp, srtp

log = logging.getLogger(__name__)

WIRE_RATE = 8000        # G.711 is 8 kHz, always
FRAME_MS = 20
FRAME_SAMPLES = WIRE_RATE * FRAME_MS // 1000   # 160
PT_PCMU = 0             # payload type 0 is mu-law and is never negotiated away
QUIET_FRAME = b"\xff" * FRAME_SAMPLES   # mu-law silence is 0xFF, not 0x00


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



class MediaPump(threading.Thread):
    """Owns the socket and keeps the 20 ms clock, and does nothing else.

    **Why this is its own thread.** Everything else in a call is bursty and slow:
    Whisper on the GPU, cvoice over HTTP, a subprocess talking to Sonnet. RTP is
    neither -- it is a metronome, and a frame that leaves 40 ms late is
    indistinguishable at the far end from a frame that was lost. Running the
    pacing on the same thread as the logic meant every transcription stalled the
    stream, which showed up on his phone as the call-quality meter rising and
    falling and as audio he described as "glitchy and segmented". No frame was
    ever missing; they were simply late.

    The thread does the minimum that has to be timely -- encrypt, send, receive,
    decrypt -- and hands everything else off through queues. It never decodes
    mu-law, never touches numpy, and never calls out to a model.

    It ALWAYS sends. With nothing queued it sends silence, because a stream that
    stops is not a quiet stream, it is a dead one.
    """

    def __init__(self, sock, tx, rx, remote, ssrc):
        super().__init__(daemon=True, name="rtp-pump")
        self.sock = sock
        self.tx = tx
        self.rx = rx
        self.remote = remote
        self.ssrc = ssrc
        self._outbox: deque[bytes] = deque()
        self._lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        # Where his packets actually come FROM, once we have seen one. See
        # `_latch`.
        self.latched: tuple[str, int] | None = None
        # Every inbound payload, still mu-law, when something asked for it.
        # None -- not an empty list -- so an unrecorded call does not pay a
        # branch per frame that appends to something nobody reads.
        self.wire: list[bytes] | None = None
        # What WE sent, and when each inbound frame arrived relative to it.
        #
        # The outbound stream is a perfect 50 fps clock -- the pump never skips
        # a frame -- so `frames_sent` at the moment a frame arrives is an exact
        # 20 ms timestamp for it. Inbound has gaps (his phone suppresses
        # silence) and wall-clock offsets do not survive them, which is why
        # aligning the two by timestamp did not work.
        #
        # This exists to answer one question that cannot be answered without it:
        # when audio arrives WHILE WE ARE TALKING, is that him talking over us
        # or our own voice echoing back off his handset? Those want opposite
        # responses -- keep it, or discard it -- and they are indistinguishable
        # from the inbound stream alone. Cross-correlating against what we sent
        # at the same instant separates them.
        self.wire_out: list[bytes] | None = None
        self.wire_at: list[int] = []
        # NOT `_stop`: threading.Thread has an internal _stop() that join()
        # calls, and shadowing it with an Event breaks join() with a
        # TypeError from deep inside the stdlib.
        self._stopping = threading.Event()
        self._seq = 0
        self._ts = 0
        self.frames_sent = 0
        self.frames_received = 0
        self.auth_failures = 0
        self.late_frames = 0

    # -- what the call logic calls ---------------------------------------

    def enqueue(self, frames: list[bytes]) -> None:
        with self._lock:
            self._outbox.extend(frames)

    def drop_queued(self) -> int:
        """Abandon un-sent audio. Used by barge-in: stopping means stopping now,
        not after the rest of the sentence has drained."""
        with self._lock:
            n = len(self._outbox)
            self._outbox.clear()
        return n

    def queued(self) -> int:
        with self._lock:
            return len(self._outbox)

    def stop(self) -> None:
        self._stopping.set()

    # -- the clock --------------------------------------------------------

    def run(self) -> None:
        next_frame = time.monotonic()
        while not self._stopping.is_set():
            now = time.monotonic()
            if now >= next_frame:
                if now - next_frame > 0.04:
                    self.late_frames += 1
                with self._lock:
                    payload = self._outbox.popleft() if self._outbox else QUIET_FRAME
                self._send(payload)
                next_frame += FRAME_MS / 1000.0
                # Resynchronise rather than firing a catch-up burst: a burst is
                # exactly what a jitter buffer discards.
                if next_frame < now - 0.1:
                    next_frame = now + FRAME_MS / 1000.0
                continue
            self._read(min(next_frame - now, 0.02))

    def _send(self, payload: bytes) -> None:
        if self.wire_out is not None:
            self.wire_out.append(payload)
        packet = rtp.build_packet(self._seq & 0xFFFF, self._ts & 0xFFFFFFFF,
                                  self.ssrc, payload)
        try:
            self.sock.sendto(self.tx.protect(packet), self.remote)
        except OSError:
            return
        finally:
            self._seq += 1
            self._ts += FRAME_SAMPLES
            self.frames_sent += 1

    def _latch(self, source) -> None:
        """Send to where his audio is actually coming from, not where his SDP said.

        Only ever called for a packet that has just AUTHENTICATED against his
        master key, which is what makes this safe: a forged source address
        cannot produce one. Symmetric RTP, and every SIP stack that survives NAT
        does it.

        The address in a c= line is what the far end believes about itself, and
        behind CGNAT -- an ordinary mobile network -- that belief is a private
        address nothing outside can route to. Without this the call is
        one-directional in the worse direction: we hear him perfectly and he
        hears nothing, which reads as the media leg being broken rather than as
        one line of SDP being unroutable.

        Latched once and then left alone. A stream that re-latches per packet
        would follow anything that happens to arrive.
        """
        if self.latched is not None:
            return
        seen = (str(source[0]), int(source[1]))
        self.latched = seen
        if seen != self.remote:
            log.info("latching audio to %s:%d; his SDP said %s:%d",
                     seen[0], seen[1], self.remote[0], self.remote[1])
            self.remote = seen

    def _read(self, budget: float) -> None:
        try:
            self.sock.settimeout(max(0.001, budget))
            data, _addr = self.sock.recvfrom(4096)
        except (socket.timeout, TimeoutError):
            return
        except OSError:
            return
        try:
            plain = self.rx.unprotect(data)
        except srtp.AuthenticationFailure:
            self.auth_failures += 1
            return
        except srtp.SrtpError:
            return
        parsed = rtp.parse_packet(plain)
        if parsed is None:
            return
        self._latch(_addr)
        self.frames_received += 1
        if self.wire is not None:
            self.wire_at.append(self.frames_sent)
            # Kept as it arrived: G.711 mu-law at 8 kHz, before anything decodes
            # or resamples it. A recording of what Whisper was given is not the
            # same artefact as a recording of what the line carried, and only
            # the second one can score a different model fairly.
            self.wire.append(parsed[3])
        # Unbounded on purpose: a turn is bounded in time by the caller, and
        # dropping inbound audio to protect memory would lose his words.
        self.inbox.put(parsed[3])


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
        self.ssrc = ssrc if ssrc is not None else int.from_bytes(
            struct.pack("!I", id(self) & 0xFFFFFFFF), "big")
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
        # The pump owns the socket and the 20 ms clock from here on. Nothing
        # else in this class touches the wire.
        self.pump = MediaPump(media_sock, self.tx, self.rx, self.remote, self.ssrc)
        self.pump.start()

    # Counters live on the pump, which is the only thing that sees a packet.
    @property
    def frames_sent(self) -> int:
        return self.pump.frames_sent

    @property
    def frames_received(self) -> int:
        return self.pump.frames_received

    @property
    def auth_failures(self) -> int:
        return self.pump.auth_failures

    @property
    def late_frames(self) -> int:
        """Frames the pump sent more than 40 ms behind schedule.

        Non-zero means something is starving the media thread, which is heard as
        chop even though no frame was ever lost.
        """
        return self.pump.late_frames

    def close(self) -> None:
        self.pump.stop()

    # -- keeping the audio ------------------------------------------------

    def record(self) -> None:
        """Keep both directions from here on. Off unless asked."""
        if self.pump.wire is None:
            self.pump.wire = []
            self.pump.wire_out = []

    @property
    def recorded_frames(self) -> int:
        """How many payloads are held, which is also the cursor a caller uses to
        mark where one turn ended and the next began."""
        return 0 if self.pump.wire is None else len(self.pump.wire)

    def recorded(self, start: int = 0, end: int | None = None) -> bytes:
        """The mu-law between two frame cursors, concatenated."""
        if self.pump.wire is None:
            return b""
        return b"".join(self.pump.wire[start:end])

    def recorded_outbound(self) -> bytes:
        """Everything we sent, which is a gapless 50 fps clock."""
        if self.pump.wire_out is None:
            return b""
        return b"".join(self.pump.wire_out)

    def outbound_at(self, cursor: int) -> int:
        """Which outbound frame we were on when inbound frame `cursor` arrived.

        The alignment between the two streams, and the only thing that makes
        them comparable -- see `MediaPump.wire_at`."""
        at = self.pump.wire_at
        if not at:
            return 0
        return at[min(cursor, len(at) - 1)]

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
    #
    # **Measured 2026-09-10, and it cannot fire on natural speech.** On nine
    # turns of his own recorded voice the longest run of CONSECUTIVE frames
    # above the bar is 16 -- 320 ms -- because the gaps between words are short
    # but not zero and every one of them resets the count. The live call agreed:
    # 87 seconds, nine turns, six of them spoken over us, and not one barge-in.
    #
    # A sliding window is what every VAD uses and it separates him from his room
    # cleanly: at the same bar, "9 of the last 15 frames" fires on 9/9 of his
    # turns and on 0/6 of synthetic room noise and distant chatter
    # (`barge-sweep.json`). It is NOT changed here, deliberately. The one
    # interferer that decides the question is our own audio echoing back off his
    # handset, which is the exact failure this 25 was written to fix -- and it
    # cannot be told apart from him talking over us without recording the
    # outbound stream alongside the inbound one. That recording now happens; the
    # constant changes when there is a call to measure it against, not before.
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
    # Frames of inbound audio to retain while we speak. Without a cap this grew
    # for the length of every reply and handed the next turn 19 s of mostly our
    # own echo, which Whisper duly transcribed.
    #
    # Raised from 60 (1.2 s) on 2026-09-10, measured. On the live benchmark call
    # SIX of his NINE turns began mid-word -- the recorded audio starts at RMS
    # 0.03-0.12 with no leading silence -- and two thirds of that call's word
    # errors were words he said which never reached the model at all. He speaks
    # over a holding clip of 1.16-2.4 s and then over the reply behind it, so
    # 1.2 s of retention could not cover even the filler. Three seconds covers a
    # filler and the first sentence after it.
    #
    # This still keeps the TAIL of what arrived, so a long overlap still loses
    # its beginning. The real fix for that is barge-in working, which it does
    # not -- see BARGE_IN_FRAMES.
    PENDING_RX_CAP = 150

    def send_audio(self, audio: np.ndarray, rate: int, *, interruptible: bool = False,
                   flush: bool | None = None) -> float:
        """Speak. Float32 mono in [-1, 1] at `rate`.

        Pacing is the pump's job now; this only queues the audio and waits for
        it to drain, watching for him cutting in if asked. That separation is
        the point: this method can block on a lock or a slow queue without a
        single frame going out late.

        With `interruptible`, stop the moment he starts talking over us and set
        `self.interrupted`. Talking over someone who has started answering is
        the rudest thing a voice agent does, and on a phone it is also useless.

        **What arrives while we talk is kept either way**, and that is a change
        of 2026-09-10. Retention used to be tied to `interruptible`, so audio
        that arrived during a non-interruptible filler was retained by nothing
        and then deleted by the next interruptible send's flush. On the live
        benchmark call that is where his words went: six of nine turns began
        mid-word, and two thirds of that call's word errors were words he said
        that never reached the model. Whether we are willing to be INTERRUPTED
        by a sound and whether we are willing to LOSE it are different
        questions, and only the first one is about the clip being played.

        `flush` discards what is already queued before starting, so a reply is
        not cut off by something he said before it began. It defaults to
        `interruptible` for exactly the callers that always wanted it -- but a
        multi-sentence answer must pass False after its first piece, or each
        piece deletes what he said during the one before it.
        """
        wire = pcm.from_model(audio, rate=rate, out_rate=WIRE_RATE)
        ulaw = pcm.ulaw_encode(wire)
        frames = [ulaw[i:i + FRAME_SAMPLES]
                  for i in range(0, len(ulaw) - FRAME_SAMPLES + 1, FRAME_SAMPLES)]
        self.interrupted = False
        if interruptible if flush is None else flush:
            self._flush_inbound()
        began = time.monotonic()
        expected = len(frames) * FRAME_MS / 1000.0
        self.pump.enqueue(frames)

        loud_run = 0
        while self.pump.queued() > 0:
            try:
                payload = self.pump.inbox.get(timeout=FRAME_MS / 1000.0)
            except queue.Empty:
                continue
            self._pending_rx.append(payload)
            if len(self._pending_rx) > self.PENDING_RX_CAP:
                del self._pending_rx[:-self.PENDING_RX_CAP]
            if not interruptible:
                continue
            samples = np.frombuffer(pcm.ulaw_decode(payload), dtype="<i2")
            level = float(np.sqrt(np.mean((samples / 32768.0) ** 2))) if samples.size else 0.0
            bar = self._barge_threshold()
            is_him = bar is not None and level >= bar and self._sounds_like_him(samples)
            loud_run = loud_run + 1 if is_him else 0
            if loud_run >= self.BARGE_IN_FRAMES:
                dropped = self.pump.drop_queued()
                self.interrupted = True
                log.info("barge-in: he started talking, dropped %d queued frames", dropped)
                break

        spent = time.monotonic() - began
        if not self.interrupted and expected > 0.5 and abs(spent - expected) > 0.25:
            log.warning("send drift: %.2fs of audio took %.2fs", expected, spent)
        return spent

    def _flush_inbound(self) -> int:
        """Discard anything already queued, and say how much there was."""
        dropped = 0
        while True:
            try:
                self.pump.inbox.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            log.debug("flushed %d stale inbound frames before speaking", dropped)
        return dropped

    def send_silence(self, seconds: float, *, calibrate: bool = False) -> None:
        """Say nothing for `seconds`, without the stream ever stopping.

        The pump emits silence whenever nothing is queued, so this only waits.
        With `calibrate`, listen while doing it: this is the one moment in a
        call when whatever arrives is definitionally the line's own noise, which
        is what the barge-in threshold has to clear.
        """
        deadline = time.monotonic() + seconds
        heard: list[float] = []
        while time.monotonic() < deadline:
            try:
                payload = self.pump.inbox.get(
                    timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if not calibrate:
                continue
            samples = np.frombuffer(pcm.ulaw_decode(payload), dtype="<i2")
            heard.append(float(np.sqrt(np.mean((samples / 32768.0) ** 2)))
                         if samples.size else 0.0)
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
        if self.his_level is not None:
            # Once we have heard him, HE is the reference and the absolute
            # minimum stops applying. On a quiet line he enrolled at 0.0112
            # while MAX_THRESHOLD held the bar at 0.0200 -- he would have had to
            # shout louder than he speaks to interrupt, so barge-in was dead
            # without saying so. His own level is the better yardstick, and the
            # noise floor still guards the bottom.
            return max(self.line_floor * self.BARGE_IN_OVER_FLOOR,
                       self.his_level * self.BARGE_IN_OF_HIS_LEVEL)
        return max(self.line_floor * self.BARGE_IN_OVER_FLOOR, self.MAX_THRESHOLD)

    # -- inbound ----------------------------------------------------------

    def receive_audio(self, seconds: float, out_rate: int = 16000) -> np.ndarray:
        """Listen for `seconds` and return what arrived, at 16 kHz.

        Missing packets are not concealed -- a gap comes back as a gap. This
        feeds an ASR model, and inventing audio to paper over loss is exactly
        the kind of helpfulness that produces a confident wrong transcript.
        """
        collected: list[bytes] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                collected.append(self.pump.inbox.get(
                    timeout=max(0.001, min(0.1, deadline - time.monotonic()))))
            except queue.Empty:
                continue
        if not collected:
            return np.zeros(0, dtype=np.float32)
        return pcm.to_model(pcm.ulaw_decode(b"".join(collected)), rate=WIRE_RATE)


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
    # And once he has been enrolled, a floor relative to his own voice. Low on
    # purpose -- a fifth of his speaking level, so it still hears him when he
    # drops his voice -- but far enough above comfort noise that a quiet line
    # cannot hold a turn open. Measured against his enrolment of 0.0948 this is
    # 0.0190: 4.7x the absolute minimum, and just under MAX_THRESHOLD.
    SPEECH_OF_HIS_LEVEL = 0.2

    # mu-law silence. Sent continuously whenever we are not saying anything, so
    # the stream never stops.
    QUIET_FRAME = b"\xff" * FRAME_SAMPLES

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

        **This keeps sending while it listens, and that is not cosmetic.** An
        RTP stream that simply stops is not a quiet stream, it is a dead one:
        his Linphone showed the call quality meter dropping and recovering on
        every turn, and each restart resets the far end's jitter buffer, which
        is heard as the start of our next sentence being chopped. He described
        the audio as clean at the start of a call and degrading after his first
        answer, which is exactly the shape of a stream that stops the moment we
        start listening. Silence goes out at the same 20 ms cadence as speech,
        so from his phone's point of view the stream never breaks.
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
        reason = "timeout"
        try:
            while time.monotonic() < deadline:
                # The pump keeps the outbound stream running on its own clock
                # while this listens, so nothing here has to be timely and a
                # slow chunk handler cannot stall the audio going to his phone.
                try:
                    payload = self.pump.inbox.get(
                        timeout=max(0.001, min(0.1, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                frames.append(payload)

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
                    if self.his_level is not None:
                        # Once we have heard him, HE is the reference -- the same
                        # rule `_barge_threshold` already applies, and it belongs
                        # here for the same reason.
                        #
                        # On the live call of 2026-09-10 the line was digitally
                        # silent at calibration (`line noise floor 0.0000`), so
                        # this landed on MIN_THRESHOLD, 0.004. He had enrolled at
                        # 0.0948. Everything above a twentieth of his speaking
                        # voice counted as speech, and one turn ran 14.48 s of
                        # which Whisper's own VAD then discarded 13.26 -- fourteen
                        # seconds in which he was saying nothing and heard nothing
                        # back, because we were still waiting for him to finish.
                        threshold = max(threshold,
                                        self.his_level * self.SPEECH_OF_HIS_LEVEL)

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
            pass

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
            "late_frames": self.late_frames,
        }
