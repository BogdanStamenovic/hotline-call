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
extra, and server-sent events are lines: `http.client` reads them fine, and
keeps the connection open between turns (see `_Pool`).

**Cost.** $0.10/M input, $0.50/M output (prompts under 100k tokens). A turn is
a few thousand tokens in and ~100 out, so a long call costs well under a cent.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import pathlib
import threading
import time
from collections.abc import Iterator

from . import calltools
from .callagent import default_context

log = logging.getLogger("hotline-call.haikuagent")

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("HOTLINE_IOS_HAIKU_MODEL", "claude-haiku-5-5")
KEY_FILE = "~/.config/anthropic/api_key"
MAX_TOKENS = 400
MAX_LOOKUPS = 4
"""Tool rounds per turn. Each costs a model round trip (~0.5-1 s) of him
waiting; after this many the model must answer with what it has."""
TIMEOUT = 30.0

# The opener list in MANNERS must stay a subset of cvoice's stream.OPENERS:
# those are rendered once and cached, which is the whole point (first audio
# 1.20 s -> 0.70 s median, cvoice bench/haiku_ws_bench.py, 2026-10-31). An
# opener missing there still works, it just renders like any other chunk.
_HEAD = """You are on a LIVE PHONE CALL with Bogdan, speaking Serbian out loud. \
Your words go straight to a text-to-speech engine and into his ear, AS YOU WRITE \
THEM: the first few words are spoken before you have written the rest.

RULES, all of them about being audible rather than readable:"""

_OPENERS = """- Open with ONE short word or phrase from this list, then a comma or a full \
stop: Važi, Dobro, Evo, Aha, Može, Razumem, Naravno, Samo sekund, Izvini, Hm, \
Pa, Super, Da, Ne, Ćao (or "Ćao Bogdane"). Those are pre-recorded and play instantly. Pick the one \
that fits, and do not open two replies in a row with the same one."""

_OPENERS_OFF = """- Start with a few short words that can stand alone, then a comma or a full \
stop - they go out first, so make them a natural opening, not filler."""

_NO_FILLERS = """- Answer DIRECTLY. Do not open with filler or acknowledgement words - no \
"Važi", "Dobro", "Evo", "Aha", "Može", "Razumem", "Naravno", "Hm", "Pa", \
"Samo sekund" - and do not echo his question back. The first words you write \
are already the answer."""

_BODY = """- ONE or two short sentences. He cannot skim a phone call, and anything past \
about six seconds of speech is too long to follow by ear. Never recite numbers, \
test counts or version strings unless he asks for them.
- NO markdown, NO lists, NO code, NO URLs, NO file paths.
- Serbian, Latin script, with proper diacritics (ć, č, š, ž, đ) - the TTS \
mispronounces stripped ASCII. Write numbers as words."""

_TOOLS = """- You can LOOK THINGS UP: his files under ~/data with read_file, grep, glob \
and git_log, and live state with bash (nvidia-smi, df, free, ps, uptime, \
journalctl...). bash is read-only and offline: you cannot change, restart, \
install or send anything, and must not try. Before a lookup, say a few words first ("Samo sekund, da proverim.") \
and end them with a full stop - they are spoken while you look. Keep lookups \
few and targeted; he is waiting on the line."""

_TOOLS_NO_FILLERS = """- You can LOOK THINGS UP: his files under ~/data with read_file, grep, glob \
and git_log, and live state with bash (nvidia-smi, df, free, ps, uptime, \
journalctl...). bash is read-only and offline: you cannot change, restart, \
install or send anything, and must not try. Before a lookup, say in a few plain words WHAT you are checking \
("Gledam poslednje izmene u cvoice repou.") and end it with a full stop - it is \
spoken while you look. Keep lookups few and targeted; he is waiting on the line."""

_TOOLS_OFF = """- You CANNOT look anything up or run anything on this call. Answer from what is \
below; if it cannot answer him, say so and that you will pass the question on."""

