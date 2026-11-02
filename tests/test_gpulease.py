"""The call's GPU lease against a fake gpu-share: begin/renew/end, no broker,
an expired lease, and a broker that is down - none of which may cost the call."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotline_call import gpulease
from hotline_call.gpulease import GpuLease


@pytest.fixture
def broker():
    seen, state = [], {"present": True, "expired": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            seen.append((self.path, body))
            if not state["present"]:
                self.send_response(404); self.end_headers(); return
            if self.path.endswith("/renew") and state["expired"]:
                state["expired"] = False
                self.send_response(404); self.end_headers(); return
            reply = {"lease": f"L{len(seen)}", "ttl_s": 90, "evicting": False} \
                if self.path.endswith("/begin") else {"ok": True}
            data = json.dumps(reply).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen, state
    server.shutdown()


def test_begin_then_end(broker):
    url, seen, _ = broker
    lease = GpuLease(url, call_id="c1")
    assert lease.begin() and lease.lease == "L1"
    lease.end()
    assert [p for p, _ in seen] == ["/gpu-share/call/begin", "/gpu-share/call/end"]
    assert seen[0][1] == {"id": "c1"} and seen[1][1] == {"lease": "L1"}


def test_no_broker_is_not_an_error(broker):
    url, seen, state = broker
    state["present"] = False
    lease = GpuLease(url)
    assert lease.begin() is False
    lease.end()                                    # nothing to release, nothing raised
    assert [p for p, _ in seen] == ["/gpu-share/call/begin"]


def test_an_expired_lease_is_taken_again(broker, monkeypatch):
    url, seen, state = broker
    monkeypatch.setattr(gpulease, "RENEW_EVERY", 0.05)
    state["expired"] = True
    lease = GpuLease(url)
    lease.begin()
    import time
    for _ in range(100):
        if sum(1 for p, _ in seen if p.endswith("/begin")) >= 2:
            break
        time.sleep(0.02)
    assert sum(1 for p, _ in seen if p.endswith("/begin")) >= 2
    lease.end()


def test_a_broker_that_is_down_costs_nothing():
    lease = GpuLease("http://127.0.0.1:9")         # discard port: connection refused
    assert lease.begin() is False
