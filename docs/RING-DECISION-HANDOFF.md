# Ring transport decision — handoff from `data-89`

Written 2026-08-25. Owner of the **brief**, not the build. `hotline-ios` owns
the build; `hotline-80`/`hotline-2c` owns the server and coordination.

**Deliverable:** https://claude.ai/code/artifact/c35a5d55-ef31-453a-a0d5-16827b4101de
(same URL across all revisions — republish that path to update, do not create a
second artifact). Also posted in full to Bogdan's Discord channel so it is
actionable without opening the link.

## DECIDED 2026-08-25 16:59 — msg `1541854587424735242`, verified kind=human

> "make your own app for delegation talking excetera which i will sideload every
> week. Telegram for the ring. And we can fully scrap the talking voice rout.
> Thats bassically a gimic"

**Neither B nor C as briefed. He split the ring from the interface.**
- **Telegram is the doorbell** (`telethon`, `phone.requestCall`).
- **His own app is the interface** — text-first, sideloaded weekly, cost accepted.
- **The voice route is scrapped**, called a gimmick.

**Every option we costed assumed the thing that RINGS is the thing you TALK
THROUGH.** B made one app do both and paid with the keepalive; C took a
stranger's app for the ring and inherited its UI. Splitting them dissolves
nearly this whole document:

- no CallKit in his app -> the unproven local-ring question is MOOT
- no `UIBackgroundModes: audio` -> all four silent-death paths GONE
- **no push entitlement needed at all** -> free provisioning was only ever a
  problem because of push
- reboot gap stops mattering for an app you tap to open
- no SIP / Linphone / Belledonne -> the ungated-endpoint dependency GONE
- no audio transport -> jitter, DERP relay, Opus-vs-G.711 all IRRELEVANT here

Surviving cost: the 7-day re-sign, taken knowingly and unprompted.

**Surviving work — now the critical path:** the toolchain (Swift 6.3.3 + xtool),
the `workflow` scope and the authorised throwaway public repo. One
`gh repo create` from the SDK build. ConfirmedRing survives and simplifies:
positive evidence or report unreachable, applied to `phone.requestCall`.
The transport-agnostic core is vindicated — the ring swapped out with no rewrite.

**CANCELLED asks — do not let him do these:** installing Linphone (answers a
question nobody asks now) and the 30-second WiFi test (was for audio quality,
which no longer exists as a concern).

**OPEN, load-bearing:** (1) Is Telegram actually on his phone? The plan presumes
it and nobody asked until after he decided. (2) Does "scrap the voice route"
mean DELETE hotline's existing Discord voice pipeline (`voice.py`, `audio.py`,
Whisper, Piper, 398 tests), or only stop investing? **Conservative reading
adopted: delete nothing.** Awaiting his word. Do not remove a working tested
subsystem on an inferred reading.

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

## Third-way sweep — COMPLETE (an earlier revision of this file said
## "do not re-run" and listed Telegram as unresolved; that was wrong)

**Telegram 1:1 calling is REAL, free, headless, released code.** VERIFIED:
- `Telegram-iOS` registers `PKPushRegistry` `.voIP` and calls
  `reportNewIncomingCall` in `CallKitIntegration.swift`, for real **1:1** calls.
- `phone.requestCall` in Telethon / Pyrogram / gramjs; `createCall` in TDLib.
  **Released, not an unmerged branch.**
