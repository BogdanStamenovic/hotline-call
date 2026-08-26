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

### STATUS AS OF 19:12 — THE SDK IS BUILT. DO NOT REBUILD IT.

Run **32887877859**, success. Verified by data-89 from the run log, not relayed:

    4. Build the Darwin SDK for Linux    success
    5. Package the SDK                   success
    6. upload-artifact                   success

    command:  xtool sdk build "$XCODE" out --arch x86_64     <- arch CORRECT
    output :  out/darwin.artifactbundle/
                swift-sdk.json  toolset.json  info.json
                Developer/Platforms  Developer/Toolchains
    artifact: name "darwin-sdk"  805,160,094 bytes  (771M tar.gz)

Both hard-won lessons are already IN the workflow — you did not lose them:
`--arch x86_64` explicit (auto would have silently built arm64 for an x86_64
box), and the step asserts on the **artifact** rather than the exit code, with
the reason written beside it.

### Next move, in order — everything above this line is DONE

1. **Download the `darwin-sdk` artifact to archserver** and install it into the
   *Linux* xtool. `gh run download 32887877859 -R BogdanStamenovic/darwin-sdk-build`.
2. **Compile something.** Until a binary comes out the far end, "the toolchain
   works" is a claim, not a fact. The SDK existing is necessary, not sufficient.
3. Only then is the throwaway repo safe to delete — the artifact lives under it.
4. Plan B (tar the ~925 MB subtree) is now unnecessary. Do not spend time on it.

### Also still true, post-compaction

- **His phone RINGS.** SIP/Linphone doorbell confirmed twice on the handset,
  both directions (`180`->`200` answered, `486` declined). Done. Do not redo.
- **Telegram doorbell is built and unstarted** — still needs `api_id`,
  `api_hash` and the second account's number from him. `tg-login` is written
  and tested (`send`/`code`/`pass`/`whoami`/`ringtest`).
- **Voice is scrapped but DELETE NOTHING** — his word was "stop investing in
  it". The Siri Shortcut path is explicitly KEPT and is a different thing from
  the Discord/Whisper/Piper pipeline.
- **`baresip` is installed** on the box, approved by him, kept only as a
  known-good SIP client to diff against while the UDP retransmit defect
  (missing RFC 3261 timer A) is open. It should be removed once nobody needs
  it; that is data-89's to clean up.

---

## SIDELOADING: WHY IT FAILS ON archserver, AND WHERE IT WORKS
## (data-89, 2026-08-26 ~04:00 — read before retrying an install here)

### The install has NEVER succeeded on archserver, over any transport

Run with his phone physically connected:

    [Unpacking app]     100%
    [Preparing device]  100%
    [Provisioning]       33%   <- dies here, every time
    NIOPosix/BaseSocketChannel.swift:1018: Fatal error:
      epoll_ctl(epfd:op:fd:event:): Operation not permitted (errno: 1)

**Provisioning is the Apple Developer Services leg — network to APPLE, not to
the phone.** So this is not a cable problem and `xtool install --network`
cannot route around it: `--network` changes the phone leg only. The `.ipa` is
also **unsigned** (no CodeDirectory in the Mach-O — checked), and signing
happens *during* that provisioning step. **You cannot install your way past an
unsigned binary.** Do not spend a night on transports.

### The `epoll_ctl` EPERM is probably NOT confinement

`epoll_ctl` returns `EPERM` specifically when the **target fd does not support
polling** — a regular file, for instance. That is a documented condition, not a
permissions verdict. So this most likely indicates a **SwiftNIO-on-Linux or
xtool bug**, not a sandbox. That is a real diagnosis to chase (which fd is being
registered, and why it is not pollable), rather than a wall to force.

**DO NOT re-run this with the sandbox disabled.** Bogdan was offered exactly
that and **refused the tool call and said stop**. That is a decision about the
action, not about who is asking — it binds any agent here, and asking a peer to
do it instead is laundering.

### Where signing DOES work: his laptop

    laptop `arch`  100.79.194.90   reachable over the tailnet, key-based SSH
    xtool ds teams list -> Bogdan Stamenović [active]: 3GAQP72Y5Z
                           Xcode Free Provisioning Program (iOS)
    ~/hotline/ has sideload.sh, xtool.AppImage, and a prior ipa

**The working route: build on archserver -> copy to the laptop -> run its
`sideload.sh` over SSH.** It signs where signing demonstrably works and installs
over whatever connection the laptop has to the phone. No `--network`, no
sandbox argument, nothing unsigned.

The profile expiring **1 Sept 22:53** is itself evidence: a signing succeeded
somewhere, and it was not here.

### Device facts, verified on archserver (several earlier assumptions were wrong)

    iPhone16,1          "Bogdan"
    iOS 18.7.8          <- NOT 26. Reasoning that assumed 26 should be rechecked.
    Developer Mode      true, already on — no reboot needed
    pairing record      /var/lib/lockdown/00008130-001669590ABA001C.plist
    idevicepair validate -> SUCCESS
    xtool devices        -> Bogdan [usb]: 00008130-001669590ABA001C

The archserver pairing persists and is real. It is simply not sufficient,
because pairing is not the thing that was broken.

### CORRECTION: `xtool install --network` DOES NOT WORK HERE (proven 2026-08-26 ~04:16)

data-89 told Bogdan the `--network` flag meant wireless re-signing was probably
one command away. **That was wrong** and hotline-ios proved it on the laptop:

