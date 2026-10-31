"""The answering machine for when archserver is off: runs on pigion, the Pi.

His words: "even when archserver is offline. When i call it rpi answers and
says archserver is offline im waking it and well call you back."

## What it does

1. **Watches hotline-calld** over the tailnet (`/api/v1/ping`, every
   `poll` seconds). calld is declared down only after `down_after` seconds
   with no successful ping (60 s by default), so a restart of calld -- a few
   seconds -- never looks like an outage.
2. **Registers his line only during an outage**, on hotline's own account
   (SIP_USER) with a `+sip.instance` of its own. It never uses production's
   instance: the same instance would REPLACE calld's binding instead of sitting
   beside it. When calld answers pings again the binding is dropped (REGISTER
   Expires: 0), so a call while archserver is up only ever reaches calld.
3. **Answers his call** (SIP_PEER only, everyone else 403) with a fixed clip in
   his cloned voice -- "archserver is off, I'm waking it, hotline will call you
   back" -- and hangs up with BYE. The clip is rendered and encoded on
   archserver at install time (`fallback_clip.py`); this side only frames it as
   RTP, encrypts it (SRTP) and paces it at 20 ms. That is why this needs nothing
   beyond the standard library and `cryptography`: no numpy, no libopus.
4. **Wakes archserver** with a Wake-on-LAN magic packet the moment it answers,
   and twice more over the next few minutes while archserver stays down.
   Deliberately NOT on detecting the outage: archserver powers itself off on
   purpose (wake's `then_do=poweroff`), and waking it every time it does would
   undo that. A call from him is the signal that he wants it up.
5. **Calls him back once**: the call is recorded in a state file, and when
   calld is healthy again this POSTs `/api/v1/call` to it -- the same request
   `hotline-call` makes -- so calld rings him. One callback per outage, and the
   state file makes a pigion reboot in between harmless.

## Test mode

`--test` (or HOTLINE_CALL_FALLBACK_TEST=1) forces "archserver down" without
touching archserver, registers under a separate test instance, accepts calls
only from hotline's OWN account (SIP_USER) instead of his, and keeps its own
state file. Production calld answers that caller 403, so an INVITE from
hotline's account to itself reaches only the test registration. See
`bench/fallback_live.py`.

## Limitations

- **"calld does not answer" is read as "archserver is off".** A crashed calld
  on a running archserver gets the same message and a harmless magic packet.
- **Wake-on-LAN is fire-and-forget.** The packet leaves pigion's wifi as a
  broadcast; whether archserver's NIC and firmware act on it is not something
  this side can see, beyond calld coming back.
- **Audio leans on linphone.org's media relay.** The answer offers pigion's
  tailnet address, but on the live test flexisip rewrote both SDPs to its relay
  and pigion sent there; symmetric latching in `play` follows wherever his
  packets actually come from.
- **One call at a time**, and while one is up a second gets 486.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import random
import re
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("hotline-call.fallback")

DEFAULT_CALLD = "http://100.72.2.62:8792"
CLIP_TEXT = ("Ćao Bogdane. Archserver je trenutno ugašen. Budim ga, i čim se podigne, "
             "hotline će te pozvati.")
CALLBACK_REASON = "Archserver je ponovo gore - zvao si dok je bio ugašen"
CLIP_FORMAT = 1
"""Bumped when the on-disk clip layout changes, so an update re-renders it."""
WOL_PORT = 9
WOL_REPEATS = (0.0, 90.0, 240.0)
"""Seconds after he called at which a magic packet goes out while archserver is
still down. The repeats are for a packet lost on wifi, which a broadcast has no
retransmission for; wake's own morning wol task repeats at +2 and +4 min for the
same reason."""
CALLBACK_ATTEMPTS = 5
PRODUCTION_HOST = "archserver"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hotline-call"


def data_dir() -> Path:
    return (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
            / "hotline-call")


def state_dir() -> Path:
    return (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            / "hotline-call")


def load_env_file(path: Path) -> int:
    """KEY=VALUE lines into os.environ, never overwriting what is already set.

    hotline's `load_env` does this on archserver; hotline is not on the Pi.
    """
    if not path.is_file():
        return 0
    count = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def instance_for(user: str, domain: str, *, test: bool) -> str:
    """This role's `+sip.instance`: stable per account, and never production's."""
    kind = "hotline-call-fallback-test" if test else "hotline-call-fallback"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}:{user}@{domain}"))


def production_instance(user: str, domain: str) -> str:
    """What calld on archserver registers with (see `SipInbound.__init__`)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"hotline-ios-inbound:{user}@{domain}:{PRODUCTION_HOST}"))


