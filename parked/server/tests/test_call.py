"""The call orchestrator, driven end to end through the loopback transport.

These use stand-in transcriber/speaker/pool objects rather than the real
whisper/piper/SessionPool, so they run without a GPU and without spawning a
Claude session. What is NOT stubbed is the part being tested: real PCM through a
real QueueStream with real framing, real barge-in arithmetic, real turn
sequencing.
"""

import asyncio
from dataclasses import dataclass

import numpy as np
import pytest

from hotline_ios.call import CallSession, TurnEvent
from hotline_ios.ring.base import AudioFormat, CallDeclined, CallTarget, CallUnanswered
from hotline_ios.ring.loopback import LoopbackTransport

FMT = AudioFormat(rate=16_000, channels=1, frame_ms=20)


@dataclass
class Utterance:
    audio: np.ndarray
    seconds: float


class OneShotSegmenter:
    """Emits one utterance after it has been fed `after` frames, then nothing."""

    def __init__(self, after: int = 1):
        self.after = after
        self.seen = 0

    def feed(self, mono):
        self.seen += 1
        if self.seen == self.after:
            yield Utterance(audio=mono, seconds=1.0)


class FakeTranscriber:
    def __init__(self, text="what is the status"):
        self.text = text
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text


class FakeSpeaker:
    rate = 16_000

    def __init__(self):
        self.said = []

    def synthesize(self, text):
        self.said.append(text)
        # 0.5 s of quiet audio -- enough to be several frames.
        return np.zeros(8_000, dtype=np.float32)


@dataclass
class Reply:
    text: str
    notice: str = ""


class FakePool:
    def __init__(self, answer="all good", delay=0.0, events=()):
        self.answer = answer
        self.delay = delay
        self.events = list(events)
        self.asked = []

    async def ask(self, key, text, narrator=None, timeout=None, origin=None):
        self.asked.append((key, text))
        for event in self.events:
            if narrator:
                narrator(event)
        if self.delay:
            await asyncio.sleep(self.delay)
        return ("fresh", Reply(self.answer))


@dataclass
class Event:
    kind: str
    detail: str
    tool: str | None = None


def make_session(pool=None, transcriber=None, speaker=None, stream=None, **kw):
    kw.setdefault("segmenter_factory", lambda: OneShotSegmenter())
    return CallSession(
        pool=pool or FakePool(),
        transcriber=transcriber or FakeTranscriber(),
        speaker=speaker or FakeSpeaker(),
        stream=stream,
        target=CallTarget(device="phone", agent="hotline-80"),
        key="ios-test",
        **kw,
    )


