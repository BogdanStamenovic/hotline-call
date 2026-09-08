#!/usr/bin/env python3
"""A real phone conversation: ring him, then talk until one of us hangs up.

**The design is his, from 2026-09-08.** A separate Sonnet session owns the call,
seeded with context before dialling so it does not have to discover anything
mid-sentence, and instructed in phone manners: short turns, and if it needs to
go and check something it SAYS so before it goes quiet.

**Why that matters, with the numbers.** After he stops speaking the pipeline
costs, measured on this box: 0.80 s to be sure he stopped, 0.93 s of Whisper,
2.8-4.3 s for the agent, 1.7-2.5 s of cvoice. Around seven seconds of silence,
against roughly one that a person tolerates on a phone. Nothing in that chain is
going to get 7x faster, so the fix is not to make the wait shorter but to stop it
being silent.

**Why the fillers are pre-rendered and not synthesised.** A filler that has to go
through cvoice first costs 1.7 s, which is most of the gap it exists to hide.
These are rendered once into `fillers/` and played from disk, so the first audio
he hears lands about 1.5 s after he stops talking instead of seven.

**Why a separate session rather than the one that placed the call.** Partly
speed -- Sonnet with a small context answers in ~2.8 s where a large session is
slower. Mostly it is that two agents answering him at once is the failure the
operator brief exists to prevent: this one owns the call, and the caller stays
off Discord while it is up.
"""
from __future__ import annotations

import asyncio, base64, ctypes, glob, io, json, logging, os, queue, subprocess, sys, threading, time, wave
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("talk")

CVOICE = "http://100.72.2.62:8760"
TOKEN = "eOuV4ySUiXOxmBMDsILZ3yJZAumdIq55EK-xxQgwxRs"
PROFILE = "bogdan-stamenovic"
WHEELS = "/home/bodas/data/cvoice/.venv/lib/python3.14/site-packages"
WORKDIR = "/home/bodas/data/hotline"
MAX_CALL_SECONDS = 1800   # a backstop, not a policy
# How long the line can be completely dead before we accept he has gone.
# His instruction: do not hang up on him, wait for HIS hangup. A silent
# stretch is him thinking; a stretch with no RTP at all is the phone gone.
DEAD_LINE_SECONDS = 90
# A single answer can run long; only stop extending it well past any
# plausible sentence.
MAX_UTTERANCE_SECONDS = 180
AGENT_SLOW_AFTER = 3.0   # seconds before a second, apologetic filler goes out

MANNERS = """You are on a LIVE PHONE CALL with Bogdan, speaking Serbian out loud. \
Your words go straight to a text-to-speech engine and into his ear.

RULES, all of them about being audible rather than readable:
- ONE short sentence. Two only if the second is genuinely necessary. He cannot
  skim a phone call, and anything past about eight seconds of speech is too long
  to follow by ear.
- NO markdown, NO lists, NO code, NO URLs, NO file paths read out character by \
character. If you must name a file, say it the way a person would.
- Serbian, with proper diacritics (c, c, s, z, dj as the real characters) -- the \
TTS mispronounces stripped ASCII.
- If you need to run a command or check something before you can answer, your \
FIRST words must say so: "Samo sekund, da proverim." Then check, then answer. \
Never go silent while you work.
- Numbers and technical terms: say them as a person would speak them.
- If you did not understand him, say so and ask him to repeat.
- If he says goodbye, say a short goodbye back and nothing else.
- Do not mention that you are an AI, and do not narrate your own tooling."""


def load_fillers():
    index = json.load(open(os.path.join(HERE, "fillers/index.json")))
    return {k: (np.load(os.path.join(HERE, f"fillers/{k}.npy")), v["rate"])
            for k, v in index.items()}


def say(text: str) -> tuple[np.ndarray, int]:
    body = json.dumps({"text": text, "profile": PROFILE, "takes": 1}).encode()
    req = urllib.request.Request(f"{CVOICE}/speak", data=body, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=300).read())
    w = wave.open(io.BytesIO(base64.b64decode(d["audio_b64"])))
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return pcm, w.getframerate()


def load_whisper():
    for lib in glob.glob(f"{WHEELS}/nvidia/*/lib/*.so*"):
        if os.path.basename(lib).startswith(("libcublas", "libcudnn", "libcudart", "libnvrtc")):
            try: ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError: pass
    from faster_whisper import WhisperModel
    return WhisperModel("large-v3", device="cuda", compute_type="float16")


