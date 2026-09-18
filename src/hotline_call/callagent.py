"""The session on the other end of a phone call.

**His design, 2026-09-08.** A separate Sonnet session owns the call, seeded with
context before the phone rings so it never has to discover anything
mid-sentence, and instructed in phone manners rather than trusted to invent
them.

**Why not the session that placed the call.** Two reasons, and the second is the
one that actually forces it. Speed: Sonnet with a small context answers in about
2.8 s where a large session is slower, and on a phone that difference is the
whole experience. But mainly, the agent that rang him is *blocked inside the
ring* for as long as the call is up -- it is waiting on this very call to
return -- so asking it a question would be asking something that cannot answer
until the conversation it is being asked in has ended.

**Why `claude -p` rather than the session pool.** Keyless, which is the standing
preference, and measured: 4.7 s to open the session, 2.8 s per resumed turn.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import threading
import time

log = logging.getLogger("hotline-ios.callagent")

DEFAULT_CWD = os.environ.get("HOTLINE_CWD", "/home/bodas/data")
# His instruction, 2026-09-10 18:48Z: "somnets either way must be read only. As
# they should relay the answere back to the original session. Thats the whole
# point. The big opus session acts on it not the sonnets."
#
# This is not only a safety rail, it is the architecture: the call agent SAYS
# things, the session that owns the work DOES them. Before this, the call ran
# `claude -p` with no restriction at all, cwd /home/bodas/data, and its own
# manners told it to run commands -- so a voice on a phone line, driven by a
# transcript that is about a third wrong, could push, delete or restart things.
#
# Why these three flags rather than a deny-list:
#   --tools              an ALLOW-list from the built-in set. No Bash, no Write,
#                        no Edit, no WebFetch. A deny-list has to stay correct
#                        forever; this one cannot be wrong by omission.
#   --restricted         ignores user/project/local settings, so a broad allow
#                        rule in settings.json cannot leak in, confines the file
#                        tools to the working directory, and refuses
#                        bypassPermissions outright.
#   --permission-prompts anything that would prompt is DENIED rather than
#     none               waiting for an answer. On a live call a prompt nobody
#                        can see is a silent phone line, which is the failure
#                        this whole path exists to end.
#
# Verified on 2026-09-10 rather than assumed: asked to write a file it answered
# "the Write tool is disabled for this session" and no file appeared; asked to
# run `touch` it answered that it has no shell tool and no file appeared; asked
# to read a file it returned the exact contents in 4.1 s over two turns. Reading
# still works, which matters -- an agent that cannot look anything up cannot
# answer a question about the build.
# `--tools` takes a VARIADIC value, so it must be written in the `=` form and
# never as two argv entries: `--tools Read,Grep,Glob "his sentence"` makes the
# argument parser swallow the sentence as another tool name, and claude exits
# with "Input must be provided either through stdin or as a prompt argument".
# That is a silent mute call, not a crash you would notice in a test.
READ_ONLY = ("--tools=Read,Grep,Glob", "--restricted", "--permission-prompts", "none")

OPEN_TIMEOUT = 180.0
TURN_TIMEOUT = 120.0

MANNERS = """You are on a LIVE PHONE CALL with Bogdan, speaking Serbian out loud. \
Your words go straight to a text-to-speech engine and into his ear.

RULES, all of them about being audible rather than readable:
- ONE short sentence. Two only if the second is genuinely necessary. He cannot
  skim a phone call, and anything past about six seconds of speech is too long
  to follow by ear. Never recite numbers, test counts or version strings unless
  he asks for them -- they are unlistenable and he can read them later.
- NO markdown, NO lists, NO code, NO URLs, NO file paths read out character by \
character. If you must name a file, say it the way a person would.
- Serbian, with proper diacritics (ć, č, š, ž, đ as the real characters) -- the \
TTS mispronounces stripped ASCII.
- You can LOOK THINGS UP -- read files and search them -- and nothing else. You \
cannot run commands and you cannot change anything, by design. If you need to \
look something up before answering, your FIRST words must say so: "Samo sekund, \
da proverim." Then look, then answer. Never go silent while you work.
- If he asks for something to be DONE rather than answered, do not promise to do \
it yourself and do not pretend it is done. Say it is being passed on -- "Vazi, \
prosledjujem" -- and answer anything you can answer from reading.
- Numbers and technical terms: say them as a person would speak them.
- If you did not understand him, say so and ask him to repeat.
- If he says goodbye, say a short goodbye back and nothing else.
- Do not mention that you are an AI, and do not narrate your own tooling."""


GUEST_MANNERS = """Na ŽIVOM SI TELEFONSKOM RAZGOVORU sa osobom koja se zove \
{callee}. Govoriš SRPSKI, naglas. Tvoje reči idu pravo u sintetizator govora i \
toj osobi u uho.

