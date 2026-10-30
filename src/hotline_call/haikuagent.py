"""The call's voice as Haiku 5.5 over the API, streamed token by token.

**Why this exists beside `callagent.py`.** `claude -p` answers a turn in ~2.8 s
and only when it has finished, so the voice cannot start before the whole
reply exists. Over the API the first token arrives in ~0.5-0.7 s and every
token after it can go straight into cvoiced's `/speak/ws`; first audio went
from 6.55 s to 1.73 s median in cvoice's `bench/haiku_ws_bench.py`
(2026-10-30). His call, 2026-10-30: try Haiku over the API instead of the
full-duplex model, if the voice could stream.

**Lookups.** Like `CallAgent` it can read, grep and glob his files (see
`calltools.py`, which also says what it refuses). Text before a tool call is
flushed to the voice, so "samo sekund, da proverim" is heard while it looks.

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

from . import calltools
from .callagent import default_context

log = logging.getLogger("hotline-ios.haikuagent")

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("HOTLINE_IOS_HAIKU_MODEL", "claude-haiku-5-5")
KEY_FILE = "~/.config/anthropic/api_key"
MAX_TOKENS = 400
MAX_LOOKUPS = 4
"""Tool rounds per turn. Each costs a model round trip (~0.5-1 s) of him
waiting; after this many the model must answer with what it has."""
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
- You can LOOK THINGS UP in his files under ~/data with read_file, grep, glob \
and git_log (recent commits of a repo), and nothing else: you cannot run commands, check live services, or change \
anything. Before a lookup, say a few words first ("Samo sekund, da proverim.") \
and end them with a full stop - they are spoken while you look. Keep lookups \
few and targeted; he is waiting on the line.
- Never guess about his systems: "how is X doing" (a server, a build, a job, \
an agent) is something you DO NOT KNOW unless a file you just read says so, \
and then say how old that file is if it matters. "As far as I know it is fine" \
is a guess. If files cannot answer it, say so and that you will pass it on.
- The context below was written by hand and is dated; where it disagrees with \
these rules (about what you can and cannot do), these rules win.
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
                 manners: str = MANNERS, key: str = "", tools: bool = True) -> None:
        self.system = f"{manners}\n\nKONTEKST:\n{context or 'Nema posebnog konteksta.'}"
        self.model = model
        self.key = key or api_key()
        self.history: list[dict] = []
        self.tools = tools
        self.failed = "" if self.key else f"no API key (ANTHROPIC_API_KEY or {KEY_FILE})"

    def start(self) -> None:
        """Nothing to seed: the API holds no session. Kept so the daemon can
        treat both agents alike."""

    def reply(self, heard: str) -> str:
        return "".join(d for d in self.reply_stream(heard) if d)

    def reply_stream(self, heard: str) -> Iterator[str | None]:
        """Text deltas of the answer to `heard`, as they arrive.

        `None` is a flush: everything said so far should be spoken now rather
        than held for a better cut point. It is yielded before a lookup, so
        "samo sekund, da proverim" goes out while the tool runs instead of
        after it.

        History: a completed turn goes in whole, tool calls and results
        included, because the API needs them paired. A turn cut short by
        barge-in goes in only as the words actually produced, as plain text,
        so the next turn neither believes it said more nor carries a tool call
        with no result."""
        if not self.key:
            raise HaikuUnavailable(self.failed)
        turn: list[dict] = [{"role": "user", "content": heard}]
        said: list[str] = []
        done = False
        try:
            for _ in range(MAX_LOOKUPS + 1):
                blocks: list[dict] = []
                stop = ""
                for event in self._stream(self.history + turn,
                                          tools=self.tools and _ < MAX_LOOKUPS):
                    if isinstance(event, str):
                        said.append(event)
                        yield event
                    else:
                        blocks, stop = event
                turn.append({"role": "assistant", "content": blocks})
                if stop != "tool_use":
                    done = True
                    return
                yield None
                results = []
                for block in blocks:
                    if block.get("type") != "tool_use":
                        continue
                    text, failed = calltools.run(block["name"], block.get("input") or {})
                    log.info("lookup %s(%s) -> %d chars%s", block["name"],
                             json.dumps(block.get("input"), ensure_ascii=False)[:200],
                             len(text), " (error)" if failed else "")
                    results.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": text, "is_error": failed})
                turn.append({"role": "user", "content": results})
            done = True
        finally:
            if done:
                self.history += turn
            elif said:
                self.history += [turn[0], {"role": "assistant", "content": "".join(said)}]

    def _stream(self, messages: list[dict], *, tools: bool = False) -> Iterator:
        """Yields text deltas, then one final (content blocks, stop_reason)."""
        body: dict = {
            "model": self.model, "max_tokens": MAX_TOKENS, "stream": True,
            "thinking": {"type": "disabled"},
            # The system prompt is the same every turn, so cache it: input
            # tokens are re-read on each turn and cached ones cost a tenth.
            "system": [{"type": "text", "text": self.system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            body["tools"] = calltools.SCHEMAS
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
        blocks: dict[int, dict] = {}
        partial: dict[int, list[str]] = {}
        stop = ""
        with response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type")
                if kind == "content_block_start":
                    block = dict(event["content_block"])
                    if block.get("type") == "tool_use":
                        block["input"] = {}
                        partial[event["index"]] = []
                    blocks[event["index"]] = block
                elif kind == "content_block_delta":
                    delta = event["delta"]
                    if delta.get("type") == "text_delta":
                        block = blocks.setdefault(event["index"], {"type": "text"})
                        block["text"] = block.get("text", "") + delta["text"]
                        yield delta["text"]
                    elif delta.get("type") == "input_json_delta":
                        partial[event["index"]].append(delta.get("partial_json", ""))
                elif kind == "content_block_stop":
                    if event["index"] in partial:
                        raw_json = "".join(partial.pop(event["index"]))
                        blocks[event["index"]]["input"] = json.loads(raw_json) if raw_json else {}
                elif kind == "message_delta":
                    stop = event.get("delta", {}).get("stop_reason") or stop
                elif kind == "error":
                    raise HaikuUnavailable(f"API stream error: {event.get('error')}")
                elif kind == "message_stop":
                    break
        content = [b for _, b in sorted(blocks.items())
                   if b.get("type") == "tool_use" or b.get("text")]
        yield content, stop


def haiku_agent(extra: str = "") -> HaikuAgent:
    return HaikuAgent(default_context(extra))
