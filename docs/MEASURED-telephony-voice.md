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

## What the second call (2026-09-10 01:41Z) changed, and what it did not

110 seconds, eleven scripted turns plus two of deliberate gibberish, on the code
with the retention fix in. Same script, same scorer, and **he read line 6 as
written this time** — "u pola devet", where on the first call he said "pola pet"
and told us so. The reference belongs to the call, not to the script.

| | 22:59 call | 01:41 call |
|---|---|---|
| total, translit | 38.6% | **37.3%** |
| line 5, the dictated number | 72.7% | **18.2%** |
| lines 3+4 together | 6 errors / 12 words | 7 errors / 12 words |
| line 1 | 0.0% | 40.0% |
| line 2 | 0.0% | 33.3% |
| turns beginning mid-word | 6 of 9 | 0 of 11 |

**The capture fix did exactly what it was supposed to on the line that proved
it.** Line 5 went from `"321 207"` to `"Moj broj je 065... 3, 2, 1... 207"` — the
opening words that had never reached the model arrived, and that line's error
rate fell 54.5 points. Line 3, which the first call lost entirely, is now
present.

**And the total did not move.** 38.6% to 37.3% is one word on 83, which is
noise. Three things ate the gain:

- Line 3 is now *heard* and *mis-transcribed* (5 substitutions) where before it
  was *absent* (6 deletions). Nearly the same score for a much better recording.
- Line 4 slipped from 0 to 2 errors.
- **Lines 1 and 2 went from 0.0% to 40.0% and 33.3%.** Those were perfect on the
  first call. The most likely cause is mine: retaining up to three seconds of
  pre-roll hands Whisper more leading ambient, and this call's turn 1 was 7.4 s
  of which 86% was silence because I asked him to stay quiet through the
  greeting. Two `"Hvala vam."` hallucinations were caught and dropped in that
  same window.

So the honest statement is: **the words that were being lost are now being
captured, and that has not yet produced a better transcript.** The earlier claim
that two thirds of the WER was recoverable by fixing capture is supported at the
level of the specific lost words and **not** supported at the level of the total.
It may need the leading ambient trimmed before it shows up.

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

**Changed on 2026-09-10 after the echo was measured rather than feared.** The
interferer that decided the question was our own audio coming back off his
handset — the exact failure the 25 was written to fix, *"on the first live
conversation it cut off every single utterance about a sixth of a second in"*.

`bench/echo_check.py` cross-correlates the two recorded directions on the
outbound frame clock. Validated in both directions first, on a loopback whose
stand-in handset can be told to leak what it hears back into its microphone:

| condition | correlation peak | measured ratio |
|---|---|---|
| 0.5 amplitude leaked back (−6.02 dB) | **1.000** at 220 ms | −6.0 dB |
| no leak | 0.019 at 356 ms | −31.0 dB |
| **his real call, 01:41Z** | **0.020** at 344 ms | −8.4 dB |

His line reads at the noise floor of the instrument. **There is no echo on this
path**, and the audio arriving while we speak is him. The rule is now six of any
ten frames — 200 ms — at the same bar: 11/11 of his turns, 0/8 of synthetic room
noise, distant chatter and that call's own measured ambient of 0.0199.

Two things about that number worth keeping. It rests on **one** 9.68-second
window from one call, though it agrees with a validated negative control. And
`their RMS` in that window was 0.0199 — not silence — while calibration in the
first 1.2 s reported `line noise floor 0.0000`, which is the same
too-early-to-measure problem enrolment had.

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

## Enrolment measured the silence, not his voice — fixed

`enrol_voice` takes the frames above the 60th percentile as "his voice" and the
rest as "the gaps between words". On a turn that is more than 60% silence, the
60th percentile **is** silence. Turn 1 measured:

    p20 0.0001   p60 0.0003   p90 0.0728   max 0.2093

so the "loud half" admitted near-silent frames. Measured across the eleven turns
of the 01:41 call, which are 39–86% silence:

| rule | median | spread across turns |
|---|---|---|
| p60 (was) | 0.0401 | **0.0010 – 0.0776, a factor of 78** |
| p85 (now) | 0.0985 | 0.0647 – 0.1497, a factor of 2.3 |
| ≥20% of peak | 0.0803 | 0.0494 – 0.1241 |

The old rule was measuring **how much silence the turn happened to contain**. On
the 01:41 call it returned `his_level` **0.0010**, putting the barge-in bar at
0.0006 — below that line's own ambient of 0.0199 — and on the 22:59 call 0.0107.
p85 is now used: still not the true speaking level, but stable, and every
threshold in this class derives from it so stability matters more than precision.
With it, the old 25-consecutive barge-in fires on **0 of 11** of his turns, which
is how that rule was finally shown to be impossible rather than merely untuned.

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