class CallAgent:
    """The Sonnet session on the other end of the conversation.

    Keyless via `claude -p`, resumed by session id so each turn keeps the last.
    Measured: 4.7 s to open the session, 2.8 s per resumed turn.
    """

    def __init__(self, context: str):
        self.session: str | None = None
        self.context = context

    def _run(self, prompt: str, timeout: float) -> str:
        cmd = ["claude", "-p", "--model", "sonnet", "--output-format", "json"]
        if self.session:
            cmd += ["--resume", self.session]
        cmd.append(prompt)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=WORKDIR)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200] or "claude exited nonzero")
        payload = json.loads(proc.stdout)
        self.session = payload.get("session_id", self.session)
        return (payload.get("result") or "").strip()

    def open(self) -> None:
        """Seed the session before dialling, so no discovery happens mid-call."""
        self._run(f"{MANNERS}\n\nCONTEXT for this call:\n{self.context}\n\n"
                  "Do not reply to this message with anything but the single word OK.", 180)

    def reply(self, heard: str) -> str:
        return self._run(f"Bogdan je upravo rekao, preko telefona: \"{heard}\"", 120)


def main() -> int:
    from hotline_ios.media import voicecall
    from hotline_ios.ring.sip import SipTransport
    from hotline_ios.ring.base import CallTarget

    os.environ.setdefault("SIP_MEDIA_HOST", "100.72.2.62")
    for line in open("/home/bodas/data/hotline-ios/.env"):
        line = line.strip()
        if line.startswith("SIP_") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k, v.strip().strip('"'))

    context = open(os.path.join(HERE, "call_context.txt")).read() \
        if os.path.exists(os.path.join(HERE, "call_context.txt")) else "Nema posebnog konteksta."

    fillers = load_fillers()
    whisper = load_whisper()
    agent = CallAgent(context)
    log.info("seeding the call agent before dialling")
    t = time.time(); agent.open()
    log.info("agent ready in %.1fs (session %s)", time.time() - t, (agent.session or "?")[:12])

    transcript: list[tuple[str, str]] = []
    # One worker: faster-whisper is not thread-safe for concurrent transcribes on
    # the same model, and serialising them is fine because they are each far
    # shorter than the speech still arriving behind them.
    pool = ThreadPoolExecutor(max_workers=1)

    def transcribe(audio) -> str:
        if audio is None or audio.size < 4000:
            return ""
        segs, _ = whisper.transcribe(audio, language="sr", beam_size=1)
        return "".join(seg.text for seg in segs).strip()

    def on_answer(reply_msg, media_sock, our_key, our_salt):
        try:
            answer = voicecall.parse_sdp_answer(reply_msg)
        except voicecall.SdpError as exc:
            log.error("SDP: %s", exc); return
        log.info("ANSWERED -- his media at %s:%d", answer.host, answer.port)
        call = voicecall.VoiceCall(media_sock, answer, our_key, our_salt)

        def play(name, interruptible=False):
            """Fillers are short and go out whole; only real answers are worth
            interrupting, and a half-spoken "mhm" is worse than none."""
            audio, rate = fillers[name]
            call.send_audio(audio, rate, interruptible=interruptible)

        def speak(text):
            t0 = time.time(); audio, rate = say(text)
            log.info("  cvoice %.2fs for %.1fs", time.time() - t0, audio.size / rate)
            call.send_audio(audio, rate, interruptible=True)
            if call.interrupted:
                log.info("  (he cut in)")

        call.send_silence(voicecall.VoiceCall.PRIMING_SECONDS, calibrate=True)
        play("greet", interruptible=True)

        # He asked explicitly not to be hung up on: this end stays on the line
        # until HIS hangup. Silence is him thinking, not him leaving, and the
        # previous version ended the call twice while he was still there.
        call_started = time.time()
        dead_since: float | None = None
        turn = 0
        while time.time() - call_started < MAX_CALL_SECONDS:
            turn += 1
            # Transcribe each phrase as he finishes it, on a worker, so the ASR
            # cost lands under his own speech instead of in the pause after it.
            pending: list = []
            captured: list[np.ndarray] = []

            def on_chunk(phrase, _p=pending, _c=captured):
                _c.append(phrase)
                _p.append(pool.submit(transcribe, phrase))

            listened = 0.0
            while True:
                heard, why = call.receive_turn(max_seconds=30.0, silence_ms=800,
                                               on_chunk=on_chunk)
                if heard.size:
                    captured.append(heard)
                    pending.append(pool.submit(transcribe, heard))
                listened += 30.0
                # "timeout" means the cap hit while he was STILL TALKING. Ending
                # the turn there is talking over him, which is what cut him off
                # mid-sentence on the 19:10 call. Keep listening.
                if why == "timeout" and listened < MAX_UTTERANCE_SECONDS:
                    log.info("turn %d: still going at %.0fs, keeping the line open",
                             turn, listened)
                    continue
                break
            log.info("turn %d: %s, %d phrase(s)", turn, why, len(pending))

            if why == "no-audio":
                # No RTP at all. Either he hung up or the media died; only after
                # a long stretch of it do we accept the call is over.
                dead_since = dead_since or time.time()
                if time.time() - dead_since > DEAD_LINE_SECONDS:
                    log.info("no media for %ds; he is gone", DEAD_LINE_SECONDS)
                    break
                continue
            dead_since = None

            if not pending and (why == "silence" or heard.size < 8000):
                # Audio is flowing, he just is not speaking. Stay quiet and wait.
                call.send_silence(1.0)
                continue

            t0 = time.time()
            said = " ".join(f.result() for f in pending).strip()
            log.info("  heard (%.2fs to finish, %d phrases): %r",
                     time.time() - t0, len(pending), said)
            if call.his_level is None and captured:
                # The first thing he says is definitionally him: he answered the
                # phone. Everything quieter or unlike it afterwards is the room.
                call.enrol_voice(np.concatenate(captured))
            if not said:
                play("notheard"); continue
            transcript.append(("bogdan", said))

            # "cao" is NOT here, and that is the whole point: in Serbian it is a
            # greeting at least as often as a farewell. He opened a call with
            # "Cao brate" and this hung up on him mid-hello.
            #
            # These also have to appear near the END of what he said. "Reci mi
            # kad zavrsis" contains a farewell word and is not one.
            low = said.lower()
            tail = low[-40:]
            farewells = ("prekini", "prekidam", "dovidjenja", "doviđenja",
                         "cujemo se", "čujemo se", "prijatno", "zdravo i prijatno",
                         "to je to", "hvala i prijatno")
            if any(w in tail for w in farewells):
                play("bye"); transcript.append(("hotline", "bye")); break

            # The agent runs in a thread so the line never goes quiet: an
            # acknowledgement goes out at once, and a second filler if it is slow.
            result: queue.Queue = queue.Queue(maxsize=1)
            def work(text=said):
                try: result.put(("ok", agent.reply(text)))
                except Exception as exc: result.put(("err", f"{type(exc).__name__}: {exc}"))
            worker = threading.Thread(target=work, daemon=True); worker.start()

            play(["ack_mhm", "ack_aha", "ack_dobro"][turn % 3])
            began = time.time()
            while worker.is_alive() and time.time() - began < 25:
                if time.time() - began > AGENT_SLOW_AFTER and result.empty():
                    play(["wait_sekund", "wait_proveri", "wait_vidim"][turn % 3])
                    began = time.time() - AGENT_SLOW_AFTER - 90  # only once per turn
                else:
                    call.send_silence(0.2)
            try:
                status, text = result.get(timeout=1.0)
            except queue.Empty:
                status, text = "err", "agent did not answer in time"
            log.info("  agent (%s): %r", status, text[:120])
            if status != "ok" or not text:
                text = "Izvini, nešto mi se zaglavilo. Pokušaj ponovo."
            transcript.append(("hotline", text))

            speak(text)

        call.send_silence(0.4)
        log.info("call stats: %s", call.stats())

    ring = SipTransport(on_answer=on_answer)

    async def go():
        await ring.start()
        await ring.ring(CallTarget(device="iphone", reason="conversation"), timeout=45.0)

    try:
        asyncio.run(go())
    except Exception as exc:
        log.error("call ended: %s: %s", type(exc).__name__, exc)

    print("\n=== TRANSCRIPT ===")
    for who, what in transcript:
        print(f"  {who:8}: {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
