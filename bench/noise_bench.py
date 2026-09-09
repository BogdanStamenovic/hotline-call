"""Where does the room start counting as him?

His request, verbatim (Discord 1547379220559302737, 2026-09-09T22:52Z): "We made
it so the barge in floor was dynamic on call start. You see my volume voice
energy excetera. And then setup the barge in depending on the voice. And
filterout background speach+noise by matching my frequency and energy. So yesh i
need yoht o test thst out. But test it out by recording these lines. And then
add nouse random poeple talking in the distsnce excetera"

So: his real recorded voice, plus synthetic room noise and distant chatter at
known SNRs, run through the ACTUAL decision functions in voicecall.py -- not a
reimplementation of them. Three questions, one per interferer:

  1. does the room falsely trigger barge-in (a false accept)?
  2. does HE still trigger it when the room is loud (a false reject)?
  3. does the spectral profile add anything the level test does not?

Offline, synthetic and repeatable, so it can be re-run after any change to the
thresholds without costing him a phone call.
"""
import glob, json, os, sys, wave
import numpy as np

sys.path.insert(0, "/home/bodas/data/hotline-ios/server/src")
from hotline_ios.media import pcm
from hotline_ios.media.voicecall import FRAME_SAMPLES, WIRE_RATE, VoiceCall

BENCH = os.environ.get(
    "HOTLINE_IOS_BENCH_DIR",
    os.path.expanduser("~/data/hotline-ios/recordings/20260910-asr-benchmark"))
"""Where the call audio and reference live. NOT in the repo: it is his voice
and this repo is public. Override with HOTLINE_IOS_BENCH_DIR."""
rng = np.random.default_rng(20260910)


class Bare(VoiceCall):
    """The judgement half of VoiceCall with no socket and no pump. Every
    threshold, constant and function below is the real one."""

    def __init__(self):
        self.line_floor = None
        self.his_level = None
        self.his_voice = None


def load(path):
    with wave.open(path) as w:
        assert w.getframerate() == WIRE_RATE
        return w.readframes(w.getnframes())


def telephony(audio16k):
    """Put a signal through the same 8 kHz mu-law path his voice came down."""
    wire = pcm.from_model(audio16k.astype(np.float32), rate=16000, out_rate=WIRE_RATE)
    return pcm.ulaw_decode(pcm.ulaw_encode(wire))


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if x.size else 0.0


def frames_of(raw_bytes):
    """Split 8 kHz PCM into the 20 ms frames the barge-in judges one at a time."""
    samples = np.frombuffer(raw_bytes, dtype="<i2")
    n = FRAME_SAMPLES
    return [samples[i:i + n] for i in range(0, samples.size - n + 1, n)]


def room_noise(seconds, level):
    """Broadband hum plus hiss: an air conditioner, a fridge, a street."""
    t = np.arange(int(seconds * 16000)) / 16000
    hum = 0.5 * np.sin(2 * np.pi * 50 * t) + 0.3 * np.sin(2 * np.pi * 100 * t)
    sig = (hum + rng.standard_normal(t.size)).astype(np.float32)
    return (sig / (rms(sig) or 1.0) * level).astype(np.float32)


def chatter(seconds, level, voices=3):
    """People talking across the room.

    It has to BE speech, not noise -- that is the whole point of his complaint.
    Distance is modelled the way distance actually behaves: the level drops and
    the high end drops faster, so a far voice is duller as well as quieter.
    """
    t = np.arange(int(seconds * 16000)) / 16000
    out = np.zeros(t.size, dtype=np.float32)
    for _ in range(voices):
        f0 = rng.uniform(90, 190)                     # a speaker's pitch
        # formants, wandering, plus a syllable envelope
        sig = sum(rng.uniform(0.4, 1.0) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6))
                  for k in (1, 2, 3, 5, 8))
        syllable = (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(2.5, 4.5) * t + rng.uniform(0, 6)))
        out += (sig * syllable).astype(np.float32)
    # A room low-passes what reaches you from across it.
    kernel = np.hanning(31); kernel /= kernel.sum()
    out = np.convolve(out, kernel, mode="same").astype(np.float32)
    return (out / (rms(out) or 1.0) * level).astype(np.float32)


def run_of_hits(call, frames, need):
    """The longest run of consecutive frames the real barge-in would accept,
    and whether that run reaches `need` -- which is BARGE_IN_FRAMES."""
    bar = call._barge_threshold()
    longest = current = 0
    for frame in frames:
        level = rms(frame / 32768.0)
        is_him = bar is not None and level >= bar and call._sounds_like_him(frame)
        current = current + 1 if is_him else 0
        longest = max(longest, current)
    return longest, longest >= need, bar


