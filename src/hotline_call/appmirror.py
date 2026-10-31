"""Copy every call into the hotline-ios app's conversation store. Best effort.

Why the app has to see calls at all: a ring opens a conversation he can answer
by typing on his phone instead of picking up, and the app lists the question
before the phone even rings (SPEC 3: he must never answer a phone to silence).
Before the split that was one process writing one store. Now the call lives
here and the store lives in hotline-iosd, so every line of a call is posted to
hotline-iosd's conversation endpoints:

    POST /api/v1/conversations/open    {conversation, agent, kind}
    POST /api/v1/conversations/append  {conversation, kind, text}
    POST /api/v1/conversations/close   {conversation}

His typed answer comes back the other way: hotline-iosd forwards `/api/v1/reply`
to this daemon's `/api/v1/call/reply`.

**Best effort, never in the call's way.** Posting happens on one worker thread,
in order, off the event loop and off the media threads. A hotline-iosd that is
down or slow costs the app its copy of the call and nothing else: the ring, the
voice and the answer handed back to the caller all run off `calllog.CallLog`.
Failures are counted and the last one is kept for `/health`, because a mirror
that silently stopped would look exactly like a quiet day.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("hotline-call.appmirror")

DEFAULT_APP_URL = "http://127.0.0.1:8789"
"""hotline-iosd on loopback. Overridden by HOTLINE_CALL_APP_URL."""

TIMEOUT = 3.0
"""Per request. Loopback answers in milliseconds; past this the app is not well
enough to be worth holding the queue for."""


class AppMirror:
    def __init__(self, url: str | None = None, api_key: str | None = None) -> None:
        self.url = (url or os.environ.get("HOTLINE_CALL_APP_URL", "") or DEFAULT_APP_URL).rstrip("/")
        self.api_key = os.environ.get("HOTLINE_API_KEY", "") if api_key is None else api_key
        self.posted = 0
        self.failed = 0
        self.last_error = ""
        # Conversations whose `open` did not land. Their appends would only be
        # 404s, so they are skipped and counted once rather than once per line.
        self._unmirrored: set[str] = set()
        self._jobs: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- what the daemon calls --------------------------------------------

    def open(self, conversation: str, agent: str | None, kind: str) -> None:
        self._put("/api/v1/conversations/open",
                  {"conversation": conversation, "agent": agent, "kind": kind})

    def append(self, conversation: str, kind: str, text: str) -> None:
        self._put("/api/v1/conversations/append",
                  {"conversation": conversation, "kind": kind, "text": text})

    def close(self, conversation: str) -> None:
        self._put("/api/v1/conversations/close", {"conversation": conversation})

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far has been tried. For tests and
        shutdown; the daemon never waits on the mirror during a call."""
        done = threading.Event()
        self._jobs.put(("__flush__", {"event": done}))
        self._ensure_worker()
        return done.wait(timeout)

    def status(self) -> dict[str, Any]:
        return {"url": self.url, "posted": self.posted, "failed": self.failed,
                "last_error": self.last_error or None}

    # ---- the worker -------------------------------------------------------

    def _put(self, path: str, body: dict[str, Any]) -> None:
        self._jobs.put((path, body))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="app-mirror",
                                                daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            path, body = job
            if path == "__flush__":
                body["event"].set()
                continue
            conversation = str(body.get("conversation", ""))
            if conversation in self._unmirrored and not path.endswith("/open"):
                continue
            try:
                self._post(path, body)
                self.posted += 1
            except Exception as exc:  # noqa: BLE001 - see the module docstring
                self.failed += 1
                self.last_error = f"{path}: {exc}"
                log.warning("could not mirror %s for %s to %s: %s",
                            path.rsplit("/", 1)[-1], conversation, self.url, exc)
                if path.endswith("/open"):
                    self._unmirrored.add(conversation)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        if self.api_key:
            request.add_header("X-Hotline-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return dict(json.loads(response.read() or b"{}"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise RuntimeError(f"{exc.code} {detail}") from exc
