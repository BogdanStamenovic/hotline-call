"""Tell gpu-share a call is using the GPU, so LLMs make room instead of the call.

On 2026-11-02 16:04 an ollama model held 5.1 GB of the 8 GB card when he
called; Whisper could not load, fell back to the CPU, and cvoiced's render
starved until it timed out. gpu-share (~/data/gpu-share) brokers the card: at
`begin` it reserves the call's budget (Whisper + voice + bge-m3, ~5.1 GB) and
evicts what is in the way; at `end` it lets LLMs back.

Contract (gpu-share, 2026-11-02): POST {url}/gpu-share/call/begin -> {"lease",
"ttl_s": 90, "evicting"}; /renew {"lease"} -> 200, or 404 when expired (begin
again); /end {"lease"}. A 404 on begin means "no broker here" (plain ollama on
:11434 until the port move) and the call carries on exactly as before.

Never costs the call anything: every request has a short timeout and every
failure is logged and swallowed. gpu-share also polls calld's /health, so a
lease missed here still reserves the GPU.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

log = logging.getLogger("hotline-call.gpulease")

DEFAULT_URL = "http://127.0.0.1:11434"
TIMEOUT = 1.5
RENEW_EVERY = 30.0


class GpuLease:
    """One call's hold on the GPU. `begin()` at ring, `end()` at hang-up."""

    def __init__(self, url: str = "", call_id: str = "") -> None:
        self.url = (url or os.environ.get("HOTLINE_CALL_GPU_SHARE_URL", DEFAULT_URL)).rstrip("/")
        self.call_id = call_id
        self.lease: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def begin(self) -> bool:
        self._stop = threading.Event()     # fresh: a renewal that re-begins must keep renewing
        try:
            status, reply = self._post("/gpu-share/call/begin",
                                       {"id": self.call_id} if self.call_id else {})
        except (OSError, ValueError) as exc:
            log.info("gpu-share unreachable (%s); the call goes ahead without a lease", exc)
            return False
        if status == 404:
            log.info("no gpu-share broker at %s; the call goes ahead without a lease", self.url)
            return False
        if status != 200 or not reply.get("lease"):
            log.warning("gpu-share begin answered %s %s; no lease", status, reply)
            return False
        self.lease = reply["lease"]
        log.info("gpu lease %s (evicting=%s, ttl %ss)", self.lease, reply.get("evicting"),
                 reply.get("ttl_s"))
        self._thread = threading.Thread(target=self._renew, daemon=True, name="gpu-lease")
        self._thread.start()
        return True

    def _renew(self) -> None:
        while not self._stop.wait(RENEW_EVERY):
            try:
                status, _ = self._post("/gpu-share/call/renew", {"lease": self.lease})
            except (OSError, ValueError) as exc:
                log.warning("gpu lease renew failed (%s)", exc)
                continue
            if status == 404:
                log.warning("gpu lease %s expired; taking a new one", self.lease)
                self.lease = None
                self._stop.set()
                self.begin()
                return

    def end(self) -> None:
        self._stop.set()
        lease, self.lease = self.lease, None
        if lease is None:
            return
        try:
            self._post("/gpu-share/call/end", {"lease": lease})
            log.info("gpu lease %s released", lease)
        except (OSError, ValueError) as exc:
            log.warning("gpu lease end failed (%s); it expires on its own", exc)
