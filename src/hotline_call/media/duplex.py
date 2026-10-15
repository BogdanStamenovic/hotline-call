"""Full-duplex bridge between an 8 kHz telephone leg and pp-stream (PersonaPlex, 24 kHz Mimi frames).

No VAD, no turn gate: caller audio is pushed continuously and the model's audio is
pulled continuously. pp-stream is lockstep (one 80 ms frame in, one out), so the
caller side must keep pushing -- silence included -- or the model stops talking.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque

import numpy as np
import soxr

WIRE_RATE = 8_000
MODEL_RATE = 24_000
FRAME_24K = 1920  # 80 ms


class DuplexEngine:
    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, log=None) -> None:
        self.cmd = cmd
        self.env = {**os.environ, **(env or {})}
        self.log = log or (lambda line: None)
        self.text: list[tuple[int, str]] = []  # (frame index, piece)
        self.stats: list[tuple[int, float, float]] = []
        self.ready = threading.Event()
        self.dead = threading.Event()
        self._up = soxr.ResampleStream(WIRE_RATE, MODEL_RATE, 1, dtype="float32")
        self._down = soxr.ResampleStream(MODEL_RATE, WIRE_RATE, 1, dtype="float32")
        self._in24 = np.zeros(0, dtype=np.float32)
        self._out8: deque[np.ndarray] = deque()
        self._out8_len = 0
        self._lock = threading.Lock()
        self.frames_out = 0
        self.underruns = 0
        self._primed = False
        self.prime = int(WIRE_RATE * 0.12)  # model frames arrive in 80 ms bursts; cover one plus compute jitter
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 120.0) -> float:
        t0 = time.monotonic()
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=self.env, bufsize=0)
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        threading.Thread(target=self._stdout_loop, daemon=True).start()
        if not self.ready.wait(timeout) or self.dead.is_set():
            self.close()
            raise RuntimeError("pp-stream did not become ready")
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
                _, n, avg, mx = line.split()
                self.stats.append((int(n), float(avg), float(mx)))
            self.log(line)
        self.dead.set()
        self.ready.set()

    def _stdout_loop(self) -> None:
        assert self.proc and self.proc.stdout
        need = FRAME_24K * 4
        while True:
            buf = b""
            while len(buf) < need:
                chunk = self.proc.stdout.read(need - len(buf))
                if not chunk:
                    self.dead.set()
                    return
                buf += chunk
            frame = np.frombuffer(buf, dtype="<f4")
            out = self._down.resample_chunk(frame)
            with self._lock:
                self._out8.append(out)
                self._out8_len += out.size
                self.frames_out += 1

    def push(self, pcm16: bytes) -> None:
        """Caller audio, 8 kHz mono LE int16, any chunk size."""
        if self.dead.is_set() or not self.proc or not self.proc.stdin:
            return
        x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        self._in24 = np.concatenate([self._in24, self._up.resample_chunk(x)])
        while self._in24.size >= FRAME_24K:
            frame, self._in24 = self._in24[:FRAME_24K], self._in24[FRAME_24K:]
            try:
                self.proc.stdin.write(frame.astype("<f4").tobytes())
            except (BrokenPipeError, OSError):
                self.dead.set()
                return

    def pull(self, samples: int) -> bytes:
        """Model audio, 8 kHz mono LE int16; zero-padded when the model is behind."""
        got: list[np.ndarray] = []
        n = 0
        with self._lock:
            if not self._primed and self._out8_len >= self.prime:
                self._primed = True
            if not self._primed:
                return bytes(samples * 2)
            if self._out8_len < samples:
                self.underruns += 1
                self._primed = False
            while self._out8 and n < samples:
                head = self._out8[0]
                take = min(samples - n, head.size)
                got.append(head[:take])
                n += take
                if take == head.size:
                    self._out8.popleft()
                else:
                    self._out8[0] = head[take:]
            self._out8_len -= n
        if n < samples:
            got.append(np.zeros(samples - n, dtype=np.float32))
        x = np.concatenate(got) if got else np.zeros(samples, dtype=np.float32)
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