def local_address_towards(host: str) -> str:
    """The source address this box would use to reach `host` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, 9))
        return str(probe.getsockname()[0])


def rss_mb() -> float:
    with contextlib.suppress(OSError):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


# ---- the clip --------------------------------------------------------------

class Clip:
    """Pre-encoded RTP payloads, one list per codec, as `fallback_clip` wrote them.

    Files: `manifest.json`, `opus.bin` (big-endian u16 length + payload, per
    frame), `pcmu.bin` and `pcma.bin` (160 bytes per frame).
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest: dict[str, Any] = {}
        self.frames: dict[str, list[bytes]] = {}
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            return
        self.manifest = json.loads(manifest.read_text())
        for name, meta in self.manifest.get("codecs", {}).items():
            path = directory / meta["file"]
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if meta.get("framing") == "u16len":
                frames, offset = [], 0
                while offset + 2 <= len(raw):
                    (size,) = struct.unpack_from("!H", raw, offset)
                    frames.append(raw[offset + 2: offset + 2 + size])
                    offset += 2 + size
            else:
                size = int(meta["frame_bytes"])
                frames = [raw[i: i + size] for i in range(0, len(raw), size)]
            self.frames[name] = frames

    def has(self, name: str) -> bool:
        return bool(self.frames.get(name))

    def describe(self) -> str:
        if not self.frames:
            return f"no clip in {self.directory}"
        parts = [f"{name} {len(frames)} frames" for name, frames in sorted(self.frames.items())]
        return f"clip {self.manifest.get('seconds', '?')} s: " + ", ".join(parts)


SILENCE = {"PCMU": b"\xff" * 160, "PCMA": b"\xd5" * 160}


def frames_for(clip: Clip, codec_name: str) -> list[bytes]:
    """The frames to send for the codec agreed on; a second of silence without a clip."""
    if clip.has(codec_name):
        return clip.frames[codec_name]
    log.error("fallback: no %s clip; sending silence -- he will hear nothing", codec_name)
    return [SILENCE.get(codec_name, b"\xff" * 160)] * 50


# ---- sending it -------------------------------------------------------------

@dataclass
class PlayStats:
    sent: int = 0
    received: int = 0
    rx_auth_failures: int = 0
    latched: str = ""
    seconds: float = 0.0
    hung_up_early: bool = False


def parse_agreed(agreed_offer: str) -> tuple[str, int, int, str]:
    """Remote host, port, payload type, and his crypto line, from `answer_offer`'s
    reduced offer."""
    host, port, pt, crypto = "", 0, 0, ""
    for raw in agreed_offer.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("c=IN IP4 "):
            host = line.split()[2]
        elif line.startswith("m=audio "):
            parts = line.split()
            port, pt = int(parts[1]), int(parts[3])
        elif line.startswith("a=crypto:"):
            crypto = line
    return host, port, pt, crypto


