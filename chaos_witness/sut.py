"""System under test (synthetic): an agent orchestrator in front of a mock LLM, tool, identity and store.

`Mocks` is the dependency side and keeps the evidence (issued tokens, side-effect ledger, store).
`Orchestrator` is the code under test. It never writes evidence; it only has knobs, and each
weakness in WEAKNESSES flips one knob of the hardened configuration.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HARDENED = {
    "max_attempts": 3,            # per dependency call
    "honor_retry_after": True,
    "idempotency_key": True,      # send the run id as Idempotency-Key to the tool
    "token_cache": "per_tenant",  # per_tenant: fail closed without a token for this tenant
    "on_bad_plan": "fail",        # unparseable LLM output is retried, then the run fails
    "call_timeout": 0.25,         # seconds per attempt
    "deadline": 0.6,              # seconds per run; None disables
    "on_store_error": "fail",     # never acknowledge a result that was not stored
}
# weakness -> (knob overrides, the obligation it should violate). The pairing is ground truth for the
# benchmark only; the campaign generator never sees it.
WEAKNESSES = {
    "unbounded_retries": ({"max_attempts": 8}, "bounded_retry"),
    "ignores_retry_after": ({"honor_retry_after": False}, "honor_retry_after"),
    "no_idempotency_key": ({"idempotency_key": False}, "no_duplicate_side_effect"),
    "global_token_fallback": ({"token_cache": "global_fallback"}, "tenant_isolation"),
    "fail_open_on_bad_plan": ({"on_bad_plan": "default_plan"}, "fail_closed_on_bad_llm_output"),
    "no_deadline": ({"call_timeout": 3.0, "deadline": None}, "bounded_latency"),
    "acks_before_durable": ({"on_store_error": "ignore"}, "ack_implies_durable"),
}
DEPS = ("llm", "tool", "identity", "store")


def variant(name: str) -> dict:
    if name == "hardened":
        return dict(HARDENED)
    if name == "all_weak":
        return {**HARDENED, **{k: v for o, _ in WEAKNESSES.values() for k, v in o.items()}}
    return {**HARDENED, **WEAKNESSES[name][0]}


def _server(handle) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            status, out = handle(self.command, self.path, dict(self.headers), body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = do_PUT = do_GET

    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    s.daemon_threads, s.block_on_close = True, False
    threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True).start()
    return s


class Mocks:
    """Mock LLM, tool (irreversible side effect), identity and store behind one listener."""

    def __init__(self):
        self.tokens: dict[str, str] = {}   # token -> tenant it was issued to
        self.ledger: list[dict] = []       # executed side effects
        self.store: dict[str, dict] = {}
        self._keys: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.server = _server(self.handle)
        self.addr = self.server.server_address

    def handle(self, method, path, headers, body):
        url = urlsplit(path)
        tenant_of = lambda: self.tokens.get(headers.get("Authorization", "").removeprefix("Bearer "))
        with self._lock:
            if url.path == "/token":
                tenant = parse_qs(url.query)["tenant"][0]
                tok = f"tok-{tenant}-{len(self.tokens) + 1}"
                self.tokens[tok] = tenant
                return 200, {"token": tok, "tenant": tenant}
            if url.path == "/v1/chat":
                return 200, {"model": "mock-llm", "plan": {"tool": "refund", "amount": 10 + len(body["task"]) % 7}}
            if url.path == "/tools/refund":
                owner = tenant_of()
                if owner is None:
                    return 401, {"error": "unknown token"}
                key = headers.get("Idempotency-Key")
                if key and key in self._keys:
                    return 200, self._keys[key]
                # the refund is booked against the token's tenant: a reused token moves money across tenants
                self.ledger.append({"run": headers.get("X-Run-Id"), "tenant": body["tenant"], "token_tenant": owner,
                                    "amount": body["amount"], "key": key})
                out = {"refund_id": len(self.ledger)}
                if key:
                    self._keys[key] = out
                return 200, out
            if url.path.startswith("/store/"):
                owner = tenant_of()
                if owner is None:
                    return 401, {"error": "unknown token"}
                self.store[url.path.removeprefix("/store/")] = {"tenant": body["tenant"], "token_tenant": owner}
                return 200, {"ok": True}
        return 404, {"error": "not found"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DeadlineExceeded(Exception):
    pass


class DependencyFailed(Exception):
    pass


class Orchestrator:
    """POST /run {tenant, run_id, task}: token -> LLM plan -> tool side effect -> store -> 200."""

    DEFAULT_PLAN = {"tool": "refund", "amount": 100}

    def __init__(self, knobs: dict, ports: dict[str, int]):
        self.k, self.ports = knobs, ports
        self._tokens: dict[str, str] = {}
        self._last_token: str | None = None
        self.server = _server(self.handle)
        self.port = self.server.server_address[1]

    def _call(self, dep, method, path, body, run_id, end, headers=None, parse=True):
        k = self.k
        for attempt in range(k["max_attempts"]):
            timeout = k["call_timeout"]
            if end is not None:
                timeout = min(timeout, end - time.monotonic())
                if timeout <= 0:
                    raise DeadlineExceeded(dep)
            wait = 0.02
            req = urllib.request.Request(f"http://127.0.0.1:{self.ports[dep]}{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "X-Run-Id": run_id,
                                                  **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                return json.loads(raw) if parse else raw
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    raise DependencyFailed(f"{dep} {e.code}")
                if e.code == 429 and k["honor_retry_after"]:
                    wait = float(e.headers.get("Retry-After") or wait)
            except (OSError, http.client.HTTPException, ValueError):  # ValueError: body is not JSON
                pass
            if attempt + 1 < k["max_attempts"]:
                if end is not None and time.monotonic() + wait >= end:
                    raise DeadlineExceeded(dep)
                time.sleep(wait)
        raise DependencyFailed(dep)

    def _token(self, tenant, run_id, end):
        if self.k["token_cache"] == "per_tenant" and tenant in self._tokens:
            return self._tokens[tenant]
        try:
            tok = self._call("identity", "GET", f"/token?tenant={tenant}", None, run_id, end)["token"]
        except (DependencyFailed, KeyError, TypeError):
            if self.k["token_cache"] == "global_fallback" and self._last_token:
                return self._last_token  # "keep serving during an IdP outage" - with whoever's token is cached
            raise
        self._tokens[tenant] = self._last_token = tok
        return tok

    def _plan(self, tenant, task, run_id, end):
        if self.k["on_bad_plan"] == "fail":
            plan = self._call("llm", "POST", "/v1/chat", {"tenant": tenant, "task": task}, run_id, end)["plan"]
        else:
            raw = self._call("llm", "POST", "/v1/chat", {"tenant": tenant, "task": task}, run_id, end, parse=False)
            try:
                plan = json.loads(raw)["plan"]
            except (ValueError, KeyError, TypeError):
                plan = self.DEFAULT_PLAN  # "best effort": act anyway
        if plan.get("tool") != "refund" or not isinstance(plan.get("amount"), int):
            raise DependencyFailed("llm plan")
        return plan

    def run(self, tenant, run_id, task):
        end = time.monotonic() + self.k["deadline"] if self.k["deadline"] is not None else None
        tok = self._token(tenant, run_id, end)
        auth = {"Authorization": f"Bearer {tok}"}
        plan = self._plan(tenant, task, run_id, end)
        idem = {"Idempotency-Key": run_id} if self.k["idempotency_key"] else {}
        res = self._call("tool", "POST", "/tools/refund", {"tenant": tenant, "amount": plan["amount"]}, run_id, end,
                         {**auth, **idem})
        try:
            self._call("store", "PUT", f"/store/{run_id}", {"tenant": tenant, "result": res}, run_id, end, auth)
        except (DependencyFailed, DeadlineExceeded):
            if self.k["on_store_error"] == "fail":
                raise
        return res

    def handle(self, method, path, headers, body):
        try:
            return 200, self.run(body["tenant"], body["run_id"], body["task"])
        except DeadlineExceeded as e:
            return 504, {"error": f"deadline exceeded at {e}"}
        except (DependencyFailed, KeyError, TypeError, ValueError) as e:
            return 503, {"error": f"dependency failed: {e}"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
