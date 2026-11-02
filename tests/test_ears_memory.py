"""Ears gives memory back after a call, and a CPU fallback does not stick.

Both measured on the live daemon (2026-11-02): RSS stayed ~1.5 GB above its
baseline after each unload until malloc_trim, and one GPU out-of-memory left
every later call on the small CPU model until a restart."""

from __future__ import annotations

import sys
import types

from hotline_call.media import ears as ears_mod
from hotline_call.media.ears import Ears


def fake_whisper(monkeypatch, fail_cuda_times: int):
    made = []

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            if device == "cuda" and fail_cuda_times > len([m for m in made if m[1] == "cuda-fail"]):
                made.append((name, "cuda-fail"))
                raise RuntimeError("CUDA failed with error out of memory")
            made.append((name, device))

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(ears_mod, "preload_cuda", lambda: 0)
    return made


def test_unload_hands_the_heap_back(monkeypatch):
    trims = []
    monkeypatch.setattr(ears_mod, "_trim", lambda: trims.append(1))
    fake_whisper(monkeypatch, fail_cuda_times=0)
    e = Ears()
    e.load()
    assert e.unload() is True
    assert trims == [1]


def test_a_cpu_fallback_is_for_that_load_only(monkeypatch):
    monkeypatch.setattr(ears_mod, "_trim", lambda: None)
    made = fake_whisper(monkeypatch, fail_cuda_times=1)
    e = Ears(model="large-v3", device="cuda")
    e.load()                                    # GPU full: falls back
    assert e.device == "cpu" and made[-1][1] == "cpu"
    e.unload()
    e.load()                                    # next call: GPU again
    assert e.device == "cuda" and e.model_name == "large-v3" and made[-1] == ("large-v3", "cuda")
