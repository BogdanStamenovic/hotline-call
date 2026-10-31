"""large-v3 against sam8000-turbo-serbian on the calls as they are now.

The September comparison (docs/MEASURED-telephony-voice.md) was run on 8 kHz
G.711 calls. Calls are Opus 48 kHz now, so both the speed and the accuracy
gap may have moved. This cuts every recorded turn into phrases the way the
live endpointer does (350 ms pauses), transcribes each phrase with the live
Ears settings, and records per-phrase wall time and the joined text.

One model per process (VRAM is only meaningful that way, see bench/README):

    .venv/bin/python bench/asr_ab.py large-v3
    .venv/bin/python bench/asr_ab.py /mnt/windows/.../ct2-converted/sam8000-turbo-serbian
    .venv/bin/python bench/asr_ab.py --compare

Results go to the gitignored recordings/bench-results/: they contain his words.
"""

from __future__ import annotations

import difflib
import glob
import json
import pathlib
import sys
import time
import wave

import numpy as np

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / "src"), "/home/bodas/data/hotline/src"]
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fragment_asr import RECORDINGS, SR, fragments  # noqa: E402

OUT = RECORDINGS / "bench-results"


def turns():
    for m in sorted(glob.glob(str(RECORDINGS / "call-*/manifest.json"))):
        d = pathlib.Path(m).parent
        manifest = json.load(open(m))
        for t in manifest.get("turns", []):
            with wave.open(str(d / t["file"])) as w:
                rate, ch = w.getframerate(), w.getnchannels()
                a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
            a = a[::ch]
            if rate != SR:
                import soxr
                a = soxr.resample(a, rate, SR).astype(np.float32)
            yield f"{d.name}/{t['file']}", manifest.get("codec", ""), a


def measure(model: str) -> None:
    from hotline_ios.media.ears import Ears

    ears = Ears(model=model, language="sr")
    t = time.perf_counter()
    ears.load()
    load = time.perf_counter() - t
    ears.transcribe(np.zeros(SR, dtype=np.float32) + 1e-3)  # first call pays CUDA warm-up
    rows = []
    for name, codec, audio in turns():
        texts, times = [], []
        for frag in fragments(audio, 350):
            t = time.perf_counter()
            text = ears.transcribe(frag)
            dt = time.perf_counter() - t
            if text:
                texts.append(text)
                times.append(round(dt, 3))
        rows.append({"turn": name, "codec": codec, "text": " ".join(texts), "phrase_s": times})
    tag = pathlib.Path(model).name
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"asr-ab-{tag}.json").write_text(json.dumps({"model": model, "load_s": round(load, 2), "rows": rows},
                                                       indent=1, ensure_ascii=False))
    speech = sorted(x for r in rows for x in r["phrase_s"])
    print(f"{tag}: load {load:.1f}s, {len(speech)} speech phrases, median {speech[len(speech) // 2]:.3f}s, "
          f"p90 {speech[int(len(speech) * 0.9)]:.3f}s, max {speech[-1]:.3f}s")


def compare() -> None:
    a = json.loads((OUT / "asr-ab-large-v3.json").read_text())
    b = json.loads((OUT / "asr-ab-sam8000-turbo-serbian.json").read_text())
    same = differ = 0
    for ra, rb in zip(a["rows"], b["rows"]):
        wa, wb = ra["text"].lower().split(), rb["text"].lower().split()
        sm = difflib.SequenceMatcher(a=wa, b=wb)
        d = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
        same += sum(i2 - i1 for op, i1, i2, j1, j2 in sm.get_opcodes() if op == "equal")
        differ += d
        if d:
            print(f"- {ra['turn']} [{ra['codec'][:12]}]\n    large-v3: {ra['text']}\n    turbo:    {rb['text']}")
    print(f"\nwords agreeing {same}, differing {differ}")


if __name__ == "__main__":
    if sys.argv[1] == "--compare":
        compare()
    else:
        measure(sys.argv[1])