def play(frames: list[bytes], *, sock: socket.socket, remote: tuple[str, int], pt: int,
         ts_step: int, key: bytes, salt: bytes, their_crypto: str = "",
         hung_up: Callable[[], bool] = lambda: False, frame_s: float = 0.020) -> PlayStats:
    """Send `frames` as SRTP at real time, latching to where his packets come from."""
    from .media import srtp

    tx = srtp.SrtpSession(key, salt)
    rx = None
    if their_crypto:
        with contextlib.suppress(srtp.SrtpError, ValueError):
            _tag, their_key, their_salt = srtp.parse_crypto_line(their_crypto)
            rx = srtp.SrtpSession(their_key, their_salt)
    stats = PlayStats()
    ssrc = random.getrandbits(32)
    seq = random.getrandbits(16)
    ts = random.getrandbits(32)
    sock.setblocking(False)
    started = time.monotonic()
    for index, payload in enumerate(frames):
        if hung_up():
            stats.hung_up_early = True
            break
        # Symmetric RTP: once an authenticated packet of his arrives, send to
        # where it came FROM. The SDP address is right on the tailnet; behind any
        # NAT it would not be, and this is what calld's media path does too.
        while True:
            try:
                packet, source = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            stats.received += 1
            if rx is None:
                continue
            try:
                rx.unprotect(packet)
            except Exception:  # noqa: BLE001 - counted, never fatal
                stats.rx_auth_failures += 1
                continue
            if not stats.latched and tuple(source[:2]) != remote:
                log.info("fallback: latching audio to %s:%d (SDP said %s:%d)",
                         source[0], source[1], *remote)
                remote = (source[0], source[1])
            stats.latched = stats.latched or f"{remote[0]}:{remote[1]}"
        marker = 0x80 if index == 0 else 0
        header = struct.pack("!BBHII", 0x80, marker | pt, seq, ts, ssrc)
        with contextlib.suppress(OSError):
            sock.sendto(tx.protect(header + payload), remote)
            stats.sent += 1
        seq = (seq + 1) & 0xFFFF
        ts = (ts + ts_step) & 0xFFFFFFFF
        delay = started + (index + 1) * frame_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    stats.seconds = round(time.monotonic() - started, 2)
    return stats


# ---- waking archserver -------------------------------------------------------

