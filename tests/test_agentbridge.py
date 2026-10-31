"""The agent bridge: "spoji me sa <agent>" puts a live hotline agent on the call.

hotline itself is faked (one object, `FakeHotline`); the call is not: the
end-to-end tests drive `AnsweredCall` over the real-socket phone from
test_conversation.py.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from hotline_ios import agentbridge, haikuagent
from hotline_ios.agentbridge import Agent, AgentBridge, BridgeError, speakable
from hotline_ios.haikuagent import HaikuAgent

from test_conversation import _voice_stream, dial, quiet, run, speech
from test_haikuagent import fake_pool, sse_events


class FakeHotline:
    def __init__(self, names=("tells-09", "manager-5", "manager-6", "hotline-80"),
                 busy=True, reply="Tu sam, radio sam na primovima.", delay=0.0):
        self.list = []
        for name in names:
            denied = ""
            if name in ("manager-6",):
                denied = "that agent is off limits to the call bridge"
            if name == "hotline-80":
                denied = "that is the operator; it is never interrupted by a call"
            self.list.append(Agent(name=name, session=SimpleNamespace(name=name),
                                   task=f"task of {name}", pane=f"{name}:0", denied=denied))
        self.is_busy, self.reply, self.delay = busy, reply, delay
        self.log: list[tuple] = []

    def agents(self):
        return list(self.list)

    def busy(self, agent):
        return self.is_busy

    def interrupt(self, agent):
        self.log.append(("interrupt", agent.name))

    def ask(self, agent, text, origin, timeout):
        self.log.append(("ask", agent.name, text, origin))
        time.sleep(self.delay)
        return self.reply(text) if callable(self.reply) else self.reply

    def tell(self, agent, text, origin):
        self.log.append(("tell", agent.name, text, origin))


def bridge(**kw):
    hot = kw.pop("hotline", None) or FakeHotline()
    return AgentBridge(hotline=hot, settle_seconds=0.0, **kw), hot


# -- names ---------------------------------------------------------------------


def test_a_spoken_name_finds_the_agent_with_spaces_for_hyphens():
    b, _ = bridge()
    assert b.find("tells 09").name == "tells-09"
    assert b.find("Manager 5.").name == "manager-5"
    assert b.find("tells").name == "tells-09"


@pytest.mark.parametrize("name", ["manager-6", "hotline 80"])
def test_the_operator_and_the_deploying_manager_are_never_connected(name):
    b, hot = bridge()
    with pytest.raises(BridgeError, match="cannot be connected"):
        b.connect(name)
    assert not [e for e in hot.log if e[0] in ("interrupt", "ask")]


def test_an_ambiguous_name_is_asked_about_not_guessed():
    b, hot = bridge()
    with pytest.raises(BridgeError, match="several"):
        b.connect("manager")
    assert hot.log == []


def test_an_unknown_name_says_who_is_live():
    b, _ = bridge()
    with pytest.raises(BridgeError) as err:
        b.find("kiri")
    assert "tells-09" in str(err.value) and "hotline-80" not in str(err.value)


def test_the_real_deny_list_defaults_to_the_operator_and_manager_6(monkeypatch):
    monkeypatch.delenv("HOTLINE_IOS_BRIDGE_DENY", raising=False)
    assert set(agentbridge.Hotline().deny) == {"manager-6", "hotline-80"}
    monkeypatch.setenv("HOTLINE_IOS_BRIDGE_DENY", "a, B")
    assert agentbridge.Hotline().deny == ("a", "b")


# -- connecting and hanging up ---------------------------------------------------


def test_a_busy_agent_is_interrupted_then_told_it_is_on_a_live_call():
    b, hot = bridge()
    first = b.connect("tells", "spoji me sa tells")
    assert first == "Tu sam, radio sam na primovima."
    assert [e[0] for e in hot.log] == ["interrupt", "ask"]
    text = hot.log[1][2]
    assert "THIS IS A LIVE CALL WITH BOGDAN" in text
    assert "DROP WHATEVER YOU ARE DOING" in text and "UNTIL HANGUP" in text
    assert "spoji me sa tells" in text
    assert "interrupted" in text
    assert b.connected and b.was_busy


def test_an_idle_agent_is_not_sent_an_escape():
    b, hot = bridge(hotline=FakeHotline(busy=False))
    b.connect("tells")
    assert [e[0] for e in hot.log] == ["ask"]
    assert "interrupted (Escape)" not in hot.log[0][2]


def test_hanging_up_tells_the_agent_to_go_back_to_its_work_once():
    b, hot = bridge()
    b.connect("tells")
    b.release("he said goodbye")
    b.release("again")
    tells = [e for e in hot.log if e[0] == "tell"]
    assert len(tells) == 1
    assert "CALL ENDED" in tells[0][2] and "Resume what you were doing" in tells[0][2]
    assert not b.connected


def test_an_agent_that_was_idle_is_not_told_to_resume_anything():
    b, hot = bridge(hotline=FakeHotline(busy=False))
    b.connect("tells")
    b.release()
    assert "nothing to resume" in hot.log[-1][2]


def test_connecting_a_second_agent_releases_the_first():
    b, hot = bridge()
    b.connect("tells")
    b.connect("manager 5")
    assert ("tell", "tells-09") == hot.log[2][:2]
    assert b.agent.name == "manager-5"


def test_an_agent_slow_to_answer_is_still_connected_and_said_so():
    hot = FakeHotline()

    def slow(*_a):
        raise TimeoutError("no reply in 40 s")

    hot.ask = slow
    b, _ = bridge(hotline=hot)
    assert "not answered yet" in b.connect("tells")
    assert b.connected


# -- turns -----------------------------------------------------------------------


def test_direct_mode_speaks_the_agent_s_reply_and_keeps_the_line_alive():
    b, hot = bridge(hotline=FakeHotline(delay=0.35), keepalive_seconds=0.1)
    b.agent = hot.list[0]
    out = list(b.direct_stream("kako ide"))
    assert out[-1] == "Tu sam, radio sam na primovima."
    assert any(isinstance(x, str) and "radi" in x.lower() or x == "I dalje gleda. "
               for x in out[:-1])
    assert None in out
    assert "kako ide" in hot.log[-1][2]
    assert b.turns[-1].said == "kako ide" and b.turns[-1].seconds >= 0.3


def test_relay_mode_hands_the_agent_s_words_to_haiku_without_lookups():
    b, hot = bridge(mode="relay")
    b.agent = hot.list[0]
    calls = []

    class Haiku:
        def reply_stream(self, text, lookups=True):
            calls.append((text, lookups))
            yield "Kaže da je tu."

    assert list(b.relay_stream("kako ide", Haiku())) == ["Kaže da je tu."]
    note, lookups = calls[0]
    assert lookups is False
    assert "kako ide" in note and "Tu sam, radio sam na primovima." in note
    assert "Ne dodaj ništa" in note and "prvom licu" in note


def test_routing_is_haiku_until_connected_then_the_agent_then_haiku_again():
    b, hot = bridge()

    class Haiku:
        heard: list = []

        def reply_stream(self, text, lookups=True):
            self.heard.append(text)
            yield "haiku"

    haiku = Haiku()
    ask = b.stream_for(haiku)
    assert list(ask("zdravo")) == ["haiku"]
    b.connect("tells")
    assert list(ask("kako ide"))[-1] == "Tu sam, radio sam na primovima."
    assert list(ask("dobro, prekini vezu sa njim")) == ["haiku"]
    assert not b.connected
    assert "CALL ENDED" in hot.log[-1][2]
    assert "isključen" in haiku.heard[-1]


def test_no_speculation_while_an_agent_is_on_the_line_or_being_asked_for():
    b, hot = bridge()
    assert b.may_speculate("kakvo je vreme")
    assert not b.may_speculate("spoji me sa tells")
    assert not b.may_speculate("add tells to the call")
    b.agent = hot.list[0]
    assert not b.may_speculate("kakvo je vreme")


def test_what_is_spoken_has_no_markup_paths_or_links():
    raw = ("## Status\n- **Build** je `zelen`.\n- Log je u /home/bodas/data/x/y.log\n"
           "```\nrm -rf /\n```\nVidi https://example.com/a.\n1. Prvo.")
    out = speakable(raw)
    for junk in ("#", "**", "`", "/home", "https", "rm -rf"):
        assert junk not in out, out
    assert "Build je zelen." in out and "Prvo." in out
    assert agentbridge.markdown_leaks(raw) == ["code", "bold", "heading", "list", "url", "path"]


def test_a_long_reply_is_cut_at_a_sentence_and_says_so():
    out = speakable("Ovo je rečenica. " * 100, limit=120)
    assert len(out) < 170 and out.endswith("ovo je početak.")


# -- Haiku's tools ---------------------------------------------------------------


def _tool_round(name, args):
    import json
    return sse_events(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Samo sekund, povezujem."}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t1", "name": name, "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"})


def _end_round(text):
    return sse_events(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"})


def test_haiku_connects_an_agent_through_its_tool(monkeypatch):
    rounds = [_tool_round("connect_agent", {"name": "tells-09", "his_words": "spoji me sa tells"}),
              _end_round("Povezan si sa tells.")]
    bodies = fake_pool(monkeypatch, lambda: rounds.pop(0))
    b, hot = bridge()
    agent = HaikuAgent("ctx", key="k", extra_tools=b.tools())
    out = [d for d in agent.reply_stream("spoji me sa tells") if d]
    assert out == ["Samo sekund, povezujem.", "Povezan si sa tells."]
    assert b.connected and hot.log[0] == ("interrupt", "tells-09")
    names = [t["name"] for t in bodies[0]["tools"]]
    assert "connect_agent" in names and "read_file" in names
    result = bodies[1]["messages"][-1]["content"][0]
    assert "CONNECTED to tells-09" in result["content"] and not result["is_error"]


def test_a_speculative_fork_cannot_connect_anyone(monkeypatch):
    rounds = [_tool_round("connect_agent", {"name": "tells-09"}), _end_round("Hm.")]
    bodies = fake_pool(monkeypatch, lambda: rounds.pop(0))
    b, hot = bridge()
    twin = HaikuAgent("ctx", key="k", extra_tools=b.tools()).fork()
    list(twin.reply_stream("spoji me sa tells"))
    assert hot.log == [] and not b.connected
    assert bodies[1]["messages"][-1]["content"][0]["is_error"] is True
    # Same tool list as the real agent, so the prompt cache is shared.
    assert [t["name"] for t in bodies[0]["tools"]][-2:] == ["list_agents", "connect_agent"]


def test_relay_turns_go_to_haiku_without_tools(monkeypatch):
    bodies = fake_pool(monkeypatch, lambda: _end_round("Kaže da je tu."))
    agent = HaikuAgent("ctx", key="k")
    list(agent.reply_stream("x", lookups=False))
    assert "tools" not in bodies[0]


# -- on a call, over real sockets --------------------------------------------------


def test_on_a_call_his_turn_reaches_the_agent_and_the_hangup_releases_it():
    b, hot = bridge(hotline=FakeHotline(reply="Radim na primovima, sve je u redu."))
    b.agent, b.was_busy = hot.list[0], True

    class Haiku:
        def reply_stream(self, text, lookups=True):
            yield "haiku should not answer"

    spoken: list[str] = []
    handler, phone, reply = dial(
        ask_stream=b.stream_for(Haiku()), speak_stream=_voice_stream(spoken),
        may_speculate=b.may_speculate, on_end=b.release,
        answer_seconds=lambda: 5.0, call_seconds=4.0)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert "Radim na primovima, sve je u redu." in spoken, spoken
    assert "haiku should not answer" not in " ".join(spoken)
    asks = [e for e in hot.log if e[0] == "ask"]
    assert asks and "da samo napred" in asks[0][2]
    assert hot.log[-1][0] == "tell" and "CALL ENDED" in hot.log[-1][2]
    assert not b.connected


def test_may_speculate_false_means_no_speculation_is_started():
    from test_conversation import _forking, _token_stream

    asked, committed, spoken = [], [], []
    handler, phone, reply = dial(
        ask_stream=_token_stream("Važi."), speak_stream=_voice_stream(spoken),
        ask_fork=_forking(asked, committed), may_speculate=lambda text: False)
    phone.say_after(0.05, quiet(1.4))
    phone.say_after(1.7, np.concatenate([speech(1.0), quiet(1.6)]))
    thread = run(handler, phone, reply)
    thread.join(timeout=20)
    phone.stop()
    assert handler.speculation["started"] == 0 and asked == []
    assert "Važi." in " ".join(spoken)