_CONTEXT_RULES = """- Never guess about his systems: "how is X doing" (a server, a build, a job, \
an agent) is something you DO NOT KNOW unless a file you just read says so, \
and then say how old that file is if it matters. "As far as I know it is fine" \
is a guess. If files cannot answer it, say so and that you will pass it on.
- The context below was written by hand and is dated; where it disagrees with \
these rules (about what you can and cannot do), these rules win. Do not bring \
up anything from it unprompted: it is background for answering him, not a list \
of topics. Asked why you called, give the reason for THIS CALL and nothing else."""

_TAIL = """- If he asks for something to be DONE, do not promise to do it and do not \
pretend it is done. Say it is being passed on - "Važi, prosleđujem."
- If you did not understand him, say so and ask him to repeat.
- If he says goodbye, say a short goodbye back and nothing else.
- Do not mention that you are an AI."""


def manners(*, openers: bool = True, tools: bool = True, context_rules: bool = True,
            fillers: bool = True) -> str:
    """The voice's rules, with each switchable addition in or out, so he can
    hear one at a time (`hotline-call features`).

    `fillers=False` overrides `openers`: he did not like the "Važi," openers
    at all (2026-10-31), so that mode forbids filler words outright."""
    if not fillers:
        opening, lookups = _NO_FILLERS, _TOOLS_NO_FILLERS
    else:
        opening, lookups = (_OPENERS if openers else _OPENERS_OFF), _TOOLS
    parts = [_HEAD, opening, _BODY, lookups if tools else _TOOLS_OFF]
    if context_rules:
        parts.append(_CONTEXT_RULES)
    parts.append(_TAIL)
    return "\n".join(parts)


MANNERS = manners()


class HaikuUnavailable(RuntimeError):
    pass


READ_ONLY_EXTRA = frozenset({"list_agents"})
"""Extra (bridge) tools that only read, and so are as safe in a speculative
fork as calltools' lookups are."""


def _not_while_speculating(**_args) -> str:
    raise RuntimeError("not available before he has finished speaking")


def _lines(response):
    while True:
        line = response.readline()
        if not line:
            return
        yield line


