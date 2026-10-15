"""Full-duplex bridge between a telephone leg and a streaming speech model process.

The leg's rate is the negotiated codec's: 48 kHz on Opus, 8 kHz on G.711, set by
`use_wire_rate` once the call is answered. On G.711 the model hears nothing above
4 kHz -- which is how both engines' first test calls went.

No VAD, no turn gate: caller audio is pushed continuously and the model's audio is
pulled continuously. Two engines speak the same pipe protocol (float32 frames in on
stdin, float32 audio out on stdout, events on stderr), at different rates:

- pp-stream (PersonaPlex, Mimi 24 kHz) is lockstep, one 80 ms frame out per frame in.
- llama-voicechat --stream (Nemotron VoiceChat) takes 16 kHz and writes 22.05 kHz
  audio in codec chunks of ~480 ms whenever it has them, so it needs a deeper
  playout buffer (measured: 0 underruns over 94 s at 500 ms, 13 at 300 ms).

Either way the caller side must keep pushing -- silence included -- or the model
stops hearing time pass.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import soxr

WIRE_RATE = 8_000   # until use_wire_rate() says otherwise


@dataclass(frozen=True)
class EngineSpec:
    name: str
    in_rate: int
    in_frame: int      # samples per 80 ms input frame at in_rate
    out_rate: int
    prime_s: float     # playout buffer before the first sample goes out


PERSONAPLEX = EngineSpec("personaplex", 24_000, 1920, 24_000, 0.12)
NEMOTRON = EngineSpec("nemotron", 16_000, 1280, 22_050, 0.5)
# Kept for callers written against the PersonaPlex-only version.
MODEL_RATE = PERSONAPLEX.in_rate
FRAME_24K = PERSONAPLEX.in_frame


class DuplexEngine:
    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, log=None,
                 spec: EngineSpec = PERSONAPLEX,
                 on_tool: Callable[[str], str] | None = None,
                 on_tool_start: Callable[[], None] | None = None) -> None:
        self.cmd = cmd
        self.spec = spec
        self.env = {**os.environ, **(env or {})}
        self.log = log or (lambda line: None)
        self.on_tool = on_tool
        self.on_tool_start = on_tool_start
        self.text: list[tuple[int, str]] = []  # (frame index, piece)
        self.stats: list[tuple[int, float, float]] = []  # pp-stream's STAT lines
        self.stat_lines: list[str] = []
        self.tool_calls: list[dict[str, object]] = []
        self.ready = threading.Event()
        self.dead = threading.Event()
        self.wire_rate = WIRE_RATE
        self._up = soxr.ResampleStream(WIRE_RATE, spec.in_rate, 1, dtype="float32")
        self._down = soxr.ResampleStream(spec.out_rate, WIRE_RATE, 1, dtype="float32")
        self._in = np.zeros(0, dtype=np.float32)
        self._out8: deque[np.ndarray] = deque()
        self._out8_len = 0
        self._overlay: deque[np.ndarray] = deque()  # our own audio mixed over the model's
        self._lock = threading.Lock()
        self.frames_out = 0
        self.underruns = 0
        self._primed = False
        self.prime = int(WIRE_RATE * spec.prime_s)
        self.proc: subprocess.Popen | None = None
        self._ctl_w: int | None = None

    def use_wire_rate(self, rate: int) -> None:
        """Set the telephone leg's rate once the call is answered.

        The engine is built when the phone starts ringing, before the answer
        says which codec won, so the resamplers are rebuilt here, before the
        first push. Anything the model already produced at the old rate is
        dropped: it was never going to be played to anyone.
        """
        if rate == self.wire_rate:
            return
        with self._lock:
            self.wire_rate = rate
            self._up = soxr.ResampleStream(rate, self.spec.in_rate, 1, dtype="float32")
            self._down = soxr.ResampleStream(self.spec.out_rate, rate, 1, dtype="float32")
            self.prime = int(rate * self.spec.prime_s)
            self._in = np.zeros(0, dtype=np.float32)
            self._out8.clear()
            self._out8_len = 0

    def start(self, timeout: float = 120.0) -> float:
        t0 = time.monotonic()
        ctl_r = None
        if self.on_tool is not None:
            # Tool responses go back on fd 3, one line each.
            ctl_r, self._ctl_w = os.pipe()

        def child() -> None:
            if ctl_r is not None:
                os.dup2(ctl_r, 3)

        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=self.env, bufsize=0,
                                     pass_fds=(ctl_r, 3) if ctl_r is not None else (),  # 3 too: close_fds runs after preexec_fn
                                     preexec_fn=child if ctl_r is not None else None)
        if ctl_r is not None:
            os.close(ctl_r)
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        threading.Thread(target=self._stdout_loop, daemon=True).start()
        if not self.ready.wait(timeout) or self.dead.is_set():
            self.close()
            raise RuntimeError(f"{self.spec.name} did not become ready")
        return time.monotonic() - t0

    def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("READY"):
                self.ready.set()
            elif line.startswith("TXT "):
                _, idx, *rest = line.split(" ", 2)
                self.text.append((int(idx), rest[0] if rest else ""))
            elif line.startswith("STAT "):
                self.stat_lines.append(line)
                parts = line.split()
                if len(parts) == 4 and parts[1].isdigit():
                    self.stats.append((int(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("TOOLSTART"):
                if self.on_tool_start is not None:
                    threading.Thread(target=self.on_tool_start, daemon=True).start()
            elif line.startswith("TOOL "):
                _, _t, *rest = line.split(" ", 2)
                threading.Thread(target=self._answer_tool, args=(rest[0] if rest else "",),
                                 daemon=True).start()
            self.log(line)
        self.dead.set()
        self.ready.set()

    def _answer_tool(self, call: str) -> None:
        began = time.monotonic()
        try:
            answer = self.on_tool(call) if self.on_tool else ""
        except Exception as exc:  # noqa: BLE001 -- the model must get *a* line back
            answer = f'{{"error": "{type(exc).__name__}"}}'
        answer = " ".join(answer.split())  # one line, it is the framing
        self.tool_calls.append({"call": call, "answer": answer,
                                "seconds": round(time.monotonic() - began, 2)})
        if self._ctl_w is not None:
            try:
                os.write(self._ctl_w, (answer + "\n").encode())
            except OSError:
                pass

    def _stdout_loop(self) -> None:
        assert self.proc and self.proc.stdout
        pending = b""
        while True:
            chunk = self.proc.stdout.read(16384)
            if not chunk:
                self.dead.set()
                return
            pending += chunk
            usable = len(pending) - len(pending) % 4
            if not usable:
                continue
            block, pending = pending[:usable], pending[usable:]
            with self._lock:
                out = self._down.resample_chunk(np.frombuffer(block, dtype="<f4"))
                self._out8.append(out)
                self._out8_len += out.size
                self.frames_out += 1

    def push(self, pcm16: bytes) -> None:
        """Caller audio at `wire_rate`, mono LE int16, any chunk size."""
        if self.dead.is_set() or not self.proc or not self.proc.stdin:
            return
        x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        self._in = np.concatenate([self._in, self._up.resample_chunk(x)])
        n = self.spec.in_frame
        while self._in.size >= n:
            frame, self._in = self._in[:n], self._in[n:]
            try:
                self.proc.stdin.write(frame.astype("<f4").tobytes())
            except (BrokenPipeError, OSError):
                self.dead.set()
                return

    def say(self, audio: np.ndarray) -> None:
        """Mix our own audio (float, at `wire_rate`) over whatever the model sends next."""
        with self._lock:
            self._overlay.append(audio.astype(np.float32))

    def _take(self, q: deque, samples: int) -> tuple[np.ndarray, int]:
        got: list[np.ndarray] = []
        n = 0
        while q and n < samples:
            head = q[0]
            take = min(samples - n, head.size)
            got.append(head[:take])
            n += take
            if take == head.size:
                q.popleft()
            else:
                q[0] = head[take:]
        if n < samples:
            got.append(np.zeros(samples - n, dtype=np.float32))
        return np.concatenate(got), n

    def pull(self, samples: int) -> bytes:
        """Model audio at `wire_rate`, mono LE int16; zero-padded when the model is behind."""
        with self._lock:
            over, _ = self._take(self._overlay, samples) if self._overlay else (None, 0)
            if not self._primed and self._out8_len >= self.prime:
                self._primed = True
            if not self._primed:
                x = np.zeros(samples, dtype=np.float32)
            else:
                if self._out8_len < samples:
                    self.underruns += 1
                    self._primed = False
                x, n = self._take(self._out8, samples)
                self._out8_len -= n
        if over is not None:
            x = x + over
        return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def buffered(self) -> int:
        with self._lock:
            return self._out8_len

    def close(self) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._ctl_w is not None:
            try:
                os.close(self._ctl_w)
            except OSError:
                pass
            self._ctl_w = None
