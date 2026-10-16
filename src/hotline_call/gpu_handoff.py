"""Hand the GPU to PersonaPlex for one call, and give it back afterwards.

PersonaPlex takes ~7.3 GiB of the 4060's 8 GiB, so it only fits with nothing else on
the card. Two things are usually there: ollama models (bge-m3, 744 MiB, loaded by
anything that embeds -- it OOM-killed a PersonaPlex call on 2026-10-15) and cvoiced,
the Serbian TTS daemon that every ordinary call and `call-bogdan` page depends on.

`acquire()` unloads the ollama models (keep_alive 0: the next request reloads them)
and stops cvoiced if it was running. `release()` starts cvoiced again and waits for
its health check. The daemon calls `release()` in a `finally`, so cvoiced comes back
however the call ends -- declined, unanswered, crashed, or PersonaPlex never starting.

Every step is best effort and logged rather than raised: a failed unload is a
PersonaPlex call that may not fit (and then falls back to the Serbian path), never an
exception out of `place()`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

OLLAMA = os.environ.get("HOTLINE_IOS_OLLAMA_URL", "http://127.0.0.1:11434")
CVOICED_UNIT = os.environ.get("HOTLINE_IOS_CVOICED_UNIT", "cvoiced.service")
HEALTH_WAIT_S = 60.0


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30, check=False)


class GpuHandoff:
    def __init__(self, health: Callable[[], Any] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] = _systemctl,
                 ollama: str = OLLAMA, unit: str = CVOICED_UNIT, wait_s: float = HEALTH_WAIT_S) -> None:
        self.health = health          # the daemon's TTS client .health(); raises while cvoiced is down
        self.run = run
        self.ollama = ollama.rstrip("/")
        self.unit = unit
        self.wait_s = wait_s
        self.stopped_cvoiced = False

    # -- before the call -------------------------------------------------------------
    def acquire(self) -> list[str]:
        """Free the GPU. Returns notes for the call's record."""
        notes = []
        for name in self._ollama_models():
            # bge-m3 is embedding-only and generate refuses it; a chat model is the reverse
            err: Exception | None = None
            for path, body in (("api/embed", {"model": name, "input": "", "keep_alive": 0}),
                               ("api/generate", {"model": name, "keep_alive": 0})):
                try:
                    req = urllib.request.Request(f"{self.ollama}/{path}", method="POST", data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=15).read()
                    err = None
                    break
                except Exception as exc:  # noqa: BLE001 -- best effort, see module doc
                    err = exc
            if err is None:
                notes.append(f"unloaded ollama model {name}")
            else:
                log.warning("could not unload ollama model %s: %s", name, err)
                notes.append(f"could not unload ollama model {name}: {err}")
        try:
            active = self.run("is-active", self.unit).stdout.strip() == "active"
            if active:
                r = self.run("stop", self.unit)
                self.stopped_cvoiced = r.returncode == 0
                notes.append(f"stopped {self.unit}" if self.stopped_cvoiced else f"could not stop {self.unit}: {r.stderr.strip()[:200]}")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not stop %s: %s", self.unit, exc)
            notes.append(f"could not stop {self.unit}: {exc}")
        log.info("gpu handoff: %s", "; ".join(notes) or "nothing to free")
        return notes

    def _ollama_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.ollama}/api/ps", timeout=3) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception:  # noqa: BLE001 -- ollama not running means nothing to unload
            return []

    # -- after the call (always) -----------------------------------------------------
    def release(self) -> str:
        """Bring cvoiced back if this handoff stopped it, and wait until it answers."""
        if not self.stopped_cvoiced:
            return ""
        try:
            r = self.run("start", self.unit)
            if r.returncode != 0:
                log.error("could not restart %s: %s", self.unit, r.stderr.strip()[:200])
                return f"could not restart {self.unit}: {r.stderr.strip()[:200]}"
        except Exception as exc:
            log.exception("could not restart %s", self.unit)
            return f"could not restart {self.unit}: {exc}"
        self.stopped_cvoiced = False
        if self.health is None:
            return f"restarted {self.unit}"
        deadline = time.monotonic() + self.wait_s
        while time.monotonic() < deadline:
            try:
                self.health()
                log.info("gpu handoff: %s back and healthy", self.unit)
                return f"restarted {self.unit}, healthy"
            except Exception:  # noqa: BLE001 -- still starting
                time.sleep(1.0)
        log.error("%s restarted but not healthy after %.0f s", self.unit, self.wait_s)
        return f"restarted {self.unit}, NOT healthy after {self.wait_s:.0f} s"
