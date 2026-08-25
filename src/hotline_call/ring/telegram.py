"""Telegram as the doorbell.

Bogdan's design, 2026-08-25: **Telegram rings, his own app is the interface.**
Decoupling those two things is what made everything else simple, and it is worth
saying why this is a good doorbell rather than a compromise.

Telegram is already on his phone with notifications he already trusts, it rings
with a real full-screen call UI that Apple grants Telegram and not us, it needs
no entitlement of ours, no sideloading, no certificate that expires in seven
days, and no keepalive that dies when someone phones him. We inherit all of
that for free, and we inherit none of Telegram's interface, because he hangs up
on the ring and opens our app.

**What this deliberately does not do: carry audio.** A Telegram call is
end-to-end encrypted with a Diffie-Hellman exchange this never completes. It
rings, and then it discards the call. He answers by typing in the app. That is
the whole design, and trying to carry voice here would be rebuilding the thing
he called a gimmick.

## Preconditions, all his

- `api_id` and `api_hash` from <https://my.telegram.org>.
- **A second Telegram account with its own phone number**, to ring *from*. A
  bot token cannot place a call -- MTProto's `phone.requestCall` is a user-only
  method -- and an account cannot call itself.
- A `.session` file created once by signing that account in interactively.

## What is verified and what is not

Verified on this box: `telethon` 1.44.0 is installed and exposes
`RequestCallRequest(user_id, g_a_hash, protocol, video, random_id)`,
`AcceptCallRequest` and `DiscardCallRequest`, matching MTProto's documented
surface.

**Not verified: that this makes his phone ring.** That needs the credentials
above and his handset, and it has not been run. Nothing here should be read as
saying it works yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets

from .base import CallDeclined, CallTarget, CallUnanswered, CallUnreachable

log = logging.getLogger("hotline-ios.ring.telegram")

MIN_LAYER = 65
MAX_LAYER = 92
"""The protocol layer range Telegram clients advertise for calls. We never
negotiate past the ring, but the field is required and a nonsense value gets the
request rejected outright."""


class TelegramTransport:
    """Ring him by placing a Telegram call, then immediately discarding it."""

    name = "telegram"
    rings_when_closed = True
    """True, and this is the reason the whole design works: Telegram's own push
    infrastructure wakes its own app. Nothing of ours has to stay alive."""

    def __init__(
        self,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        session: str | None = None,
        peer: str | None = None,
        client: object | None = None,
    ) -> None:
        self.api_id = api_id or int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
        self.api_hash = api_hash or os.environ.get("TELEGRAM_API_HASH", "")
        self.session = session or os.environ.get(
            "TELEGRAM_SESSION", os.path.expanduser("~/.config/hotline-ios/telegram")
        )
        # Who to ring: a username, a phone number, or a numeric user id.
        self.peer = peer or os.environ.get("TELEGRAM_PEER", "")
        self.ringing = asyncio.Event()
        self._client = client
        self._owns_client = client is None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._client is not None:
            return
        if not (self.api_id and self.api_hash and self.peer):
            # Fail here rather than at ring time. A doorbell that only reveals
            # it was never configured at the moment someone needs it is worse
            # than one that says so at startup.
            raise CallUnreachable(
                "telegram is not configured: needs TELEGRAM_API_ID, "
                "TELEGRAM_API_HASH and TELEGRAM_PEER"
            )
        from telethon import TelegramClient

        client = TelegramClient(self.session, self.api_id, self.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise CallUnreachable(
                f"the telegram session at {self.session} is not signed in -- "
                "sign the calling account in once, interactively"
            )
        self._client = client

    async def stop(self) -> None:
        if self._client is not None and self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()  # type: ignore[attr-defined]
        self._client = None

    # ---- ringing ---------------------------------------------------------

    async def ring(self, target: CallTarget, *, timeout: float = 45.0) -> None:
        self.ringing.clear()
        if self._client is None:
            await self.start()
        assert self._client is not None

        from telethon.tl.functions.phone import DiscardCallRequest, RequestCallRequest
        from telethon.tl.types import PhoneCallProtocol

        try:
            peer = await self._client.get_input_entity(self.peer)  # type: ignore[attr-defined]
        except Exception as exc:
            raise CallUnreachable(f"telegram does not know {self.peer!r}: {exc}") from exc

        protocol = PhoneCallProtocol(
            min_layer=MIN_LAYER, max_layer=MAX_LAYER,
            udp_p2p=True, udp_reflector=True, library_versions=["4.0.0"],
        )
        # The real g_a_hash is SHA256 of our half of a Diffie-Hellman exchange.
        # We never complete that exchange, because we are not carrying audio --
        # so this is 32 random bytes of the right shape. It is enough to make
        # the phone ring, which is the entire job.
        request = RequestCallRequest(
            user_id=peer,
            g_a_hash=secrets.token_bytes(32),
            protocol=protocol,
            video=False,
            random_id=secrets.randbelow(2**31),
        )

        try:
            result = await self._client(request)  # type: ignore[operator]
        except Exception as exc:
            text = str(exc)
            if "PARTICIPANT_VERSION_OUTDATED" in text or "USER_PRIVACY" in text:
                raise CallUnreachable(f"telegram refused the call: {text}") from exc
            raise CallUnreachable(f"telegram call request failed: {text}") from exc

        # The request being accepted IS the evidence the phone is alerting --
        # Telegram has taken responsibility for delivering it. That is what
        # ConfirmedRing waits on.
        self.ringing.set()
        call = getattr(result, "phone_call", None)
        log.info("telegram is ringing %s (call %s)", self.peer, getattr(call, "id", "?"))

        try:
            answered = await self._wait_for_answer(call, timeout)
        finally:
            # Always discard. Leaving a Telegram call ringing after we have got
            # what we needed would keep buzzing his phone while he is already
            # reading the question in the app.
            if call is not None:
                with contextlib.suppress(Exception):
                    from telethon.tl.types import InputPhoneCall

                    await self._client(  # type: ignore[operator]
                        DiscardCallRequest(
                            peer=InputPhoneCall(id=call.id, access_hash=call.access_hash),
                            duration=0,
                            reason=_discard_reason(),
                            connection_id=0,
                        )
                    )

        if answered is False:
            raise CallDeclined("he declined the telegram call")
        if answered is None:
            raise CallUnanswered(f"telegram rang for {timeout:.0f}s with no answer")

    async def _wait_for_answer(self, call: object, timeout: float) -> bool | None:
        """True answered, False declined, None rang out.

        Telegram reports the outcome as an update on the call object. We do not
        need the answer to deliver the message -- the question is already in the
        app before the phone rings -- but distinguishing declined from ignored
        is what stops the chain nagging him.
        """
        from telethon import events
        from telethon.tl.types import PhoneCallAccepted, PhoneCallDiscarded

        outcome: asyncio.Future[bool | None] = asyncio.get_event_loop().create_future()
        call_id = getattr(call, "id", None)

        async def on_update(update: object) -> None:
            phone_call = getattr(update, "phone_call", None)
            if phone_call is None or getattr(phone_call, "id", None) != call_id:
                return
            if outcome.done():
                return
            if isinstance(phone_call, PhoneCallAccepted):
                outcome.set_result(True)
            elif isinstance(phone_call, PhoneCallDiscarded):
                reason = type(getattr(phone_call, "reason", None)).__name__
                # Busy or a deliberate hangup is him saying no. A missed call is
                # him not being there, and they deserve different responses.
                outcome.set_result("Busy" in reason or "Hangup" in reason and False)

        handler = self._client.add_event_handler(  # type: ignore[attr-defined]
            on_update, events.Raw()
        )
        try:
            return await asyncio.wait_for(outcome, timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        finally:
            with contextlib.suppress(Exception):
                self._client.remove_event_handler(on_update)  # type: ignore[attr-defined]


def _discard_reason() -> object:
    from telethon.tl.types import PhoneCallDiscardReasonHangup

    return PhoneCallDiscardReasonHangup()
