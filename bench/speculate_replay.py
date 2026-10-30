"""Offline replay for speculative turn-taking: how often could we have known?

His design (2026-10-30): while he is still talking, a predictor guesses the
whole sentence from what has been heard so far, a drafter answers the guess
and pre-renders it, and a judge re-checks the guess after every new chunk;
a contradiction squashes the pipeline. If the guess survives to the end of
his turn, the answer is already rendered and can go out at once.

This measures the part that decides whether that is worth wiring: for each
utterance cut at word boundaries, does the predictor's completion mean the
same as what he actually went on to say? And can a 10 ms embedding screen
(bge-m3, already used by speculate.py) stand in for the LLM judge on the
critical path?

Text-level only: prefixes are cut by words, not by real Whisper partials, so
ASR noise in the prefix is not modelled. Two corpora, reported separately:
his spoken turns from recorded calls (Serbian, few), and his typed Discord
messages to agents (mostly English, many) as a register proxy.

    .venv/bin/python bench/speculate_replay.py [out.json]
"""

from __future__ import annotations

import concurrent.futures as cf
import glob
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from hotline_ios.haikuagent import API, api_key  # noqa: E402

MODEL = os.environ.get("BENCH_MODEL", "claude-haiku-5-5")
KEY = api_key()
STEP = 3
MIN_WORDS = 5
RECORDINGS = pathlib.Path(__file__).resolve().parents[2] / "recordings"
ARCHIVE = pathlib.Path.home() / ".local/state/hotline/discord-archive"
HIM = "1329897799336071311"

PREDICT = ("You are predicting what a person on a phone call is about to say. Given the "
           "words heard so far, write the single most likely COMPLETE utterance they are "
           "saying, in the same language, starting with the words heard. Output only the "
           "utterance.")
JUDGE = ("Two versions of what a person said. A is a guess made before they finished; B "
         "is what they actually said. Would a good reply to A also be a good reply to B - "
         "same request or question, same meaning, nothing in B that contradicts or "
         "materially changes A? Answer exactly SAME or DIFFERENT.")


def call(system: str, user: str, max_tokens: int = 120) -> str:
    body = {"model": MODEL, "max_tokens": max_tokens, "thinking": {"type": "disabled"},
            "system": system, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(API, json.dumps(body).encode(), {
        "x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            return "".join(c.get("text", "") for c in d["content"]).strip()
        except (OSError, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def embed(texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request("http://localhost:11434/api/embed",
                                 json.dumps({"model": "bge-m3", "input": texts}).encode(),
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["embeddings"]


def cos(a, b) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def spoken() -> list[str]:
    out = []
    for m in sorted(glob.glob(str(RECORDINGS / "call-*/manifest.json"))):
        for t in json.load(open(m)).get("turns", []):
            out.append(t["heard"])
    return out


def typed() -> list[str]:
    out = []
    for f in sorted(glob.glob(str(ARCHIVE / "*/*.json"))):
        d = json.load(open(f))
        for m in (d if isinstance(d, list) else d.get("messages", [])):
            if str(m.get("author", {}).get("id")) == HIM:
                text = re.sub(r"<[@#!&]?\d+>|https?://\S+", "", m.get("content", "")).strip()
                if text and "\n" not in text:
                    out.append(text)
    return out


def cases(utterances: list[str], corpus: str) -> list[dict]:
    rows = []
    for u in utterances:
        words = u.split()
        if len(words) < MIN_WORDS or len(words) > 60:
            continue
        for k in range(STEP, len(words), STEP):
            rows.append({"corpus": corpus, "final": u, "prefix": " ".join(words[:k]),
                         "heard": k, "remaining": len(words) - k})
    return rows


def run(row: dict) -> dict | None:
    try:
        return _run(row)
    except Exception as exc:  # noqa: BLE001 - one dead request is not a dead bench
        print(f"dropped a cut: {type(exc).__name__}: {exc}", flush=True)
        return None


def _run(row: dict) -> dict:
    row["guess"] = call(PREDICT, f"Heard so far: {row['prefix']}")
    verdict = call(JUDGE, f"A: {row['guess']}\nB: {row['final']}", max_tokens=5)
    row["judge_same"] = verdict.upper().startswith("SAME")
    return row


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "bench/speculate-replay.json")
    rows = cases(spoken(), "spoken") + cases(typed(), "typed")
    print(f"{len(rows)} cut points", flush=True)
    with cf.ThreadPoolExecutor(8) as pool:
        rows = [r for r in pool.map(run, rows) if r is not None]
    for i in range(0, len(rows), 32):
        batch = rows[i:i + 32]
        vecs = embed([r["guess"] for r in batch] + [r["final"] for r in batch])
        for j, r in enumerate(batch):
            r["cos"] = round(cos(vecs[j], vecs[len(batch) + j]), 4)
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    for corpus in ("spoken", "typed"):
        sub = [r for r in rows if r["corpus"] == corpus]
        if not sub:
            continue
        print(f"\n== {corpus}: {len({r['final'] for r in sub})} utterances, {len(sub)} cuts")
        for lo, hi, label in ((1, 3, "1-3 words left"), (4, 6, "4-6 left"), (7, 999, "7+ left")):
            b = [r for r in sub if lo <= r["remaining"] <= hi]
            if b:
                print(f"  {label:15s} judge SAME {sum(r['judge_same'] for r in b):3d}/{len(b):3d}"
                      f" = {100 * sum(r['judge_same'] for r in b) / len(b):.0f}%")
        last = {}
        for r in sub:
            if r["final"] not in last or r["heard"] > last[r["final"]]["heard"]:
                last[r["final"]] = r
        ls = list(last.values())
        print(f"  LAST cut before he stopped: SAME {sum(r['judge_same'] for r in ls)}/{len(ls)}")
        for t in (0.80, 0.85, 0.90, 0.95):
            acc = [r for r in sub if r["cos"] >= t]
            agree = sum(r["judge_same"] for r in acc)
            print(f"  cos>={t:.2f}: passes {len(acc):3d}, judge agrees SAME on {agree}"
                  f" ({100 * agree / len(acc):.0f}% precision)" if acc else f"  cos>={t:.2f}: none")


if __name__ == "__main__":
    main()