- Telethon maintainer tested live (issue #3981): recipient's device **rang**;
  only audio negotiation failed — irrelevant for a doorbell.
- TDLib maintainer confirms `createCall` rings (issue #2008).
- `bbimer/tg-alarm-sentinel` (pushed 2026-08-08) exists for exactly this use
  case and confirms the ring fires on the bare call request.
- Needs a **real account with a phone number** to call FROM; bot tokens cannot.
  `pip install telethon`, no ffmpeg/C++ if only the ring is wanted.

**Why it matters:** it is an independent doorbell. C's ring depends on
Belledonne relaying pushes through an endpoint we do not control; a Telegram
ring depends on none of that. **Uncorrelated failure modes** — worth wiring in
even though C ships.

**Unproven:** whether Telegram can carry the AUDIO (1:1 media needs a key
exchange the live test never completed). Telegram anti-abuse flood limits on
automated calling. Whether Telegram is even on his phone — nobody has asked.

**Also real, both DOWNGRADES (buzz, not ring):**
- **Home Assistant critical alerts** — VERIFIED bypass DND + silent (HA docs),
  fire from a plain `curl` over Tailscale, free self-hosted. Needs the HA app
  already installed.
- **iOS PWA web push** — VERIFIED needs NO App Store interaction and NO Apple
  Developer account (WebKit announcement, standard VAPID). But behaves as an
  ordinary notification and respects Focus.

**Confirmed dead** for free Linux-triggered ringing: Signal (`signal-cli` is
text-only), Messenger, Viber, Discord (no endpoint rings a DM), Zoom, Google
Meet, FaceTime (no API exists at all), Skype (retired 2025-05-05), Shortcuts
remote triggering (no webhook trigger exists), iMessage-from-Linux (pypush
blocked by Apple), CalDAV alarms, Emergency Bypass, Safari WebRTC in a
background tab, and **sideloading Linphone** (no .ipa published, and re-signing
replaces the Team ID the push is bound to). WhatsApp Business Calling API is
real but needs Meta verification + WABA + callee opt-in, and is geo-blocked.

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

Six confident readings were wrong across three sessions today; nearly all were
caught by someone other than their author. Most shared one pattern: **a status
field read as a signal without testing the thing the field supposedly
indicates.** Empty `Endpoints` column. A peer absent from the map that answers
pings anyway. A capabilities-table cell. The fix each time was a **control** —
probe it directly, or compare against a row whose answer you already know.

**One was a different species and it is the dangerous one.** The "all third
ways are dead" entry was not a misreading — it was a summary of my OWN
incomplete work, published as a result while the parent agent was still
running. **No control row catches that**, because there is nothing to control
against: the claim was about the state of my own knowledge. A careful reviewer
has no way in. Guard: before writing "we checked X and found nothing", confirm
every agent that was checking X has actually reported.

**And the damaging half was the instruction, not the finding.** A wrong entry
is one bad fact someone trips over. **"Do not re-run this research" steers the
next person away from the answer and looks like diligence while doing it.**
Never attach a do-not-revisit instruction to a null result unless you can name
who finished the search and when.

Telegram was independently re-verified twice before this file was corrected —
telethon 1.44.0 installed clean in a throwaway venv, `RequestCallRequest`
present with params `user_id, g_a_hash, protocol, video, random_id`, plus
`AcceptCallRequest`/`DiscardCallRequest`. Installed rather than read, precisely
because a guessed raw-GitHub URL 404'd first and "the file is not where I
guessed" is exactly the null that invites a false finding.

---

## SDK: FOR THE RESPAWNED hotline-ios — READ THIS FIRST (data-89, 2026-08-25 ~19:10)

Recorded while you were killed, from runs I read myself. Three findings, and
the third is a trap that would produce a silently useless artifact.

### 1. The macOS-runner strategy IS valid. `install` was the wrong subcommand.

I told him earlier the runner strategy might rest on a false premise. **I was
wrong, and your attempt-1 instinct was right all along** — it was only masked
by the GUI hang. Run 32887720735, `xtool sdk build --help` on macOS:

    Usage: xtool sdk build <path> <output-dir> [--arch <arch>]
      --arch <arch>   The architecture of the Linux host the SDK is being
                      built for. (default: auto)

`sdk build` exists on macOS and its entire purpose is **building an SDK for a
Linux host**. That is precisely this plan. `sdk install` no-oping on macOS
("the iOS SDK ships with Xcode on macOS") is correct and irrelevant — it is
the local-install command, not the cross-build command.

That run failed **only** on `Error: Missing expected argument '<output-dir>'`.
A missing positional, nothing more.

### 2. THE TRAP: `--arch` auto is WRONG here, silently.

`auto` matches the **current host**. Measured:

    runner image : macos-15-arm64   (arm64)
    archserver   : x86_64           (uname -m, verified)

So `--arch` left at auto on that runner builds an **arm64** SDK that will not
work on archserver. **Pass `--arch x86_64` explicitly.** This would not fail
loudly — it produces a bundle that installs and then does not work, which is
the worst shape available and the same "green but wrong" class as the no-op
install and the 18 vanishing tests.

### 3. Plan B measurements confirmed independently (run 32887587919)

    /Applications/Xcode_26.3.app                       4.3 G
      Platforms/iPhoneOS.platform/Developer/SDKs         56 M
      Platforms/MacOSX.platform/Developer/SDKs          228 M
      Platforms/iPhoneSimulator.platform/Developer/SDKs  66 M
      Toolchains/.../usr/lib/swift                      525 M
      Toolchains/.../usr/lib/clang                       21 M
      Frameworks + PrivateFrameworks (all three)        ~28 M
                                                       -------
      what xtool keeps                                 ~925 M

Every earlier estimate (3-8 GB bundle, 15-80 GB transient) was far too high.
The runner's Xcode is already unpacked, so the extraction spike that was
supposed to make this impossible **does not exist there**. No `.xip` needed,
and **no Apple ID download needed** — which is the decision I had warned him
might come back. It probably does not.

### Next move, in order
1. Re-run `sdk build "$XCODE" <output-dir> --arch x86_64` — one positional and
   one flag away from an artifact.
2. Assert on the **artifact**, not the exit code. Step 5 failing on
   `test -n "$BUNDLE"` is the only reason the no-op was ever caught; keep that
   deliberately rather than by accident.
3. Plan B (tar the ~925 MB subtree) stays as the fallback and is cheap.

I have queued nothing and touched nothing in your repo but this file.
