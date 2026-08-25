"""Try each way of ringing him in turn, and say which one worked.

Both of the free outcomes survive, and neither dominates:

  * **B** -- his own app, woken over a live Tailscale socket, `reportNewIncoming
    Call` locally. The only shape where the doorbell never leaves the tailnet.
    Fails silently on a reboot, a force-quit, a certificate expiry, or an audio
    session interrupted by an ordinary phone call.
  * **C** -- the stock Linphone app, rung through Belledonne's push relay. Works
    when the app is dead and the phone is locked. Fails if Belledonne tightens
    the check, and it puts a third party in the doorbell.

The composition both reviewers arrived at independently is not to choose: run
them in order and fall through. B when it is there, C when it is not, the
existing Discord mention when neither is, and the physical siren last.

**This is only correct because of `ConfirmedRing`.** A fall-through chain over
transports that cannot tell you whether they worked does not degrade -- it just
stops at the first one that fails to raise, and the call vanishes. So every link
here is expected to be confirmation-wrapped, and the chain refuses to treat a
silent success as a success.

The chain deliberately does **not** fall through on `CallDeclined`. He saw it
and said not now; ringing him again by another route one second later is exactly
what he was declining.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

from .base import (
    CallDeclined,
    CallError,
    CallTarget,
    CallUnanswered,
    CallUnreachable,
    MediaStream,
)

log = logging.getLogger("hotline-ios.ring.chain")


class RingChain:
    """Ring by the first transport that can prove it worked."""

    def __init__(
        self,
        links: list[object],
        *,
        on_fallthrough: Callable[[str, str], None] | None = None,
        fall_through_on_unanswered: bool = False,
    ) -> None:
        if not links:
            raise ValueError("a ring chain with no transports can never ring")
        self.links = links
        self.on_fallthrough = on_fallthrough
        # Off by default. A phone that rang for 45 s and was ignored has
        # delivered the message; ringing a second device immediately afterwards
        # is nagging, not redundancy. Discord's own pager already escalates over
        # minutes, which is the right timescale for "he did not pick up".
        self.fall_through_on_unanswered = fall_through_on_unanswered
        self.name = "+".join(str(getattr(link, "name", "?")) for link in links)
        self.used: str | None = None

    @property
    def rings_when_closed(self) -> bool:
        """True if ANY link can wake a closed app.

        Any rather than all: the chain is as strong as its best working link,
        and reporting False because the last-resort mention cannot ring would
        misdescribe what he actually gets.
        """
        return any(bool(getattr(link, "rings_when_closed", False)) for link in self.links)

    async def start(self) -> None:
        for link in self.links:
            try:
                await link.start()  # type: ignore[attr-defined]
            except Exception:
                # One transport failing to start must not take the chain down;
                # that is the entire reason there is a chain.
                log.exception("transport %s failed to start", getattr(link, "name", "?"))

    async def stop(self) -> None:
        for link in self.links:
            try:
                await link.stop()  # type: ignore[attr-defined]
            except Exception:
                log.exception("transport %s failed to stop", getattr(link, "name", "?"))

    def incoming(self) -> AsyncIterator[tuple[CallTarget, MediaStream]]:
        # Inbound belongs to the first transport that offers it; merging several
        # would need a policy for which one owns a call he places, and there is
        # no evidence yet about what that policy should be.
        for link in self.links:
            incoming = getattr(link, "incoming", None)
            if incoming is not None:
                return incoming()  # type: ignore[no-any-return]
        raise CallUnreachable("no transport in the chain accepts incoming calls")

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> MediaStream:
        self.used = None
        failures: list[str] = []

        for index, link in enumerate(self.links):
            name = str(getattr(link, "name", f"link{index}"))
            try:
                stream = await link.ring(target, timeout=timeout)  # type: ignore[attr-defined]
            except CallDeclined:
                # An answer, not a failure. Stop here.
                self.used = name
                raise
            except CallUnanswered as exc:
                failures.append(f"{name}: rang out")
                if not self.fall_through_on_unanswered:
                    self.used = name
                    raise
                self._note(name, f"rang out: {exc}")
                continue
            except (CallUnreachable, CallError) as exc:
                failures.append(f"{name}: {exc}")
                self._note(name, str(exc))
                continue
            except Exception as exc:
                # A transport raising something unexpected is a bug in it, and
                # the chain existing to survive exactly that is the point.
                log.exception("transport %s raised", name)
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                self._note(name, f"{type(exc).__name__}: {exc}")
                continue
            else:
                self.used = name
                if index:
                    log.warning("rang via %s after %d failed: %s", name, index, "; ".join(failures))
                return stream

        raise CallUnreachable(
            "every way of reaching him failed -- " + "; ".join(failures)
        )

    def _note(self, name: str, why: str) -> None:
        log.warning("ring transport %s did not work: %s", name, why)
        if self.on_fallthrough is not None:
            try:
                self.on_fallthrough(name, why)
            except Exception:
                log.exception("fallthrough notifier failed")
