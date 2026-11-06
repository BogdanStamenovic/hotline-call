# hotline-call

Phone calls between Claude Code sessions on `archserver` and people: the ring,
the voice on an answered call, the line he can dial in on, and `hotline-call`,
the command agents use to ring Bogdan and get his answer back on stdout.

It was the call half of [`hotline-ios`](https://github.com/BogdanStamenovic/hotline-ios)
until 2026-10-31, when he moved call semantics out of the iOS app's server
(Discord 1566035363657420962). The history came along with `git filter-repo`.
hotline-ios keeps the iPhone app and its backbone: roster, agent channels,
store, delegation.

## How it works

```
agent ──hotline-call──▶ hotline-calld :8792 ──SIP INVITE──▶ linphone.org ──▶ his iPhone
                          │      ▲                         (or Telegram)
                          │      │ /api/v1/call/reply  (his typed answer, forwarded)
            AppMirror ────┘      │ /api/v1/call/hangup (he dismissed it in the app)
   /api/v1/conversations/*       │
                          ▼      │
                      hotline-iosd :8789  ◀──── the iPhone app
                      (proxies /api/v1/call here for old callers)
```

| piece | what it does |
|---|---|
| `ring/sip.py`, `ring/telegram.py` | the doorbells: an outbound SIP INVITE with digest auth, or a Telegram user-account call |
| `ring/whatsapp.py`, `media/pcmlink.py` | a WhatsApp call through [wa-call](https://github.com/BogdanStamenovic/wa-call)'s daemon, its audio as raw PCM over a Unix socket |
| `ring/watch.py`, `ring/chain.py` | a ring counts only with evidence it rang (`180 Ringing`); a chain falls through on that evidence |
| `ring/inbound.py` | keeps the account registered so he can dial hotline, answers his INVITE |
| `media/` | RTP, SRTP, Opus and G.711, jitter buffer, Whisper ears, the cvoiced TTS client, recording |
| `conversation.py` | one answered call: listen, transcribe, ask, speak, barge-in |
| `callagent.py`, `haikuagent.py`, `calltools.py` | who talks: a Claude session, or Haiku 5.5 streamed into cvoiced, with read-only lookups |
| `agentbridge.py` | "spoji me sa <agent>": puts a live hotline agent on his call |
| `speculate.py` | guessing how a sentence ends while he is still saying it |
| `daemon.py` | `hotline-calld`: the HTTP API, the model refcount, the GPU lease (`gpulease.py`) |
| `calllog.py` | the call's own in-memory record, which `place()` waits on |
| `appmirror.py` | copies every call into hotline-iosd's store so the app shows it |
| `client.py`, `call_cli.py` | `hotline-call`, standard library only |
| `sipprobe.py` | the instrument that settled whether Linphone could be the doorbell |
| `bench/` | the measurements behind the ASR, barge-in and speculation choices |

**Why the app still sees calls.** A ring opens a conversation he can answer by
typing instead of picking up, and the question is in the app before the phone
rings. That conversation is the app's and lives in hotline-iosd's store, so this
daemon mirrors each line there over HTTP, on one worker thread, best effort. His
typed answer comes back as `/api/v1/call/reply`, which hotline-iosd forwards.
With hotline-iosd down a call still rings, talks and returns his spoken answer;
it is only missing from the app, and `/health` says so under `app_mirror`.

## The HTTP API

| route | who calls it | body → reply |
|---|---|---|
| `POST /api/v1/call` | `hotline-call`, `hotline-registry`, any agent | unchanged from hotline-iosd: `{reason, context, source, agent, to, whatsapp, via, via_order, callee, engine, transport, ring_timeout, timeout, wait}` → `{call_id, conversation, state, reply, transcript, detail, waited_seconds, transport, via, rings_when_closed, fake, engine, note}` (see [WhatsApp](#whatsapp) for `whatsapp`/`via`) |
| `POST /api/v1/call/reply` | hotline-iosd | `{conversation, text}` → `{conversation, known, waiting?}` |
| `POST /api/v1/call/hangup` | hotline-iosd | `{call_id}` → `{call_id, ended}` |
| `GET /health` | anyone | `ok` (false for a fake doorbell or one that did not start), `ring_ready`, `can_talk`, `transport`, `transports_available`, `inbound`, `active_calls`, `app_mirror`, `whatsapp` (wa-call's daemon and link state, or null), `degradations` |
| `GET /api/v1/ping` | probes | `{ok: true}` |

Same gate as hotlined and hotline-iosd: loopback always allowed, otherwise the
`HOTLINE_ALLOW_IPS` allowlist, plus `X-Hotline-Key` when `HOTLINE_API_KEY` is set.
It binds `127.0.0.1` and the tailnet address `100.72.2.62` on port 8792 (8790
and 8791 are taken there); if the tailnet bind fails it exits so systemd retries.

## WhatsApp

Bogdan's request, 2026-11-06: ring a person on the channel they prefer, WhatsApp
included. The protocol stack is not here. `wa-call daemon` (his Rust repo, on
whatsapp-rust) is a linked companion device of a WhatsApp account. hotline-calld
asks it over its control socket to dial, and to carry the call's audio on a Unix
socket that hotline-calld listens on (`wa-call --socket`). That socket becomes the
same `VoiceCall` a SIP call has, through `VoiceCall.over(PcmPump)`, so the same
voice, ears, barge-in and recording hold the call.

```
place() ─▶ WhatsAppTransport ─dial {to, spec.audio.socket}─▶ wa-call daemon ─▶ WhatsApp
               ▲  peer-ringing / accepted / rejected / ended          │
               └── $XDG_RUNTIME_DIR/hotline-call/wa-<id>.sock ◀─ s16le 16 kHz ─┘
```

**Routing.** `via` on `POST /api/v1/call`:

| request | rings |
|---|---|
| no `to`, no `whatsapp` (a call to him) | the configured doorbell, as before; `via: whatsapp` uses `HOTLINE_CALL_WA_BOGDAN` |
| `to` and/or `whatsapp`, `via` empty or `auto` | the channels they have an address on, in `via_order` (default `sip, whatsapp`), falling through on unreachable |
| `via: sip` or `via: whatsapp` | that channel only; refused (400) without an address for it, never swapped for the other |

A guest is never rung on a doorbell that can only reach him (Telegram,
loopback): with no usable channel the call is a 503. `hotline-registry call` sends
the person's numbers and their order; the daemon knows nothing about the registry.

**Evidence that it rang.** `peer-ringing` is the callee's device sending
`<preaccept>`, the WhatsApp counterpart of SIP's `180`, and it is what
`ConfirmedRing` waits for (8 s). wa-call's `connected` is not used, since it fires
on relay allocation, which can happen while the phone is offline. A WhatsApp ring
with no `peer-ringing` is `CallUnreachable`: the dial is hung up and `auto` moves on
to SIP, with a line in `degradations`.

**Degrading.** No wa-call daemon, an unlinked one, or a refused dial is
`CallUnreachable`, so an `auto` call rings SIP instead. `/health` has a `whatsapp`
object (`daemon`, `logged_in`, `error`). While WhatsApp is not usable,
`degradations` also says so.

**Inbound.** It is opt-in, by number (`HOTLINE_CALL_WA_ANSWER`). The linked account
may be his personal one, and then every caller would reach this daemon. Calls
from other numbers are left alone and ring his phone as they would without
wa-call. An answered inbound call is the same "he called hotline" leg that SIP
inbound gets.

**Why a socket and not PipeWire.** wa-call's `--live` mode with two null sinks
would have needed no wa-call change. Measured here on 2026-11-06 with 20 ms frames
written in real time, one `pw-cat` playback-to-monitor-capture hop took 135-255 ms
(median 191 ms, n=11) and crept up about 24 ms/s. A bridge needs one hop per
direction, plus nodes to clean up after a crash. The Unix socket measured
0.005 ms median and 0.1 ms max (n=200).

## Install

```bash
ownbox install hotline-call --yes
```

`ownbox.sh setup` does the whole job:

1. A Python 3.12 venv with uv, `-e .[voice,telegram,hotline,dev]`: numpy, soxr,
   faster-whisper, websockets, cryptography, telethon, pynacl (which hotline
   imports without declaring), pytest.
2. Checks that hotline's source is at `~/data/hotline/src` (the daemon imports
   `hotline.httpd`, `hotline.config` and the agent registry from it, over
   PYTHONPATH, as hotline-iosd always did) and that cvoice's venv has CUDA
   wheels to borrow for Whisper.
3. `~/.config/hotline-call/env` (mode 600): the call keys copied out of
   `~/data/hotline-ios/.env` if it exists (asked on a terminal, done by default
   without one, since the file never leaves the box and without it nothing
   rings), otherwise `.env.example` with a note of what to fill in.
4. Renders `systemd/hotline-calld.service.in` into
   `~/.config/systemd/user/hotline-calld.service`, with the bounded
   wait-for-cvoiced start gate, and enables it.
5. Starts it, **unless** a hotline-iosd from before the split is on 8789 (its
   `/health` has no `calld` field): two call daemons on one SIP account would
   both ring and both answer his inbound INVITE. Then it says so and leaves the
   start to the cut-over. `HOTLINE_CALL_START=1|0` overrides.

`ownbox update hotline-call --yes` rebuilds the venv, re-renders the unit from
the remembered settings (`~/.config/hotline-call/install.env`) and restarts the
daemon if it was running. Nothing is persisted here, so there is nothing to
migrate and a rollback has nothing to undo. `ownbox uninstall hotline-call`
stops and removes the unit and asks before deleting `~/.config/hotline-call`
(kept by default).

Setup knobs, for a second instance that must not touch production:

```bash
HOTLINE_CALL_UNIT=hotline-calld-test HOTLINE_CALL_PORT=8799 HOTLINE_CALL_HOST=127.0.0.1 \
HOTLINE_IOS_INBOUND=0 HOTLINE_IOS_RING=loopback HOTLINE_IOS_ALLOW_FAKE=1 \
HOTLINE_IOS_VOICE=0 HOTLINE_CALL_APP_MIRROR=0 HOTLINE_CALL_START=1 ./ownbox.sh setup
```

## Usage

```bash
answer=$(hotline-call --source "the deploy script" "Staging is green. Push to prod?")
```

Exit codes are `hotline-page`'s: 0 answered, 1 undeliverable, 2 usage, 3
unanswered, plus 4 he declined. When the call cannot be delivered it falls back
to `hotline-page` by itself. `--engine classic|haiku` picks who talks (an unknown name rings on the default, with a note);
`hotline-call --help` has the rest.

The client talks to `HOTLINE_CALL_URL`, else `HOTLINE_IOS_URL`, else
`http://127.0.0.1:8792`. With neither variable set, a *refused* connection on
8792 is retried once on hotline-iosd's 8789, which rang him itself before the
split and forwards to hotline-calld after it -- so the client works on either
side of the cut-over. A refused connect placed no call, so this cannot ring him
twice; a timeout or a reset mid-call is never retried.

`./ownbox.sh status` shows the unit, the config and the live `/health`.

## Configuration

Read in this order, the first to set a key winning and the real environment
beating all of them: hotline's own `~/data/hotline/.env` (the order hotline-iosd
has always used, so `HOTLINE_ALLOW_IPS` there still wins), then
`~/.config/hotline-call/env`, then a `.env` in the checkout. The call knobs keep their
`HOTLINE_IOS_*` names from before the split so the existing `.env` works as is:

| variable | default | |
|---|---|---|
| `SIP_USER`, `SIP_PASSWORD`, `SIP_DOMAIN`, `SIP_PEER` | | the account that rings, and his |
| `HOTLINE_IOS_RING` | `sip` | doorbells in order: `sip`, `telegram`, `loopback` |
| `HOTLINE_IOS_INBOUND` | `1` | `0`: do not register, he cannot dial in |
| `HOTLINE_IOS_DEFAULT_ENGINE` | `haiku` | who talks on a call to him: `haiku` or `classic` |
| `HOTLINE_IOS_VOICE` | `1` | `0`: ring only, no voice leg |
| `HOTLINE_IOS_RECORD_DIR` | unset | record calls (his voice: never into git) |
| `HOTLINE_CALL_HOST`, `HOTLINE_CALL_PORT` | `100.72.2.62`, `8792` | where hotline-calld listens |
| `HOTLINE_CALL_APP_URL` | `http://127.0.0.1:8789` | hotline-iosd, for the mirror |
| `HOTLINE_CALL_APP_MIRROR` | `1` | `0`: keep calls out of the app |
| `HOTLINE_CALL_WHATSAPP` | `1` | `0`: no WhatsApp transport at all |
| `HOTLINE_CALL_WA_BOGDAN` | unset | his WhatsApp number, for `--via whatsapp` on a call to him |
| `HOTLINE_CALL_WA_ANSWER` | unset | comma-separated numbers whose WhatsApp calls hotline answers; unset answers none |
| `WA_CALL_SOCKET` | `$XDG_RUNTIME_DIR/wa-call.sock` | wa-call's control socket |

## The fallback on pigion

When archserver is off, a call to hotline would ring out unanswered. The
fallback role runs on pigion (a Pi Zero 2 W that is always on) and covers
that: it answers him, says in his own cloned voice *"Ćao Bogdane. Archserver je
trenutno ugašen. Budim ga, i čim se podigne, hotline će te pozvati."*, hangs
up, wakes archserver with a Wake-on-LAN packet, and once hotline-calld is back
it asks calld to ring him.

```
pigion: ping calld /api/v1/ping every 10 s ──60 s of silence──▶ REGISTER hotline-caller
        (instance of its own)                                    │
his call ─▶ linphone.org ─▶ pigion: 200 OK, 6.3 s clip (SRTP) , BYE
                                    └─▶ magic packet now, +90 s, +240 s
calld answers again ─▶ REGISTER Expires: 0 ─▶ POST calld /api/v1/call (once)
```

- **Only during an outage.** While calld answers pings, pigion holds no
  registration, so his calls only ever reach calld. Down means 60 s with no
  successful ping (`HOTLINE_CALL_FALLBACK_DOWN_AFTER`), so a calld restart is
  not an outage.
- **Its own `+sip.instance`**, never production's: the same instance would
  replace calld's binding rather than sit beside it.
- **Lean on purpose.** The Pi has 415 MB of RAM, no numpy and no libopus, so
  nothing is decoded or encoded there. `ownbox.sh` renders the clip on
  archserver with cvoiced (takes=1) and pre-encodes it into 20 ms Opus payloads
  and G.711 mu-law and A-law frames; the Pi only adds RTP headers, SRTP and
  pacing, with the system `python3` and `cryptography`. Measured on pigion
  (2026-10-31): 25.9 MB RSS idle, 30.3 MB at the peak of a call.
- **No wake on outage, only on his call.** archserver powers itself off on
  purpose (wake's `then_do=poweroff`); waking it every time would undo that.
- **One callback per outage**, recorded in
  `~/.local/state/hotline-call/fallback.json` before the request goes out, so a
  pigion reboot neither loses it nor repeats it. Retried only when calld
  refused it before ringing (connection refused or an HTTP error), up to 5 times.

Install on the Pi (ownbox has no roles, so `ownbox.sh` picks: `HOTLINE_CALL_ROLE`,
the remembered role, a prompt on a terminal, or a guess from the RAM):

```bash
HOTLINE_CALL_ROLE=fallback ownbox install hotline-call --yes
```

Setup checks the system python has `cryptography` and that the fallback
imports without numpy, copies the SIP keys from `archserver:~/.config/hotline-call/env`
over ssh (asked for on a terminal if that fails; `SIP_MEDIA_HOST` is not copied,
the Pi offers its own tailnet address), reads archserver's MAC over ssh, renders
the clip, and installs and starts `hotline-call-fallback.service`. Update
re-renders the clip only when its text or format changed and keeps the old one if
archserver is unreachable; the state file is never touched. Remove asks before
deleting the clip, the state and the credentials (kept by default).

```bash
hotline-call-fallback status         # config, clip, callback state, does calld answer
hotline-call-fallback wol --dry-run  # what the magic packet would be
hotline-call-fallback serve --test --for-seconds 120
```

`--test` forces "down" without touching archserver, registers a separate test
instance, takes calls only from hotline's own account (which production calld
refuses with 403) and keeps its own state file. `bench/fallback_live.py` on
archserver places that call through linphone.org and checks what arrives.
Measured that way on 2026-10-31: calld logged the 403, pigion answered with
Opus, 314 of 314 frames arrived and decrypted with 0 authentication failures
(6.28 s decoded, 0 decode errors), pigion's BYE came 6.5 s after the 200, and a
magic packet for archserver's MAC was captured on archserver's NIC within the
same second the INVITE arrived.

## Tests

No GPU, no models: the suite fakes Whisper, cvoiced and the SIP peer.

```bash
PYTHONPATH=src:$HOME/data/hotline/src .venv/bin/python -m pytest -q tests
```

442 passed, 2 skipped in about 262 s on archserver (2026-11-06), in the venv
`ownbox install` builds.

## Limitations

- **The fallback reads "calld does not answer" as "archserver is off".** A
  crashed calld on a running archserver gets the same message and a magic
  packet it does not need.
- **Wake-on-LAN is fire-and-forget.** The packet leaves pigion's wifi as a
  broadcast to 255.255.255.255:9; whether archserver wakes is only visible as
  calld coming back. archserver's NIC has `wake-on-lan magic` in
  NetworkManager, and wake's own 06:00Z wol tasks on 2026-09-04 and 09-05 were
  each followed by a boot at 08:00 local.
- **The fallback's clip is screened, not checked.** A take is rejected for an
  implausible length, silence or clipping; nothing confirms it says the text.
- **A fallback call's audio depends on linphone.org's media relay.** On the live
  test (2026-10-31) flexisip rewrote both SDPs to its relay (176.31.149.179), so
  pigion sent to the relay over the internet, not over the tailnet. That is the
  path that was measured; a call that bypassed the relay would need his phone
  to reach pigion's tailnet address.

- **WhatsApp calls are untested against WhatsApp.** No account is linked on
  archserver. Everything above is exercised against a fake wa-call daemon that
  speaks its real control protocol and audio socket (`tests/test_whatsapp.py`),
  and the conversation loop has been run over the PCM link. The real call
  path, from the dial through to whether `preaccept` arrives and how the audio
  sounds, has not been run.
- **Calls from the linked account come from that account's number.** If it is
  his personal WhatsApp, a guest sees Bogdan calling. The voice says it calls
  on his behalf, but the caller ID is his. Calling him over WhatsApp from his
  own account is impossible, which is why `via: whatsapp` to him needs a
  number of its own.
- **Inbound matching needs a phone number.** WhatsApp may identify a caller
  only by a LID; wa-call passes `caller_pn` when the offer has one. A call that
  carries neither is not answered.
- **It depends on hotline's source tree being at `~/data/hotline/src`.** hotline
  is not packaged; this reads it over PYTHONPATH like hotline-iosd did.
- **Whisper on CUDA borrows cvoice's cuBLAS/cuDNN wheels.** Without cvoice
  installed, set `HOTLINE_IOS_ASR_DEVICE=cpu` (large-v3 on CPU measured 5.39 s
  per 2.5 s utterance, 14x slower than on the GPU).
- **The app's copy of a call is best effort.** If hotline-iosd is down when a
  call opens, the app never sees that call, even after hotline-iosd comes back;
  the failures are counted on `/health`, not replayed.
- **A typed answer reaches a waiting call only through hotline-iosd's
  forward.** If hotline-iosd cannot reach this daemon at that moment, the answer
  is in the app but the calling agent waits out its timeout.
- **A call in progress does not survive a restart of this daemon.** It never
  did; the RTP socket lives in the process.
- **The `HOTLINE_IOS_*` names are a leftover**, kept for the existing `.env`.
  Renaming them is a separate change.
- **There is no English full-duplex engine any more.** PersonaPlex was removed on
  2026-11-03: Bogdan no longer used it, and its weights and venvs were deleted.
  A request for `engine=personaplex` rings on the default engine with a note.
- **The SIP User-Agent and the inbound `+sip.instance` still say hotline-ios.**
  That is wire identity: a new instance id would register a second contact for
  the same account instead of refreshing the one linphone.org holds.
- **The ownbox launcher routes `update`, `remove`, `info` and the other ownbox
  words.** `hotline-call info` prints ownbox's info and exits 0 instead of
  ringing anybody. Agents should use `~/.claude/bin/hotline-call`, which execs
  the venv's console script directly.
- **Recordings and bench data stay out of git** (`.gitignore`): this repository
  is public and they are his voice and his words.
