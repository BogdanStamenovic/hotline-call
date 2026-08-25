"""Make an unconfirmed ring an error instead of a silence.

This is the requirement every option shares, and it is the one piece of design
that is right under every outcome.

**On the justification, and a correction to an earlier version of this file.**
This module first cited an Apple Developer Forums thread (756941) in which an
Apple engineer supposedly answered "100%, no" to whether a packet tunnel
provider keeps running while the phone is locked. **That citation could not be
verified.** Two agents independently tried to fetch it: the page serves a
JavaScript shell with none of the quoted terms in the HTML, and the forums API
404s for that thread. It reached this file second-hand and was written down as
settled fact, which it was not. Recorded here rather than quietly deleted,
because a mechanism resting on a quote that evaporates when someone checks is
exactly the thing that gets a sound design thrown out with a bad source.

What *is* verifiable, and what actually happened when someone measured instead
of arguing:

- `tailscale/tailscale#17575`, `nickoneill` (a Tailscale contributor, attribution
  checkable): the behaviour is "largely driven by the timing around iOS starting
  the VPN based on on-demand rules" and "will still result in some of this
  waiting behavior between states" -- a 5-10 s reconnect after sleep/wake.
- **Measured on Bogdan's own phone, locked and idle: 20/20 `tailscale ping`
  answered, 0% loss; peer-map sampled every 30 s for 20 minutes, present every
  time.** The tunnel is not dead when the phone is locked. The strong claim was
  wrong, and the measurement beats both the forum post and the argument.

So the honest statement is the weaker one: **the tailnet cannot be *assumed* up
at ring time** -- there is a real cold-start penalty on a phone nobody has
touched for hours, which nobody has measured -- not "it is categorically
suspended".

**None of that changes what this module does**, which is why the mechanism
survived the correction intact. Failing closed is correct in both worlds:

- Outcome C fails if Belledonne tightens a check or drops a free tier.
- Outcome B fails on a phone reboot, a certificate expiry, a force-quit, or an
  audio session interrupted by an ordinary incoming phone call.
- None of those produce an error. The call simply never arrives, the agent that
  placed it waits, and Bogdan is never told.

**That is worse than the Discord mention it replaced**, because he will have
learned to trust it.

So: placing a call is not evidence that it rang. A transport must produce
positive evidence -- a SIP `180 Ringing`, a push-service accept, an ack from the
app -- and if it does not produce that evidence in time, this converts the
silence into `CallUnreachable`, which `hotline-call` already turns into a loud
fallback to `hotline-page`.

Fails closed on purpose. A transport that never signals `ringing` is treated as
unreachable rather than trusted, because "I could not tell" has to mean no.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from .base import CallError, CallTarget, CallUnreachable, MediaStream

log = logging.getLogger("hotline-ios.ring.watch")

DEFAULT_CONFIRM_WITHIN = 8.0
"""How long a transport gets to prove the device is alerting.

Generous on purpose. The path to his phone is relayed through a DERP server --
measured at 92-623 ms with 172 ms of jitter, and `tailscale ping` reports a
direct connection is never established -- and a tunnel started by an on-demand
rule adds a documented 5-10 s before traffic moves. Eight seconds is slow for a
doorbell and still far quicker than the silence it replaces.
"""


class ConfirmedRing:
    """Wraps any `RingTransport` and requires it to prove the phone rang.

    Wrapper rather than a base class so a transport that cannot confirm needs no
    changes to be handled correctly -- it simply never signals, and is correctly
    reported as unreachable.
    """

    def __init__(
        self,
        inner: object,
        *,
        confirm_within: float = DEFAULT_CONFIRM_WITHIN,
        on_degrade: Callable[[str], None] | None = None,
    ) -> None:
        self.inner = inner
        self.confirm_within = confirm_within
        self.on_degrade = on_degrade
        self.name = f"{getattr(inner, 'name', 'unknown')}+confirmed"
        self.rings_when_closed = bool(getattr(inner, "rings_when_closed", False))

    async def start(self) -> None:
        await self.inner.start()  # type: ignore[attr-defined]

    async def stop(self) -> None:
        await self.inner.stop()  # type: ignore[attr-defined]

    def incoming(self) -> AsyncIterator[tuple[CallTarget, MediaStream]]:
        return self.inner.incoming()  # type: ignore[attr-defined,no-any-return]

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> MediaStream:
        ringing: asyncio.Event | None = getattr(self.inner, "ringing", None)
        if ringing is not None:
            ringing.clear()

        attempt = asyncio.ensure_future(self.inner.ring(target, timeout=timeout))  # type: ignore[attr-defined]

        if ringing is None:
            # The transport has no way to tell us. Do not silently trust it.
            self._degrade(f"{getattr(self.inner, 'name', '?')} cannot confirm a ring")
            attempt.cancel()
            raise CallUnreachable(
                f"{getattr(self.inner, 'name', '?')} gives no ring confirmation; "
                "refusing to report a call as delivered on no evidence"
            )

        confirmed = asyncio.ensure_future(ringing.wait())
        try:
            done, _ = await asyncio.wait(
                {attempt, confirmed},
                timeout=self.confirm_within,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not confirmed.done():
                confirmed.cancel()

        # The call can legitimately finish before confirmation lands -- an
        # instant decline, or a transport that answers in under 8 s. Either way
        # the attempt itself is the answer and there is nothing to second-guess.
        if attempt in done:
            return await attempt

        if not ringing.is_set():
            attempt.cancel()
            with_suppress = getattr(asyncio, "CancelledError")
            try:
                await attempt
            except (with_suppress, CallError, Exception):
                pass
            self._degrade(
                f"no ring confirmation from {getattr(self.inner, 'name', '?')} "
                f"within {self.confirm_within:.0f}s"
            )
            raise CallUnreachable(
                f"the phone never confirmed it was ringing within "
                f"{self.confirm_within:.0f}s -- treating as undeliverable"
            )

        return await attempt

    def _degrade(self, why: str) -> None:
        # Loudly, always. A degradation nobody hears about is the failure mode
        # this whole module exists to prevent.
        log.warning("ring degraded: %s", why)
        if self.on_degrade is not None:
            try:
                self.on_degrade(why)
            except Exception:
                log.exception("degrade notifier failed")
