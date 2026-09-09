"""Speaking on a phone call, in his own cloned voice.

**Why HTTP to cvoiced rather than a model in this process.** The voice is
OmniVoice with his enrolled profile, and it is already resident in `cvoiced` --
6.4 GB of an 8 GB card on 2026-09-09. Loading a second copy here is not slow,
it is impossible. So this is a client, and the only thing it owns is the
request.

**Why `takes=1`.** cvoice's default is three takes scored against each other by
a Whisper model, which is the right trade for a recording nobody is waiting on.
On a call it is the wrong one twice over: generation time triples, and the
scorer is a `large-v3` that costs 3.7 GB of VRAM this box does not have spare.
Measured on 2026-09-09, `takes=1` returns 2.84 s of audio in 1.68-1.72 s across
three runs -- roughly 0.6x realtime, which is what makes speaking a long answer
sentence by sentence worthwhile rather than cosmetic.

**Why it reads cvoice's own config file.** The server address, the token and the
profile name are cvoice's facts, not ours, and a second copy of them here would
be a second thing to update when he re-enrols a voice. Everything is
env-overridable for a test or a second box.
"""

from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import urllib.error
import urllib.request
import wave

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_CONFIG = "~/.config/cvoice/config.toml"


class VoiceUnavailable(RuntimeError):
    """cvoiced could not be reached or refused. A call with no voice is not a
    call, so this is raised rather than swallowed -- the answered-call handler
    turns it back into the plain doorbell, which at least rings honestly."""


def _load_config(path: str = "") -> dict:
    import tomllib

    where = pathlib.Path(os.path.expanduser(path or os.environ.get(
        "CVOICE_CONFIG", DEFAULT_CONFIG)))
    try:
        with where.open("rb") as handle:
            return tomllib.load(handle)
    except OSError:
        log.warning("no cvoice config at %s; relying on the environment", where)
        return {}


class Voice:
    """cvoiced's `/speak`, and nothing else.

    `fallback` is the same fix cvoice's own client carries and for the same
    reason: the daemon binds ONE address -- the tailnet one, so a laptop can
    reach it -- while the client config points at loopback, which on this box
    gets ECONNREFUSED from a daemon that is running perfectly well. The first
    connection failure retries against the server's own bind address and sticks
    with whichever answered.
    """

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        profile: str = "",
        *,
        fallback: str = "",
        takes: int = 1,
        timeout: float = 60.0,
        config_path: str = "",
    ) -> None:
        config = _load_config(config_path) if not (base_url and token) else {}
        client = config.get("client", {})
        server = config.get("server", {})
        bound = ""
        if server.get("host") and server.get("port"):
            bound = f"http://{server['host']}:{server['port']}"
        self.base = (base_url or os.environ.get("CVOICE_URL", "")
                     or client.get("server", "") or bound).rstrip("/")
        self.fallback = (fallback or bound).rstrip("/")
        self.token = token or os.environ.get("CVOICE_TOKEN", "") or client.get("token", "")
        self.profile = (profile or os.environ.get("CVOICE_PROFILE", "")
                        or client.get("profile", ""))
        self.takes = takes
        self.timeout = timeout
        # Deliberately the same shape as `hotline.audio.Speaker`: `synthesize`
        # plus a `rate`. That is what lets the answered-call handler take either
        # one without knowing which, and it is why this does not return a
        # (audio, rate) tuple that only this class would speak.
        self.rate = 24_000

    def __repr__(self) -> str:
        return f"Voice(base={self.base!r}, profile={self.profile!r}, takes={self.takes})"

    def health(self) -> dict:
        return self._get("/health")

    def synthesize(self, text: str) -> np.ndarray:
        """Float32 mono in [-1, 1] at `self.rate`."""
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        body: dict[str, object] = {"text": text, "takes": self.takes}
        if self.profile:
            body["profile"] = self.profile
        payload = self._post("/speak", body)
        import base64

        audio, rate = _wav_to_float(base64.b64decode(payload["audio_b64"]))
        self.rate = rate
        return audio

    # -- the wire ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str) -> dict:
        return self._try(lambda base: self._call(base + path, None, 15.0))

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        return self._try(lambda base: self._call(base + path, data, self.timeout))

    def _call(self, url: str, data: bytes | None, timeout: float) -> dict:
        request = urllib.request.Request(url, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise VoiceUnavailable(f"cvoiced said {exc.code}: {detail}") from exc

    def _try(self, call) -> dict:
        """Only connection-level failures fall through to the fallback. A 4xx
        means we reached the right daemon and it said no, and retrying elsewhere
        would hide that."""
        try:
            return call(self.base)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            if not self.fallback or self.fallback == self.base:
                raise VoiceUnavailable(f"cvoiced unreachable at {self.base}: {exc}") from exc
            try:
                result = call(self.fallback)
            except (urllib.error.URLError, OSError) as second:
                raise VoiceUnavailable(
                    f"cvoiced unreachable at {self.base} and {self.fallback}: {second}"
                ) from second
            self.base = self.fallback  # only after it actually worked
            return result


def _wav_to_float(raw: bytes) -> tuple[np.ndarray, int]:
    """cvoiced returns a 16-bit mono WAV. Anything else is a bug worth seeing."""
    with wave.open(io.BytesIO(raw)) as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise VoiceUnavailable(f"cvoiced returned {width * 8}-bit audio; expected 16")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


class Fillers:
    """Short Serbian phrases, synthesised once and played from disk.

    **Why they are not synthesised on demand.** Measured: after he stops
    speaking the pipeline costs about 0.8 s to be sure he stopped, 0.4 s of
    Whisper, 2.8-4.3 s for the agent and 1.7 s of cvoice -- call it seven
    seconds, against roughly one that a person tolerates on a phone. Nothing in
    that chain is getting 7x faster, so the fix is not a shorter wait, it is a
    wait that is not silent. A filler that has to go through cvoiced first costs
    1.7 s of the gap it exists to hide; from disk the first audio lands about
    1.5 s after he stops.

    His idea, 2026-09-08, and it is why the second live call sounded like a
    conversation rather than a lookup.

    Missing files are not an error. `get()` returns None and the caller says
    nothing instead, which costs dead air and never the call.
    """

    def __init__(self, directory: str = "") -> None:
        self.directory = pathlib.Path(
            directory or os.environ.get("HOTLINE_IOS_FILLERS", "")
            or pathlib.Path(__file__).resolve().parents[3] / "fillers")
        self.clips: dict[str, tuple[np.ndarray, int]] = {}
        self.texts: dict[str, str] = {}

    def load(self) -> int:
        index = self.directory / "index.json"
        try:
            entries = json.loads(index.read_text())
        except (OSError, ValueError) as exc:
            log.warning("no fillers at %s (%s); holding phrases will be silence",
                        self.directory, exc)
            return 0
        for name, meta in entries.items():
            try:
                audio = np.load(self.directory / f"{name}.npy")
            except OSError:
                log.warning("filler %r is in the index but not on disk", name)
                continue
            self.clips[name] = (audio, int(meta.get("rate", 24_000)))
            self.texts[name] = str(meta.get("text", ""))
        log.info("%d fillers from %s", len(self.clips), self.directory)
        return len(self.clips)

    def get(self, name: str) -> tuple[np.ndarray, int] | None:
        return self.clips.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.clips
