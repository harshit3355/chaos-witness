"""Benchmark: six campaigns against the hardened platform, seven single-weakness mutants and an all-weak build.

The mutants are the ground truth: each flips one knob, and the benchmark asks whether a campaign
falsifies the obligation that knob breaks. Campaigns never see the pairing.
"""
from __future__ import annotations

import datetime
import hashlib
import platform
import subprocess
import sys
import random
import time
from pathlib import Path

from .sut import WEAKNESSES, variant
from .witness import all_faults, campaigns, load_contract, run_matrix, score, untargeted

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract.json"
LABELS = {
    "manual_checklist": "Manual chaos checklist (kill, slow, 500 per dependency)",
    "http_500_everywhere": "HTTP 500 everywhere (every dependency x schedule)",
    "llm_api_only": "LLM-API-only campaign (every fault x schedule on the LLM)",
    "random_same_budget": "Random fault injection, same budget",
    "untargeted_ablation": "Ablation: same fault kinds, not aimed at obligations",
    "targeted": "**Obligation-targeted campaign (mechanism)**",
    "exhaustive": "Exhaustive reference: every single fault (not a campaign anyone would run)",
}


def provenance(seed: int, **extra) -> dict:
    def git(*args):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--", "chaos_witness", "contract.json"))
    return {"commit": (sha or "unknown") + ("-dirty" if dirty else ""),
            "command": "python -m chaos_witness " + " ".join(sys.argv[1:]), "seed": seed,
            "python": platform.python_version(), "platform": platform.platform(),
            "fault_injector": "chaos_witness.proxy (pure Python; Toxiproxy not used)",
            "contract_sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest()[:16],
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **extra}


def _summary(sc: dict) -> dict:
    h, w = sc["hardened"], sc["all_weak"]
    det = {k: sc[k]["obligations"][ob]["status"] for k, (_, ob) in WEAKNESSES.items()}
    return {
        "configs": h["configs"], "triggered_hardened": h["triggered"], "acc_hardened": h["acc"],
        "witnessed_hardened": sum(x["status"] == "witnessed" for x in h["obligations"].values()),
        "false_alarms_hardened": sorted(o for o, x in h["obligations"].items() if x["status"] == "falsified"),
        "weaknesses_detected": sum(s == "falsified" for s in det.values()),
        "false_assurance": sorted(k for k, s in det.items() if s == "witnessed"),
        "weakness_status": det,
        "acc_all_weak": w["acc"], "unsafe_runs_all_weak": w["unsafe_runs"], "runs_all_weak": w["runs"],
        "max_attempts_all_weak": w["max_attempts_per_dependency_per_run"],
        "max_attempts_hardened": h["max_attempts_per_dependency_per_run"],
    }


def _dist(rows: list[dict], ceiling: float) -> dict:
    n, k = len(rows), len(WEAKNESSES)
    return {"seeds": n,
            "acc_mean": round(sum(r["acc_hardened"] for r in rows) / n, 4),
            "acc_min": min(r["acc_hardened"] for r in rows),
            "p_acc_at_ceiling": round(sum(r["acc_hardened"] >= ceiling for r in rows) / n, 4),
            "detected_mean": round(sum(r["weaknesses_detected"] for r in rows) / n, 3),
            "p_all_detected": round(sum(r["weaknesses_detected"] == k for r in rows) / n, 4),
            "false_assurance_mean": round(sum(len(r["false_assurance"]) for r in rows) / n, 3),
            "false_alarms_total": sum(len(r["false_alarms_hardened"]) for r in rows)}