KO SI: zoveš u ime Bogdanove automatizacije. {callee} se sam prijavio -- ušao je \
na njegov server za kontakte i popunio formular, i to je dozvola na osnovu koje \
zoveš. U prvoj rečenici reci ko zove.

PRAVILA, sva su o tome da se TEBE ČUJE, a ne da te neko čita:
- JEDNA kratka rečenica. Dve samo ako je druga zaista neophodna. Telefonski \
razgovor se ne može preleteti pogledom, a sve preko šest sekundi govora je \
predugo da se isprati sluhom.
- BEZ markdowna, BEZ lista, BEZ koda, BEZ URL-ova, BEZ putanja koje se slovkaju.
- Srpski, sa pravim dijakritičkim znacima (ć, č, š, ž, đ kao prava slova) -- \
sintetizator pogrešno izgovara ogoljeni ASCII.
- NEMAŠ šta da proveriš. Znaš ono što ti piše u zadatku i ništa više. Ako te \
pitaju nešto van toga, reci otvoreno da ne znaš i da ćeš preneti pitanje. Nikada \
ne pogađaj i nikada ne izmišljaj detalj o Bogdanu, njegovom poslu ili njegovim \
sistemima.
- Gost si na tuđem telefonu. Ako kažu da im nije zgodno, izvini se, reci da ćeš \
preneti, i završi razgovor.
- Ako traže da se nešto URADI, ne obećavaj da ćeš ti to uraditi. Reci da \
prosleđuješ.
- Ako ih nisi razumeo, reci to i zamoli ih da ponove.
- Ako se pozdrave, kratko se pozdravi nazad i ništa više.
- Ne pričaj o sopstvenim alatima."""
"""Manners for a call to somebody who is NOT Bogdan.

