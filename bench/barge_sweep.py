"""Find a barge-in rule that fires for him and not for his room.

The current rule needs BARGE_IN_FRAMES=25 CONSECUTIVE frames above 60% of his
median speaking level. Natural speech does not do that: the gaps between words
are shorter than 500 ms but they are not zero, and every one of them resets the
run. Measured on his own recorded turns the longest unbroken run is 16 frames.
So the rule cannot fire on him, which is what the live call showed -- nine turns,
six of them spoken over us, and not one barge-in.

This sweeps the three knobs against real audio on both sides of the question and
prints the ones that separate him from the room. A window rule ("most of the last
N frames") is what every VAD uses, and for the same reason.
"""
import glob, itertools, json, os, sys, wave
import numpy as np

sys.path.insert(0, "/home/bodas/data/hotline-ios/server/src")
from hotline_ios.media import pcm
from hotline_ios.media.voicecall import FRAME_SAMPLES, WIRE_RATE, VoiceCall
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from noise_bench import Bare, chatter, frames_of, load, rms, room_noise, telephony

BENCH = os.environ.get(
    "HOTLINE_IOS_BENCH_DIR",
    os.path.expanduser("~/data/hotline-ios/recordings/20260910-asr-benchmark"))
"""Where the call audio and reference live. NOT in the repo: it is his voice
and this repo is public. Override with HOTLINE_IOS_BENCH_DIR."""


def fires(call, frames, bar, window, need_fraction, use_profile=True):
    """Would a sliding-window rule accept this as him starting to talk?"""
    hits = []
    for frame in frames:
        level = rms(frame / 32768.0)
        ok = level >= bar and (call._sounds_like_him(frame) if use_profile else True)
        hits.append(1 if ok else 0)
    if len(hits) < window:
        return False
    run = np.convolve(hits, np.ones(window, dtype=int), mode="valid")
    return bool((run >= int(np.ceil(window * need_fraction))).any())


def main():
    call_dir = glob.glob(os.path.join(BENCH, "call-*"))[0]
    turns = sorted(glob.glob(os.path.join(call_dir, "turn-*.wav")))

    call = Bare()
    call.line_floor = float(np.percentile(
        [rms(f / 32768.0) for f in frames_of(telephony(room_noise(1.2, 0.0009)))], 75))
    call.enrol_voice(pcm.to_model(load(turns[0]), rate=WIRE_RATE))

    # HIM: every turn he spoke, as it arrived. All of these must fire.
    him = [(os.path.basename(p), frames_of(load(p))) for p in turns]
    # THE ROOM: must never fire. Levels relative to his enrolled 0.0399.
    room = []
    for level in (0.005, 0.01, 0.02):
        room.append((f"chatter@{level}", frames_of(telephony(chatter(4.0, level)))))
        room.append((f"noise@{level}", frames_of(telephony(room_noise(4.0, level)))))

    print(f"his_level {call.his_level:.4f}  line_floor {call.line_floor:.4f}")
    print(f"current rule: bar 0.6*his = {0.6*call.his_level:.4f}, "
          f"25 consecutive -> fires on {sum(fires(call, f, 0.6*call.his_level, 25, 1.0) for _, f in him)}"
          f"/{len(him)} of his turns\n")

    print(f"{'bar/his':>8}{'window':>8}{'need':>7}{'his turns':>11}{'false':>7}   verdict")
    good = []
    for frac, window, need in itertools.product(
            (0.15, 0.2, 0.25, 0.3, 0.4, 0.6), (10, 15, 20, 25), (0.5, 0.6, 0.7, 0.8, 1.0)):
        bar = max(call.line_floor * VoiceCall.BARGE_IN_OVER_FLOOR, frac * call.his_level)
        hit = sum(fires(call, f, bar, window, need) for _, f in him)
        false = sum(fires(call, f, bar, window, need) for _, f in room)
        mark = "OK" if hit == len(him) and false == 0 else ""
        if mark:
            good.append((frac, window, need, window * 20))
        if mark or (hit >= len(him) - 1):
            print(f"{frac:>8.2f}{window:>8}{need:>7.1f}{hit:>8}/{len(him)}{false:>7}   {mark}")

    print(f"\n{len(good)} settings separate him from the room completely.")
    if good:
        # Prefer the one that reacts fastest, then the most conservative bar --
        # a barge-in that takes a second is not a barge-in.
        best = sorted(good, key=lambda g: (g[3], -g[0], -g[2]))[0]
        print(f"fastest clean rule: bar {best[0]:.2f}*his_level, "
              f"{int(best[2]*best[1])}/{best[1]} frames ({best[3]} ms window)")
        with open(os.path.join(BENCH, "barge-sweep.json"), "w") as fh:
            json.dump(dict(his_level=call.his_level, line_floor=call.line_floor,
                           clean=[dict(bar_fraction=g[0], window=g[1], need=g[2],
                                       window_ms=g[3]) for g in good],
                           chosen=dict(bar_fraction=best[0], window=best[1],
                                       need=best[2], window_ms=best[3])), fh, indent=2)


if __name__ == "__main__":
    main()