def main():
    call_dir = glob.glob(os.path.join(BENCH, "call-*"))[0]
    his_turns = sorted(glob.glob(os.path.join(call_dir, "turn-*.wav")))

    # Calibrate exactly as a call does: the priming silence, then his first turn.
    call = Bare()
    quiet = telephony(room_noise(1.2, 0.0009))
    heard = [rms(f / 32768.0) for f in frames_of(quiet)]
    call.line_floor = float(np.percentile(heard, 75))
    first = pcm.to_model(load(his_turns[0]), rate=WIRE_RATE)
    call.enrol_voice(first)

    print(f"line floor {call.line_floor:.4f}   his_level {call.his_level:.4f}   "
          f"barge-in bar {call._barge_threshold():.4f}   "
          f"(BARGE_IN_FRAMES={VoiceCall.BARGE_IN_FRAMES} = "
          f"{VoiceCall.BARGE_IN_FRAMES * 20} ms)\n")

    rows = []
    print("== 1. does the room falsely trigger it? (want: never) ==")
    print(f"{'interferer':<12}{'level':>8}{'SNR vs him':>12}{'longest run':>13}{'fires?':>9}")
    for name, maker in (("room noise", room_noise), ("chatter", chatter)):
        for level in (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2):
            frames = frames_of(telephony(maker(4.0, level)))
            longest, fires, bar = run_of_hits(call, frames, VoiceCall.BARGE_IN_FRAMES)
            snr = 20 * np.log10(call.his_level / level) if level else float("inf")
            print(f"{name:<12}{level:>8.3f}{snr:>10.1f} dB{longest:>13}{'FIRES' if fires else 'no':>9}")
            rows.append(dict(test="false accept", interferer=name, level=level,
                             snr_db=round(float(snr), 1), longest_run=longest, fires=fires))

    print("\n== 2. can HE still interrupt over it? (want: always) ==")
    print(f"{'over':<12}{'level':>8}{'SNR':>10}{'longest run':>13}{'fires?':>9}")
    him = pcm.to_model(load(his_turns[3]), rate=WIRE_RATE)   # a turn that is mostly speech
    for name, maker in (("room noise", room_noise), ("chatter", chatter)):
        for level in (0.0, 0.005, 0.02, 0.05, 0.1):
            mixed = him + (maker(him.size / 16000, level) if level else 0.0)
            frames = frames_of(telephony(mixed))
            longest, fires, bar = run_of_hits(call, frames, VoiceCall.BARGE_IN_FRAMES)
            snr = 20 * np.log10(call.his_level / level) if level else float("inf")
            print(f"{name:<12}{level:>8.3f}{snr:>8.1f} dB{longest:>13}{'FIRES' if fires else 'MISSED':>9}")
            rows.append(dict(test="false reject", interferer=name, level=level,
                             snr_db=round(float(snr), 1), longest_run=longest, fires=fires))

    print("\n== 3. what does the voice profile add over the level test alone? ==")
    print(f"{'signal':<26}{'level':>8}{'passes level':>14}{'sounds like him':>17}")
    checks = [("him (turn 4)", him, None)]
    for level in (0.02, 0.05, 0.1, 0.2):
        checks.append((f"chatter @{level}", chatter(3.0, level), level))
        checks.append((f"room noise @{level}", room_noise(3.0, level), level))
    for label, signal, _ in checks:
        frames = frames_of(telephony(signal))
        bar = call._barge_threshold()
        loud = [f for f in frames if rms(f / 32768.0) >= bar]
        similar = [f for f in loud if call._sounds_like_him(f)]
        share = 100 * len(similar) / len(loud) if loud else 0.0
        print(f"{label:<26}{rms(signal):>8.4f}{len(loud):>9}/{len(frames):<4}{share:>15.0f}%")
        rows.append(dict(test="profile", signal=label, level=round(rms(signal), 4),
                         loud_frames=len(loud), total_frames=len(frames),
                         passed_profile_pct=round(share, 1)))

    with open(os.path.join(BENCH, "noise-results.json"), "w") as fh:
        json.dump(dict(line_floor=call.line_floor, his_level=call.his_level,
                       bar=call._barge_threshold(),
                       barge_in_frames=VoiceCall.BARGE_IN_FRAMES,
                       voice_similarity=VoiceCall.VOICE_SIMILARITY, rows=rows), fh, indent=2)


if __name__ == "__main__":
    main()
