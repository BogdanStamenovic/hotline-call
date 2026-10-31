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
| `ring/watch.py`, `ring/chain.py` | a ring counts only with evidence it rang (`180 Ringing`); a chain falls through on that evidence |
| `ring/inbound.py` | keeps the account registered so he can dial hotline, answers his INVITE |
| `media/` | RTP, SRTP, Opus and G.711, jitter buffer, Whisper ears, the cvoiced TTS client, recording |
| `conversation.py` | one answered call: listen, transcribe, ask, speak, barge-in |
| `callagent.py`, `haikuagent.py`, `calltools.py` | who talks: a Claude session, or Haiku 5.5 streamed into cvoiced, with read-only lookups |
| `agentbridge.py` | "spoji me sa <agent>": puts a live hotline agent on his call |
| `duplexcall.py`, `gpu_handoff.py` | PersonaPlex (English full duplex) and handing the 8 GiB card over to it and back |
| `speculate.py` | guessing how a sentence ends while he is still saying it |
| `daemon.py` | `hotline-calld`: the HTTP API, the model refcount, the GPU lock |
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
| `POST /api/v1/call` | `hotline-call`, `hotline-registry`, any agent | unchanged from hotline-iosd: `{reason, context, source, agent, to, callee, engine, transport, ring_timeout, timeout, wait}` → `{call_id, conversation, state, reply, transcript, detail, waited_seconds, transport, rings_when_closed, fake, engine, note}` |
| `POST /api/v1/call/reply` | hotline-iosd | `{conversation, text}` → `{conversation, known, waiting?}` |
| `POST /api/v1/call/hangup` | hotline-iosd | `{call_id}` → `{call_id, ended}` |
| `GET /health` | anyone | `ok` (false for a fake doorbell or one that did not start), `ring_ready`, `can_talk`, `transport`, `transports_available`, `inbound`, `active_calls`, `app_mirror`, `degradations` |
| `GET /api/v1/ping` | probes | `{ok: true}` |

Same gate as hotlined and hotline-iosd: loopback always allowed, otherwise the
`HOTLINE_ALLOW_IPS` allowlist, plus `X-Hotline-Key` when `HOTLINE_API_KEY` is set.
It binds `127.0.0.1` and the tailnet address `100.72.2.62` on port 8792 (8790
and 8791 are taken there); if the tailnet bind fails it exits so systemd retries.

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
to `hotline-page` by itself. `--engine personaplex|classic|haiku` picks who talks;
`hotline-call --help` has the rest.

The client talks to `HOTLINE_CALL_URL`, else `HOTLINE_IOS_URL`, else
`http://127.0.0.1:8792`.

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
| `HOTLINE_IOS_DEFAULT_ENGINE` | `personaplex` | who talks on a call to him |
| `HOTLINE_IOS_VOICE` | `1` | `0`: ring only, no voice leg |
| `HOTLINE_IOS_RECORD_DIR` | unset | record calls (his voice: never into git) |
| `HOTLINE_CALL_HOST`, `HOTLINE_CALL_PORT` | `100.72.2.62`, `8792` | where hotline-calld listens |
| `HOTLINE_CALL_APP_URL` | `http://127.0.0.1:8789` | hotline-iosd, for the mirror |
| `HOTLINE_CALL_APP_MIRROR` | `1` | `0`: keep calls out of the app |

## Tests

No GPU, no models: the suite fakes Whisper, cvoiced and the SIP peer.

```bash
PYTHONPATH=src:$HOME/data/hotline/src .venv/bin/python -m pytest -q tests
```

363 passed in 241 s on archserver (2026-10-31), in the venv `ownbox install` built.

## Limitations

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
- **The SIP User-Agent and the inbound `+sip.instance` still say hotline-ios.**
  That is wire identity: a new instance id would register a second contact for
  the same account instead of refreshing the one linphone.org holds.
- **The ownbox launcher routes `update`, `remove`, `info` and the other ownbox
  words.** `hotline-call info` prints ownbox's info and exits 0 instead of
  ringing anybody. Agents should use `~/.claude/bin/hotline-call`, which execs
  the venv's console script directly.
- **Recordings and bench data stay out of git** (`.gitignore`): this repository
  is public and they are his voice and his words.
