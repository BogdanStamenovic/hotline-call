"""Thin HTTP client for `hotline-iosd`. Standard library only.

Deliberately not `httpx`/`requests`: this is what a *blocked* agent runs, and
the one thing it must not do is fail because a virtualenv is missing a wheel.
hotline's pager is REST-only and synchronous for the same reason.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DEFAULT_URL = os.environ.get("HOTLINE_IOS_URL", "http://127.0.0.1:8789")


class DaemonError(Exception):
    """The daemon could not be reached or refused the request."""


class CallTimeout(DaemonError):
    """The request ran past its deadline while the daemon stayed reachable.

    Subclasses ``DaemonError`` so any caller that only catches ``DaemonError``
    keeps its existing fallback behaviour. It carries ``daemon_up`` because the
    whole reason this class exists is that a read timeout and a dead daemon
    surfaced from ``urllib`` as the same exception, so "the call rang and no
    answer came back" was reported as "cannot reach the daemon" -- once, while
    his phone was ringing in his hand (2026-09-01). ``daemon_up`` is answered
    by a real socket connect at the moment of failure, not inferred.
    """

    def __init__(self, elapsed: float, daemon_up: bool, url: str) -> None:
        self.elapsed = elapsed
        self.daemon_up = daemon_up
        self.url = url
        where = "reachable" if daemon_up else "unreachable"
        super().__init__(
            f"no response from hotline-iosd at {url} within {elapsed:.0f}s "
            f"(daemon {where} on a fresh connect)"
        )


def _daemon_reachable(url: str, timeout: float = 2.0) -> bool:
    """Ask the socket whether the port is open right now.

    urllib raises the same exception for a read timeout and a connect failure,
    so the only honest way to tell "the call is still running" from "nothing is
    listening" is to open a second connection and see.
    """
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class CallOutcome:
    state: str  # "answered" | "declined" | "unanswered" | "unreachable"
    reply: str = ""
    transcript: list[dict[str, str]] | None = None
    waited_seconds: float = 0.0
    transport: str = ""
    detail: str = ""
    # True when the doorbell was a test double. Not a detail: it is the
    # difference between "he was rung" and "nothing happened".
    fake: bool = False


def _post(path: str, payload: dict[str, Any], *, url: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    key = os.environ.get("HOTLINE_API_KEY", "")
    if key:
        request.add_header("X-Hotline-Key", key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise DaemonError(f"{exc.code} {exc.reason}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A timeout can arrive bare (read phase) or wrapped in a URLError whose
        # `.reason` is the TimeoutError (connect phase), so unwrap before
        # deciding. A timeout means the connection was accepted and held: the
        # daemon is up and the call ran long. A dead daemon fails to connect in
        # milliseconds -- a different thing that must not read the same.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
            reason, (TimeoutError, socket.timeout)
        ):
            elapsed = time.monotonic() - started
            raise CallTimeout(elapsed, _daemon_reachable(url), url) from exc
        raise DaemonError(f"cannot reach hotline-iosd at {url}: {exc}") from exc


def place_call(
    reason: str,
    *,
    agent: str | None = None,
    context: str = "",
    source: str = "an agent",
    timeout: float = 900.0,
    ring_timeout: float = 45.0,
    wait: bool = True,
    transport: str = "auto",
    url: str = DEFAULT_URL,
) -> CallOutcome:
    payload = {
        "reason": reason,
        "agent": agent,
        "context": context,
        "source": source,
        "ring_timeout": ring_timeout,
        "wait": wait,
        "transport": transport,
    }
    # The HTTP timeout has to outlive the call itself, or a long conversation
    # looks to the caller exactly like a dead daemon.
    data = _post("/api/v1/call", payload, url=url, timeout=timeout + 30.0)
    return CallOutcome(
        state=str(data.get("state", "unreachable")),
        reply=str(data.get("reply", "")),
        transcript=data.get("transcript"),
        waited_seconds=float(data.get("waited_seconds", 0.0)),
        transport=str(data.get("transport", "")),
        fake=bool(data.get("fake", False)),
        detail=str(data.get("detail", "")),
    )


def status(*, url: str = DEFAULT_URL, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(json.loads(response.read() or b"{}"))
    except Exception as exc:
        raise DaemonError(f"cannot reach hotline-iosd at {url}: {exc}") from exc
