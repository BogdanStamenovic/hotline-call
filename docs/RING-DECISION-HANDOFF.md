# Ring transport decision — handoff from `data-89`

Written 2026-08-25. Owner of the **brief**, not the build. `hotline-ios` owns
the build; `hotline-80`/`hotline-2c` owns the server and coordination.

**Deliverable:** https://claude.ai/code/artifact/c35a5d55-ef31-453a-a0d5-16827b4101de
(same URL across all revisions — republish that path to update, do not create a
second artifact). Also posted in full to Bogdan's Discord channel so it is
actionable without opening the link.

## State

- **Money question: closed.** No $99 Apple Developer. His words, verified
  (msg `1541843383616806982`): *"B was the olan either way. Just do whatever is
  free. But bread me on both free ways"*.
- **B-vs-C: he has NOT yet responded to the brief.** He is out on bad cellular
  and will read it at home. **Do not run `hotline --done` on `data-89` until he
  has actually engaged** — closing it deletes the channel he would ask questions
  in.
- **Recommendation delivered: build C now, keep B as the upgrade, let runtime
  detection arbitrate.** Socket alive → ring his own app over pure tailnet;
  socket gone → Linphone; → Discord mention; → siren.

## The two things a successor is most likely to get wrong

**1. `SPEC.md` §2's conclusion is wrong and the file may still say so.**
"No `aps-environment` → no PushKit → B cannot ring when closed" is *fact, fact,
ASSUMPTION*. A push wakes a **dead** process; a live one needs no waking.
- `UIBackgroundModes` is an **Info.plist property, not an entitlement** — Apple
  DTS (Quinn), Forums 791736. Free provisioning never gated it.
- CallKit needs no entitlement. The iOS 13 kill is thrown from
  `[PKPushRegistry _terminateAppIfThereAreUnhandledVoIPPushes]` — a method on an
  object an app that never registers for VoIP pushes never instantiates.

**2. The transport is NOT the weak link, despite a day of us believing it was.**
Measured on his phone: answers while locked, **warm and cold**, ~87 ms
inbound-initiated. The feared 5-10 s on-demand wake penalty **does not exist**.
And those numbers were taken on bad cellular — i.e. close to a **worst case**;
at home it should go direct at single-digit ms.
**Every remaining way B fails is at the app layer**, not the network.

## Verified (primary source)

- Linphone emits RFC 8599 push params for **third-party** SIP domains.
  `Account::guessContactForRegister()` gates only on global +
  per-account flags; grepping `account.cpp`, `account-params.cpp`,
  `push-notification-config.cpp` for `linphone.org` → zero hits outside
  copyright headers. The app's own third-party warning lists what you lose
  (group messaging, video conferencing) — **push is not on it**. Token is
  **per-install**, not per-account.
- The FAQ line "third-party SIP accounts do not receive push notifications" is
  **service policy, not app incapability** — its next sentence is a SaaS upsell.
- Belledonne's `push_notification` endpoint is not admin-gated; no ownership
  check on `pn_prid`/`pn_param`. Verified in source by two sessions.
- Self-hosted flexisip pushing the **stock** app is **dead** — needs an APNs
  cert for `org.linphone.phone`, issued only to Belledonne.
- Linphone is **structurally the only** viable client: its push model is a
  stateless relay. Every competitor proxies *into* your PBX and needs public
  inbound reachability (Acrobits: *"The PBX must be reachable from the public
  internet"*). A tailnet-only backend fails that.
- Only **one** `NEPacketTunnelProvider` slot on iOS → SideStore's local VPN and
  Tailscale collide.
- Toolchain builds on Arch: Swift 6.3.3 + xtool 1.17.0, clean to the SDK wall.
- `gh` now has the **`workflow`** scope; throwaway **public** repo authorised
  (msg `1541848751113773157`). SDK build is one `gh repo create` away.

## Unproven — do not state these as fact

- Whether a **local** CallKit ring actually presents on a locked phone. The only
  on-device field report found (voximplant/flutter_callkit#24) says it did
  **not**. Needs hardware.
- Whether `baresip` can register to `sip.linphone.org` (one attempt settles it).
- Whether a third party can push the **stock** app via Belledonne's endpoint.
  The controller reading is unrefuted but not re-hit. **The SIP probe's captured
  token is the input this needs** — one API call after the probe fires.
- Battery cost of a silent audio session (~2-5%/hr is an estimate, not a figure).
- A phone left untouched overnight, as opposed to merely quiet.

## Retracted — do not resurrect

- **"Phone advertises zero endpoints."** `Endpoints`/`Addrs` are `None` for
  **every** peer including directly-connected ones. Null field, not a signal.
- **tailscale#11328 as a maintainer statement.** It is a reporter paraphrasing
  an unlinked internal doc. Use `nickoneill` on **#17575** instead.
- **Apple Forums 756941 ("100%, no" on tunnels suspended at lock).**
  **Unfetchable** by two sessions — serves a JS shell containing none of the
  quoted terms — **and contradicted by his own device**. Do not cite it.

## Do not re-run

The "already-installed app" sweep. All dead for free Linux-triggered ringing:
Signal, Messenger, Viber, Discord, Zoom, Google Meet, FaceTime, Skype (retired
May 2025). WhatsApp Business Calling API is real but needs Meta verification,
a WABA number, callee opt-in, and is geo-blocked in several countries.
**Unresolved, resume here if C ever dies:** Telegram (TDLib/pytgcalls — is 1:1
outgoing calling released or an unmerged branch?), iOS web push to a home-screen
PWA, Home Assistant critical alerts.

## Standing principle for the degradation ladder

Every rung below the ring is an **alert, not a call**. He called the Discord
mention a fake call and was right; a critical alert is a louder fake call. When
the system degrades it must say **which rung it landed on** rather than quietly
substituting a notification and letting it read as success. Fail closed:
"I could not tell" means no.

## Waiting on him — one trip, both asks

Both need home wifi, so they are one sitting:
1. Install Linphone, point it at the SIP probe (already live, tailnet-only).
   Yields the push-token answer **and** the last unverified piece of C.
2. Leave Tailscale up unlocked ~30 s → direct-path / audio-quality answer.

## Method note

Five confident readings were wrong across three sessions today; every one was
caught by someone other than its author, twice by the person who supplied the
datum. All five were the same error: **a status field read as a signal without
testing the thing the field supposedly indicates.** The fix each time was a
control — probe it directly, or compare against a row whose answer you know.
