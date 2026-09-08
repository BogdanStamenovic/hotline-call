"""Work out what he is getting at while he is still saying it.

**Why this exists.** After he stops talking the pipeline costs about five
seconds: roughly a second to be sure he stopped, a second of Whisper, three for
the agent, two for cvoice. A person tolerates about one. Nothing in that chain
is going to get five times faster, so the fix is not a shorter wait -- it is a
wait that is not silent, and one that starts before he has finished.

**What it does, and deliberately does not do.** It classifies the partial
transcript into one of a few intents and nothing more. The intent selects a
pre-rendered holding phrase -- "aha, važi, da vidim" -- which is spoken the
instant he stops, at zero synthesis cost, while the real answer is still being
worked out. It never composes an answer, because a holding phrase asserts
nothing and therefore cannot be wrong. Measured on his own transcripts, a
speculative *answer* is flatly contradicted by the rest of the sentence 25% of
the time however much has been heard, so answers are never spoken unvalidated.

**Why an embedding model rather than an LLM.** This is a classification job, and
measured on twelve unseen Serbian phrasings on 2026-09-08:

    bge-m3 embeddings   12/12   10 ms
    keyword regex        5/12    0 ms
    qwen2.5:1.5b         5/12   39 ms
    qwen2.5:0.5b         4/12   25 ms
    piccolo-gorgone:9b   2/12  4693 ms

The generative models are worse at this than a regex, and the 9B is also far too
slow to sit in a turn. An encoder at 10 ms wins outright. The keyword numbers
are worth reading carefully: on a first test set written alongside the keyword
list they scored 10/12, and on phrasings they had not been tuned against they
collapsed to 5/12. They are kept only as a fallback.

**Degrading.** If ollama is not answering, this falls back to those keywords, and
if nothing matches, to the neutral intent. A missing embedder costs quality --
a less apt holding phrase -- never function.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "bge-m3"
NEUTRAL = "NEJASNO"

# What each intent sounds like. Never the test set, and never his live speech --
# these are fixed reference points, not something that learns during a call.
SEEDS: dict[str, list[str]] = {
    "PITANJE": ["Sta se desilo sa tim", "Zanima me kako je proslo", "Reci mi rezultat",
                "Hteo sam da pitam nesto", "Kakvo je stanje sa tim poslom"],
    "ZALBA":   ["Ne cujem te dobro", "Zvuk je uzasan", "Puca mi u usima",
                "Ovo ne radi kako treba", "Prekida se stalno"],
    "POZDRAV": ["Cao, kako si", "Evo me tu sam", "Dobro vece, sve u redu",
                "Zdravo, javljam se", "Sve je super kod mene"],
    "ZADATAK": ["Mozes li da uradis nesto za mene", "Proveri mi ovo molim te",
                "Pokreni to ponovo", "Posalji mi izvestaj", "Trebalo bi da popravis ovo"],
    NEUTRAL:   ["Pa", "E sad", "Ovaj", "Dakle", "Znaci"],
}

# The fallback. Ordered: the first match wins, so the specific ones come first.
KEYWORDS: list[tuple[str, str]] = [
    ("ZALBA",   r"\b(ne cujem|ne čujem|glitch|seck|seck|puc|ne zvuci|ne zvuči|lose|loše|kvar|krklja|smeta|lomi|grebe)"),
    ("ZADATAK", r"\b(mozes li|možeš li|proveri|uradi|posalji|pošalji|napravi|pokreni|restartuj|javi mi|treba mi)"),
    ("PITANJE", r"\b(sta |šta |zasto|zašto|kako to|da li je|da li ima|kada|gde|pitam|pitanje|zanima me|jel )"),
    ("POZDRAV", r"\b(cao|ćao|zdravo|kako si|sta se radi|tu sam|evo me|dobar dan|dobro jutro)"),
]

# Which pre-rendered clips answer which intent. Two each so a long call does not
# repeat one phrase until it grates.
HOLDING: dict[str, list[str]] = {
    "PITANJE": ["hold_pitanje_1", "hold_pitanje_2"],
    "ZALBA":   ["hold_zalba_1", "hold_zalba_2"],
    "POZDRAV": ["hold_pozdrav_1", "hold_pozdrav_2"],
    "ZADATAK": ["hold_zadatak_1", "hold_zadatak_2"],
    NEUTRAL:   ["hold_nejasno_1", "hold_nejasno_2"],
}


def _cosine_ranking(vec: list[float], centroids: dict[str, list[float]]) -> list[tuple[str, float]]:
    scored = [(name, sum(a * b for a, b in zip(vec, c))) for name, c in centroids.items()]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


class Speculator:
    """Partial Serbian in, an intent out. Never an answer."""

    # How far ahead the best intent must be before we believe it. Below this the
    # neutral phrase is used, which fits anything and commits to nothing.
    MIN_MARGIN = 0.02
    # Shorter than this is not enough to classify -- "pa", "e sad" and the like
    # are the neutral case by definition.
    MIN_WORDS = 2

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, model: str = DEFAULT_MODEL,
                 seeds: dict[str, list[str]] | None = None, timeout: float = 3.0):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.seeds = seeds or SEEDS
        self.timeout = timeout
        self.centroids: dict[str, list[float]] = {}
        self.embedder_ok = False

    # -- setup -------------------------------------------------------------

    def warm(self) -> bool:
        """Embed the seeds once, before the phone rings.

        Called at startup on purpose: the first embed loads the model, and doing
        that inside a live call would cost seconds at the worst possible moment.
        """
        try:
            for intent, examples in self.seeds.items():
                vectors = self._embed(examples)
                mean = [sum(col) / len(col) for col in zip(*vectors)]
                self.centroids[intent] = _normalise(mean)
            self.embedder_ok = True
            log.info("speculator warm: %d intents via %s", len(self.centroids), self.model)
        except Exception as exc:
            self.embedder_ok = False
            log.warning("speculator falling back to keywords: %s: %s",
                        type(exc).__name__, exc)
        return self.embedder_ok

    def _embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(f"{self.endpoint}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as fh:
            payload = json.loads(fh.read())
        vectors = payload.get("embeddings") or [payload["embedding"]]
        return [_normalise(v) for v in vectors]

    # -- the one thing it does --------------------------------------------

    def intent(self, partial: str) -> tuple[str, float]:
        """(intent, margin). Margin is 0.0 when it was decided without embeddings."""
        text = (partial or "").strip()
        if len(text.split()) < self.MIN_WORDS:
            return NEUTRAL, 0.0
        if self.embedder_ok:
            try:
                ranked = _cosine_ranking(self._embed([text])[0], self.centroids)
                best, second = ranked[0], ranked[1]
                margin = best[1] - second[1]
                if margin < self.MIN_MARGIN:
                    # Two intents fit equally well; say something that fits both.
                    return NEUTRAL, margin
                return best[0], margin
            except Exception as exc:
                # One failed call must not disable the feature for the rest of
                # the conversation, so this does not clear embedder_ok.
                log.debug("embed failed mid-call, using keywords: %s", exc)
        return self._keyword(text), 0.0

    @staticmethod
    def _keyword(text: str) -> str:
        low = text.lower()
        for intent, pattern in KEYWORDS:
            if re.search(pattern, low):
                return intent
        return NEUTRAL

    def holding_clip(self, intent: str, turn: int = 0) -> str:
        """Which pre-rendered clip to play for this intent."""
        options = HOLDING.get(intent) or HOLDING[NEUTRAL]
        return options[turn % len(options)]


def _normalise(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else list(vec)
