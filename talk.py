#!/usr/bin/env python3
"""Ring him and hold a real conversation, without going through the daemon.

**What this is for now that the daemon does the same thing.** It is the bench
version: one process, one call, everything it uses passed in on the command
line, and a transcript printed at the end. When a call sounds wrong, this is
where you change one thing and ring again without restarting `hotline-iosd` or
involving the roster, the store or a blocked agent.

**Everything that decides how the call FEELS moved out of here** into
`hotline_ios.conversation`, `media/tts.py`, `media/ears.py` and `callagent.py`,
because the daemon needs exactly the same behaviour and two copies of it would
drift within a week. The rules that were learned here -- Serbian, never hanging
up on him, "ćao" not being a farewell, fillers off disk -- are documented where
they now live.

This used to crash the instant he answered: it called
`voicecall.VoiceCall.PRIMING_SECONDS`, which commit `1ad8e2b` deleted on
2026-09-08 without updating this caller. `on_answer` raised `AttributeError`,
`_finish_answered` logged it and hung up, and from his end that is
indistinguishable from the daemon's own bug -- he answers and hears silence.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("talk")


def load_env() -> None:
    """SIP_* out of hotline-ios's own .env, the same file the daemon reads."""
    os.environ.setdefault("SIP_MEDIA_HOST", "100.72.2.62")
    try:
        lines = (HERE.parent / ".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith(("SIP_", "HOTLINE_")) and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="talk", description=__doc__)
    parser.add_argument("--reason", default="", help="what to say after the greeting")
    parser.add_argument("--no-agent", action="store_true",
                        help="carry audio but do not route his turns to a session")
    parser.add_argument("--timeout", type=float, default=45.0, help="how long to ring")
    parser.add_argument("--record", default="",
                        help="keep the inbound audio here, as received (8 kHz G.711)")
    args = parser.parse_args(argv)

    load_env()
    from hotline_ios.callagent import CallAgent, default_context
    from hotline_ios.conversation import AnsweredCall
    from hotline_ios.media.ears import Ears
    from hotline_ios.media.record import Recorder, from_environment
    from hotline_ios.media.tts import Fillers, Voice
    from hotline_ios.ring.base import CallTarget
    from hotline_ios.ring.sip import SipTransport

    voice = Voice()
    log.info("cvoiced: %s", voice.health())
    ears = Ears()
    ears.load()
    fillers = Fillers()
    fillers.load()

    agent = None
    if not args.no_agent:
        agent = CallAgent(default_context(
            f"THIS CALL: {args.reason}" if args.reason else ""))
        log.info("seeding the call agent before dialling")
        agent.open()

    ring = SipTransport()
    handler = AnsweredCall(
        speak=lambda text: (voice.synthesize(text), voice.rate),
        transcribe=ears.transcribe,
        fillers=fillers,
        greeting=args.reason,
        ask=agent.reply if agent is not None else None,
        hung_up=ring.far_end_hung_up,
        recorder=(Recorder(args.record, note=args.reason) if args.record
                  else from_environment(args.reason)),
        model=repr(ears),
    )
    ring.on_answer = handler

    async def go() -> None:
        await ring.start()
        await ring.ring(CallTarget(device="iphone", reason=args.reason or "conversation"),
                        timeout=args.timeout)

    try:
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the outcome is the point, not a traceback
        log.error("call ended: %s: %s", type(exc).__name__, exc)

    print(f"\n=== {handler.ended} after {handler.turns} turn(s) ===")
    print(f"stats: {handler.stats}")
    if handler.recording:
        print(f"recording: {handler.recording}")
    for who, what in handler.transcript:
        print(f"  {who:8}: {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