def magic_packet(mac: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(cleaned) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


def send_wol(mac: str, broadcasts: list[str], port: int = WOL_PORT) -> list[str]:
    """Send one magic packet per broadcast address; returns where it went."""
    packet = magic_packet(mac)
    sent = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address in broadcasts:
            try:
                sock.sendto(packet, (address, port))
                sent.append(f"{address}:{port}")
            except OSError as exc:
                log.error("wol: could not send to %s:%d: %s", address, port, exc)
    return sent


# ---- state that outlives a restart --------------------------------------------

class State:
    """A small JSON file: whether he is owed a callback, and what happened last."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"pending_callback": None, "last_callback": None}
        if path.is_file():
            with contextlib.suppress(ValueError, OSError):
                self.data.update(json.loads(path.read_text()))
        self._lock = threading.Lock()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def he_called(self, caller: str) -> bool:
        """Record a call. True when this one is the first of the outage."""
        with self._lock:
            pending = self.data.get("pending_callback")
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if pending:
                pending["calls"] = int(pending.get("calls", 1)) + 1
                pending["last_call"] = now
                self.save()
                return False
            self.data["pending_callback"] = {"first_call": now, "last_call": now, "calls": 1,
                                             "caller": caller, "attempts": 0}
            self.save()
            return True

    def take_pending(self) -> dict[str, Any] | None:
        """Claim the callback, written to disk BEFORE the request goes out: a
        crash mid-request then costs at most the callback, never a second ring."""
        with self._lock:
            pending = self.data.get("pending_callback")
            if not pending:
                return None
            self.data["pending_callback"] = None
            self.data["in_flight"] = pending
            self.save()
            return dict(pending)

    def finish(self, pending: dict[str, Any], outcome: str, *, retry: bool) -> None:
        with self._lock:
            self.data.pop("in_flight", None)
            attempts = int(pending.get("attempts", 0)) + 1
            if retry and attempts < CALLBACK_ATTEMPTS and not self.data.get("pending_callback"):
                self.data["pending_callback"] = {**pending, "attempts": attempts}
            self.data["last_callback"] = {**pending, "attempts": attempts, "outcome": outcome,
                                          "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            self.save()


# ---- configuration -------------------------------------------------------------

@dataclass
class Config:
    calld_url: str = DEFAULT_CALLD
    callback_url: str = ""
    poll: float = 10.0
    down_after: float = 60.0
    test: bool = False
    clip_dir: Path = field(default_factory=lambda: data_dir() / "fallback-clip")
    state_path: Path = field(default_factory=lambda: state_dir() / "fallback.json")
    wol_mac: str = ""
    wol_broadcasts: list[str] = field(default_factory=lambda: ["255.255.255.255"])
    wol_port: int = WOL_PORT
    media_host: str = ""
    callback_grace: float = 15.0

    @classmethod
    def from_env(cls, *, test: bool | None = None) -> Config:
        env = os.environ.get
        cfg = cls()
        cfg.calld_url = env("HOTLINE_CALL_FALLBACK_CALLD", DEFAULT_CALLD).rstrip("/")
        cfg.callback_url = env("HOTLINE_CALL_FALLBACK_CALLBACK_URL", "").rstrip("/")
        cfg.poll = float(env("HOTLINE_CALL_FALLBACK_POLL", "10"))
        cfg.down_after = float(env("HOTLINE_CALL_FALLBACK_DOWN_AFTER", "60"))
        cfg.test = (env("HOTLINE_CALL_FALLBACK_TEST", "") == "1") if test is None else test
        if env("HOTLINE_CALL_FALLBACK_CLIP"):
            cfg.clip_dir = Path(env("HOTLINE_CALL_FALLBACK_CLIP", ""))
        default_state = state_dir() / ("fallback-test.json" if cfg.test else "fallback.json")
        cfg.state_path = Path(env("HOTLINE_CALL_FALLBACK_STATE", "") or default_state)
        cfg.wol_mac = env("HOTLINE_CALL_FALLBACK_WOL_MAC", "")
        cfg.wol_broadcasts = [b.strip() for b in
                              env("HOTLINE_CALL_FALLBACK_WOL_BROADCAST", "255.255.255.255")
                              .split(",") if b.strip()]
        cfg.wol_port = int(env("HOTLINE_CALL_FALLBACK_WOL_PORT", str(WOL_PORT)))
        cfg.media_host = env("HOTLINE_CALL_FALLBACK_MEDIA_HOST", "")
        cfg.callback_grace = float(env("HOTLINE_CALL_FALLBACK_CALLBACK_GRACE", "15"))
        return cfg


# ---- the watch ---------------------------------------------------------------

class Watch:
    """Up/down with hysteresis: down only after `down_after` s of failed pings."""

    def __init__(self, down_after: float, now: float) -> None:
        self.down_after = down_after
        self.last_up = now
        self.down = False

    def observe(self, up: bool, now: float) -> str:
        """"" when nothing changed, else "down" or "up"."""
        if up:
            self.last_up = now
            if self.down:
                self.down = False
                return "up"
            return ""
        if not self.down and now - self.last_up >= self.down_after:
            self.down = True
            return "down"
        return ""


def ping(url: str, timeout: float = 5.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/api/v1/ping", timeout=timeout) as response:
            return int(response.status) == 200
    except Exception:  # noqa: BLE001 - every failure is "not answering"
        return False


class Fallback:
    def __init__(self, cfg: Config, *, inbound_factory: Callable[..., Any] | None = None,
                 probe: Callable[[str], bool] = ping,
                 wol: Callable[[str, list[str], int], list[str]] = send_wol,
                 place: Callable[..., Any] | None = None,
                 health: Callable[..., dict[str, Any]] | None = None) -> None:
        self.cfg = cfg
        self.clip = Clip(cfg.clip_dir)
        self.state = State(cfg.state_path)
        self.watch = Watch(cfg.down_after, time.monotonic())
        self.probe = probe
        self.wol = wol
        self.place = place
        self.health = health
        self.inbound_factory = inbound_factory
        self.inbound: Any = None
        self.outage_since = 0.0
        self.last_call_at = 0.0
        self.calls = 0
        self.callback_thread: threading.Thread | None = None
        self.up_since = 0.0
        self.last_play: PlayStats | None = None
        self._stop = threading.Event()
        self.user = os.environ.get("SIP_USER", "")
        self.domain = os.environ.get("SIP_DOMAIN", "sip.linphone.org")
        self.instance = instance_for(self.user, self.domain, test=cfg.test)
        if self.instance == production_instance(self.user, self.domain):
            raise SystemExit("hotline-call-fallback: refusing production's +sip.instance")

    # ---- registration --------------------------------------------------------

    def _peer(self) -> str:
        if self.cfg.test:
            # Hotline's own account: what production calld refuses with 403,
            # so a test call reaches only this registration.
            return f"sip:{self.user}@{self.domain}"
        return os.environ.get("SIP_PEER", "")

    def _make_inbound(self) -> Any:
        if self.inbound_factory is not None:
            return self.inbound_factory(self)
        from .ring.inbound import SipInbound

        media_host = self.cfg.media_host
        if not media_host:
            try:
                media_host = local_address_towards(
                    re.sub(r"^https?://|:\d+.*$", "", self.cfg.calld_url))
            except OSError:
                media_host = ""
        return SipInbound(peer=self._peer(), instance=self.instance, media_host=media_host,
                          on_call=self._on_call, opus=self.clip.has("opus"),
                          answer_after=1.0, user_agent="hotline-call fallback")

    def register(self) -> None:
        if self.inbound is not None:
            return
        self.inbound = self._make_inbound()
        why = self.inbound.configured() if hasattr(self.inbound, "configured") else ""
        if why:
            log.error("fallback: cannot register: %s", why)
            self.inbound = None
            return
        self.inbound.start()
        log.info("fallback: registering %s (instance %s%s), %s; RSS %.1f MB",
                 self.user, self.instance[:8], ", TEST" if self.cfg.test else "",
                 self.clip.describe(), rss_mb())

    def unregister(self) -> None:
        if self.inbound is None:
            return
        self.inbound.stop()
        log.info("fallback: dropped the registration")
        self.inbound = None

    # ---- a call --------------------------------------------------------------

    def _on_call(self, call: Any) -> Callable[..., None]:
        self.calls += 1
        self.last_call_at = time.monotonic()
        first = self.state.he_called(call.caller)
        log.info("fallback: %s is calling (%s call of this outage)", call.caller,
                 "first" if first else "another")
        threading.Thread(target=self._wake_archserver, name="wol", daemon=True).start()

        def handler(agreed_offer: str, media: socket.socket, key: bytes, salt: bytes) -> None:
            from .media import codec

            choice = codec.negotiate(agreed_offer, opus=self.clip.has("opus"))
            host, port, pt, crypto = parse_agreed(agreed_offer)
            name = choice.name if choice is not None else "PCMU"
            clock = choice.clock if choice is not None else 8000
            call.acked.wait(2.0)
            frames = frames_for(self.clip, name)
            stats = play(frames, sock=media, remote=(host, port), pt=pt,
                         ts_step=clock * codec.FRAME_MS // 1000, key=key, salt=salt,
                         their_crypto=crypto, hung_up=call.far_end_hung_up)
            log.info("fallback: played %s: %d/%d frames in %.2f s, %d of his packets "
                     "(%d failed auth)%s; hanging up; RSS %.1f MB",
                     name, stats.sent, len(frames), stats.seconds, stats.received,
                     stats.rx_auth_failures, ", he hung up first" if stats.hung_up_early else "",
                     rss_mb())
            self.last_play = stats

        return handler

    def _wake_archserver(self) -> None:
        if not self.cfg.wol_mac:
            log.error("wol: HOTLINE_CALL_FALLBACK_WOL_MAC is not set; cannot wake archserver")
            return
        started = time.monotonic()
        for offset in WOL_REPEATS:
            delay = started + offset - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            if offset and not self.watch.down:
                return   # it is back
            sent = self.wol(self.cfg.wol_mac, self.cfg.wol_broadcasts, self.cfg.wol_port)
            log.info("wol: magic packet for %s to %s (+%.0f s)", self.cfg.wol_mac,
                     ", ".join(sent) or "nowhere", offset)

    # ---- the callback ----------------------------------------------------------

    def _callback_ready(self) -> bool:
        if self.callback_thread is not None and self.callback_thread.is_alive():
            return False
        if not self.state.data.get("pending_callback"):
            return False
        if self.cfg.test:
            return False   # forced down: there is no "back up" to call back from
        if time.monotonic() - self.up_since < self.cfg.callback_grace:
            return False
        if self.cfg.callback_url:
            return True    # a test target: nothing to ask it
        health = self.health or _health
        try:
            body = health(self.cfg.calld_url)
        except Exception as exc:  # noqa: BLE001
            log.info("fallback: calld /health not answering yet (%s)", exc)
            return False
        inbound = body.get("inbound")
        if not body.get("ok") or (isinstance(inbound, dict) and not inbound.get("registered")):
            log.info("fallback: calld is up but not ready to ring (ok=%s); waiting",
                     body.get("ok"))
            return False
        return True

    def _callback(self) -> None:
        pending = self.state.take_pending()
        if pending is None:
            return
        from . import client

        url = self.cfg.callback_url or self.cfg.calld_url
        calls = int(pending.get("calls", 1))
        context = (f"Zvao je {calls} {'put' if calls == 1 else 'puta'} dok archserver "
                   f"nije radio, prvi put u {pending.get('first_call')}. Odgovorio mu je "
                   "pigion i rekao da budi archserver.")
        place = self.place or client.place_call
        log.info("fallback: calling him back through %s", url)
        try:
            outcome = place(CALLBACK_REASON, url=url, wait=False, context=context,
                            source="hotline-call-fallback", agent="hotline-call-fallback",
                            ring_timeout=45.0, timeout=600.0)
            state = getattr(outcome, "state", str(outcome))
            log.info("fallback: callback placed: %s", state)
            self.state.finish(pending, f"placed: {state}", retry=False)
        except client.CallTimeout as exc:
            # The request was accepted and is still running: it may be ringing
            # right now. Never retried.
            log.info("fallback: callback request outlived its timeout (%s)", exc)
            self.state.finish(pending, f"timeout: {exc}", retry=False)
        except client.DaemonUnreachable as exc:
            log.warning("fallback: callback refused at connect: %s; will retry", exc)
            self.state.finish(pending, f"unreachable: {exc}", retry=True)
        except client.DaemonError as exc:
            # An HTTP error status means calld refused it before ringing; a
            # reset mid-response may not have. Retry only the former.
            refused = bool(re.match(r"^\d{3} ", str(exc)))
            log.warning("fallback: callback failed: %s%s", exc, "; will retry" if refused else "")
            self.state.finish(pending, f"error: {exc}", retry=refused)
        except Exception as exc:
            log.exception("fallback: callback raised")
            self.state.finish(pending, f"raised: {exc}", retry=False)

    # ---- the loop ----------------------------------------------------------------

    def tick(self) -> None:
        now = time.monotonic()
        up = False if self.cfg.test else self.probe(self.cfg.calld_url)
        change = self.watch.observe(up, now)
        if self.cfg.test and self.inbound is None:
            change = "down"
            self.watch.down = True
        if change == "down":
            self.outage_since = now
            log.warning("fallback: %s for %.0f s; taking his calls",
                        "TEST MODE: pretending calld is down" if self.cfg.test
                        else f"calld at {self.cfg.calld_url} has not answered",
                        now - self.watch.last_up)
            self.register()
        elif change == "up":
            self.up_since = now
            log.info("fallback: calld answers again after %.0f s down",
                     now - self.outage_since if self.outage_since else 0)
        # Never cut a call he is on; with one up, the next tick drops it.
        if up and self.inbound is not None and getattr(self.inbound, "call", None) is None:
            self.unregister()
        if up and self._callback_ready():
            self.callback_thread = threading.Thread(target=self._callback, name="callback",
                                                    daemon=True)
            self.callback_thread.start()

    def run(self) -> None:
        log.info("fallback: watching %s every %.0f s, down after %.0f s%s; %s; state %s",
                 self.cfg.calld_url, self.cfg.poll, self.cfg.down_after,
                 " (TEST MODE)" if self.cfg.test else "", self.clip.describe(),
                 self.cfg.state_path)
        if self.state.data.get("in_flight"):
            log.warning("fallback: a callback was in flight when the last run stopped; "
                        "not repeating it")
            self.state.finish(self.state.data["in_flight"], "interrupted", retry=False)
        if self.state.data.get("pending_callback"):
            log.info("fallback: he is owed a callback from before the restart")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # the loop is the recovery
                log.exception("fallback: tick raised")
            self._stop.wait(self.cfg.poll)
        self.unregister()

    def stop(self) -> None:
        self._stop.set()


def _health(url: str) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
        return dict(json.loads(response.read() or b"{}"))


# ---- the command -------------------------------------------------------------

def _load_env() -> None:
    """Credentials, then the fallback's settings. The real environment wins."""
    chosen = os.environ.get("HOTLINE_CALL_ENV", "")
    load_env_file(Path(chosen) if chosen else config_dir() / "env")
    load_env_file(config_dir() / "fallback.env")


def cmd_serve(args: argparse.Namespace) -> int:
    _load_env()
    cfg = Config.from_env(test=True if args.test else None)
    if not (os.environ.get("SIP_USER") and os.environ.get("SIP_PASSWORD")):
        print("hotline-call-fallback: SIP_USER and SIP_PASSWORD are not set "
              f"(looked in {config_dir() / 'env'})", file=sys.stderr)
        return 2
    service = Fallback(cfg)
    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())
    if args.for_seconds:
        threading.Timer(args.for_seconds, service.stop).start()
    service.run()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _load_env()
    cfg = Config.from_env(test=True if args.test else None)
    state = State(cfg.state_path)
    clip = Clip(cfg.clip_dir)
    user = os.environ.get("SIP_USER", "")
    domain = os.environ.get("SIP_DOMAIN", "sip.linphone.org")
    out = {
        "calld": cfg.calld_url,
        "calld_answers": ping(cfg.calld_url),
        "test_mode": cfg.test,
        "account": user,
        "peer": os.environ.get("SIP_PEER", ""),
        "instance": instance_for(user, domain, test=cfg.test),
        "clip": clip.describe(),
        "clip_text": clip.manifest.get("text", ""),
        "wol_mac": cfg.wol_mac or None,
        "wol_broadcasts": cfg.wol_broadcasts,
        "down_after_s": cfg.down_after,
        "state_file": str(cfg.state_path),
        "state": state.data,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_wol(args: argparse.Namespace) -> int:
    _load_env()
    cfg = Config.from_env()
    mac = args.mac or cfg.wol_mac
    if not mac:
        print("hotline-call-fallback: no MAC (HOTLINE_CALL_FALLBACK_WOL_MAC)", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"would send {len(magic_packet(mac))} bytes for {mac} to "
              f"{', '.join(f'{b}:{args.port}' for b in cfg.wol_broadcasts)}")
        return 0
    sent = send_wol(mac, cfg.wol_broadcasts, args.port)
    print(f"sent magic packet for {mac} to {', '.join(sent) or 'nowhere'}")
    return 0 if sent else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hotline-call-fallback",
                                     description="answer his calls while archserver is down")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="watch calld and answer while it is down")
    serve.add_argument("--test", action="store_true",
                       help="force 'down', test instance, accept only hotline's own account")
    serve.add_argument("--for-seconds", type=float, default=0.0,
                       help="stop (and unregister) after this long")
    status = sub.add_parser("status", help="configuration, clip and callback state")
    status.add_argument("--test", action="store_true", help="the test-mode view")
    wol = sub.add_parser("wol", help="send the magic packet now")
    wol.add_argument("--mac", default="")
    wol.add_argument("--port", type=int, default=WOL_PORT)
    wol.add_argument("--dry-run", action="store_true")
    render = sub.add_parser("render-clip",
                            help="on archserver: render and pre-encode the clip (needs numpy)")
    render.add_argument("--out", required=True)
    render.add_argument("--text", default=CLIP_TEXT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "wol":
        return cmd_wol(args)
    from . import fallback_clip

    return fallback_clip.render(Path(args.out), args.text)


if __name__ == "__main__":
    raise SystemExit(main())