- `xtool install --network` **hangs with no output** and times out. **The phone
  does not advertise itself over the network at all**, so `--network` has
  nothing to target. `xtool devices` only ever reports `Bogdan [usb]`.
- **Tailscale does not bridge this.** It carries IP; the install path needs
  usbmux-level device discovery, which is a different layer. Being on the same
  tailnet is irrelevant to it.
- **`idevice_id` and `xtool` do not share discovery.** `idevice_id -l` returns
  nothing and `idevicepair validate` says "No device found" *while xtool sees
  the phone fine*. So `idevice_id` will tell you there is no phone when there
  is one — do not use it to decide whether a device is present.
- **usbmuxd goes inactive on its own.** It must be started immediately before
  an install or the install hangs silently.

So the weekly re-sign is **cable-bound today**. SideStore's on-device refresh
remains the only known no-cable route, and it costs the single iOS VPN slot
that Tailscale occupies.

### The install that DID work

Laptop, 04:16: signed and installed, device reported Verifying 100% /
Successfully installed.

**Trap that nearly shipped the wrong build:** `~/hotline/sideload.sh` installs
`$HERE/HotlineCall.ipa` **unconditionally**. Without overwriting that exact
path it re-installs the previous build and reports success. Verify the ipa
actually changed before running it — MD5 went `f3da1bfa` -> `41d2bc5a` on the
run that worked. A stale install is indistinguishable from a fresh one in its
output.

---

## data-89's RETRACTION LEDGER — the corrections, and why they are here

Seven confident readings were wrong across three sessions on 2026-08-25. Every
one was caught by someone other than its author; twice by the person who had
supplied the datum. **A successor who does not read this will re-derive at
least three of them.**

1. **"Phone advertises zero endpoints."** `Endpoints`/`Addrs` are `None` for
   EVERY peer in `tailscale status --json`, including ones with a live direct
   connection. Null field, not a signal.
2. **tailscale#11328 cited as a maintainer statement.** It is a reporter
   paraphrasing an unlinked internal doc. Use `nickoneill` on **#17575**.
3. **"iOS suspends VPN tunnels on lock, so B is impossible."** Source
   (Apple Forums 756941) is **unfetchable by two independent attempts** and
   **contradicted by his device**: 20/20 probes answered while locked, and a
   cold inbound packet returned in **87 ms**. Do not cite it.
4. **"All third-way alternatives are dead; C by elimination."** Published while
   the research that refuted it was still running. **Telegram 1:1 calling is
   real, free and released** (`RequestCallRequest` verified present in telethon
   1.44.0). This one reversed a *"do not re-run this research"* instruction
   that had already been filed — the worst kind of error, because an
   instruction not to look again steers the next person away from the answer.
5. **"The macOS-runner strategy rests on a false premise."** Wrong. `sdk build`
   exists on macOS and builds *for a Linux host*; `install` was simply the
   wrong subcommand.
6. **"`xtool install --network` means wireless re-signing is one command away."**
   Wrong — see the correction section above. Cable-bound today.
7. **A conclusion left standing after its premise moved.** When he split the
   ring from the interface, that invalidated the main objection to option C —
   but C had already been shelved, so nobody re-ran the conclusion. Every fact
   in it stayed true; only the answer went stale.

**The shared shape of most of them:** a status field read as a signal without
testing the thing the field supposedly indicates. The fix is always a
**control** — probe it directly, or compare against a row whose answer you
already know. #4 is the exception and the dangerous one: it was a summary of
*my own incomplete work*, which no control row catches, because the claim was
about the state of my own knowledge. **Guard: before writing "we checked X and
found nothing", confirm every agent that was checking X has actually reported.**

## OPEN ITEMS — nothing here is decided

- **Telegram doorbell is built and has never been logged in.** No `.session`
  file exists. `tg-login` (`send`/`code`/`pass`/`whoami`/`ringtest`) is written
  and tested on every path that does not need real credentials. Needs from him:
  `api_id`, `api_hash`, **and a second account with its own phone number** — a
  bot token CANNOT place calls (`phone.requestCall` is user-only; verified by
  the "Bots can use this method" marker being present on `messages.sendMessage`
  and absent here, with a known user-only method as a control).
- **PRIVACY CAVEAT, nobody else has written this down.** Logging archserver
  into his second Telegram account gives this box a full session on that
  account — **it can read that account's chats**, like any other logged-in
  device. He was told once, in passing, and has not confirmed he is comfortable
  with it. **Confirm before using a number that has personal history on it.**
  He can revoke it any time under Settings > Devices.
- **The weekly re-sign is cable-bound** until someone proves otherwise.
  SideStore's on-device refresh is the only known no-cable route and it costs
  the single iOS VPN slot Tailscale occupies.
- **The SIP plumbing changed after it last rang his handset** (~20:00). Nobody
  re-rang him to prove the refactor. One ring closes that gap.
- **`baresip` is installed** and was only ever a diff tool. Remove it once
  nobody is debugging SIP.

## Deliverables that are NOT in this repo

- The decision brief: https://claude.ai/code/artifact/c35a5d55-ef31-453a-a0d5-16827b4101de
  Republish that same file path to update it; do not create a second artifact.
- `~/.claude/skills/call-bogdan/SKILL.md` — rewritten to ring first, page as
  fallback, with the "verify the ring is real" section.
- `~/.claude/bin/hotline-call` — shim, venv-or-PYTHONPATH.
