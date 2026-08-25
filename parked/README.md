# Parked, not deleted

All of this worked. None of it is needed by the design Bogdan chose on
2026-08-25, when he decoupled the ringer from the talker: **Telegram rings, his
own app is the interface, and the voice route is scrapped.**

It is parked rather than deleted for one specific reason: **the whole plan rests
on Telegram actually being on his phone, and he has not confirmed that.** If it
is not, outcome C — the stock Linphone app rung through Belledonne's push relay
— is the branch to come back to, and this is the work that branch needs.

| parked | was | why it is not needed now |
|---|---|---|
| `hotline_ios/sipprobe.py` | a SIP registrar that captures what a client puts in its REGISTER `Contact` header | it exists to settle whether outcome C works, and C is not the plan |
| `hotline_ios/media/rtp.py` | RTP + G.711 + a jitter buffer sized against the measured DERP path | there is no audio leg any more |
| `hotline_ios/ring/local.py` | ring his own app over a live tailnet socket, with an ack as proof | the app does not ring; Telegram does |
| `systemd/hotline-sipprobe.service` | ran the probe on `100.72.2.62:5060` | stood down, unit removed from `~/.config/systemd/user` |
| `docs/SIP-PROBE.md` | the five-minute experiment for him to run | the experiment is not needed unless C comes back |
| `hotline_ios/call.py` | one voice call: VAD, transcription, `pool.ask`, synthesis, barge-in, tool narration | the voice route is scrapped -- he called it a gimmick |
| `hotline_ios/media/pcm.py` | format conversion and G.711 as a lookup table | nothing carries audio any more |
| `hotline_ios/media/queue.py` | the queue that made barge-in possible | same |

Nothing here was ever run against his phone. The probe answered its own smoke
tests over a real socket, and the RTP layer talked to itself over two real UDP
sockets, but no handset was ever involved.

`RingTransport` also lost its `MediaStream` when this was parked. It used to
carry both the ring and the audio, because every option on the table at the time
assumed the ringer and the talker were one program. Restoring the audio branch
means restoring that too — see `ring/base.py` in git history before the pivot.

**To bring any of it back**: move the file back under `server/src/hotline_ios/`
and its test back under `server/tests/`. The tests are unmodified and passed
where they sat — 7 for the probe, 9 for RTP, 7 for the local transport, 8 for the call
orchestrator, 7 for the format conversion.
