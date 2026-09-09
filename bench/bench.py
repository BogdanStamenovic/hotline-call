"""Score ASR models on HIS voice, HIS line, HIS conditions.

The handoff's "large-v3 beats sam8000 by 3.8 WER points on telephony" was
measured on somebody else's audio. This measures the same question on nine turns
of Bogdan reading a script designed to attack Serbian's hard contrasts, captured
as 8 kHz G.711 through linphone.org's relay.

Reports WER, VRAM and per-turn latency together, because a model that wins on
WER and does not fit beside cvoiced, or takes four seconds a turn, has not won.
"""
import argparse, ctypes, glob, json, os, subprocess, sys, time, wave
import numpy as np

BENCH = os.environ.get(
    "HOTLINE_IOS_BENCH_DIR",
    os.path.expanduser("~/data/hotline-ios/recordings/20260910-asr-benchmark"))
"""Where the call audio and reference live. NOT in the repo: it is his voice
and this repo is public. Override with HOTLINE_IOS_BENCH_DIR."""
sys.path.insert(0, "/home/bodas/data/hotline-ios/server/src")
from hotline_ios.media.ears import preload_cuda

CT2 = "/mnt/windows/Users/Korisnik/ai-models/hf-cache/ct2-converted"

MODELS = {
    "large-v3":            dict(path="large-v3",                          ct="int8_float16"),
    "large-v3-fp16":       dict(path="large-v3",                          ct="float16"),
    "sam8000-turbo-sr":    dict(path=f"{CT2}/sam8000-turbo-serbian",      ct="int8_float16"),
    "drishtisharma-med-sr":dict(path=f"{CT2}/drishtisharma-medium-serbian",ct="int8_float16"),
    "samil24-small-sr":    dict(path=f"{CT2}/samil24-small-serbian",      ct="int8_float16"),
    "medium":              dict(path="medium",                            ct="int8_float16"),
    "small":               dict(path="small",                             ct="int8_float16"),
}


def vram() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip().splitlines()[0]
    return int(out)


def turns(call_dir):
    found = []
    for path in sorted(glob.glob(os.path.join(call_dir, "turn-*.wav"))):
        with wave.open(path) as w:
            assert w.getframerate() == 8000, f"{path} is not 8 kHz"
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        # 8 kHz -> 16 kHz is what every Whisper build wants; the BAND is still
        # telephony, which is the point of the measurement.
        import soxr
        audio = soxr.resample(pcm.astype(np.float32) / 32768.0, 8000, 16000).astype(np.float32)
        found.append((int(os.path.basename(path)[5:7]), audio))
    return found


def run(name, spec, clips, beam=1, prompt=None, language="sr"):
    from faster_whisper import WhisperModel
    preload_cuda()
    base = vram()
    began = time.monotonic()
    try:
        model = WhisperModel(spec["path"], device="cuda", compute_type=spec["ct"])
    except Exception as exc:
        print(f"  {name}: LOAD FAILED {type(exc).__name__}: {str(exc)[:160]}")
        return None
    load_s = time.monotonic() - began
    hyp, times = {}, []
    for index, audio in clips:
        t0 = time.monotonic()
        segs, _ = model.transcribe(audio, language=language, beam_size=beam,
                                   vad_filter=True, condition_on_previous_text=False,
                                   initial_prompt=prompt)
        text = " ".join(s.text.strip() for s in segs).strip()
        times.append(time.monotonic() - t0)
        hyp[str(index)] = text
    peak = vram() - base
    del model
    import gc; gc.collect()
    time.sleep(1.0)
    return dict(hyp=hyp, load_s=load_s, vram=peak,
                median_s=float(np.median(times)), max_s=float(np.max(times)),
                audio_s=sum(a.size for _, a in clips) / 16000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--call", default=glob.glob(os.path.join(BENCH, "call-*"))[0])
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--out", default=BENCH)
    ap.add_argument("--suffix", default="")
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--prompt", default=None)
    args = ap.parse_args()

    clips = turns(args.call)
    print(f"{len(clips)} turns, {sum(a.size for _, a in clips)/16000:.1f}s of his speech")
    print(f"free VRAM at start: {8188 - vram()} MiB\n")

    summary = {}
    for name, spec in MODELS.items():
        if args.only and name not in args.only:
            continue
        label = name + args.suffix
        print(f"== {label} ({spec['ct']}) ==")
        result = run(name, spec, clips, beam=args.beam, prompt=args.prompt)
        if result is None:
            continue
        path = os.path.join(args.out, f"hyp-{label}.json")
        with open(path, "w") as fh:
            json.dump(result["hyp"], fh, ensure_ascii=False, indent=2)
        rtf = result["median_s"] / (result["audio_s"] / len(clips))
        print(f"  load {result['load_s']:.1f}s  vram {result['vram']} MiB  "
              f"median {result['median_s']:.2f}s/turn  max {result['max_s']:.2f}s  "
              f"rtf {rtf:.2f}x")
        summary[label] = dict(load_s=round(result["load_s"], 1), vram=result["vram"],
                              median_s=round(result["median_s"], 2),
                              max_s=round(result["max_s"], 2), rtf=round(rtf, 3))
        print()
    with open(os.path.join(args.out, f"cost{args.suffix or ''}.json"), "w") as fh:
        json.dump(summary, fh, indent=2)


if __name__ == "__main__":
    main()
