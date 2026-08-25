"""`hotline-call` -- ring Bogdan's phone and block until he has spoken an answer.

This is the thing `SPEC.md` §1 exists to create: the replacement for
`hotline-page`, which posts an escalating Discord `@mention` and which he calls
a fake call, correctly, because it does not ring.

The contract is deliberately `hotline-page`'s, character for character where it
can be, because there is already a `call-bogdan` skill and an unknown number of
agent prompts that depend on it:

    answer=$(hotline-call "may I spend money on a UI agency?")

Exit codes 0/1/2/3 mean exactly what they mean for `hotline-page` -- answered,
undeliverable, usage, delivered-but-unanswered. One code is added:

    4  he declined the call

which `hotline-page` had no way to express, because a mention cannot be
declined. It is a real answer and worth distinguishing from ringing out: a
decline means he saw it and said not now, so escalating again immediately is
the wrong move.

**Falling back is the default.** If the call cannot be delivered at all -- the
daemon is down, no transport is registered, the phone is off the network -- this
shells out to `hotline-page` rather than failing. Adopting `hotline-call` is
then never worse than staying on `hotline-page`, which is the only honest way to
ship a replacement for something that currently works.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import NoReturn

from .client import DaemonError, place_call

DEFAULT_TIMEOUT = 900.0
DEFAULT_RING_TIMEOUT = 45.0

EXIT_ANSWERED = 0
EXIT_UNDELIVERABLE = 1
EXIT_USAGE = 2
EXIT_UNANSWERED = 3
EXIT_DECLINED = 4


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="hotline-call",
        description="Ring Bogdan's iPhone and wait for him to answer, out loud.",
    )
    parser.add_argument("reason", nargs="*", help="what you need from him, in one or two sentences")
    parser.add_argument("--context", default="", help="extra detail, shown on the call screen")
    parser.add_argument("--source", default="an agent", help="who is calling, e.g. 'the ios build'")
    parser.add_argument(
        "--agent",
        default=None,
        help="which session he is connected to when he answers; anything "
        "Router.resolve accepts (a registered name, 'newest', a directory). "
        "Default: the session that placed the call.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"give up on the whole call after SEC (default {DEFAULT_TIMEOUT:.0f})",
    )
    parser.add_argument(
        "--ring-timeout",
        type=float,
        default=DEFAULT_RING_TIMEOUT,
        metavar="SEC",
        help=f"stop ringing after SEC (default {DEFAULT_RING_TIMEOUT:.0f})",
    )
    parser.add_argument(
        "--transport",
        default="auto",
        help="force a ring transport instead of the configured one "
        "(auto, sip, apns, page, loopback)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="ring and exit without waiting for him to answer",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="fail rather than falling back to hotline-page when the call "
        "cannot be delivered",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    return parser


def _fall_back(args: argparse.Namespace, reason: str, why: str, log) -> int:
    """Hand over to `hotline-page`, which needs no daemon and no phone."""
    page = shutil.which("hotline-page") or os.path.expanduser("~/.claude/bin/hotline-page")
    if not os.path.exists(page) and not shutil.which("hotline-page"):
        print(f"hotline-call: error: {why}, and hotline-page is not installed", file=sys.stderr)
        return EXIT_UNDELIVERABLE
    log(f"{why} -- falling back to hotline-page")
    argv = [page, reason, "--source", args.source, "--timeout", str(args.timeout)]
    if args.context:
        argv += ["--context", args.context]
    if args.no_wait:
        argv.append("--no-wait")
    # Deliberately inherits stdout: the whole point is that the caller's
    # `$(hotline-call ...)` still captures his answer when the call path failed.
    return subprocess.call(argv)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"hotline-call: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    reason = " ".join(args.reason).strip()
    if not reason:
        print("hotline-call: error: say what you need from him", file=sys.stderr)
        return EXIT_USAGE

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        outcome = place_call(
            reason,
            agent=args.agent,
            context=args.context,
            source=args.source,
            timeout=args.timeout,
            ring_timeout=args.ring_timeout,
            wait=not args.no_wait,
            transport=args.transport,
        )
    except DaemonError as exc:
        if args.no_fallback:
            print(f"hotline-call: error: {exc}", file=sys.stderr)
            return EXIT_UNDELIVERABLE
        return _fall_back(args, reason, str(exc), log)

    if args.no_wait:
        log(f"ringing via {outcome.transport} (not waiting)")
        return EXIT_ANSWERED

    log(f"{outcome.state} after {outcome.waited_seconds:.0f}s via {outcome.transport}")

    if outcome.state == "answered":
        print(outcome.reply)
        return EXIT_ANSWERED
    if outcome.state == "declined":
        # Not a fallback case. He saw it and said not now; ringing him again
        # through another channel a second later is exactly what he was
        # declining.
        log("he declined")
        return EXIT_DECLINED
    if outcome.state == "unanswered":
        if args.no_fallback:
            return EXIT_UNANSWERED
        return _fall_back(args, reason, "the call rang out", log)
    if args.no_fallback:
        print(f"hotline-call: error: {outcome.detail or outcome.state}", file=sys.stderr)
        return EXIT_UNDELIVERABLE
    return _fall_back(args, reason, outcome.detail or "the call was undeliverable", log)


if __name__ == "__main__":
    raise SystemExit(main())
