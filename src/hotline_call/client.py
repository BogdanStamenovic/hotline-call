"""Thin HTTP client for `hotline-iosd`. Standard library only.

Deliberately not `httpx`/`requests`: this is what a *blocked* agent runs, and
the one thing it must not do is fail because a virtualenv is missing a wheel.
hotline's pager is REST-only and synchronous for the same reason.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_URL = os.environ.get("HOTLINE_IOS_URL", "http://127.0.0.1:8789")


class DaemonError(Exception):
    """The daemon could not be reached or refused the request."""


@dataclass
class CallOutcome:
    state: str  # "answered" | "declined" | "unanswered" | "unreachable"
    reply: str = ""
    transcript: list[dict[str, str]] | None = None
    waited_seconds: float = 0.0
    transport: str = ""
    detail: str = ""


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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise DaemonError(f"{exc.code} {exc.reason}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
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
        detail=str(data.get("detail", "")),
    )


def status(*, url: str = DEFAULT_URL, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(json.loads(response.read() or b"{}"))
    except Exception as exc:
        raise DaemonError(f"cannot reach hotline-iosd at {url}: {exc}") from exc
