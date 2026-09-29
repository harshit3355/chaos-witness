"""Fault-injecting HTTP proxy: one listener per dependency, one scheduled fault, a request log as evidence.

The log is written by the proxy, never by the system under test, so it is the witness that a fault
was actually delivered (\"triggered\") and what the caller did next.
"""
from __future__ import annotations

import http.client
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fault parameters are scaled down so a campaign runs in seconds; see README "Limitations".
LATENCY_S = 1.5       # `latency`: wait this long before forwarding
HOLD_S = 1.2          # `timeout`: forward, then hold the response this long
RETRY_AFTER_S = 0.3   # `http_429`: fractional Retry-After (real servers send integer seconds)

KINDS = {
    "latency": "delay the request before forwarding it",
    "http_500": "answer 500 without forwarding",
    "http_429": f"answer 429 with Retry-After: {RETRY_AFTER_S} without forwarding",
    "reset": "close the connection without forwarding or answering",
    "timeout": "forward, then hold the response past the caller's timeout",
    "drop_response": "forward (the side effect happens), then close without answering",
    "duplicate": "deliver the request to the upstream twice",
    "truncate": "forward, then return only the first half of the body",
    "garble": "forward, then return the body with its quotes corrupted (invalid JSON)",
}
SCHEDULES = {"first": lambda n: n == 1, "all": lambda n: True, "from2": lambda n: n >= 2}
PASS_HEADERS = ("Content-Type", "Authorization", "Idempotency-Key", "X-Run-Id")


@dataclass
class Fault:
    kind: str
    schedule: str  # key of SCHEDULES, applied to this proxy's 1-based request count

    def __post_init__(self):
        if self.kind not in KINDS or self.schedule not in SCHEDULES:
            raise ValueError(f"unknown fault {self.kind}/{self.schedule}")


class FaultProxy:
    def __init__(self, dep: str, upstream: tuple[str, int], fault: Fault | None = None):
        self.dep, self.upstream, self.fault = dep, upstream, fault
        self.log: list[dict] = []
        self._lock = threading.Lock()
        self._inflight = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                proxy._handle(self)

            do_POST = do_PUT = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def _forward(self, h, body: bytes) -> tuple[int, dict, bytes]:
        c = http.client.HTTPConnection(*self.upstream, timeout=10)
        try:
            c.request(h.command, h.path, body=body or None,
                      headers={k: h.headers[k] for k in PASS_HEADERS if h.headers.get(k)})
            r = c.getresponse()
            return r.status, {k: v for k, v in r.getheaders() if k.lower() == "content-type"}, r.read()
        finally:
            c.close()

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        with self._lock:
            self._inflight += 1
            seq = len(self.log) + 1
            f = self.fault if self.fault and SCHEDULES[self.fault.schedule](seq) else None
            rec = {"dep": self.dep, "seq": seq, "run": h.headers.get("X-Run-Id"), "path": h.path.split("?")[0],
                   "fault": f.kind if f else None, "forwarded": 0, "t_recv": time.monotonic(),
                   "t_done": None, "status": None}
            self.log.append(rec)
        try:
            self._serve(h, body, f.kind if f else None, rec)
        except OSError:  # the caller gave up; the evidence record already says what we did
            pass
        finally:
            rec["t_done"] = time.monotonic()
            with self._lock:
                self._inflight -= 1

    def _serve(self, h, body: bytes, kind: str | None, rec: dict) -> None:
        def reply(status, headers, payload):
            rec["status"] = status
            h.send_response(status)
            for k, v in headers.items():
                h.send_header(k, v)
            h.send_header("Content-Length", str(len(payload)))
            h.end_headers()
            h.wfile.write(payload)

        if kind == "reset":
            h.close_connection = True
            return
        if kind == "http_500":
            return reply(500, {}, b"injected")
        if kind == "http_429":
            return reply(429, {"Retry-After": str(RETRY_AFTER_S)}, b"injected")
        if kind == "latency":
            time.sleep(LATENCY_S)
        status, headers, payload = self._forward(h, body)
        rec["forwarded"] = 1
        if kind == "duplicate":
            status, headers, payload = self._forward(h, body)
            rec["forwarded"] = 2
        if kind == "drop_response":
            h.close_connection = True
            return
        if kind == "timeout":
            time.sleep(HOLD_S)
        if kind == "truncate":
            payload = payload[: len(payload) // 2]
        if kind == "garble":
            payload = payload.replace(b'"', b"'")
        reply(status, headers, payload)

    def drain(self, timeout: float = 10.0) -> None:
        """Wait for held/delayed requests, so late (ghost) deliveries are always in the evidence."""
        end = time.monotonic() + timeout
        while self._inflight and time.monotonic() < end:
            time.sleep(0.01)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
