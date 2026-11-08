"""A browser call, with the page's server played by a socket in this test."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from hotline_call.ring.webcall import WebCall, connect_first

FRAME = bytes(640)


@pytest.fixture(autouse=True)
def _runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


class Leg:
    def __init__(self, body=None):
        self.body = body or (lambda call: None)
        self.held = threading.Event()
        self.abandoned = ""

    def hold(self, call):
        self.body(call)
        self.held.set()

    def abandon(self, why):
        self.abandoned = why


def test_audio_flows_both_ways_and_closing_the_socket_ends_the_call():
    heard = []
    started = threading.Event()

    def body(call):
        started.set()
        deadline = time.monotonic() + 5
        while not call.pump.eof and time.monotonic() < deadline:
            time.sleep(0.01)
        heard.append(call.pump.frames_received)

    call = WebCall()
    leg = Leg(body)
    call.start(leg)
    page = socket.socket(socket.AF_UNIX)
    page.connect(str(call.path))
    assert started.wait(2)
    for _ in range(10):
        page.sendall(FRAME)
    page.settimeout(2)
    assert page.recv(4096), "the pump's clock should send audio back"
    time.sleep(0.1)
    page.close()
    assert leg.held.wait(3)
    assert heard[0] == 10
    deadline = time.monotonic() + 2
    while not call.far_end_hung_up() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert call.far_end_hung_up()
    while call.path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not call.path.exists()


def test_nobody_connecting_abandons_without_touching_the_agent():
    began = []
    call = WebCall(agent="manager-14")
    call.on_media = lambda: began.append(True)
    leg = Leg()
    call.start(leg, accept_within=0.05).join(2)
    assert "never connected" in leg.abandoned
    assert not began, "an agent must not be told about a call that never had media"
    assert not call.path.exists()


def test_the_agent_is_connected_when_media_starts_before_the_conversation():
    order = []
    call = WebCall(agent="manager-14")
    call.on_media = lambda: order.append("connect")
    leg = Leg(lambda c: order.append("hold"))
    call.start(leg)
    page = socket.socket(socket.AF_UNIX)
    page.connect(str(call.path))
    assert leg.held.wait(2)
    page.close()
    assert order == ["connect", "hold"]


class Bridge:
    def __init__(self, gate=None, error=None):
        self.gate, self.error = gate, error
        self.connected_to = []

    def connect(self, name, asked):
        if self.gate:
            self.gate.wait(2)
        if self.error:
            raise self.error
        self.connected_to.append((name, asked))


def test_his_first_turn_waits_for_the_connect():
    gate = threading.Event()
    bridge = Bridge(gate)
    got = []
    ask, begin = connect_first(bridge, "manager-14", lambda said: iter([f"reply to {said}"]), "asked", got.append)
    begin()
    stream = ask("zdravo")
    assert next(stream) is None, "still connecting: only keepalive"
    gate.set()
    assert [x for x in stream if x] == ["reply to zdravo"]
    assert bridge.connected_to == [("manager-14", "asked")]
    assert got == []


def test_a_failed_connect_is_reported_and_the_voice_keeps_the_call():
    reports = []
    bridge = Bridge(error=RuntimeError("no live agent"))
    ask, begin = connect_first(bridge, "manager-14", lambda said: iter(["haiku answers"]), "", reports.append)
    begin()
    assert [x for x in ask("zdravo") if x] == ["haiku answers"]
    assert "no live agent" in reports[0]


def test_inbound_web_call_reaches_the_same_listener_contract():
    call = WebCall(agent="x", asked="y")
    assert call.caller == "web"
    assert call.call_seconds > 1800, "a working session must outlast a phone call's cap"
    assert call.path.name.startswith("web-")
    assert not call.far_end_hung_up()
    call.close()
    assert call.far_end_hung_up()