Serbian, like `MANNERS`, and that is not only his preference (2026-09-19: *"Just
one thing speak serbian on the call not english"*). The stack is Serbian at both
ends: the TTS is a Serbian piper voice, which mangles English text, and the ASR
runs with a Serbian language hint, which transcribes English replies badly. The
first guest call went out in English and fought both.

What still differs from `MANNERS`, and each difference is load-bearing:

- **It says who is calling.** He knows why his own automation is ringing him.
  Somebody else does not, and a voice that opens without identifying itself is
  indistinguishable from a scam call.
- **It is told it can look nothing up.** `MANNERS` grants Read/Grep/Glob and
  says so. On a guest call the same tools point at HIS filesystem, and the model
  is talking to a third party -- so the tools are confined to an empty directory
  (see `GUEST_CWD`) and the manners are told not to try. Belt and braces, because
  either one alone fails quietly: a prompt rule can be talked around, and a
  confinement nobody mentions produces a voice that goes silent trying to read.
- **It is told it is a guest.** A bad moment on somebody else's phone ends the
  call; his own rules about when to reach him do not transfer to a colleague.
"""

GUEST_CWD = os.environ.get(
    "HOTLINE_IOS_GUEST_CWD", os.path.expanduser("~/.local/state/hotline-registry/callagent")
)
"""Working directory for a guest call agent, deliberately empty.

`--restricted` confines the file tools to the working directory, so pointing the
session at an empty directory is what actually stops a voice on a stranger's
phone from reading his home directory -- rather than the prompt rule above, which
is only the second line of defence. Created on demand by `guest_agent`."""


def guest_agent(callee: str, brief: str, *, model: str = "sonnet") -> "CallAgent":
    """A call agent for somebody in the registry, rather than for him.

    Deliberately does NOT go through `default_context`: that prepends
    `call_context.txt`, the standing briefing about his projects, which is his
    and not a third party's. A guest agent knows exactly what the calling agent
    chose to tell it and nothing more.
    """
    pathlib.Path(GUEST_CWD).mkdir(parents=True, exist_ok=True)
    return CallAgent(
        brief.strip() or "Nema posebnog konteksta za ovaj poziv.",
        model=model,
        cwd=GUEST_CWD,
        manners=GUEST_MANNERS.format(callee=callee or "them"),
        speaker=callee or "They",
    )


class CallAgent:
    """A Sonnet session, opened before the ring and resumed each turn."""

    def __init__(self, context: str = "", *, model: str = "sonnet",
                 cwd: str = DEFAULT_CWD, manners: str = MANNERS,
                 speaker: str = "Bogdan") -> None:
        self.context = context or "Nema posebnog konteksta."
        self.model = model
        self.cwd = cwd
        self.manners = manners
        # Whose sentence `reply` is relaying. The turn prompt used to name him
        # unconditionally, in Serbian, which on a guest call told the model the
        # stranger on the line was Bogdan.
        self.speaker = speaker
        self.session: str | None = None
        # Seeding costs ~4.7 s. On a ring that is paid WHILE the phone is
        # ringing rather than after he answers, so `reply` has to wait for it
        # rather than assume it -- without the seed there is no `--resume` and
        # the turn would open a second, unmannered session that answers in
        # markdown.
        self._ready = threading.Event()
        self.failed = ""

    def _run(self, prompt: str, timeout: float) -> str:
        command = ["claude", "-p", "--model", self.model, "--output-format", "json",
                   *READ_ONLY]
        if self.session:
            command += ["--resume", self.session]
        command.append(prompt)
        # check=False: a non-zero exit is reported with its stderr below, which
        # is what the caller can actually say out loud.
        # stdin=DEVNULL is not tidiness. `claude -p` READS STDIN as extra input
        # when there is any, and subprocess inherits the parent's. On
        # 2026-09-10 a test harness ran this from a shell heredoc and the call
        # agent silently answered the leftover heredoc text instead of his
        # sentence -- in English, about source code, mid-"phone call". Under
        # systemd stdin is already null, so the daemon never showed it; anything
        # else driving a call would.
        proc = subprocess.run(command, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL,
                              timeout=timeout, cwd=self.cwd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200] or "claude exited nonzero")
        payload = json.loads(proc.stdout)
        self.session = payload.get("session_id", self.session)
        return str(payload.get("result") or "").strip()

    def open(self) -> None:
        """Seed it before dialling, so no discovery happens mid-call."""
        began = time.monotonic()
        try:
            self._run(
                f"{self.manners}\n\nCONTEXT for this call:\n{self.context}\n\n"
                "Do not reply to this message with anything but the single word OK.",
                OPEN_TIMEOUT,
            )
            log.info("call agent ready in %.1fs (session %s)",
                     time.monotonic() - began, (self.session or "?")[:12])
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised here
            self.failed = f"{type(exc).__name__}: {exc}"
            log.error("call agent could not be seeded: %s", self.failed)
        finally:
            # Set either way. A `reply` blocked forever on a seed that already
            # failed is a silent phone line, which is strictly worse than an
            # apology spoken out loud.
            self._ready.set()

    def start(self) -> threading.Thread:
        """Seed it in the background, so the cost lands while the phone rings."""
        thread = threading.Thread(target=self.open, daemon=True, name="call-agent-open")
        thread.start()
        return thread

    def reply(self, heard: str) -> str:
        if not self._ready.wait(OPEN_TIMEOUT):
            raise RuntimeError("the call agent never finished opening")
        if self.failed:
            raise RuntimeError(self.failed)
        # Serbian for everybody: the guest manners are Serbian too, and a turn
        # prompt in a different language than the manners is how a model ends up
        # answering a Serbian speaker in English.
        return self._run(
            f'{self.speaker} je upravo rekao, preko telefona: "{heard}"', TURN_TIMEOUT)


def default_context(extra: str = "") -> str:
    """`call_context.txt` if it is there, plus whatever this call adds.

    The file is the standing "who you are and what happened lately" briefing;
    `extra` is the one thing that is true only of this call. Keeping them
    separate means a ring does not have to restate the former and the file does
    not have to know about rings."""
    where = pathlib.Path(__file__).resolve().parents[2] / "call_context.txt"
    try:
        standing = where.read_text().strip()
    except OSError:
        standing = ""
    return "\n\n".join(part for part in (standing, extra.strip()) if part)
