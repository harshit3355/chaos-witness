# CHAOS WITNESS benchmark: which assurance obligations does a fault campaign witness?

_Synthetic system under test (mock LLM, tool, identity and store on 127.0.0.1). Every number below was produced by the command in the provenance section._

- 10 obligations in `contract.json` (total weight 21), 9 system configurations (hardened, 7 single-weakness mutants, all-weak), 3 runs per fault configuration, 972 isolated executions in 88.0 s.
- **ACC** = weight of required obligations with at least one triggered, observed, passing witness and no counterexample / total required weight.
- A mutant counts as **detected** when the campaign falsifies the obligation its weakness breaks; **false assurance** means the campaign reported that obligation as witnessed-and-passing on the mutant.

## Campaigns

| Campaign | Fault configs | Triggered (hardened) | ACC, hardened | Obligations witnessed | False alarms | Weaknesses detected | False assurance | Unsafe runs, all-weak | Max attempts/dep/run, all-weak |
|---|---|---|---|---|---|---|---|---|---|
| Manual chaos checklist (kill, slow, 500 per dependency) | 12 | 12/12 | 0.714 | 6/10 | 0 | 4/7 | 2 | 0/36 | 8 |
| HTTP 500 everywhere (every dependency x schedule) | 12 | 12/12 | 0.524 | 5/10 | 0 | 3/7 | 1 | 1/36 | 8 |
| LLM-API-only campaign (every fault x schedule on the LLM) | 27 | 27/27 | 0.429 | 5/10 | 0 | 4/7 | 0 | 12/81 | 8 |
| Random fault injection, same budget | 30 | 30/30 | 0.809 | 8/10 | 0 | 6/7 | 1 | 8/90 | 8 |
| Ablation: same fault kinds, not aimed at obligations | 30 | 30/30 | 0.809 | 8/10 | 0 | 7/7 | 0 | 5/90 | 8 |
| **Obligation-targeted campaign (mechanism)** | 30 | 30/30 | 0.809 | 8/10 | 0 | 7/7 | 0 | 10/90 | 8 |
| Exhaustive reference: every single fault (not a campaign anyone would run) | 108 | 108/108 | 0.809 | 8/10 | 0 | 7/7 | 0 | 42/324 | 8 |

Rows for random and ablation use seed 7; their distributions over seeds are below.

## Seeded campaigns over 200 seeds

ACC ceiling for this fault injector and contract: 0.809 (reached by the exhaustive sweep of all 108 single faults).

| Campaign | Budget | ACC mean | ACC min | P(ACC at ceiling) | Weaknesses detected, mean | P(all 7 detected) | False assurance, mean | False alarms |
|---|---|---|---|---|---|---|---|---|
| Random fault injection, same budget | 30 | 0.804 | 0.667 | 95.0% | 6.48 | 57.5% | 0.485 | 0 |
| Ablation: same fault kinds, not aimed at obligations | 30 | 0.804 | 0.667 | 96.0% | 6.345 | 39.5% | 0.615 | 0 |
| **Obligation-targeted campaign (mechanism)** (deterministic) | 30 | 0.809 | 0.809 | 100.0% | 7 | 100.0% | 0 | 0 |

## Random injection: budget needed to match the targeted campaign

| Random budget | ACC mean | P(ACC at ceiling) | Weaknesses detected, mean | P(all 7 detected) |
|---|---|---|---|---|
| 10 | 0.708 | 29.5% | 4.785 | 3.0% |
| 20 | 0.783 | 78.5% | 5.935 | 25.5% |
| 30 | 0.804 | 95.0% | 6.48 | 57.5% |
| 45 | 0.808 | 99.0% | 6.805 | 82.0% |
| 60 | 0.809 | 100.0% | 6.98 | 98.0% |
| 90 | 0.809 | 100.0% | 7.0 | 100.0% |

Unsafe run = a duplicate, cross-tenant or fail-open side effect in the ledger, whatever the fault. Retry amplification on the hardened build never exceeds 3 attempts per dependency per run.

## Obligation status on the hardened platform

`W` witnessed and passing, `F` falsified, `-` never exercised, `U` unwitnessable by this fault injector.

| Obligation | Weight | manual checklist | http 500 everywhere | llm api only | random same budget | untargeted ablation | targeted | exhaustive |
|---|---|---|---|---|---|---|---|---|
| `bounded_retry` | 2 | W | W | W | W | W | W | W |
| `recovers_from_transient` | 1 | - | W | W | W | W | W | W |
| `honor_retry_after` | 1 | - | - | W | W | W | W | W |
| `no_duplicate_side_effect` | 3 | W | - | - | W | W | W | W |
| `tenant_isolation` | 3 | W | W | - | W | W | W | W |
| `fail_closed_on_bad_llm_output` | 3 | W | W | W | W | W | W | W |
| `bounded_latency` | 2 | W | - | W | W | W | W | W |
| `ack_implies_durable` | 2 | W | W | - | W | W | W | W |
| `token_expiry_under_clock_skew` | 2 | U | U | U | U | U | U | U |
| `rollback_preserves_healthy_revision` | 2 | U | U | U | U | U | U | U |

## Planted weaknesses: status of the obligation each one breaks

| Weakness | Obligation | manual checklist | http 500 everywhere | llm api only | random same budget | untargeted ablation | targeted | exhaustive |
|---|---|---|---|---|---|---|---|---|
| `unbounded_retries` | `bounded_retry` | F | F | F | F | F | F | F |
| `ignores_retry_after` | `honor_retry_after` | - | - | F | F | F | F | F |
| `no_idempotency_key` | `no_duplicate_side_effect` | F | - | - | F | F | F | F |
| `global_token_fallback` | `tenant_isolation` | W | F | - | F | F | F | F |
| `fail_open_on_bad_plan` | `fail_closed_on_bad_llm_output` | W | W | F | W | F | F | F |
| `no_deadline` | `bounded_latency` | F | - | F | F | F | F | F |
| `acks_before_durable` | `ack_implies_durable` | F | F | - | F | F | F | F |

## Counterexamples found by the targeted campaign (first per weakness)

| Weakness | Fault config @ run |
|---|---|
| `unbounded_retries` | `llm.http_500.all@r1` |
| `ignores_retry_after` | `llm.http_429.first@r1` |
| `no_idempotency_key` | `tool.drop_response.first@r1` |
| `global_token_fallback` | `identity.http_500.from2@r2` |
| `fail_open_on_bad_plan` | `llm.truncate.all@r1` |
| `no_deadline` | `tool.timeout.first@r1` |
| `acks_before_durable` | `store.http_500.all@r1` |

## Negative result: obligations this method cannot witness

- `token_expiry_under_clock_skew` (weight 2): An expired token is never used, with up to 30 s of clock skew between orchestrator and identity provider. Reason: no proxy fault expresses ['clock_skew'].
- `rollback_preserves_healthy_revision` (weight 2): A failed deployment rolls back to the last healthy revision without dropping in-flight runs. Reason: dependency ['deploy'] is not part of the system under test.

The ceiling for ACC with this fault injector is therefore 0.809 on this contract, whatever the campaign.

## Provenance

- commit: `6c4ed1dc89de9d910e0ad85da74117f97a190b03`
- command: `python -m chaos_witness bench --out reports`
- seed: `7`
- python: `3.10.6`
- platform: `Windows-10-10.0.26200-SP0`
- fault_injector: `chaos_witness.proxy (pure Python; Toxiproxy not used)`
- contract_sha256: `31ee7c73ef6cf506`
- generated_at: `2026-09-29T09:26:08+00:00`
