"""One-time interactive sign-in for the Telegram ring account.

`ring/telegram.py` needs a `.session` file for a real user account and refuses
to run without one. This creates it. It runs once, by hand, and then never
again unless the session is revoked.

**Why this is three non-interactive commands instead of one interactive one.**
Telegram does not send the login code by SMS when the account is already active
on a device -- it delivers it *inside Telegram on that phone*. So the code has
to be read off a handset by Bogdan and relayed over Discord, and it expires in
roughly two minutes. Nobody is sitting at a TTY that the code can be typed
into: the agent driving this is on one side of a chat relay and the handset is
on the other. An `input()` prompt would deadlock that loop.

So the login is split, with the resumable half of Telegram's state persisted
between processes:

    tg-login send --phone +3816...     # sends the code, stores phone_code_hash
    tg-login code 12345                # signs in, writes the .session
    tg-login pass 'hunter2'            # only if the account has 2FA

`phone_code_hash` is the piece that makes this possible: `sign_in` will accept
it from a different process, so step two does not need the client object that
requested the code.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

DEFAULT_SESSION = os.path.expanduser("~/.config/hotline-ios/telegram")
STATE_SUFFIX = ".login.json"


def _paths(session: str) -> tuple[str, Path]:
    return session, Path(session + STATE_SUFFIX)


def _credentials() -> tuple[int, str]:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        sys.exit(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set.\n"
            "Both come from https://my.telegram.org -> API development tools."
        )
    try:
        return int(api_id), api_hash
    except ValueError:
        sys.exit(f"TELEGRAM_API_ID must be a number, got {api_id!r}")


def _client(session: str):
    try:
        from telethon import TelegramClient
    except ModuleNotFoundError:
        sys.exit(
            "telethon is not installed in this interpreter.\n"
            "  pip install 'hotline-ios[telegram]'   (or: pip install telethon)"
        )
    api_id, api_hash = _credentials()
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(session, api_id, api_hash)


async def _send(session: str, phone: str) -> int:
    _, state_path = _paths(session)
    client = _client(session)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"already signed in as {me.first_name} (@{me.username}) -- nothing to do")
            return 0
        sent = await client.send_code_request(phone)
        # Opened 0600 rather than written-then-chmod'd: the latter leaves
        # phone_code_hash at the default umask for however long the write takes.
        blob = json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash})
        fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(blob)
        print(f"code sent to {phone} -- it arrives INSIDE Telegram on that phone, not by SMS")
        print("then run:  tg-login code <the 5 digits>")
        return 0
    finally:
        await client.disconnect()


async def _code(session: str, code: str) -> int:
    from telethon.errors import (
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )

    _, state_path = _paths(session)
    if not state_path.exists():
        sys.exit(f"no pending login at {state_path} -- run 'tg-login send --phone ...' first")
    state = json.loads(state_path.read_text())

    client = _client(session)
    await client.connect()
    try:
        await client.sign_in(
            phone=state["phone"],
            code=code,
            phone_code_hash=state["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        # The session keeps the half-finished login, so `pass` resumes from here.
        print("this account has 2FA -- run:  tg-login pass '<your telegram password>'")
        return 2
    except PhoneCodeInvalidError:
        sys.exit("that code was rejected -- check the digits and try again")
    except PhoneCodeExpiredError:
        state_path.unlink(missing_ok=True)
        sys.exit("that code expired -- run 'tg-login send' again and relay the new one faster")
    finally:
        await client.disconnect()

    state_path.unlink(missing_ok=True)
    return await _whoami(session)


async def _password(session: str, password: str) -> int:
    from telethon.errors import PasswordHashInvalidError

    _, state_path = _paths(session)
    client = _client(session)
    await client.connect()
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        sys.exit("that 2FA password was rejected")
    finally:
        await client.disconnect()
    state_path.unlink(missing_ok=True)
    return await _whoami(session)


async def _whoami(session: str) -> int:
    client = _client(session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("not signed in")
            return 1
        me = await client.get_me()
        # A bot here would be a silent dead end: phone.requestCall is user-only,
        # so a bot session signs in fine and then can never ring anybody.
        if getattr(me, "bot", False):
            print(f"SIGNED IN AS A BOT (@{me.username}) -- this CANNOT place calls.")
            print("Telegram bots have no calling surface. Use a real user account.")
            return 1
        print(f"signed in as {me.first_name} (@{me.username}), id={me.id}")
        print(f"session: {session}")
        print("this account is the one that will RING him -- it must not be his own account")
        return 0
    finally:
        await client.disconnect()


async def _ringtest(session: str, peer: str, seconds: float) -> int:
    """Actually ring him once, through the real transport, and hang up.

    This is the only preflight that proves anything. `whoami` proves we are
    signed in; nothing short of a ring proves his phone alerts. It drives
    `TelegramTransport` rather than reimplementing the call, so a pass here is
    evidence about the code that will run in production and not about a
    lookalike written for the test.

    The failure it exists to catch: with Settings -> Privacy -> Calls set to
    "Nobody" (or "My Contacts" between two accounts that have not saved each
    other), Telegram rejects the request. `UserPrivacyRestrictedError` is
    telethon's mapping for that refusal, and catching it by name turns a
    baffling silent non-ring into one sentence naming the setting to change.
    """
    from .ring.base import CallTarget, CallUnanswered, CallUnreachable
    from .ring.telegram import TelegramTransport

    api_id, api_hash = _credentials()
    transport = TelegramTransport(
        api_id=api_id, api_hash=api_hash, peer=peer, session=session
    )
    # Blind catches here are the point, not an oversight: this runs while he is
    # sitting there waiting, and a traceback is the one output that helps nobody.
    # Every failure gets turned into a sentence and a non-zero exit.
    try:
        await transport.start()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"transport refused to start: {exc}")

    print(f"ringing {peer} -- his phone should light up within a second or two")
    try:
        await transport.ring(CallTarget(device="preflight", reason="ring test"), timeout=seconds)
    except CallUnanswered:
        # Nobody was meant to answer. Reaching the timeout means it rang.
        print(f"RANG (unanswered after {seconds:g}s, which is the expected result)")
        return 0
    except CallUnreachable as exc:
        print(f"DID NOT RING: {exc}")
        _privacy_hint(exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"DID NOT RING: {type(exc).__name__}: {exc}")
        _privacy_hint(exc)
        return 1
    else:
        print("call was answered or ended early -- it rang")
        return 0
    finally:
        with contextlib.suppress(Exception):
            await transport.stop()


def _privacy_hint(exc: BaseException) -> None:
    if "privacy" in f"{type(exc).__name__}{exc}".lower():
        print()
        print("This is the Calls privacy setting, not a bug in the ring.")
        print("On the phone being called:  Settings -> Privacy and Security -> Calls")
        print("Set it to 'Everybody', or to 'My Contacts' with both accounts saved.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tg-login",
        description="Sign the Telegram ring account in, once, across a chat relay.",
    )
    p.add_argument("--session", default=DEFAULT_SESSION, help=f"session path (default {DEFAULT_SESSION})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="request a login code")
    s.add_argument("--phone", required=True, help="the ringing account's number, e.g. +381...")

    c = sub.add_parser("code", help="complete sign-in with the relayed code")
    c.add_argument("code", help="the 5 digits from inside Telegram on that phone")

    w = sub.add_parser("pass", help="supply the 2FA password, if the account has one")
    w.add_argument("password")

    sub.add_parser("whoami", help="report who this session is signed in as")

    r = sub.add_parser("ringtest", help="actually ring him once and hang up -- the only real proof")
    r.add_argument("--peer", required=True, help="who to ring: @username, +phone, or numeric id")
    r.add_argument("--seconds", type=float, default=8.0, help="how long to let it ring (default 8)")

    a = p.parse_args(argv)
    if a.cmd == "send":
        return asyncio.run(_send(a.session, a.phone))
    if a.cmd == "code":
        return asyncio.run(_code(a.session, a.code))
    if a.cmd == "pass":
        return asyncio.run(_password(a.session, a.password))
    if a.cmd == "ringtest":
        return asyncio.run(_ringtest(a.session, a.peer, a.seconds))
    return asyncio.run(_whoami(a.session))


if __name__ == "__main__":
    raise SystemExit(main())
