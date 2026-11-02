"""Per-call switches for every addition since 2026-10-30, so each can be heard alone.

His request (2026-10-31): "I want to test out each addition alone until i pick
and choose what stays and what goes." So every addition that changes how a
call sounds or behaves has a switch here, the file is read at the start of
EVERY call (no restart: flip, ring, listen), and the defaults are what was
live when this was written.

The file is ~/.config/hotline-call/features.toml (or $HOTLINE_CALL_FEATURES),
flat `name = value`. A missing file, a missing key, or a value of the wrong
type means the default; a bad file is logged and ignored, never a failed call.
`hotline-call features` lists and sets them.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass

log = logging.getLogger("hotline-call.features")


@dataclass(frozen=True)
class Feature:
    name: str
    default: object
    choices: tuple = ()
    what: str = ""


FEATURES = (
    Feature("streaming", True, (), "Haiku's tokens streamed into the voice clause by clause (off: whole reply, then speak it)"),
    Feature("tts_steps", 8, (32, 16, 8), "voice diffusion steps (32 was the original)"),
    Feature("join", "roomtone", ("roomtone", "zeros", "library"), "how voice chunks are joined: room tone / silent pauses / library padding (first call)"),
    Feature("openers", True, (), "cached pre-rendered openers + one-word first chunk"),
    Feature("opener_nudge", True, (), "Haiku told to open with a cached opener"),
    Feature("intent_fillers", True, (), "play 'Hm, pa...' (question) / 'Aha, ahhh...' (directive) the moment he stops; the voice then answers directly"),
    Feature("filler_words", True, (), "let Haiku open with 'Važi,' / 'Samo sekund,' (off: answer directly, no fillers)"),
    Feature("thinking", False, (), "let Haiku think before answering (on doubles the tokens)"),
    Feature("tools", True, (), "read-only lookups: read_file / grep / glob / git_log"),
    Feature("context_rules", True, (), "today's date + 'don't guess, don't volunteer stale context' rules"),
    Feature("keepalive", True, (), "keep the API connection open between turns, warmed while ringing"),
    Feature("speculation", True, (), "start answering at the first 150 ms pause"),
    Feature("streamed_greeting", True, (), "greeting and apologies through the streamed voice"),
    Feature("bridge", True, (), "'spoji me sa <agent>' puts a live agent on the call"),
    Feature("bridge_mode", "direct", ("direct", "relay"), "connected agent speaks as-is, or re-spoken by Haiku"),
    Feature("gpu_lease", True, (), "ask gpu-share to make room on the GPU for the call (no broker: no effect)"),
    Feature("inbound_engine", "haiku", ("haiku", "classic"), "who answers when he calls hotline"),
)
BY_NAME = {f.name: f for f in FEATURES}


def path() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(os.environ.get(
        "HOTLINE_CALL_FEATURES", "~/.config/hotline-call/features.toml")))


def _read() -> dict:
    import tomllib

    try:
        with path().open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("features file %s unreadable (%s); using the defaults", path(), exc)
        return {}


def _valid(feature: Feature, value: object) -> bool:
    if isinstance(feature.default, bool):
        return isinstance(value, bool)
    if isinstance(feature.default, int):
        return isinstance(value, int) and not isinstance(value, bool) and 4 <= value <= 64
    return value in feature.choices


def load() -> dict:
    """Every feature's value for the call about to start."""
    raw = _read()
    out = {}
    for feature in FEATURES:
        value = raw.get(feature.name, feature.default)
        if not _valid(feature, value):
            log.error("features: %s = %r is not valid; using %r", feature.name, value, feature.default)
            value = feature.default
        out[feature.name] = value
    off = {k: v for k, v in out.items() if v != BY_NAME[k].default}
    if off:
        log.info("features differing from default: %s", off)
    return out


def parse(name: str, text: str) -> object:
    feature = BY_NAME.get(name)
    if feature is None:
        raise ValueError(f"no feature {name!r}; have: {', '.join(BY_NAME)}")
    if isinstance(feature.default, bool):
        lowered = text.lower()
        if lowered in ("on", "true", "1", "yes"):
            return True
        if lowered in ("off", "false", "0", "no"):
            return False
        raise ValueError(f"{name} is on/off")
    if isinstance(feature.default, int):
        value = int(text)
    else:
        value = text
    if not _valid(feature, value):
        raise ValueError(f"{name} must be one of {feature.choices or '4-64'}")
    return value


def write(values: dict) -> None:
    """Rewrite the file with only the non-default values, so `reset` is just
    an empty file and the defaults stay owned by this module."""
    lines = ["# hotline-call per-call switches; `hotline-call features` lists them.",
             "# Only values that differ from the default are kept here."]
    for feature in FEATURES:
        value = values.get(feature.name, feature.default)
        if value == feature.default:
            continue
        rendered = ("true" if value else "false") if isinstance(value, bool) else (
            str(value) if isinstance(value, int) else f'"{value}"')
        lines.append(f"{feature.name} = {rendered}")
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(target)


def show(values: dict) -> str:
    rows = []
    for feature in FEATURES:
        value = values[feature.name]
        shown = ("on" if value else "off") if isinstance(value, bool) else str(value)
        mark = " " if value == feature.default else "*"
        rows.append(f"{mark} {feature.name:18s} {shown:9s} {feature.what}")
    return "\n".join(rows) + "\n(* = changed from the default; read at the start of every call)"