def bench(seed: int, contract: Path = CONTRACT, workers: int = 12, seeds: int = 200,
          budgets: tuple = (10, 20, 30, 45, 60, 90)) -> dict:
    obs, workload = load_contract(contract)
    camps = {**campaigns(obs, seed), "exhaustive": all_faults()}
    names = ["hardened", *WEAKNESSES, "all_weak"]
    t0 = time.monotonic()
    ev = run_matrix({v: variant(v) for v in names}, set(all_faults()), workload, workers)
    wall = round(time.monotonic() - t0, 1)

    def summarize(faults):
        return _summary({v: score(obs, [ev[(v, f)] for f in faults]) for v in names})

    scores = {c: {v: score(obs, [ev[(v, f)] for f in faults]) for v in names} for c, faults in camps.items()}
    summary = {c: _summary(sc) for c, sc in scores.items()}
    ceiling = summary["exhaustive"]["acc_hardened"]
    budget = len(camps["targeted"])
    seeded = {
        "random_same_budget": _dist([summarize(random.Random(s).sample(all_faults(), budget))
                                     for s in range(seed, seed + seeds)], ceiling),
        "untargeted_ablation": _dist([summarize(untargeted(obs, random.Random(s)))
                                      for s in range(seed, seed + seeds)], ceiling),
    }
    sweep = {b: _dist([summarize(random.Random(s).sample(all_faults(), b)) for s in range(seed, seed + seeds)],
                      ceiling) for b in budgets}
    return {"seed": seed, "obligations": [{"id": o.id, "weight": o.weight, "statement": o.statement,
                                           "falsified_by": list(o.falsified_by), "scope": list(o.scope)} for o in obs],
            "workload_runs": len(workload), "variants": names, "weaknesses": {k: v[1] for k, v in WEAKNESSES.items()},
            "fault_space": len(all_faults()), "executions": len(ev), "wall_clock_s": wall, "acc_ceiling": ceiling,
            "campaigns": {c: [".".join(f) for f in fs] for c, fs in camps.items()},
            "summary": summary, "seeded": seeded, "random_budget_sweep": sweep, "scores": scores}


