"""Assurance contract -> fault campaign -> execution -> per-obligation witness verdicts -> ACC.

A verdict is computed only from evidence the system under test does not write: the proxies'
request logs, the mocks' side-effect ledger and store, and the client's own observation of each run.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .proxy import KINDS, RETRY_AFTER_S, SCHEDULES, Fault, FaultProxy
from .sut import DEPS, Mocks, Orchestrator

# Failure classes an obligation can name, and the proxy faults that realise each one.
FAULT_CLASSES = {
    "persistent_failure": [("http_500", "all"), ("reset", "all")],
    "transient_failure": [("http_500", "first"), ("reset", "first")],
    "throttle": [("http_429", "first")],
    "ambiguous_outcome": [("drop_response", "first"), ("duplicate", "first"), ("timeout", "first")],
    "mid_session_outage": [("http_500", "from2"), ("reset", "from2"), ("timeout", "from2")],
    "malformed_response": [("truncate", "all"), ("garble", "all")],
    "slow": [("latency", "all"), ("timeout", "all")],
    "clock_skew": [],       # needs control of the SUT's clock: not an HTTP-level fault
    "deploy_rollback": [],  # needs a deployment control plane: the SUT has none
}
NO_GOOD_RESPONSE = {"truncate", "garble", "http_500", "http_429", "reset", "drop_response"}


@dataclass(frozen=True)
class Obligation:
    id: str
    statement: str
    weight: float
    required: bool
    scope: tuple
    falsified_by: tuple
    check: str
    params: dict


def load_contract(path: Path) -> tuple[list[Obligation], list[dict]]:
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    obs = [Obligation(o["id"], o["statement"], o["weight"], o["required"], tuple(o["scope"]),
                      tuple(o["falsified_by"]), o["check"], o.get("params", {})) for o in c["obligations"]]
    for o in obs:
        unknown = set(o.falsified_by) - FAULT_CLASSES.keys()
        if unknown or (o.check != "none" and o.check not in CHECKS):
            raise ValueError(f"{o.id}: unknown fault class {unknown} or check {o.check!r}")
    return obs, c["workload"]


# ---------------------------------------------------------------- campaign generation

def falsifiers(o: Obligation) -> tuple[list[tuple], str | None]:
    """Proxy faults that could falsify `o`, or the reason there are none."""
    faults = [(d, k, s) for c in o.falsified_by for d in o.scope if d in DEPS for k, s in FAULT_CLASSES[c]]
    if faults:
        return faults, None
    missing = [d for d in o.scope if d not in DEPS]
    if missing:
        return [], f"dependency {missing} is not part of the system under test"
    return [], f"no proxy fault expresses {list(o.falsified_by)}"


def targeted(obs: list[Obligation]) -> list[tuple]:
    out: list[tuple] = []
    for o in obs:
        out += [f for f in falsifiers(o)[0] if f not in out]
    return out


def untargeted(obs: list[Obligation], rng: random.Random) -> list[tuple]:
    """Ablation: the same fault kinds as `targeted`, aimed at a random dependency and schedule."""
    out: list[tuple] = []
    for _, kind, _ in targeted(obs):
        free = [(d, kind, s) for d in DEPS for s in SCHEDULES if (d, kind, s) not in out]
        out.append(rng.choice(free))
    return out


def all_faults() -> list[tuple]:
    return [(d, k, s) for d in DEPS for k in KINDS for s in SCHEDULES]


def campaigns(obs: list[Obligation], seed: int) -> dict[str, list[tuple]]:
    t = targeted(obs)
    return {
        "manual_checklist": [(d, k, "all") for d in DEPS for k in ("reset", "latency", "http_500")],
        "http_500_everywhere": [(d, "http_500", s) for d in DEPS for s in SCHEDULES],
        "llm_api_only": [f for f in all_faults() if f[0] == "llm"],
        "random_same_budget": random.Random(seed).sample(all_faults(), len(t)),
        "untargeted_ablation": untargeted(obs, random.Random(seed + 1)),
        "targeted": t,
    }


# ---------------------------------------------------------------- execution

def execute(knobs: dict, fault: tuple | None, workload: list[dict]) -> dict:
    """Fresh mocks, proxies and orchestrator; run the workload once; return the evidence."""
    mocks = Mocks()
    proxies = {d: FaultProxy(d, mocks.addr, Fault(fault[1], fault[2]) if fault and fault[0] == d else None)
               for d in DEPS}
    orch = Orchestrator(knobs, {d: p.port for d, p in proxies.items()})
    client = {}
    try:
        for i, w in enumerate(workload):
            run = f"r{i + 1}"
            req = urllib.request.Request(f"http://127.0.0.1:{orch.port}/run", method="POST",
                                         data=json.dumps({**w, "run_id": run}).encode(),
                                         headers={"Content-Type": "application/json"})
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            except OSError:
                status = None
            client[run] = {"tenant": w["tenant"], "status": status, "latency_s": round(time.monotonic() - t0, 3)}
        for p in proxies.values():
            p.drain()
    finally:
        orch.close()
        for p in proxies.values():
            p.close()
        mocks.close()
    log = sorted((r for p in proxies.values() for r in p.log), key=lambda r: r["t_recv"])
    return {"fault": fault, "log": log, "ledger": list(mocks.ledger), "store": dict(mocks.store), "client": client}


# ---------------------------------------------------------------- checks: evidence -> None | True | False

def _recs(ev, run, dep=None):
    return [r for r in ev["log"] if r["run"] == run and (dep is None or r["dep"] == dep)]


def _effects(ev, run):
    return [e for e in ev["ledger"] if e["run"] == run]


def check_bounded_retry(p, ev, run):
    rs = _recs(ev, run)
    if not any(r["fault"] for r in rs):
        return None
    return max(sum(r["dep"] == d for r in rs) for d in DEPS) <= p["max_attempts"]


def check_recovers_from_transient(p, ev, run):
    if sum(bool(r["fault"]) for r in _recs(ev, run)) != 1:
        return None
    return ev["client"][run]["status"] == 200


def check_honor_retry_after(p, ev, run):
    throttled = [r for r in _recs(ev, run) if r["fault"] == "http_429"]
    if not throttled:
        return None
    for r in throttled:
        nxt = [x for x in _recs(ev, run, r["dep"]) if x["seq"] > r["seq"]]
        if nxt and nxt[0]["t_recv"] - r["t_done"] < RETRY_AFTER_S - p["tolerance_s"]:
            return False
    return True


def check_no_duplicate_side_effect(p, ev, run):
    # precondition: a tool request reached the tool but its outcome was disturbed on the way back
    if not any(r["fault"] and r["forwarded"] for r in _recs(ev, run, "tool")):
        return None
    return len(_effects(ev, run)) <= 1


def check_tenant_isolation(p, ev, run):
    if not any(r["fault"] for r in _recs(ev, run, "identity")):
        return None
    tenant = ev["client"][run]["tenant"]
    store = [ev["store"][run]] if run in ev["store"] else []
    return all(e["token_tenant"] == tenant for e in _effects(ev, run) + store)


def check_fail_closed_on_bad_llm_output(p, ev, run):
    llm = _recs(ev, run, "llm")
    if not llm or not all(r["fault"] in NO_GOOD_RESPONSE for r in llm):
        return None
    return not _effects(ev, run) and ev["client"][run]["status"] != 200


def check_bounded_latency(p, ev, run):
    if not any(r["fault"] in ("latency", "timeout") for r in _recs(ev, run)):
        return None
    return ev["client"][run]["latency_s"] <= p["limit_s"]


def check_ack_implies_durable(p, ev, run):
    if not any(r["fault"] for r in _recs(ev, run, "store")):
        return None
    return ev["client"][run]["status"] != 200 or run in ev["store"]


CHECKS = {k.removeprefix("check_"): v for k, v in globals().items() if k.startswith("check_")}


def _unsafe(ev, run) -> bool:
    """Duplicate, cross-tenant, or fail-open side effect, regardless of which fault was injected."""
    eff, llm = _effects(ev, run), _recs(ev, run, "llm")
    return (len(eff) > 1 or any(e["token_tenant"] != ev["client"][run]["tenant"] for e in eff)
            or bool(eff and llm and all(r["fault"] in NO_GOOD_RESPONSE for r in llm)))


# ---------------------------------------------------------------- scoring

def score(obs: list[Obligation], evidence: list[dict]) -> dict:
    """Per-obligation status and ACC for one campaign against one system configuration."""
    per = {}
    for o in obs:
        _, reason = falsifiers(o)
        if o.check == "none" or reason:
            per[o.id] = {"status": "unwitnessable", "reason": reason, "witnesses": [], "counterexamples": []}
            continue
        wit, cex = [], []
        for ev in evidence:
            for run in ev["client"]:
                v = CHECKS[o.check](o.params, ev, run)
                if v is not None:
                    (wit if v else cex).append(f"{'.'.join(ev['fault'])}@{run}")
        status = "falsified" if cex else "witnessed" if wit else "not_exercised"
        per[o.id] = {"status": status, "witnesses": wit, "counterexamples": cex}
    req = [o for o in obs if o.required]
    total = sum(o.weight for o in req)
    runs = [(ev, run) for ev in evidence for run in ev["client"]]
    unsafe = sum(_unsafe(ev, run) for ev, run in runs)
    return {
        "acc": round(sum(o.weight for o in req if per[o.id]["status"] == "witnessed") / total, 4),
        "obligations": per,
        "configs": len(evidence),
        "triggered": sum(any(r["fault"] for r in ev["log"]) for ev in evidence),
        "runs": len(runs),
        "unsafe_runs": unsafe,
        "max_attempts_per_dependency_per_run": max(
            (sum(r["dep"] == d for r in _recs(ev, run)) for ev, run in runs for d in DEPS), default=0),
    }


def run_matrix(variants: dict[str, dict], faults: set[tuple], workload, workers: int = 12) -> dict:
    """Evidence for every (variant, fault) pair, executed concurrently; each pair is isolated."""
    jobs = [(v, f) for v in variants for f in sorted(faults)]
    with ThreadPoolExecutor(workers) as pool:
        out = pool.map(lambda j: execute(variants[j[0]], j[1], workload), jobs)
        return dict(zip(jobs, out))
