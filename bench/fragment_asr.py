"""Does cutting his speech at 150 ms pauses cost transcription quality?

Speculating at the first pause needs the transcript ready the moment he
pauses. Today a phrase is cut at a 350 ms pause and Whisper sees the whole
phrase; on the 2026-10-31 call each 150 ms pause re-sent the whole growing
phrase (2.6 -> 3.9 -> 4.2 -> 5.7 s of audio), so the last pass was the slowest.
Cutting at 150 ms instead means each pass is only the new audio - but Whisper
then hears shorter fragments with less context.

On every recorded turn: cut the way the endpointer does (frame RMS against a
floor-relative threshold) at 350 ms and at 150 ms, transcribe with the live
settings, and with the 150 ms cut also try passing the turn so far as
`initial_prompt`. Reports the texts side by side, how many words differ from
the 350 ms reference, and the Whisper time of each turn's LAST fragment - the
one that sits on the critical path.

    .venv/bin/python bench/fragment_asr.py
"""

from __future__ import annotations

import glob
import json
import pathlib
import sys
import time

import wave

import numpy as np

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / "src"), "/home/bodas/data/hotline/src"]
from hotline_ios.media.ears import Ears  # noqa: E402

RECORDINGS = pathlib.Path(__file__).resolve().parents[2] / "recordings"
SR = 16000
FRAME = SR // 50


def fragments(audio: np.ndarray, pause_ms: int) -> list[np.ndarray]:
    n = audio.size // FRAME
    rms = np.sqrt((audio[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1))
    thr = max(float(np.percentile(rms, 20)) * 3, 0.1 * float(np.percentile(rms, 95)), 0.004)
    need = pause_ms // 20
    out, start, quiet, spoke = [], 0, 0, False
    for i, level in enumerate(rms):
        if level >= thr:
            spoke, quiet = True, 0
        else:
            quiet += 1
            if spoke and quiet == need:
                out.append(audio[start * FRAME:(i + 1) * FRAME])
                start, spoke = i + 1, False
    if spoke or start < n:
        out.append(audio[start * FRAME:])
    return [f for f in out if f.size > FRAME * 5]


def run(ears: Ears, frags: list[np.ndarray], prompt: bool) -> tuple[str, float]:
    said, last = [], 0.0
    for f in frags:
        t = time.perf_counter()
        if prompt and said:
            segs, _ = ears._model.transcribe(f, language=ears.language, beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False,
                                             initial_prompt=" ".join(said))
            text = " ".join(s.text.strip() for s in segs).strip()
        else:
            text = ears.transcribe(f)
        last = time.perf_counter() - t
        if text:
            said.append(text)
    return " ".join(said), last


def main() -> None:
    ears = Ears(model="large-v3", language="sr")
    ears.load()
    rows = []
    for m in sorted(glob.glob(str(RECORDINGS / "call-*/manifest.json"))):
        d = pathlib.Path(m).parent
        for t in json.load(open(m)).get("turns", []):
            with wave.open(str(d / t["file"])) as w:
                rate, ch = w.getframerate(), w.getnchannels()
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
            if ch > 1:
                audio = audio[::ch]
            if rate != SR:
                import soxr
                audio = soxr.resample(audio, rate, SR).astype(np.float32)
            f350, f150 = fragments(audio, 350), fragments(audio, 150)
            ref, t350 = run(ears, f350, False)
            if len(ref.split()) < 2:
                continue
            bare, t150 = run(ears, f150, False)
            prom, t150p = run(ears, f150, True)
            row = {"turn": f"{d.name}/{t['file']}", "n350": len(f350), "n150": len(f150),
                   "ref": ref, "bare150": bare, "prompt150": prom,
                   "last_s": {"350": round(t350, 3), "150": round(t150, 3), "150p": round(t150p, 3)}}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    out = RECORDINGS / "bench-results/fragment-asr.json"   # his words: never in this public repo
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))

    def wdiff(a, b):
        import difflib
        sm = difflib.SequenceMatcher(a=a.lower().split(), b=b.lower().split())
        return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")

    for key in ("bare150", "prompt150"):
        diffs = [wdiff(r["ref"], r[key]) for r in rows]
        words = sum(len(r["ref"].split()) for r in rows)
        print(f"{key}: {sum(diffs)} words differ from the 350 ms cut over {words} words, "
              f"{sum(1 for x in diffs if x == 0)}/{len(rows)} turns identical")
    for k in ("350", "150", "150p"):
        v = sorted(r["last_s"][k] for r in rows)
        print(f"last-fragment Whisper time, cut {k}: median {v[len(v) // 2]:.3f} s, max {v[-1]:.3f} s")


if __name__ == "__main__":
    main()
