"""Whisper, tuned for a telephone rather than for a microphone.

Separate from `hotline.audio.Transcriber` because the two want opposite
settings, not because of duplication. That one is fed already-segmented Discord
audio and turns the VAD off on purpose -- "doing it twice clips words". This one
is fed phrases off an 8 kHz phone line, where the last frames of a phrase are
routinely trailing silence, and Whisper does not return nothing for silence: it
invents. Measured on this box, two seconds of room noise transcribes as
*"Hvala vam."* every single time.

Three defences, in order of how much they catch:

1. `vad_filter=True`. Silence comes back empty, it is 6x faster on silence
   (0.45 s to 0.07 s), and real speech is byte-identical. It uses
   faster-whisper's own bundled Silero ONNX, which is why it is available at all
   on a box with no torch.
2. `condition_on_previous_text=False`, or one invention becomes context for the
   next phrase and repeats down the whole turn.
3. A blocklist, because one hallucinated phrase per chunk is enough to make an
   agent answer a question he never asked -- which it did.

**Where the CUDA libraries come from.** `ctranslate2` wants `libcublas.so.12`
and this box's system CUDA is 13, so the GPU is unreachable from this venv. The
libraries do exist, in cvoice's virtualenv, and are loaded here with RTLD_GLOBAL
before ctranslate2 asks for them. Borrowing beats reinstalling 1.2 GB of wheels
onto a root partition at 81%. If the borrow fails this falls back to the CPU and
says so, which costs latency and nothing else.

Measured on 2026-09-09, on 2.5 s of his cloned Serbian through a real G.711
roundtrip: `large-v3` cuda/int8_float16 **0.38 s** and word-perfect;
`large-v3` cpu/int8 5.39 s for the same text; `small` cpu/int8 0.99 s but
*"Zdravo o The Hotline, imam pitanju za tebe"* -- fast and wrong, which on a
phone call is the worse failure.
"""

from __future__ import annotations

import ctypes
import gc
import glob
import logging
import os
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

CUDA_WHEELS = "/home/bodas/data/cvoice/.venv/lib/python*/site-packages"
"""Where the cu12 libraries live on this box. Overridden by HOTLINE_IOS_CUDA_LIBS."""

# Enough audio to be worth asking about. Below a quarter of a second there is
# nothing in it but the tail of a pause.
MIN_SAMPLES = 4000

HALLUCINATIONS = {
    "hvala vam", "hvala na gledanju", "hvala", "titlovi", "prevod",
    "prodavanje", "продавање", "хвала вам", "titlovi hrt", "podnapisi",
    "amara.org", "subscribe", "thanks for watching", "thank you",
}
"""Phrases Whisper produces out of near-silence, not out of him. Written without
trailing punctuation; `_is_invented` strips it before comparing."""


def preload_cuda(pattern: str = "") -> int:
    """Put the CUDA libraries in the process before ctranslate2 looks for them.

    They are dlopened by soname off the linker path, and LD_LIBRARY_PATH cannot
    be changed from inside a running process -- so loading them explicitly with
    RTLD_GLOBAL is the only way to do this without relaunching.
    """
    roots = glob.glob(pattern or os.environ.get("HOTLINE_IOS_CUDA_LIBS", "") or CUDA_WHEELS)
    loaded = 0
    for root in roots:
        for lib in sorted(glob.glob(f"{root}/nvidia/*/lib/*.so*")):
            if not os.path.basename(lib).startswith(
                    ("libcublas", "libcudnn", "libcudart", "libnvrtc")):
                continue
            try:
                ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                loaded += 1
            except OSError:
                continue
    return loaded


def _is_invented(text: str) -> bool:
    return text.lower().strip(" .,!?…").strip() in HALLUCINATIONS


class Ears:
    """One Whisper model, and the phone-call settings around it."""

    # A screen, not a verdict, and both have to fail before anything is dropped.
    #
    # The reference point is measured rather than assumed: his own voice through
    # a real G.711 roundtrip on 2026-09-09 scored `no_speech=0.127,
    # avg_logprob=-0.123`, which is nowhere near either bar. On 2026-09-10 a turn
    # whose VAD had discarded 13.26 of 14.48 seconds produced the English phrase
    # "Hello there!" out of a Serbian model -- and I could not reproduce that
    # synthetically, so this is deliberately loose. It exists to catch the
    # obviously-bad, and the per-segment numbers are now logged either way so the
    # next occurrence is diagnosable instead of arguable.
    NO_SPEECH_MAX = 0.6
    LOGPROB_MIN = -0.8

    def __init__(
        self,
        model: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "int8_float16",
        language: str = "sr",
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model: Any = None

    def __repr__(self) -> str:
        return f"Ears({self.model_name} on {self.device}/{self.compute_type}, {self.language})"

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        if self.device == "cuda":
            found = preload_cuda()
            log.info("borrowed %d cuda libraries for ctranslate2", found)
        began = time.monotonic()
        try:
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type)
        except Exception as exc:
            if self.device != "cuda":
                raise
            # A full card or a missing library should cost latency, not the
            # call. `small` on this CPU is 0.36x realtime, which is slow and
            # usable; no transcriber at all is a phone call that cannot hear.
            log.warning("GPU transcriber unavailable (%s); falling back to CPU", exc)
            self.device, self.compute_type = "cpu", "int8"
            self.model_name = os.environ.get("HOTLINE_IOS_ASR_CPU_MODEL", "small")
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        log.info("%r ready in %.1fs", self, time.monotonic() - began)

    def unload(self) -> bool:
        """Drop the model and hand the VRAM back. Idempotent.

        His rule, 2026-09-11: *"nothing should be loaded prematurely ... after
        the call is done then everything unloaded."* `large-v3` int8_float16
        holds 1,918 MiB, and it used to hold it from boot to shutdown whether a
        call ever happened or not.

        ctranslate2 frees the device memory when the `WhisperModel` is
        collected, so the drop has to be a real drop -- hence the explicit
        `gc.collect()` rather than trusting refcounting through faster-whisper's
        own wrapper objects. Returns whether there was anything to unload, so a
        caller tearing down after a call need not track who loaded it.
        """
        if self._model is None:
            return False
        self._model = None
        gc.collect()
        log.info("%r unloaded", self)
        return True

    def transcribe(self, audio: np.ndarray) -> str:
        """What he said, or an empty string. Never a guess at silence."""
        if audio is None or audio.size < MIN_SAMPLES:
            return ""
        self.load()
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=1,          # a spoken turn is not worth a beam search
            vad_filter=True,
            condition_on_previous_text=False,
        )
        kept = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            no_speech = float(getattr(segment, "no_speech_prob", 0.0))
            logprob = float(getattr(segment, "avg_logprob", 0.0))
            log.info("heard %r (no_speech=%.3f avg_logprob=%+.3f, %.2fs)",
                     text, no_speech, logprob, segment.end - segment.start)
            if _is_invented(text):
                log.info("dropped an invented phrase: %r", text)
                continue
            if no_speech >= self.NO_SPEECH_MAX and logprob <= self.LOGPROB_MIN:
                log.warning("dropped %r as low confidence (no_speech=%.3f "
                            "avg_logprob=%+.3f)", text, no_speech, logprob)
                continue
            kept.append(text)
        return " ".join(kept).strip()
