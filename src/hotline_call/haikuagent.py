"""The call's voice as Haiku 5.5 over the API, streamed token by token.

**Why this exists beside `callagent.py`.** `claude -p` answers a turn in ~2.8 s
and only when it has finished, so the voice cannot start before the whole
reply exists. Over the API the first token arrives in ~0.5-0.7 s and every
token after it can go straight into cvoiced's `/speak/ws`; first audio went
from 6.55 s to 1.73 s median in cvoice's `bench/haiku_ws_bench.py`
(2026-10-30). His call, 2026-10-30: try Haiku over the API instead of the
full-duplex model, if the voice could stream.

**What it gives up.** `CallAgent` can Read/Grep/Glob his files to answer a
question about the box; this cannot look anything up, and its manners say so.
That is the same as PersonaPlex, which it is meant to replace for a test.

**Why thinking is off.** Haiku 5.5 thinks by default. On a one-line Serbian
question that was 220 output tokens instead of 111, and every one of them is
silence on the line.

**Why stdlib HTTP.** hotline-ios has no runtime dependencies outside the voice
extra, and server-sent events are lines: `urllib` reads them fine.

**Cost.** $0.10/M input, $0.50/M output (prompts under 100k tokens). A turn is
a few thousand tokens in and ~100 out, so a long call costs well under a cent.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import urllib.error
import urllib.request
from collections.abc import Iterator

from .callagent import default_context

log = logging.getLogger("hotline-ios.haikuagent")

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("HOTLINE_IOS_HAIKU_MODEL", "claude-haiku-5-5")
KEY_FILE = "~/.config/anthropic/api_key"
MAX_TOKENS = 400
TIMEOUT = 30.0

MANNERS = """You are on a LIVE PHONE CALL with Bogdan, speaking Serbian out loud. \
Your words go straight to a text-to-speech engine and into his ear, AS YOU WRITE \
THEM: the first few words are spoken before you have written the rest.

RULES, all of them about being audible rather than readable:
- Start with a few short words that can stand alone, then a comma or a full \
stop - they go out first, so make them a natural opening, not filler.
- ONE or two short sentences. He cannot skim a phone call, and anything past \
about six seconds of speech is too long to follow by ear. Never recite numbers, \
test counts or version strings unless he asks for them.
- NO markdown, NO lists, NO code, NO URLs, NO file paths.
- Serbian, Latin script, with proper diacritics (ć, č, š, ž, đ) - the TTS \
mispronounces stripped ASCII. Write numbers as words.
- You CANNOT look anything up or run anything on this call. Answer from what is \
below. If he asks about something you do not know, say plainly that you do not \
have it on this call and that you will pass the question on. Never guess about \
his systems: "how is X doing" (a server, a build, a job, an agent) is something \
you DO NOT KNOW unless the context below states it, and "as far as I know it is \
fine" is a guess. Say you cannot check it from the call.
- If he asks for something to be DONE, do not promise to do it and do not \
pretend it is done. Say it is being passed on - "Važi, prosleđujem."
- If you did not understand him, say so and ask him to repeat.
- If he says goodbye, say a short goodbye back and nothing else.
- Do not mention that you are an AI."""


class HaikuUnavailable(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        return pathlib.Path(os.path.expanduser(KEY_FILE)).read_text().strip()
    except OSError:
        return ""


class HaikuAgent:
    """One call's conversation with Haiku. Same `start`/`reply` shape as
    `CallAgent`, plus `reply_stream`, which is the point."""

    def __init__(self, context: str = "", *, model: str = DEFAULT_MODEL,
                 manners: str = MANNERS, key: str = "") -> None:
        self.system = f"{manners}\n\nKONTEKST:\n{context or 'Nema posebnog konteksta.'}"
        self.model = model
        self.key = key or api_key()
        self.history: list[dict] = []
        self.failed = "" if self.key else f"no API key (ANTHROPIC_API_KEY or {KEY_FILE})"

    def start(self) -> None:
        """Nothing to seed: the API holds no session. Kept so the daemon can
        treat both agents alike."""

    def reply(self, heard: str) -> str:
        return "".join(self.reply_stream(heard))

    def reply_stream(self, heard: str) -> Iterator[str]:
        """Text deltas of the answer to `heard`, as they arrive.

        The turn goes into history only once it is complete, and only what
        was actually produced: a reply cut short by barge-in (the generator
        closed early) is recorded as far as it got, so the next turn does not
        believe it said things it never said."""
        if not self.key:
            raise HaikuUnavailable(self.failed)
        self.history.append({"role": "user", "content": heard})
        said: list[str] = []
        try:
            for delta in self._stream(self.history):
                said.append(delta)
                yield delta
        finally:
            if said:
                self.history.append({"role": "assistant", "content": "".join(said)})
            elif self.history and self.history[-1]["role"] == "user":
                self.history.pop()

    def _stream(self, messages: list[dict]) -> Iterator[str]:
        body = {
            "model": self.model, "max_tokens": MAX_TOKENS, "stream": True,
            "thinking": {"type": "disabled"},
            # The system prompt is the same every turn, so cache it: input
            # tokens are re-read on each turn and cached ones cost a tenth.
            "system": [{"type": "text", "text": self.system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        request = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise HaikuUnavailable(f"API said {exc.code}: {detail}") from exc
        except OSError as exc:
            raise HaikuUnavailable(f"API unreachable: {exc}") from exc
        with response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type")
                if kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
                    yield event["delta"]["text"]
                elif kind == "error":
                    raise HaikuUnavailable(f"API stream error: {event.get('error')}")
                elif kind == "message_stop":
                    return


def haiku_agent(extra: str = "") -> HaikuAgent:
    return HaikuAgent(default_context(extra))
