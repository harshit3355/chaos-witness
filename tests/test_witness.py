from pathlib import Path

import pytest

from chaos_witness.sut import WEAKNESSES, variant
from chaos_witness.witness import (CHECKS, campaigns, falsifiers, load_contract, run_matrix, score, targeted)

OBS, WORKLOAD = load_contract(Path(__file__).resolve().parent.parent / "contract.json")


def _ev(ledger=(), log=(), store=None, status=200, tenant="acme"):
    return {"fault": ("tool", "duplicate", "first"), "ledger": list(ledger), "store": store or {},
            "log": list(log), "client": {"r1": {"tenant": tenant, "status": status, "latency_s": 0.1}}}


def test_checks_read_evidence_not_claims():
    faulted_tool = {"dep": "tool", "run": "r1", "seq": 1, "fault": "duplicate", "forwarded": 2}
    effect = {"run": "r1", "tenant": "acme", "token_tenant": "acme"}
    dup = CHECKS["no_duplicate_side_effect"]
    assert dup({}, _ev([effect, effect], [faulted_tool]), "r1") is False  # the platform said 200; the ledger says twice
    assert dup({}, _ev([effect], [faulted_tool]), "r1") is True
    assert dup({}, _ev([effect, effect]), "r1") is None                   # no fault delivered: not a witness
    iso = CHECKS["tenant_isolation"]
    outage = {"dep": "identity", "run": "r1", "seq": 2, "fault": "http_500", "forwarded": 0}
    assert iso({}, _ev([{**effect, "token_tenant": "globex"}], [outage]), "r1") is False


def test_generator_and_negative_cases():
    t = targeted(OBS)
    assert len(t) == len(set(t)) == 30
    reasons = {o.id: falsifiers(o)[1] for o in OBS}
    assert {o for o, r in reasons.items() if r} == {"token_expiry_under_clock_skew",
                                                     "rollback_preserves_healthy_revision"}
    assert campaigns(OBS, 3)["random_same_budget"] == campaigns(OBS, 3)["random_same_budget"]


@pytest.fixture(scope="module")
def targeted_evidence():
    t = targeted(OBS)
    names = ["hardened", *WEAKNESSES]
    return t, run_matrix({v: variant(v) for v in names}, set(t), WORKLOAD)


def test_targeted_campaign_witnesses_hardened_and_falsifies_every_planted_weakness(targeted_evidence):
    t, ev = targeted_evidence
    hard = score(OBS, [ev[("hardened", f)] for f in t])
    status = {o: x["status"] for o, x in hard["obligations"].items()}
    assert "falsified" not in status.values(), status
    assert hard["acc"] == round(17 / 21, 4) and hard["unsafe_runs"] == 0
    for weakness, (_, obligation) in WEAKNESSES.items():
        s = score(OBS, [ev[(weakness, f)] for f in t])
        assert s["obligations"][obligation]["status"] == "falsified", weakness
