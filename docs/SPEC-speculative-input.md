# Speculative input — spec

Predict how Bogdan finishes a sentence while he is still saying it, start the
real answer on that prediction, and throw the work away if the rest of the
sentence contradicts it. The point is that when he stops talking, the answer is
already there.

His design, 2026-09-10, in his words:

> i say something and a small model classifies intent [...] It tries to finish
> the sentence through context of the call the build. [...] And then every next
> passage which gets transcribed is checked to see if it contradicts the current
> SI, if so another SI is run, the sonnet fork deleted and another run started.
> That is the point in SI.

## Why: the budget this exists to spend

Measured on this box, in the order they happen after he stops speaking:

| leg | cost | measured |
|---|---|---|
| endpointing — being sure he stopped | ~1 s | `conversation.py` turn logic |
| Whisper on the final phrase | ~0.4 s | 0.38 s median/turn, `MEASURED-telephony-voice.md` |
| the Sonnet turn | **2.7–3.0 s** | live, 2026-09-10 |
| cvoice synthesising the reply | ~2 s | `media/tts.py` |
| **total** | **~5–6 s** | against a person's tolerance of about 1 s |

Nothing in that chain gets five times faster. Speculation is the only way to
spend the agent leg and the TTS leg *before* he has finished the sentence.

**What is already banked, and must not be double-counted.** A holding phrase
("aha, važi") is played from disk the instant he stops, at zero synthesis cost.
That already covers the silence. **Speculation does not make the phrase come
sooner — it makes the real answer arrive sooner.** Anyone measuring "time to
first sound" will see no improvement and conclude this does nothing.

## How it works

```
he speaks ──► phrase 1 ──► phrase 2 ──► phrase 3 ──► (stops)
                 │            │            │             │
                 ▼            ▼            ▼             ▼
             predict      contradicts?  contradicts?   validate
             completion    no → keep     YES → squash   whole
                 │                         │           transcript
                 ▼                         ▼             │
            dispatch Sonnet          kill, re-predict,    ├─ held → speak the
            on the prediction        dispatch again      │   precomputed answer
                                                          └─ missed → run for real
```

`conversation.py:listen()` already submits each phrase to a worker as he
finishes it, so partial transcripts genuinely exist mid-turn. Today nothing
reads them until the turn ends. **That join is the hook.**

### The three parts

| part | job | latency budget |
|---|---|---|
| **predictor** | partial → predicted full utterance | < 200 ms |
| **contradiction check** | does the new phrase invalidate the standing prediction? | < 50 ms |
| **speculative executor** | dispatch / kill / retire Sonnet runs | free (threads) |

### Retirement, which is the part that decides correctness

A CPU may speculate freely because results sit in a reorder buffer and are never
committed until the branch resolves. **A Claude fork has no reorder buffer**, so
retirement is our job:

1. **Never speak an unvalidated answer.** Measured on his own transcripts, a
   speculative *answer* is contradicted by the rest of the sentence 25% of the
   time however much has been heard. Read the other way, **75% of forks land** —
   a good hit rate for speculative execution, and the reason this is worth
   building. The 25% is a wasted-fork rate, not an error rate, *provided* the
   answer is validated before it is spoken.
2. **Contradiction is the cheap early squash. Equality is the gate.** "Restart
   the daemon" → "…but not tonight" contradicts nothing and the precomputed
   answer is wrong. So chunk-by-chunk contradiction checking only decides *when
   to give up early*; the decision to actually speak a precomputed answer must
   compare the whole real transcript against the speculated one.
3. **Speculative forks must not act.** Done — see below.

## What is already built

| | state |
|---|---|
| phrase-by-phrase transcription mid-turn | **live**, `conversation.py:listen()` |
| holding phrase from disk on turn end | **live**, hardcoded `hold_nejasno_2` |
| `speculate.py` intent classifier + 10 intent-keyed phrases | **built, imported by nothing** |
| read-only call agent | **live**, `b5a640d` |
| Serbian → English in the same Whisper call | **available**, `task="translate"` |
| the predictor | does not exist |
| the contradiction check | does not exist |
| the speculative executor | does not exist |

`speculate.py` predicts a *category* to pick a nicer noise. It is not this, and
wiring it would not deliver this. Keep it or delete it on its own merits.

## Read-only forks — DONE, and it is architecture not just safety

His instruction: *"sonnets either way must be read only. As they should relay
the answer back to the original session. That's the whole point. The big opus
session acts on it not the sonnets."*

`callagent.py` now runs with `--tools=Read,Grep,Glob --restricted
--permission-prompts none`. An allow-list rather than a deny-list, so it cannot
be wrong by omission; `--restricted` ignores user/project/local settings so a
broad rule cannot leak in; prompts are denied rather than waited on, because a
prompt nobody can see is a silent phone line.

Verified live: asked to run a command or delete a file it answers *"Ne mogu sam
… ali prosleđujem dalje"*; asked what was fixed today it answers from context.

**This is what makes speculation safe at all.** N forks that can only read are N
wasted reads. N forks that could push are N chances to push something he never
finished asking for.

## Models are loaded on demand, not held

His instruction: *"When a call is made. Or when i call, whisper and cvoice should
load only then. Not always be loaded."*

