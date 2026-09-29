import json
import urllib.error
import urllib.request

import pytest

from chaos_witness.proxy import RETRY_AFTER_S, Fault, FaultProxy
from chaos_witness.sut import Mocks


@pytest.fixture
def mocks():
    m = Mocks()
    m.tokens["t1"] = "acme"
    yield m
    m.close()


def _refund(port, key=None):
    headers = {"Content-Type": "application/json", "Authorization": "Bearer t1", "X-Run-Id": "r1",
               **({"Idempotency-Key": key} if key else {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/tools/refund", method="POST",
                                 data=json.dumps({"tenant": "acme", "amount": 5}).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, r.read()


def _with(mocks, kind, schedule="all"):
    return FaultProxy("tool", mocks.addr, Fault(kind, schedule))


def test_429_carries_retry_after_and_is_not_forwarded(mocks):
    p = _with(mocks, "http_429")
    with pytest.raises(urllib.error.HTTPError) as e:
        _refund(p.port)
    p.close()
    assert e.value.code == 429 and float(e.value.headers["Retry-After"]) == RETRY_AFTER_S
    assert mocks.ledger == [] and p.log[0]["forwarded"] == 0 and p.log[0]["fault"] == "http_429"


def test_drop_response_executes_the_side_effect_but_the_caller_sees_a_failure(mocks):
    p = _with(mocks, "drop_response")
    with pytest.raises(OSError):
        _refund(p.port)
    p.drain()
    p.close()
    assert len(mocks.ledger) == 1 and p.log[0]["forwarded"] == 1


def test_duplicate_delivery_is_absorbed_only_by_an_idempotency_key(mocks):
    p = _with(mocks, "duplicate")
    _refund(p.port)
    _refund(p.port, key="k1")
    p.close()
    assert [e["key"] for e in mocks.ledger] == [None, None, "k1"]


def test_truncate_breaks_json_and_schedule_from2_spares_the_first_request(mocks):
    p = _with(mocks, "truncate", "from2")
    first = _refund(p.port)[2]
    second = _refund(p.port)[2]
    p.close()
    json.loads(first)
    with pytest.raises(ValueError):
        json.loads(second)
    assert [r["fault"] for r in p.log] == [None, "truncate"]