def markdown(rep: dict) -> str:
    s, obs = rep["summary"], rep["obligations"]
    tot = sum(o["weight"] for o in obs)
    hard = {c: rep["scores"][c]["hardened"]["obligations"] for c in s}
    L = ["# CHAOS WITNESS benchmark: which assurance obligations does a fault campaign witness?", "",
         "_Synthetic system under test (mock LLM, tool, identity and store on 127.0.0.1). Every number below was "
         "produced by the command in the provenance section._", "",
         f"- {len(obs)} obligations in `contract.json` (total weight {tot}), {len(rep['variants'])} system "
         f"configurations (hardened, {len(rep['weaknesses'])} single-weakness mutants, all-weak), "
         f"{rep['workload_runs']} runs per fault configuration, {rep['executions']} isolated executions "
         f"in {rep['wall_clock_s']} s.",
         "- **ACC** = weight of required obligations with at least one triggered, observed, passing witness and no "
         "counterexample / total required weight.",
         "- A mutant counts as **detected** when the campaign falsifies the obligation its weakness breaks; "
         "**false assurance** means the campaign reported that obligation as witnessed-and-passing on the mutant.", "",
         "## Campaigns", "",
         "| Campaign | Fault configs | Triggered (hardened) | ACC, hardened | Obligations witnessed | False alarms "
         "| Weaknesses detected | False assurance | Unsafe runs, all-weak | Max attempts/dep/run, all-weak |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for c, x in s.items():
        L.append(f"| {LABELS[c]} | {x['configs']} | {x['triggered_hardened']}/{x['configs']} | "
                 f"{x['acc_hardened']:.3f} | {x['witnessed_hardened']}/{len(obs)} | {len(x['false_alarms_hardened'])} | "
                 f"{x['weaknesses_detected']}/{len(rep['weaknesses'])} | {len(x['false_assurance'])} | "
                 f"{x['unsafe_runs_all_weak']}/{x['runs_all_weak']} | {x['max_attempts_all_weak']} |")
    t, k = s["targeted"], len(rep["weaknesses"])
    L += ["", f"Rows for random and ablation use seed {rep['seed']}; their distributions over seeds are below.", "",
          f"## Seeded campaigns over {rep['seeded']['random_same_budget']['seeds']} seeds", "",
          f"ACC ceiling for this fault injector and contract: {rep['acc_ceiling']:.3f} (reached by the exhaustive "
          f"sweep of all {rep['fault_space']} single faults).", "",
          "| Campaign | Budget | ACC mean | ACC min | P(ACC at ceiling) | Weaknesses detected, mean "
          f"| P(all {k} detected) | False assurance, mean | False alarms |", "|---|---|---|---|---|---|---|---|---|"]
    rows = [(LABELS[c], rep["seeded"][c], s[c]["configs"]) for c in rep["seeded"]]
    rows.append((LABELS["targeted"] + " (deterministic)", {
        "acc_mean": t["acc_hardened"], "acc_min": t["acc_hardened"],
        "p_acc_at_ceiling": float(t["acc_hardened"] >= rep["acc_ceiling"]), "detected_mean": t["weaknesses_detected"],
        "p_all_detected": float(t["weaknesses_detected"] == k), "false_assurance_mean": len(t["false_assurance"]),
        "false_alarms_total": len(t["false_alarms_hardened"])}, t["configs"]))
    for label, d, b in rows:
        L.append(f"| {label} | {b} | {d['acc_mean']:.3f} | {d['acc_min']:.3f} | {d['p_acc_at_ceiling']:.1%} | "
                 f"{d['detected_mean']} | {d['p_all_detected']:.1%} | {d['false_assurance_mean']} | "
                 f"{d['false_alarms_total']} |")
    L += ["", "## Random injection: budget needed to match the targeted campaign", "",
          f"| Random budget | ACC mean | P(ACC at ceiling) | Weaknesses detected, mean | P(all {k} detected) |",
          "|---|---|---|---|---|"]
    for b, d in rep["random_budget_sweep"].items():
        L.append(f"| {b} | {d['acc_mean']:.3f} | {d['p_acc_at_ceiling']:.1%} | {d['detected_mean']} | "
                 f"{d['p_all_detected']:.1%} |")
    L += ["", "Unsafe run = a duplicate, cross-tenant or fail-open side effect in the ledger, whatever the fault. "
              "Retry amplification on the hardened build never exceeds "
              f"{max(x['max_attempts_hardened'] for x in s.values())} attempts per dependency per run.", "",
          "## Obligation status on the hardened platform", "",
          "`W` witnessed and passing, `F` falsified, `-` never exercised, `U` unwitnessable by this fault injector.", "",
          "| Obligation | Weight | " + " | ".join(c.replace("_", " ") for c in s) + " |",
          "|---|---|" + "---|" * len(s)]
    code = {"witnessed": "W", "falsified": "F", "not_exercised": "-", "unwitnessable": "U"}
    for o in obs:
        L.append(f"| `{o['id']}` | {o['weight']} | " + " | ".join(code[hard[c][o['id']]['status']] for c in s) + " |")
    L += ["", "## Planted weaknesses: status of the obligation each one breaks", "",
          "| Weakness | Obligation | " + " | ".join(c.replace("_", " ") for c in s) + " |",
          "|---|---|" + "---|" * len(s)]
    for k, ob in rep["weaknesses"].items():
        L.append(f"| `{k}` | `{ob}` | " + " | ".join(code[s[c]["weakness_status"][k]] for c in s) + " |")
    t = rep["scores"]["targeted"]
    L += ["", "## Counterexamples found by the targeted campaign (first per weakness)", "",
          "| Weakness | Fault config @ run |", "|---|---|"]
    for k, ob in rep["weaknesses"].items():
        cex = t[k]["obligations"][ob]["counterexamples"]
        L.append(f"| `{k}` | {'`' + cex[0] + '`' if cex else 'none'} |")
    L += ["", "## Negative result: obligations this method cannot witness", ""]
    for o in obs:
        x = hard["targeted"][o["id"]]
        if x["status"] == "unwitnessable":
            L.append(f"- `{o['id']}` (weight {o['weight']}): {o['statement']} Reason: {x['reason']}.")
    L += ["", f"The ceiling for ACC with this fault injector is therefore "
              f"{(tot - sum(o['weight'] for o in obs if hard['targeted'][o['id']]['status'] == 'unwitnessable')) / tot:.3f}"
              " on this contract, whatever the campaign.", "", "## Provenance", ""]
    L += [f"- {k}: `{v}`" for k, v in rep["provenance"].items()]
    return "\n".join(L) + "\n"