Both already load lazily; **neither unloads**. Measured 2026-09-10:

| | |
|---|---|
| Whisper `large-v3` load | **4.78 s**, **+1,923 MiB** |
| constructing `Ears` without loading | 0 s, 0 MiB |
| transcribe once loaded | 0.02 s |
| cvoice | ~2.2 GiB, seconds, by its own engine docs |

**Load on RING, not on ANSWER.** The ring lasts up to 45 s and both models
together are under ten. `CallAgent.start()` already seeds during the ring for
exactly this reason. Unload at call end.

**A live bug this fixes:** because `Ears.load()` is called from `transcribe()`,
that 4.78 s is currently paid **in the middle of his first sentence** on the
first call after any restart. Every call so far has had a warm daemon, so nobody
has heard it.

For inbound calls there is no ring to hide behind, but we choose when to send
the 200 OK — it rings two or three seconds longer while loading.

**This is what makes the predictor affordable at all.** Idle GPU between calls
is the only reason there is room for a third model.

## The predictor

### It works in English, and that is not a compromise

Whisper translates Serbian to English in the same call, on the model already
resident, at no extra cost — **44.8 s to translate 9 clips against 47.2 s to
transcribe them**. And his own writing to his agents is **239 English-only
messages against zero Serbian-only**: he types to agents in English, so his
English idiolect is the thing a predictor can actually be trained on.

Against the phonetic benchmark, translation scores **52.5% WER** where Serbian
transcription scores 36.1% — but that set is tongue-twisters, the worst case for
a translator, and the English WER is measured against one hand-written reference
which punishes valid paraphrase as hard as error. On his one real conversational
sentence it went the other way: Serbian carried `fotovoltačnom mesaju` forward,
English produced *"just tell me that everything is working perfectly and that's
it"* — his actual meaning. **n=1. An observation, not a measurement.**

**The risk is the same mechanism.** Forced to produce fluent English, it
invents: *"I'm done with the fire"* is not anything he said.

### Do NOT swap models per turn

Rejected explicitly. `large-v3` costs 4.78 s to load. The entire prize is 3–5 s
per turn. A load/unload cycle inside a turn spends the prize to buy it. Per
*call* loading is free because it hides in the ring; per *turn* loading is not.

### Candidate, unresolved

Research, 2026-09-10: no purpose-built completion model exists that is
downloadable and none are Serbian. The production pattern (Deepgram Flux) skips
predicting the text and fires the real generation early on the ASR partial,
cancel-on-contradiction — his design minus the predictor, and **worth
prototyping first because it needs no new model at all**. Amazon's nearest
published system predicted only **28% of utterances**, gained **356 ms** average
lead, with a **20% false-prefetch rate**.

For Serbian and for small models there is **no published generation-quality
benchmark below ~8B on anything resembling this task**. Base-model choice has to
be settled by our own eval, not by paper credentials nobody has.

## The finetune

Corpus built 2026-09-10 at `~/data/si-corpus`, **deliberately outside both git
repos** — it is 407 of his private messages and this repository is public.

| | |
|---|---|
| training pairs | 1,086 (977 train / 109 val) |
| **distinct source utterances** | **362** — the honest number |
| his real typed messages, ≥6 words | 189, after dropping 9 carrying an email, a credential, an IP or a long number |
| synthetic spoken-register utterances | 182, Sonnet few-shot on 60 real messages |
| real spoken examples in existence | **4**, held out, never trained on |

Each utterance is cut at 30/50/70%, so three pairs share a source. **1,086 is
not 1,086 independent examples.** Against the 300–1,000 floor the research
cites, this clears it only if the cuts count, which they do not really.

**Training**: Unsloth QLoRA, 3B at 4-bit needs 3.5 GB — it fits the 4060 only
with Whisper and cvoice unloaded, so it is an offline job that takes the phone
down while it runs. On-demand loading above makes that a scheduling question
rather than a conflict. Profiled on this exact GPU with Qwen2.5-1.5B: 500 tok/s,
628 with a paged optimiser.

**The eval does not exist.** Four held-out utterances are a smoke test. A real
eval needs accumulated real calls, which means logging every call transcript
starting now so the set builds itself.

## The metric

Bake it into the runtime, not a one-off study:

> **What fraction of turns have an answer ready the instant he stops talking**,
> and the mean added latency on the turns that miss.

Secondary: wasted forks per turn, squashes per turn, and how often a *retired*
prediction produced an answer that was wrong anyway — the only one that measures
harm rather than cost.

## Limitations

- **Nothing here is built** except the read-only forks and on-demand loading
  groundwork. No predictor, no contradiction check, no executor exists.
- **No model has been trained**, so there is no evidence any of this predicts
  anything. Every quality claim is inherited from other people's papers on other
  languages.
- **36% WER is the floor on everything.** The predictor eats a transcript a
  third of which is wrong. Validation compares two equally noisy transcripts, so
  it does not make speculation relatively worse — but it caps the ceiling, and
  no amount of speculation fixes a system that misheard him.
- **The corpus register is wrong for half the data.** His real half is typed;
  the task is spoken. The spoken half is written by a model, not by him.
- **The 25% contradiction figure is from his own transcripts but a small
  sample**, and it was measured to justify not speaking speculative answers, not
  to size a fork budget.
