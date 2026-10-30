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
        ev = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}
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
                             {"role": "assistant",
                              "content": [{"type": "text", "text": "Dobro, evo me."}]}]
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


def sse_events(*events):
    lines = []
    for ev in events:
        lines += [b"data: " + json.dumps(ev).encode(), b""]
    return io.BytesIO(b"\n".join(lines) + b"\n")


def test_a_lookup_flushes_first_then_answers_from_the_result(monkeypatch, tmp_path):
    (tmp_path / "status.md").write_text("build: zelen\n")
    monkeypatch.setattr(haikuagent.calltools, "ROOT", tmp_path)
    rounds = [
        sse_events(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Samo sekund."}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '{"path": "status.md"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"}),
        sse_events(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Build je zelen."}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"}),
    ]
    bodies = []

    def fake(request, timeout=None):
        bodies.append(json.loads(request.data))
        return rounds.pop(0)

    monkeypatch.setattr(haikuagent.urllib.request, "urlopen", fake)
    agent = HaikuAgent("ctx", key="k")
    assert list(agent.reply_stream("kako stoji build?")) == ["Samo sekund.", None, "Build je zelen."]
    result = bodies[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "zelen" in result["content"]
    assert [m["role"] for m in agent.history] == ["user", "assistant", "user", "assistant"]


def test_tools_refuse_secrets_and_paths_outside_the_root(monkeypatch, tmp_path):
    from hotline_ios import calltools

    (tmp_path / ".env").write_text("API_KEY=x")
    (tmp_path / "notes.md").write_text("API_KEY=x")
    monkeypatch.setattr(calltools, "ROOT", tmp_path)
    assert calltools.run("read_file", {"path": ".env"}) [1] is True
    assert calltools.run("read_file", {"path": "/etc/passwd"})[1] is True
    assert calltools.run("read_file", {"path": "../x"})[1] is True
    text, failed = calltools.run("grep", {"pattern": "API_KEY"})
    assert not failed and ".env" not in text and "notes.md" in text
    assert ".env" not in calltools.run("glob", {"pattern": "*"})[0]


def test_git_log_answers_what_changed(monkeypatch, tmp_path):
    import subprocess

    from hotline_ios import calltools

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "Prvi commit"], check=True)
    monkeypatch.setattr(calltools, "ROOT", tmp_path.parent)
    text, failed = calltools.run("git_log", {"repo": tmp_path.name})
    assert not failed and "Prvi commit" in text
