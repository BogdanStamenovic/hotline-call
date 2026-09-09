# Measured: Serbian ASR and barge-in on his actual phone line

Numbers from one real call — 2026-09-10 00:59:51Z to 01:01:18Z, 87 seconds,
nine turns, ten scripted lines he read aloud. Captured as 8 kHz G.711 through
linphone.org's relay at `176.31.149.179`. The audio and the scorer are in
`recordings/20260910-asr-benchmark/`, which is gitignored on purpose: it is his
voice and this repo is public.

Everything here is measured on **83 reference words**. That is a small sample and
it is the main limitation of the whole document — see *What these numbers cannot
tell you*.

## Word error rate

Three columns because two of them are misleading on their own.

- **raw** — what the model emitted, character for character.
- **norm** — digit strings spelled out. `16` for *šesnaest* is a formatting
  choice, not a mishearing, and counting it flatters a model that writes words.
- **translit** — Serbian Cyrillic folded to Latin as well. Serbian is officially
  digraphic; both scripts are correct Serbian. This column exists because the
  first run of the scorer nearly discarded two working models: they answer in
  Cyrillic and scored 96–101% against a Latin reference. Diacritics are **never**
  stripped in any column — č/ć and dž/đ are the distinction being measured.

| model | VRAM | load | median/turn | RTF | raw | norm | **translit** |
|---|---|---|---|---|---|---|---|
| `large-v3` + Serbian prompt | 2005 MiB | 3.9 s | 0.38 s | 0.10x | 42.2% | 36.1% | **36.1%** |
| `large-v3` int8_float16 | 2005 MiB | 3.3 s | 0.38 s | 0.10x | 51.8% | 45.8% | **37.3%** |
| `large-v3` beam 5 | 2005 MiB | 3.0 s | 0.41 s | 0.11x | 45.8% | 38.6% | 38.6% |
| `large-v3` live, chunked | 2005 MiB | — | — | — | 44.6% | 38.6% | 38.6% |
| `sam8000-turbo-serbian` beam 5 | 1173 MiB | 0.7 s | 0.23 s | 0.06x | 43.4% | 41.0% | 41.0% |
| `sam8000-turbo-serbian` | 1173 MiB | 1.0 s | 0.20 s | 0.05x | 44.6% | 42.2% | 42.2% |
| `drishtisharma-medium-serbian` | 1077 MiB | 0.6 s | 0.23 s | 0.06x | 101.2% | 100.0% | 49.4% |
| `medium` | 1109 MiB | 2.0 s | 0.26 s | 0.07x | 63.9% | 57.8% | 51.8% |
| `samil24-small-serbian` | 469 MiB | 0.4 s | 0.11 s | 0.03x | 102.4% | 97.6% | 57.8% |
| `small` | 469 MiB | 0.7 s | 0.10 s | 0.03x | 78.3% | 77.1% | 77.1% |
| `sam8000-turbo-serbian` + prompt | 1173 MiB | 0.7 s | 0.20 s | 0.05x | 100.0% | 100.0% | 100.0% |

**The `large-v3` family wins**, so the handoff's ranking survives contact with
his own voice. Its four variants cluster at 36.1–38.6% and the two
`sam8000-turbo-serbian` variants at 41.0–42.2%, so the margin is about four to
five points — around four words out of 83. That is not significant on its own;
what makes it worth acting on is that the direction is consistent across six
runs, not that any single pair separates.

**A correction, kept because it is the more useful part.** An earlier version of
this document reported that a Serbian `initial_prompt` made `large-v3` *worse*
(34.9% → 39.8%). That was an artefact of the scorer, not of the model. The digit
normaliser was a lookup table keyed on the exact number groups that appear in
this one reference, and a table tuned to a reference measures that reference.
Replaced with a general rule — anything longer than two digits is read out digit
by digit, which is how a person dictates a number down a phone — and the ranking
of the top two reversed. The prompt is now marginally *better* (36.1% vs 37.3%),
by one word, which means the honest conclusion is that **it makes no measurable
difference either way** and neither did the original claim.

**What is robust, across both scorers:**

- **A Serbian `initial_prompt` destroys `sam8000-turbo-serbian` outright**: it
  answers `","` for every single turn, 100% WER. Do not prime that finetune.
- **`beam_size=5` buys nothing** and costs 8% more time. The production
  `beam_size=1` is not a latency compromise; it is at least as accurate here.
- **Transcribing phrase-by-phrase as he talks scores the same** as transcribing
  the whole turn afterwards (38.6% vs 37.3%, one word apart). So the live
  pipeline's chunking costs no accuracy and it moves nearly all the recognition
  time under his own speech. Keep it.