async def settle(session, turns=1, timeout=5.0):
    """Wait for `turns` completed turns rather than for a fixed sleep.

    A sleep long enough to be reliable here is also long enough to make the
    suite slow, and a sleep short enough to be fast is flaky. Waiting on the
    transcript is neither.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while len(session.transcript) < turns * 2:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"only {session.transcript} after {timeout}s")
        await asyncio.sleep(0.01)


async def test_a_full_turn_runs_through_the_loopback():
    transport = LoopbackTransport(FMT)
    await transport.start()
    stream = await transport.ring(CallTarget(device="phone"))
    pool = FakePool(answer="three failures in the auth module")
    speaker = FakeSpeaker()
    session = make_session(pool=pool, speaker=speaker, stream=stream)

    task = asyncio.create_task(session.run())
    stream.feed(b"\x10\x00" * 320)          # one frame of non-silent audio
    await settle(session)

    assert pool.asked == [("ios-test", "what is the status")]
    assert session.transcript == [
        ("you", "what is the status"),
        ("claude", "three failures in the auth module"),
    ]
    assert "three failures in the auth module" in speaker.said
    # And the synthesised answer actually reached the wire. Checked BEFORE the
    # hangup, because closing deliberately discards queued speech -- an earlier
    # version of this test closed first and then asserted on an empty queue.
    assert stream.pending_seconds > 0
    played = stream.take()
    assert played is not None and len(played) == stream.format.frame_bytes

    await stream.close()
    await asyncio.wait_for(task, timeout=5)


async def test_tool_events_reach_the_phone_even_when_not_spoken():
    # SPEC §5 wants the tool Claude is running shown visually. Speech is
    # throttled; the screen is not. This is the test that says so.
    events = [Event("tool", "Reading the nginx config", "Read"),
              Event("tool", "Running the test suite", "Bash")]
    transport = LoopbackTransport(FMT)
    stream = await transport.ring(CallTarget(device="phone"))
    seen: list[TurnEvent] = []
    session = make_session(pool=FakePool(events=events), stream=stream,
                           on_event=seen.append)

    task = asyncio.create_task(session.run())
    stream.feed(b"\x10\x00" * 320)
    await settle(session)
    await stream.close()
    await asyncio.wait_for(task, timeout=5)

    tools = [e for e in seen if e.kind == "tool"]
    assert [e.text for e in tools] == ["Reading the nginx config", "Running the test suite"]
    assert [e.tool for e in tools] == ["Read", "Bash"]
    # Neither was spoken: both arrived inside NARRATE_AFTER.
    assert "Reading the nginx config" not in session._speakable("")


async def test_second_utterance_during_a_turn_is_refused_not_queued():
    transport = LoopbackTransport(FMT)
    stream = await transport.ring(CallTarget(device="phone"))
    speaker = FakeSpeaker()
    pool = FakePool(delay=0.3)
    session = make_session(pool=pool, speaker=speaker, stream=stream)
    # Drive _turn directly so the test is about turn sequencing rather than
    # about segmentation.
    task = asyncio.create_task(session.run())
    first = asyncio.create_task(session._turn(np.zeros(16_000, dtype=np.float32), 1.0))
    await asyncio.sleep(0.05)
    await session._turn(np.zeros(16_000, dtype=np.float32), 1.0)
    await first
    await stream.close()
    await asyncio.wait_for(task, timeout=5)

    assert len(pool.asked) == 1
    assert "Hang on, still working on the last one." in speaker.said


async def test_barge_in_drops_queued_speech():
    transport = LoopbackTransport(FMT)
    stream = await transport.ring(CallTarget(device="phone"))
    session = make_session(stream=stream, segmenter_factory=lambda: OneShotSegmenter(after=99))

    stream.send(b"\x00\x00" * 16_000)   # a second of queued outbound speech
    assert stream.pending_seconds >= 0.9

    task = asyncio.create_task(session.run())
    loud = (np.ones(320, dtype=np.float32) * 0.5)
    stream.feed((loud * 32767).astype("<i2").tobytes())
    await asyncio.sleep(0.05)
    assert stream.pending_seconds == 0.0
    await stream.close()
    await asyncio.wait_for(task, timeout=5)


async def test_a_failing_turn_is_reported_not_swallowed():
    class Angry(FakePool):
        async def ask(self, *a, **kw):
            raise RuntimeError("session is gone")

    transport = LoopbackTransport(FMT)
    stream = await transport.ring(CallTarget(device="phone"))
    speaker = FakeSpeaker()
    session = make_session(pool=Angry(), speaker=speaker, stream=stream)
    task = asyncio.create_task(session.run())
    stream.feed(b"\x10\x00" * 320)
    await settle(session)
    await stream.close()
    await asyncio.wait_for(task, timeout=5)

    assert any("RuntimeError" in s for s in speaker.said)
    assert any(e.kind == "error" for e in session.events)


async def test_a_broken_event_subscriber_does_not_kill_the_call():
    def explode(event):
        raise ValueError("the phone went away")

    transport = LoopbackTransport(FMT)
    stream = await transport.ring(CallTarget(device="phone"))
    session = make_session(stream=stream, on_event=explode)
    task = asyncio.create_task(session.run())
    stream.feed(b"\x10\x00" * 320)
    await settle(session)
    await stream.close()
    await asyncio.wait_for(task, timeout=5)
    assert session.transcript  # the turn still completed


async def test_declined_and_unanswered_are_different_answers():
    declined = LoopbackTransport(FMT, decline=True)
    with pytest.raises(CallDeclined):
        await declined.ring(CallTarget(device="phone"))

    # An explicit short timeout: ring() defaults to 45 s and the loopback now
    # actually waits it out, because ringing-out and never-ringing have to be
    # distinguishable rather than collapsing into one instant exception.
    rang_out = LoopbackTransport(FMT, answer=False)
    with pytest.raises(CallUnanswered):
        await rang_out.ring(CallTarget(device="phone"), timeout=0.05)
