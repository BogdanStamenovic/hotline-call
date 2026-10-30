"""HaikuAgent: SSE parsing, and what goes into history when a turn is cut short."""

from __future__ import annotations

import io
import json

import pytest

from hotline_ios import haikuagent
from hotline_ios.haikuagent import HaikuAgent, HaikuUnavailable


def sse(*texts, stop=True):
    lines = [b"event: message_start", b'data: {"type":"message_start"}', b""]
    for t in texts:
        ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
        lines += [b"event: content_block_delta", b"data: " + json.dumps(ev).encode(), b""]
    if stop:
        lines += [b'data: {"type":"message_stop"}']
    return io.BytesIO(b"\n".join(lines) + b"\n")


@pytest.fixture
def sent(monkeypatch):
    bodies = []

    def fake_urlopen(request, timeout=None):
        bodies.append(json.loads(request.data))
        return sse("Dobro, ", "evo me.")

    monkeypatch.setattr(haikuagent.urllib.request, "urlopen", fake_urlopen)
    return bodies


def test_deltas_arrive_in_order_and_the_turn_is_remembered(sent):
    agent = HaikuAgent("ctx", key="k")
    assert list(agent.reply_stream("zdravo")) == ["Dobro, ", "evo me."]
    assert agent.history == [{"role": "user", "content": "zdravo"},
                             {"role": "assistant", "content": "Dobro, evo me."}]
    body = sent[0]
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is True
    assert body["model"] == haikuagent.DEFAULT_MODEL


def test_a_cut_short_reply_is_remembered_only_as_far_as_it_got(sent):
    agent = HaikuAgent("ctx", key="k")
    gen = agent.reply_stream("zdravo")
    assert next(gen) == "Dobro, "
    gen.close()
    assert agent.history[-1] == {"role": "assistant", "content": "Dobro, "}


def test_a_failed_turn_leaves_no_dangling_question(monkeypatch):
    def boom(request, timeout=None):
        raise OSError("down")

    monkeypatch.setattr(haikuagent.urllib.request, "urlopen", boom)
    agent = HaikuAgent("ctx", key="k")
    with pytest.raises(HaikuUnavailable):
        list(agent.reply_stream("zdravo"))
    assert agent.history == []


def test_no_key_is_reported_not_crashed(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(haikuagent, "KEY_FILE", str(tmp_path / "nope"))
    agent = HaikuAgent("ctx")
    assert agent.failed
    with pytest.raises(HaikuUnavailable):
        list(agent.reply_stream("x"))
