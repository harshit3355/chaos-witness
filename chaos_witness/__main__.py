"""CLI: run a campaign against one system configuration, or run the full benchmark."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bench as bench_mod
from .sut import WEAKNESSES, variant
from .witness import all_faults, campaigns, load_contract, run_matrix, score


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m chaos_witness", description=__doc__)
    ap.add_argument("--contract", default=str(bench_mod.CONTRACT))
    ap.add_argument("--seed", type=int, default=7)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one campaign; exit 1 if any obligation is falsified")
    r.add_argument("--variant", default="hardened", choices=["hardened", "all_weak", *WEAKNESSES])
    r.add_argument("--campaign", default="targeted", choices=list(bench_mod.LABELS))
    b = sub.add_parser("bench", help="all campaigns x all system configurations -> reports/")
    b.add_argument("--out", default="reports")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        obs, workload = load_contract(Path(a.contract))
        faults = {**campaigns(obs, a.seed), "exhaustive": all_faults()}[a.campaign]
        ev = run_matrix({a.variant: variant(a.variant)}, set(faults), workload)
        s = score(obs, [ev[(a.variant, f)] for f in faults])
        for o, x in s["obligations"].items():
            first = (x["counterexamples"] or x["witnesses"] or [x.get("reason") or ""])[0]
            print(f"{x['status']:14s} {o:38s} {first}")
        print(f"ACC {s['acc']}  triggered {s['triggered']}/{s['configs']}  unsafe runs {s['unsafe_runs']}/{s['runs']}")
        return 1 if any(x["status"] == "falsified" for x in s["obligations"].values()) else 0

    rep = bench_mod.bench(a.seed, Path(a.contract))
    rep["provenance"] = bench_mod.provenance(a.seed)
    for c, x in rep["summary"].items():
        print(f"{c:22s} ACC {x['acc_hardened']:.3f}  detected {x['weaknesses_detected']}/{len(WEAKNESSES)}"
              f"  false alarms {len(x['false_alarms_hardened'])}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8", newline="\n")
    (out / "benchmark.md").write_text(bench_mod.markdown(rep), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'benchmark.json'} and {out / 'benchmark.md'} ({rep['wall_clock_s']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
