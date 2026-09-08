#!/usr/bin/env python3
"""Ring him and hold a real two-way conversation, to answer one question:
does he hear me, and do I hear him.

Speak a Serbian greeting, listen, transcribe what came back, then say it back to
him. Saying it back is the point -- it proves BOTH directions in one call
without him having to describe what happened afterwards.
"""
from __future__ import annotations

import asyncio, base64, ctypes, glob, io, json, logging, os, sys, time, wave
import urllib.request
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("talk")

CVOICE = "http://100.72.2.62:8760"
TOKEN = "eOuV4ySUiXOxmBMDsILZ3yJZAumdIq55EK-xxQgwxRs"
PROFILE = "bogdan-stamenovic"
WHEELS = "/home/bodas/data/cvoice/.venv/lib/python3.14/site-packages"


def say(text: str) -> tuple[np.ndarray, int]:
    """Serbian TTS. Diacritics are REQUIRED -- ASCII-stripped Serbian is
    mispronounced, which is his explicit instruction."""
    body = json.dumps({"text": text, "profile": PROFILE, "takes": 1}).encode()
    req = urllib.request.Request(f"{CVOICE}/speak", data=body, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    t = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=300).read())
    w = wave.open(io.BytesIO(base64.b64decode(d["audio_b64"])))
    pcm16 = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    log.info("cvoice: %.2fs audio in %.2fs wall", d["duration"], time.time() - t)
    return pcm16, w.getframerate()


def load_whisper():
    for lib in glob.glob(f"{WHEELS}/nvidia/*/lib/*.so*"):
        if os.path.basename(lib).startswith(("libcublas", "libcudnn", "libcudart", "libnvrtc")):
            try: ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError: pass
    from faster_whisper import WhisperModel
    return WhisperModel("large-v3", device="cuda", compute_type="float16")


def main() -> int:
    from hotline_ios.media import voicecall
    from hotline_ios.ring.sip import SipTransport
    from hotline_ios.ring.base import CallTarget

    os.environ.setdefault("SIP_MEDIA_HOST", "100.72.2.62")
    for line in open("/home/bodas/data/hotline-ios/.env"):
        line = line.strip()
        if line.startswith("SIP_") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k, v.strip().strip('"'))

    log.info("synthesising the greeting before dialling, so there is no dead air")
    greeting, rate = say("Zdravo Bogdane. Ovde hotline, javljam se sa arch servera preko "
                         "SIP-a. Ako me čuješ, reci nešto posle signala, slušam te deset "
                         "sekundi, pa ću ti ponoviti šta sam razumeo.")
    whisper = load_whisper()
    log.info("whisper loaded; dialling")

    outcome = {}

    def on_answer(reply, media_sock, our_key, our_salt):
        open("/tmp/claude-1000/-home-bodas-data-hotline/f53d29f9-ee6b-4bef-a52b-ff60fa60720a/scratchpad/answer200.txt","w").write(reply)
        try:
            answer = voicecall.parse_sdp_answer(reply)
        except voicecall.SdpError as exc:
            log.error("SDP: %s", exc); outcome["error"] = str(exc); return
        log.info("ANSWERED. his media at %s:%d", answer.host, answer.port)
        call = voicecall.VoiceCall(media_sock, answer, our_key, our_salt)

        call.send_silence(voicecall.VoiceCall.PRIMING_SECONDS)  # fill his jitter buffer first
        spoke = call.send_audio(greeting, rate)
        log.info("spoke %.1fs, %d frames sent", spoke, call.frames_sent)

        heard = call.receive_audio(10.0)
        log.info("listened: %d frames in, %d auth failures, %.2fs of audio",
                 call.frames_received, call.auth_failures, heard.size / 16000)

        text = ""
        if heard.size > 8000:
            segs, _ = whisper.transcribe(heard, language="sr", beam_size=1)
            text = "".join(s.text for s in segs).strip()
        log.info("TRANSCRIPT: %r", text)

        reply_text = (f"Čuo sam sledeće: {text}. Znači čujemo se u oba smera. Prekidam vezu."
                      if text else
                      "Nisam čuo ništa od tebe. Ja tebe očigledno mogu da dozovem, ali "
                      "zvuk u drugom smeru ne stiže. Prekidam vezu.")
        back, r2 = say(reply_text)
        call.send_audio(back, r2)
        call.send_silence(0.3)
        outcome.update(call.stats()); outcome["transcript"] = text
        np.save("/tmp/claude-1000/-home-bodas-data-hotline/f53d29f9-ee6b-4bef-a52b-ff60fa60720a/scratchpad/heard.npy", heard)

    ring = SipTransport(on_answer=on_answer)

    async def go():
        await ring.start()
        await ring.ring(CallTarget(device="iphone", reason="audio test"), timeout=45.0)

    try:
        asyncio.run(go())
    except Exception as exc:
        log.error("call ended: %s: %s", type(exc).__name__, exc)
        outcome["ended"] = f"{type(exc).__name__}: {exc}"
    print("\n=== OUTCOME ===")
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
