"""A call from a web page: he presses a button instead of dialling.

Bogdan's request, 2026-11-08 (Discord 1568932611345227909): talk to an agent from
the room-studio site. The browser and the microphone are that site's job. What
arrives here is what wa-call already delivers, s16le 16 kHz over a Unix socket,
so this is `VoiceCall.over(PcmPump)` with no new audio code: the same ears, voice,
barge-in and recording. It takes the inbound path because he started the call,
so nobody is waiting on an answer and no question gets read out to him.

**Connecting an agent is deferred until media is up.** A socket nobody connects
to must not have interrupted an agent and told it a call is live, because
`abandon` has no way to release it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from .whatsapp import _AudioListener

log = logging.getLogger("hotline-call.ring.webcall")

ACCEPT_WITHIN = 15.0
"""How long the page's server has to connect, after asking for the call."""

_POLL = 0.25


class WebCall:
    """One browser call: the `link` `_answered_call` sees, and its audio socket."""

    def __init__(self, agent: str = "", asked: str = "") -> None:
        self.caller = "web"
        self.connect_agent = agent
        self.asked = asked
        self.on_media: Callable[[], None] | None = None
        self.gone = threading.Event()
        self.listener = _AudioListener(prefix="web")
        self.path = self.listener.path

    def far_end_hung_up(self) -> bool:
        return self.gone.is_set()

    def start(self, leg: Any, *, accept_within: float = ACCEPT_WITHIN) -> threading.Thread:
        thread = threading.Thread(target=self._run, args=(leg, accept_within),
                                  daemon=True, name="web-call")
        thread.start()
        return thread

    def close(self) -> None:
        self.gone.set()
        self.listener.close()

    def _run(self, leg: Any, accept_within: float) -> None:
        from ..media.pcmlink import PcmPump
        from ..media.voicecall import VoiceCall

        try:
            conn = self.listener.accept(accept_within)
            if conn is None:
                log.error("web call: nobody connected the audio socket")
                leg.abandon("the page never connected")
                return
            pump = PcmPump(conn)

            def watch() -> None:
                while not self.gone.is_set() and not pump.eof:
                    time.sleep(_POLL)
                self.gone.set()

            threading.Thread(target=watch, daemon=True, name="web-call-end").start()
            if self.on_media is not None:
                self.on_media()
            leg.hold(VoiceCall.over(pump))
        except Exception:
            log.exception("web call failed")
            leg.abandon("the call failed")
        finally:
            self.close()


def connect_first(bridge: Any, name: str, ask_stream: Callable[[str], Iterator[str | None]],
                  asked: str, report: Callable[[str], None],
                  ) -> tuple[Callable[[str], Iterator[str | None]], Callable[[], None]]:
    """Wrap `ask_stream` so his turns wait for the agent, and return the connector.

    Without the wait, words said during the connect would go to Haiku, which
    would answer as if it were the agent he asked for. A failed connect leaves
    Haiku holding the call: he can still talk, and it can say what happened.
    """
    done = threading.Event()

    def work() -> None:
        try:
            bridge.connect(name, asked or f"spoji me sa {name}")
        except Exception as exc:  # noqa: BLE001 - reported on the call's record
            log.error("web call: could not connect %s: %s", name, exc)
            report(f"could not connect {name}: {exc}")
        finally:
            done.set()

    def begin() -> None:
        threading.Thread(target=work, daemon=True, name="web-call-connect").start()

    def waiting(said: str) -> Iterator[str | None]:
        while not done.wait(_POLL):
            yield None
        yield from ask_stream(said)

    return waiting, begin