- **The Cyrillic models are not broken.** `drishtisharma-medium-serbian` goes
  from 100.0% to 49.4% once its script is folded, and it is the best of the
  small models on content. Judged on the raw column it looks like a failure.

## Two thirds of those errors are not the model's fault

**Six of his nine turns begin mid-word.** Measured on the recorded audio: the
first 200 ms of turns 3, 4, 5, 6, 7 and 9 is at RMS 0.03–0.12 with no leading
silence at all, where turns 1, 2 and 8 start from digital silence.

| turn | first 200 ms RMS | starts |
|---|---|---|
| 1, 2, 8 | 0.0000–0.0001 | clean |
| 3 | 0.0977 | mid-word |
| 4 | 0.1210 | mid-word |
| 5 | 0.0727 | mid-word |
| 6 | 0.0327 | mid-word |
| 7 | 0.0401 | mid-word |
| 9 | 0.0732 | mid-word |

The cost is concentrated: turn 3 lost *"Džem i đevrek, džak i đubre"* entirely
and turn 4 lost the opening half of a dictated phone number. **19 of the 29 translit errors are
on those two turns alone** — words he said which never reached any model. Swapping
models cannot recover them; only capturing them can.

## Barge-in cannot fire, and that is arithmetic

`BARGE_IN_FRAMES = 25` required 25 **consecutive** 20 ms frames above the bar.
Natural speech does not do that: the gaps between words are short but not zero,
and each one resets the count.

- Longest consecutive run on his own recorded turns: **16 frames (320 ms)**.
- Required: 25.
- Barge-in events in the 87-second live call, across six turns he spoke over us:
  **zero**.

A sliding window is what every VAD uses. At the same bar, **9 of the last 15
frames** fires on **9/9** of his turns and on **0/6** synthetic room-noise and
distant-chatter cases (`barge-sweep.json`). Five settings separate him from his
room completely; the fastest is 8 of 10 (200 ms).

**It is deliberately not changed yet.** The interferer that decides the question
is our own audio echoing back off his handset, which is the exact failure the 25
was written to fix — *"on the first live conversation it cut off every single
utterance about a sixth of a second in"*. In the windows where we were speaking,
inbound audio measured RMS 0.03–0.09 — **louder than his own enrolled level of
0.0399** — and 100% of it passed the voice profile. That is either him talking
over us (in which case firing is correct) or our voice coming back (in which case
firing is the old bug). The inbound stream alone cannot tell them apart, and
wall-clock offsets do not survive his phone's silence suppression. So the
outbound stream is now recorded alongside the inbound one, tagged frame by frame,
and the constant changes when there is a call to measure it against.

## What the voice profile does and does not do

`_sounds_like_him` compares an 8-band spectral envelope, threshold 0.82. Against
the same barge-in bar:

| signal | RMS | frames over the bar | of those, "sounds like him" |
|---|---|---|---|
| him (turn 4) | 0.0563 | 60/156 | 100% |
| room noise @0.02–0.2 | 0.02–0.2 | 0–150/150 | **0%** |
| distant chatter @0.02–0.2 | 0.02–0.2 | 30–150/150 | **100%** |

So it is a **noise** filter, not a **speaker** filter. It rejects hum and hiss
completely and admits anything speech-shaped. Synthetic chatter louder than about
6 dB below his own level triggers the level test and then sails through the
profile. Whether real distant chatter is as easy to fake is not established here
— the interferer is synthetic, and that is the honest limit of this particular
result.

## Enrolment understates his voice

`enrol_voice` takes the frames above the 60th percentile as "his voice" and the
rest as "the gaps between words". On a turn that is more than 60% silence, the
60th percentile **is** silence. Turn 1 measured:

    p20 0.0001   p60 0.0003   p90 0.0728   max 0.2093

so the "loud half" admitted near-silent frames. Whole-turn enrolment gives
`his_level` 0.0399 against a true speaking level nearer 0.07–0.10, and the live
path — which also sees the retained pre-roll — reported **0.0107**, roughly 4x
low. Every threshold derived from `his_level` inherits that error.

## What these numbers cannot tell you

- **83 reference words.** The gap between the top two models is about four word
  errors, and swapping one defensible scoring rule for another already reversed
  their order once. It is not significant, and neither is any other gap under
  roughly 10 points here. Ranking the top two would need on the order of a
  thousand reference words — about ten more calls of this length.
- **One call, one handset, one network path, one speaker.** Nothing here
  generalises to a different phone or a quieter room.
- **35% WER is a bad number and it is the ceiling as things stand**, not a
  target. Most of it is recoverable by capturing what he says rather than by
  changing models, which is where the effort should go.
- The interferers in the barge-in tests are **synthetic**. They establish that
  the profile fails against speech-shaped audio; they do not establish how loud
  real chatter has to be.
