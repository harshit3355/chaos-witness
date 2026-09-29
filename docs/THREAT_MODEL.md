# Threat model

CHAOS WITNESS produces assurance evidence: a claim that an obligation held (or broke) while a fault was
actually delivered. The main threat is evidence that looks like assurance but is not. v0.1 runs
entirely on 127.0.0.1 against a synthetic platform. It never points faults at real services.

## Assets

- The integrity of the verdicts and of the ACC figure used as audit evidence.
- The correctness of the assurance contract (`contract.json`), which is the specification.
- The machine running the campaign: its network position and anything the platform under test can reach.

## Trust boundaries

| Component | Trust | Why |
|---|---|---|
| `contract.json` | trusted, code-reviewed | a wrong or weak obligation gets "witnessed" as written |
| Fault classes in `chaos_witness/witness.py` | trusted, code-reviewed | they decide which faults count as falsifiers; a wrong mapping aims the campaign at the wrong thing |
| Proxies, mocks, client (`proxy.py`, `sut.Mocks`, `witness.execute`) | trusted evidence sources | verdicts are computed only from their records |
| Orchestrator under test (`sut.Orchestrator`) | **untrusted** | its answers are claims, never evidence; it gets proxy ports and nothing else |

## Threats and controls

| Threat | Control |
|---|---|
| The platform certifies itself ("I retried at most 3 times") | checkers read only the proxy logs, the side-effect ledger, the store and client-side timing; the orchestrator holds no reference to any of them (`tests/test_witness.py::test_checks_read_evidence_not_claims`) |
| A fault is configured but never delivered, and the obligation is counted anyway | a verdict exists only for runs whose proxy log shows a delivered fault matching the check's precondition; `triggered` is reported per campaign |
| A campaign produces a witness that could not have failed (for example an identity outage before any token is cached "proves" tenant isolation) | measured, not prevented: the benchmark reports *false assurance* on planted weaknesses. ACC alone does not grade witness strength (see README limitations) |
| An obligation needs a fault the injector cannot express, and silently drops out | the generator reports it as `unwitnessable`, with a reason, and it stays in ACC's denominator |
| Delayed ("ghost") requests land after evidence is collected, so verdicts depend on timing | proxies drain in-flight requests before evidence is read |
| Timing-based verdicts flip under load | the hardened (0.6 s deadline) and weak (at least 1.2 s) behaviours sit 0.2 s or more either side of the 1.0 s latency limit. The Retry-After check allows 20 ms for clock resolution, because the hardened build waits exactly Retry-After (load can only lengthen that wait). CI runs the full benchmark on two Python versions |
| The fault proxy becomes an open relay | it listens on 127.0.0.1 only and forwards only to the one upstream it was built with |
| A tampered report is presented as evidence | reports record commit SHA (with `-dirty` if code or contract changed), command, seed, Python and platform, contract hash and UTC time; regenerate from the commit to verify |
| Supply-chain compromise | no runtime dependencies; pytest is test-only; CI actions pinned by commit SHA with a read-only token |

## Out of scope for v0.1

Process isolation between the platform under test and the evidence collectors. They share one
Python process here, so a hostile orchestrator could tamper with the evidence. A real deployment must
run the proxy and ledgers outside the platform's trust domain. Also out of scope: faults against
real or cloud dependencies, signed reports, and multi-tenant use of the harness itself.
