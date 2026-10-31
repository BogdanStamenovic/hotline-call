"""Bench the agent bridge against a REAL hotline agent, without a phone.

Connects the bridge to a live agent exactly as a call would (Escape into its
pane, the live-call message, provenance and warrant), plays scripted Serbian
turns at it in one mode, hangs up, and then watches whether the agent went back
to its work. Writes one JSON file per run.

    HOTLINE_IOS_BRIDGE_WARRANT=chan/msg python bench/bridge_bench.py \
        --agent bridge-test-agent-cf --mode direct --watch notes.md

**Point it only at a throwaway agent.** It interrupts whatever the agent is
doing, which is the whole feature, and is not something to do to real work for
a measurement. The sentences below are written for a dummy whose busy task is
finding primes; they are not his words.

Timing per turn:
- `agent_s`: message delivered -> the agent's turn read back from its transcript.
- relay adds `haiku_first_s` (Haiku's first token of the spoken version) and
  `haiku_s` (the whole spoken version), both after `agent_s`.
- `to_voice_s`: when text could first be handed to cvoiced. TTS adds ~0.4 s to
  first audio in both modes and is not measured here.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hotline_call.agentbridge import (  # noqa: E402
    AgentBridge, markdown_leaks, relay_note, speakable)

TURNS = [
    "Ćao, na čemu tačno radiš sada?",
    "Koji je poslednji prost broj koji si zapisao?",
    "Koliko ih ukupno ima u tvom fajlu sa beleškama?",
    "Dobro. A koliko misliš da ti treba do stotog prostog broja?",
    "Objasni mi u jednoj rečenici kako proveravaš da li je broj prost.",
    "Napravi fajl zdravo točka tekst sa rečju zdravo u njemu.",
]


def lines(path: pathlib.Path) -> int:
    try:
        return len(path.read_text().splitlines())
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--mode", choices=("relay", "direct"), required=True)
    ap.add_argument("--watch", help="a file the agent's busy task grows; resumption is "
                                    "judged by it growing after the hangup")
    ap.add_argument("--resume-wait", type=float, default=90.0)
    ap.add_argument("--out", default=os.path.expanduser(
        "~/data/hotline-call/recordings/bench-results"))
    args = ap.parse_args()

    bridge = AgentBridge(mode=args.mode)
    haiku = None
    if args.mode == "relay":
        from hotline_call.callagent import default_context
        from hotline_call.haikuagent import MANNERS, HaikuAgent

        haiku = HaikuAgent(default_context(), manners=f"{MANNERS}\n- {bridge.manners()}",
                           extra_tools=bridge.tools())
        if haiku.failed:
            print(f"no Haiku: {haiku.failed}", file=sys.stderr)
            return 1
        haiku.start()

    watch = pathlib.Path(args.watch) if args.watch else None
    run: dict = {"agent": args.agent, "mode": args.mode, "started": time.time(),
                 "turns": [], "warrant": bool(os.environ.get("HOTLINE_IOS_BRIDGE_WARRANT"))}

    before = lines(watch) if watch else None
    t0 = time.monotonic()
    first = bridge.connect(args.agent, f"spoji me sa {args.agent}")
    run["connect"] = {"seconds": round(time.monotonic() - t0, 3), "was_busy": bridge.was_busy,
                      "raw": first, "spoken": speakable(first), "leaks": markdown_leaks(first),
                      "notes_lines_at_connect": before}
    print(f"connect {run['connect']['seconds']:.2f}s busy={bridge.was_busy}: {first}")

    for said in TURNS:
        began = time.monotonic()
        row: dict = {"said": said}
        try:
            raw = bridge.ask(said)
        except Exception as exc:  # noqa: BLE001
            row.update(error=f"{type(exc).__name__}: {exc}",
                       agent_s=round(time.monotonic() - began, 3))
            run["turns"].append(row)
            print(f"  ERR {row['error']}")
            continue
        row["agent_s"] = round(time.monotonic() - began, 3)
        row["raw"], row["leaks"] = raw, markdown_leaks(raw)
        if args.mode == "direct":
            row["spoken"] = speakable(raw)
            row["to_voice_s"] = row["agent_s"]
        else:
            note = relay_note(said, raw)
            h0 = time.monotonic()
            out, firsts = [], None
            for delta in haiku.reply_stream(note, lookups=False):
                if delta and firsts is None:
                    firsts = time.monotonic() - h0
                out.append(delta or "")
            row["haiku_first_s"] = round(firsts or 0.0, 3)
            row["haiku_s"] = round(time.monotonic() - h0, 3)
            row["spoken"] = "".join(out)
            row["to_voice_s"] = round(row["agent_s"] + row["haiku_first_s"], 3)
        row["spoken_chars"] = len(row["spoken"])
        run["turns"].append(row)
        print(f"  {row['to_voice_s']:6.2f}s  {said}\n          -> {row['spoken']}")

    bridge.release("bench finished")
    hung = time.monotonic()
    at_hangup = lines(watch) if watch else None
    resumed_after = None
    if watch:
        while time.monotonic() - hung < args.resume_wait:
            if lines(watch) > at_hangup:
                resumed_after = round(time.monotonic() - hung, 2)
                break
            time.sleep(0.5)
    run["hangup"] = {"notes_lines_at_hangup": at_hangup, "resumed_after_s": resumed_after,
                     "notes_lines_after": lines(watch) if watch else None}
    print(f"hangup: resumed_after={resumed_after}s")

    ok = [t["to_voice_s"] for t in run["turns"] if "to_voice_s" in t]
    if ok:
        run["summary"] = {"n": len(ok), "median": round(statistics.median(ok), 2),
                          "min": round(min(ok), 2), "max": round(max(ok), 2)}
        print(run["summary"])
    run["events"] = bridge.events
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"bridge-{args.mode}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