class _Pool:
    """Idle HTTPS connections to the API, reused across turns.

    urllib opens a new TCP + TLS connection per request; on this box that is
    ~0.1-0.2 s before the first byte of every turn. A pool rather than one
    connection because a dropped speculation can still be mid-stream while the
    real turn starts, and one HTTP/1.1 connection carries one request at a time.
    """

    def __init__(self, host: str = "api.anthropic.com", keep: int = 3) -> None:
        self.host = host
        self.keep = keep
        self.idle: list[http.client.HTTPSConnection] = []
        self.lock = threading.Lock()

    def _new(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(self.host, timeout=TIMEOUT)

    def warm(self) -> None:
        try:
            conn = self._new()
            conn.connect()
            self.give_back(conn, reuse=True)
        except OSError as exc:
            log.info("could not pre-open the API connection (%s); the first turn will", exc)

    def post(self, body: bytes, headers: dict):
        for attempt in range(2):
            with self.lock:
                conn = self.idle.pop() if self.idle else None
            fresh = conn is None
            conn = conn or self._new()
            try:
                conn.request("POST", "/v1/messages", body=body, headers=headers)
                return conn, conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                # A pooled connection the server already closed fails here;
                # one retry on a fresh connection, never two.
                if fresh or attempt:
                    raise HaikuUnavailable(f"API unreachable: {exc}") from exc
        raise AssertionError("unreachable")

    def give_back(self, conn, *, reuse: bool) -> None:
        if reuse:
            with self.lock:
                if len(self.idle) < self.keep:
                    self.idle.append(conn)
                    return
        conn.close()


_POOL = _Pool()


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
                 manners: str = MANNERS, key: str = "", tools: bool = True,
                 extra_tools: dict | None = None, thinking: bool = False,
                 keepalive: bool = True, date: bool = True) -> None:
        # The date, because call_context.txt narrates "today" as of the day it
        # was written; on the 2026-10-30 test call the voice offered a seven-week-old
        # fix as current news.
        today = time.strftime("%Y-%m-%d, %A")
        dated = f"DANAS JE {today}.\n\n" if date else ""
        self.system = (f"{manners}\n\n{dated}"
                       f"KONTEKST:\n{context or 'Nema posebnog konteksta.'}")
        self.thinking = thinking
        self.keepalive = keepalive
        self.model = model
        self.key = key or api_key()
        self.history: list[dict] = []
        self.tools = tools
        # name -> (schema, fn): tools with side effects, from outside this
        # module (the agent bridge). Kept apart from `calltools`, which stays
        # read-only, and never handed to a fork -- see `fork`.
        self.extra_tools = dict(extra_tools or {})
        self.failed = "" if self.key else f"no API key (ANTHROPIC_API_KEY or {KEY_FILE})"

    def start(self) -> None:
        """Nothing to seed, but open a connection to the API now, while the
        phone rings, so the first turn does not pay for TLS."""
        if self.key and self.keepalive:
            threading.Thread(target=_POOL.warm, daemon=True, name="api-warm").start()

    def fork(self) -> "HaikuAgent":
        """A copy to answer speculatively with. Its history is its own until
        `adopt` makes it the real one; a dropped fork changes nothing."""
        twin = HaikuAgent.__new__(HaikuAgent)
        twin.__dict__.update(self.__dict__)
        twin.history = list(self.history)
        # A speculative answer may be thrown away, but a tool that acted on the
        # world cannot be: connecting an agent interrupts it. So in a fork those
        # tools refuse. The schemas stay, because the tool list is the front of
        # the prompt cache and a fork with a different one pays for a cold read.
        # Only tools with side effects refuse. Blocking the read-only ones too
        # (list_agents) made a speculative answer read the refusal out to him:
        # "not available before he has finished speaking" -> "Završio sam govor"
        # (live call, 2026-10-31 18:31).
        twin.extra_tools = {name: (schema, fn if name in READ_ONLY_EXTRA else _not_while_speculating)
                            for name, (schema, fn) in self.extra_tools.items()}
        return twin

    def adopt(self, twin: "HaikuAgent") -> None:
        self.history = twin.history

    def reply(self, heard: str) -> str:
        return "".join(d for d in self.reply_stream(heard) if d)

    def reply_stream(self, heard: str, *, lookups: bool = True) -> Iterator[str | None]:
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
                                          tools=lookups and self.tools and _ < MAX_LOOKUPS):
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
                    text, failed = self._run_tool(block["name"], block.get("input") or {})
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

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        extra = self.extra_tools.get(name)
        if extra is None:
            return calltools.run(name, args)
        try:
            return str(extra[1](**args)), False
        except TypeError as exc:
            return f"TypeError: {exc}", True
        except Exception as exc:  # noqa: BLE001 - said out loud, never a dropped call
            log.exception("tool %s failed", name)
            return f"{type(exc).__name__}: {exc}", True

    def _stream(self, messages: list[dict], *, tools: bool = False) -> Iterator:
        """Yields text deltas, then one final (content blocks, stop_reason)."""
        body: dict = {
            "model": self.model, "max_tokens": MAX_TOKENS, "stream": True,
            # Omitted rather than enabled: Haiku 5.5 thinks by default.
            **({} if self.thinking else {"thinking": {"type": "disabled"}}),
            # The system prompt is the same every turn, so cache it: input
            # tokens are re-read on each turn and cached ones cost a tenth.
            "system": [{"type": "text", "text": self.system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            body["tools"] = calltools.SCHEMAS + [s for s, _ in self.extra_tools.values()]
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        conn, response = _POOL.post(json.dumps(body).encode(), headers)
        if response.status != 200:
            detail = response.read()[:400].decode("utf-8", "replace")
            _POOL.give_back(conn, reuse=False)
            raise HaikuUnavailable(f"API said {response.status}: {detail}")
        blocks: dict[int, dict] = {}
        partial: dict[int, list[str]] = {}
        stop = ""
        finished = False
        try:
            for raw in _lines(response):
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
                    finished = True
                    break
        finally:
            # A stream abandoned mid-way (barge-in, a dropped speculation)
            # leaves unread bytes on the socket; that connection cannot be reused.
            if finished:
                response.read()
            _POOL.give_back(conn, reuse=finished and self.keepalive)
        content = [b for _, b in sorted(blocks.items())
                   if b.get("type") == "tool_use" or b.get("text")]
        yield content, stop


def haiku_agent(extra: str = "") -> HaikuAgent:
    return HaikuAgent(default_context(extra))
