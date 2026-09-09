"""Was that him talking over us, or our own voice coming back?

The one question blocking a barge-in that actually works. Those two want opposite
handling -- keep it, or discard it -- and from the inbound stream alone they are
indistinguishable: on the 2026-09-10 call, audio arriving while we were speaking
measured RMS 0.03-0.09, LOUDER than his own enrolled level of 0.0399, and 100%
of it passed the voice profile.

Wall-clock offsets cannot align the two streams: his phone suppresses silence, so
the inbound recording has gaps and 59.4 s of it spans 87 s of call. What does
align them is `MediaPump.wire_at` -- the outbound frame counter at the instant
each inbound frame arrived. Outbound never skips a frame, so it is an exact 20 ms
clock.

Echo has one property nothing else has: it is a delayed, attenuated copy of what
we sent. So cross-correlate the two, and the answer is in the shape of the peak:

  * a sharp peak at a plausible handset delay (roughly 40-400 ms) means echo
  * no peak means the inbound audio is independent of ours -- it is him

Reports the echo-to-his-voice ratio in dB, which is what decides whether the
barge-in bar can be lowered. If echo sits well below his speaking level, the
9-of-15 window rule in `barge-sweep.json` is safe to ship. If it does not, it is
not, and the 25-consecutive rule stays until something suppresses the echo.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import wave

import numpy as np

BENCH = os.environ.get(
    "HOTLINE_IOS_BENCH_DIR",
    os.path.expanduser("~/data/hotline-ios/recordings"))
sys.path.insert(0, "/home/bodas/data/hotline-ios/server/src")

WIRE_RATE = 8000
FRAME = 160
# A handset's own speaker into its own microphone, plus a relay round trip.
# Anything outside this is not echo, it is coincidence.
MIN_DELAY_MS = 20
MAX_DELAY_MS = 500


def read(path):
    with wave.open(path) as handle:
        assert handle.getframerate() == WIRE_RATE, path
        return np.frombuffer(handle.readframes(handle.getnframes()),
                             dtype="<i2").astype(np.float64) / 32768.0


def rms(x):
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def aligned(call_dir):
    """Both streams at 8 kHz on ONE timebase, and the windows where we spoke.

    Inbound is rebuilt onto the outbound clock: each inbound frame is placed at
    the outbound frame index recorded for it, and the gaps his phone left are
    filled with silence. Without that step index *i* means a different instant in
    each stream and every comparison below is meaningless.
    """
    def need(name):
        path = os.path.join(call_dir, name)
        if not os.path.exists(path):
            raise SystemExit(
                f"{call_dir} has no {name}. It was recorded before two-way "
                "capture existed, so nothing here can align it -- this needs a "
                "call placed by a daemon running the current code.")
        return path

    manifest = json.load(open(need("manifest.json")))
    outbound = read(need("outbound.wav"))
    raw_in = read(need("inbound.wav"))
    clock = json.load(open(need("alignment.json")))["outbound_frame_per_inbound_frame"]

    frames = min(len(clock), raw_in.size // FRAME)
    track = np.zeros(outbound.size, dtype=np.float64)
    for i in range(frames):
        at = int(clock[i]) * FRAME
        if at + FRAME <= track.size:
            track[at:at + FRAME] = raw_in[i * FRAME:(i + 1) * FRAME]
    spans = [t.get("outbound_frames") for t in manifest["turns"]]
    if not any(spans):
        raise SystemExit(f"{call_dir} manifest has no outbound_frames; cannot mark turns.")
    return manifest, outbound, track, [s for s in spans if s], frames


def correlate(ours, theirs):
    """Cross-correlation peak between two equal-length windows."""
    if ours.size < FRAME * 5 or theirs.size < FRAME * 5:
        return None
    n = min(ours.size, theirs.size)
    a = ours[:n] - ours[:n].mean()
    b = theirs[:n] - theirs[:n].mean()
    if not a.any() or not b.any():
        return None
    full = np.correlate(b, a, mode="full") / (np.linalg.norm(a) * np.linalg.norm(b))
    lags = np.arange(-n + 1, n)
    lo = int(MIN_DELAY_MS * WIRE_RATE / 1000)
    hi = int(MAX_DELAY_MS * WIRE_RATE / 1000)
    window = (lags >= lo) & (lags <= hi)
    if not window.any():
        return None
    best = int(np.argmax(np.abs(full[window])))
    peak = float(np.abs(full[window])[best])
    delay_ms = float(lags[window][best]) * 1000.0 / WIRE_RATE
    baseline = float(np.median(np.abs(full[window])))
    return dict(peak=round(peak, 4), delay_ms=round(delay_ms, 1),
                baseline=round(baseline, 4),
                ratio=round(peak / baseline, 2) if baseline else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", default="")
    args = parser.parse_args()
    # Newest call under BENCH when none is named. `max` rather than sorted()[-1]
    # because the names are UTC stamps and lexical order IS chronological.
    call_dir = args.call or max(glob.glob(os.path.join(BENCH, "**", "call-*"),
                                          recursive=True))
    print(f"call: {call_dir}\n")

    _manifest, outbound, inbound, spans, placed = aligned(call_dir)
    print(f"outbound {outbound.size/WIRE_RATE:6.1f}s (gapless, this is the clock)")
    print(f"inbound  {placed} frames = {placed*0.02:.1f}s of audio, placed onto it\n")

    # The gaps BETWEEN his turns are the windows in which only we were talking.
    print("windows where we were speaking and he was not (by turn boundary):")
    print(f"{'window':<14}{'secs':>6}{'our RMS':>9}{'their RMS':>10}{'ratio dB':>10}"
          f"{'corr peak':>11}{'delay':>9}   verdict")
    rows = []
    previous = 0
    for index, span in enumerate(spans, start=1):
        start, end = int(span[0]), int(span[1])
        lo, hi = previous, start
        previous = end
        if hi - lo < 25:            # under half a second is not worth judging
            continue
        ours = outbound[lo * FRAME:hi * FRAME]
        # Inbound on the same clock: the frames whose wire_at fell in this span.
        theirs = inbound[lo * FRAME:hi * FRAME]
        found = correlate(ours, theirs)
        ratio_db = 20 * np.log10(rms(theirs) / rms(ours)) if rms(ours) and rms(theirs) else None
        if found is None:
            verdict = "nothing to judge"
        elif found["peak"] >= 0.30 and found["ratio"] >= 3.0:
            verdict = "ECHO"
        elif found["peak"] >= 0.15:
            verdict = "maybe echo"
        else:
            verdict = "independent (him)"
        print(f"before turn {index:<3}{(hi-lo)*0.02:>6.2f}{rms(ours):>9.4f}{rms(theirs):>10.4f}"
              f"{(f'{ratio_db:+.1f}' if ratio_db is not None else '   -'):>10}"
              f"{(found['peak'] if found else 0):>11.3f}"
              f"{(f'{found["delay_ms"]:.0f}ms' if found else '-'):>9}"
              f"   {verdict}")
        rows.append(dict(window=index, seconds=round((hi - lo) * 0.02, 2),
                         our_rms=round(rms(ours), 4), their_rms=round(rms(theirs), 4),
                         ratio_db=round(float(ratio_db), 1) if ratio_db is not None else None,
                         verdict=verdict, **(found or {})))

    echo = [r for r in rows if r["verdict"].startswith("ECHO")]
    print()
    if echo:
        worst = max(r["ratio_db"] or -99 for r in echo)
        print(f"ECHO CONFIRMED in {len(echo)}/{len(rows)} windows, worst at {worst:+.1f} dB "
              f"relative to what we sent.")
        print("Compare that against his enrolled speaking level before lowering the")
        print("barge-in bar: echo above the bar is the failure the 25-frame rule exists")
        print("for, and a window rule would cut us off on our own voice.")
    else:
        print(f"NO ECHO FOUND in {len(rows)} windows. Inbound audio arriving while we")
        print("spoke is independent of what we sent -- it is him talking over us, which")
        print("is what barge-in is supposed to catch. The 9-of-15 window rule in")
        print("barge-sweep.json is safe to ship on this evidence.")

    out = os.path.join(call_dir, "echo-check.json")
    with open(out, "w") as handle:
        json.dump(dict(call=os.path.basename(call_dir), windows=rows), handle, indent=2)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
