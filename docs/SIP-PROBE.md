# The five-minute experiment that decides outcome C

**Status: the listener is running and waiting.** Nothing else is needed from
this machine.

## What this settles, and why it is worth your five minutes

Outcome C rings your phone for real, for free, with no Apple developer account,
no sideloading, and no weekly re-signing — by using the stock Linphone app from
the App Store as the thing that rings, pointed at a SIP server on archserver.

It hangs on one fact nobody can settle by reading, because the sources
contradict each other:

- **Linphone's FAQ says** "Push notifications are enabled only for Linphone
  accounts. Third-party SIP accounts do not receive push notifications."
- **Linphone's source code says otherwise.** `Account::guessContactForRegister()`
  gates the push parameters on two booleans and there is no domain check
  anywhere in the path. The FAQ line is followed immediately by "We offer this
  feature as a SaaS solution!", which makes it read as pricing, not capability.

The disagreement is resolvable by looking. When a SIP client registers, it puts
its push token in the `Contact` header of the REGISTER — that is what RFC 8599
is *for*, so a SIP server can ask the push service to ring its own client. So:
point Linphone at a server we control, and read what it actually sends.

If the token is there, C works and it is almost certainly the option to build.
If it is not, C dies and you should be told before you choose it, not after.

## What you do

**Five minutes, entirely reversible, and you keep nothing running afterwards.**
You will need to do this part anyway if you pick C, so it is not a cost the
experiment adds.

1. Install **Linphone** from the App Store (free).
2. Open it and choose **"Use a third-party SIP account"** — *not* "Create an
   account" and *not* the sign-in screen. It may be behind a "Third party SIP
   account" link at the bottom.
3. Enter:

   | field | value |
   |---|---|
   | Username | `bogdan` |
   | Password | anything — `hotline` will do; the probe does not check it |
   | Domain | `100.72.2.62` |
   | Transport | **UDP** |

4. Save. It will try to register. **It does not matter whether it shows as
   connected** — the REGISTER packet is the whole experiment, and we have it
   either way.
5. Then, and this part matters: **lock the phone, wait about a minute, and
   unlock it.** Linphone re-registers on wake, and the interesting case is what
   it sends when it is not in the foreground.
6. Tell whoever is asking that you have done it. Then delete the account in
   Linphone, or delete the app. Nothing persists.

Your phone must be on the tailnet, which it already is. Wifi or cellular both
work.

## What comes back

Everything is written **verbatim** to `sip-capture.txt` before anything parses
it — deliberately, because a parser that finds nothing and a phone that sent
nothing look identical in a summary and completely different in a capture.

Live view:

```bash
journalctl --user -u hotline-sipprobe -f
tail -f /home/bodas/data/hotline-ios/sip-capture.txt
```

The answer is one line in the log, either:

```
>>> THIS IS THE ANSWER: the app DOES emit a push token for a third-party
    domain. Outcome C-TAILNET is viable.
```

or:

```
>>> NO PUSH PARAMS IN THIS REGISTER. ... C-TAILNET is dead.
```

## What is running, and how to stop it

A single UDP socket on **`100.72.2.62:5060`, the Tailscale address only** — it
is deliberately not listening on your LAN, which was checked rather than
assumed. It accepts a REGISTER, writes it to disk, and replies `200 OK`. It
refuses INVITEs with `405` rather than half-accepting a call it cannot carry.
It does not authenticate, because an instrument that runs once on a private
tailnet should not have a second thing in it that can fail.

```bash
systemctl --user stop hotline-sipprobe      # stop it
systemctl --user disable hotline-sipprobe   # and don't start at boot
```

## What it does not settle

One thing is still open even if the token appears: whether Belledonne's push
relay will accept a request from us for the stock app. Their server code shows
the endpoint takes ordinary account auth and does no ownership check, but no
third party has been observed doing it in the wild. **The token this experiment
captures is exactly what is needed to test that**, so the two unknowns fall to
one experiment and one follow-up API call.
