# Bench: measuring the phone leg without ringing him

Four tools that score ASR models and barge-in thresholds against a **recorded
real call**, so a threshold can be changed and re-checked as often as you like
without costing him a phone call.

The audio is **not** here and never will be: it is his voice and this repo is
public. `recordings/` is gitignored. Point these at a call directory with
`HOTLINE_IOS_BENCH_DIR`, which defaults to
`~/data/hotline-ios/recordings/20260910-asr-benchmark`.

A call directory is what `media/record.py` writes when `HOTLINE_IOS_RECORD_DIR`
is set — `turn-NN.wav` at 8 kHz, `inbound.wav`, `outbound.wav` and
`manifest.json` — plus a `reference.json` you write by hand saying what he
actually said.

| tool | question |
|---|---|
| `bench.py` | which ASR model, at what VRAM and what latency |
| `score.py` | word error rate, three ways |
| `noise_bench.py` | does the room trigger barge-in, and can he still interrupt |
| `barge_sweep.py` | which barge-in thresholds separate him from his room |

    HOTLINE_IOS_BENCH_DIR=... python bench.py --only large-v3
    python score.py $DIR/reference.json $DIR/hyp-large-v3.json
    python noise_bench.py
    python barge_sweep.py

## reference.json

    {
      "call": "call-20260909-225940",
      "lines": ["Da li me čuješ dobro?", ...],
      "turn_to_lines": {"1": [1], "2": [2], "3": [3, 4], ...}
    }

**Ground truth is what he ACTUALLY said, not the script you handed him.** On the
2026-09-10 call he read "u pola pet" where the script said "u pola devet" and
told us so afterwards; scoring against the script would have marked a correct
transcription wrong.

`turn_to_lines` exists because turn boundaries are decided by his pauses, not by
your line numbers. One turn regularly holds two lines.

## Limitations

- **A small reference is a weak measurement.** 83 words on the 2026-09-10 call.
  Gaps under roughly ten WER points are not significant there, and replacing one
  defensible digit-normalisation rule with another already reversed the top two
  once. See `docs/MEASURED-telephony-voice.md`.
- **The interferers in `noise_bench.py` are synthetic.** Hum, hiss and
  formant-shaped chatter, low-passed for distance. They establish that the voice
  profile rejects noise and admits anything speech-shaped; they do not establish
  how loud real chatter has to be.
- **Neither noise tool models our own audio echoing back off his handset**,
  which is the interferer that decides whether barge-in can be made more
  sensitive. That needs `outbound.wav` cross-correlated against `inbound.wav`,
  which the recorder now captures and nothing yet analyses.
- `bench.py` measures VRAM as a delta around one load, so run one model per
  process. Two in a row report nonsense, because the first model's memory is
  freed asynchronously — the first version of this tool reported -374 MiB.
