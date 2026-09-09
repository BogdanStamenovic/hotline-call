"""The session on the other end of a phone call.

**His design, 2026-09-08.** A separate Sonnet session owns the call, seeded with
context before the phone rings so it never has to discover anything
mid-sentence, and instructed in phone manners rather than trusted to invent
them.

**Why not the session that placed the call.** Two reasons, and the second is the
one that actually forces it. Speed: Sonnet with a small context answers in about
2.8 s where a large session is slower, and on a phone that difference is the
whole experience. But mainly, the agent that rang him is *blocked inside the
ring* for as long as the call is up -- it is waiting on this very call to
return -- so asking it a question would be asking something that cannot answer
until the conversation it is being asked in has ended.

**Why `claude -p` rather than the session pool.** Keyless, which is the standing
preference, and measured: 4.7 s to open the session, 2.8 s per resumed turn.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import threading
import time

log = logging.getLogger("hotline-ios.callagent")

DEFAULT_CWD = os.environ.get("HOTLINE_CWD", "/home/bodas/data")
OPEN_TIMEOUT = 180.0
TURN_TIMEOUT = 120.0

MANNERS = """You are on a LIVE PHONE CALL with Bogdan, speaking Serbian out loud. \
Your words go straight to a text-to-speech engine and into his ear.

RULES, all of them about being audible rather than readable:
- ONE short sentence. Two only if the second is genuinely necessary. He cannot
  skim a phone call, and anything past about six seconds of speech is too long
  to follow by ear. Never recite numbers, test counts or version strings unless
  he asks for them -- they are unlistenable and he can read them later.
- NO markdown, NO lists, NO code, NO URLs, NO file paths read out character by \
character. If you must name a file, say it the way a person would.
- Serbian, with proper diacritics (ć, č, š, ž, đ as the real characters) -- the \
TTS mispronounces stripped ASCII.
- If you need to run a command or check something before you can answer, your \
FIRST words must say so: "Samo sekund, da proverim." Then check, then answer. \
Never go silent while you work.
- Numbers and technical terms: say them as a person would speak them.
- If you did not understand him, say so and ask him to repeat.
- If he says goodbye, say a short goodbye back and nothing else.
- Do not mention that you are an AI, and do not narrate your own tooling."""


class CallAgent:
    """A Sonnet session, opened before the ring and resumed each turn."""

    def __init__(self, context: str = "", *, model: str = "sonnet",
                 cwd: str = DEFAULT_CWD, manners: str = MANNERS) -> None:
        self.context = context or "Nema posebnog konteksta."
        self.model = model
        self.cwd = cwd
        self.manners = manners
        self.session: str | None = None
        # Seeding costs ~4.7 s. On a ring that is paid WHILE the phone is
        # ringing rather than after he answers, so `reply` has to wait for it
        # rather than assume it -- without the seed there is no `--resume` and
        # the turn would open a second, unmannered session that answers in
        # markdown.
        self._ready = threading.Event()
        self.failed = ""

    def _run(self, prompt: str, timeout: float) -> str:
        command = ["claude", "-p", "--model", self.model, "--output-format", "json"]
        if self.session:
            command += ["--resume", self.session]
        command.append(prompt)
        # check=False: a non-zero exit is reported with its stderr below, which
        # is what the caller can actually say out loud.
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, cwd=self.cwd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200] or "claude exited nonzero")
        payload = json.loads(proc.stdout)
        self.session = payload.get("session_id", self.session)
        return str(payload.get("result") or "").strip()

    def open(self) -> None:
        """Seed it before dialling, so no discovery happens mid-call."""
        began = time.monotonic()
        try:
            self._run(
                f"{self.manners}\n\nCONTEXT for this call:\n{self.context}\n\n"
                "Do not reply to this message with anything but the single word OK.",
                OPEN_TIMEOUT,
            )
            log.info("call agent ready in %.1fs (session %s)",
                     time.monotonic() - began, (self.session or "?")[:12])
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised here
            self.failed = f"{type(exc).__name__}: {exc}"
            log.error("call agent could not be seeded: %s", self.failed)
        finally:
            # Set either way. A `reply` blocked forever on a seed that already
            # failed is a silent phone line, which is strictly worse than an
            # apology spoken out loud.
            self._ready.set()

    def start(self) -> threading.Thread:
        """Seed it in the background, so the cost lands while the phone rings."""
        thread = threading.Thread(target=self.open, daemon=True, name="call-agent-open")
        thread.start()
        return thread

    def reply(self, heard: str) -> str:
        if not self._ready.wait(OPEN_TIMEOUT):
            raise RuntimeError("the call agent never finished opening")
        if self.failed:
            raise RuntimeError(self.failed)
        return self._run(f'Bogdan je upravo rekao, preko telefona: "{heard}"', TURN_TIMEOUT)


def default_context(extra: str = "") -> str:
    """`call_context.txt` if it is there, plus whatever this call adds.

    The file is the standing "who you are and what happened lately" briefing;
    `extra` is the one thing that is true only of this call. Keeping them
    separate means a ring does not have to restate the former and the file does
    not have to know about rings."""
    where = pathlib.Path(__file__).resolve().parents[2] / "call_context.txt"
    try:
        standing = where.read_text().strip()
    except OSError:
        standing = ""
    return "\n\n".join(part for part in (standing, extra.strip()) if part)
