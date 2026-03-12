# Design: BLIS Admission Control Discovery Experiments

## Goal

Discover an adaptive admission control policy for multi-tenant LLM inference clusters. BLIS currently has **no admission control** — under overload, all requests are admitted and queues explode. We want to evolve a policy that **sheds low-priority work to protect high-priority tenants** while maximizing throughput.

This is practical: the evolved policy will transfer directly to llm-d's admission control hook.

### Success Criteria

1. Evolved policy beats `always-admit` on SLO attainment under overload (critical requests meet targets)
2. Evolved policy maintains >70% throughput (doesn't over-reject)
3. Policy uses only production-available signals (InFlightRequests, SLOClass, TenantID)

---

## Background

### What We're Evolving

The `AdaptiveAdmission.Admit()` method in BLIS's `admission.go`. The baseline admits everything (identical to `always-admit`). Evolution makes this adaptive based on request properties (`SLOClass`, `TenantID`, `InputTokens`) and cluster state (`InFlightRequests`, `KVUtilization`, `QueueDepth`).

### Existing Work (BLIS Router Benchmark)

The routing benchmark (`benchmarks/blis_router/`) provides the proven pattern:
- `initial_program.go`: Full Go source file with EVOLVE-BLOCK markers, replaces `sim/routing.go` during evaluation
- `evaluator.py`: Extract Go → write file → `go build` → run simulations → parse metrics → return score
- Sequential evaluation (shared Go file), ~30-60s per iteration
- OpenEvolve: 0% build errors, best results in 16/50 iterations (~10 min)

### Key Differences from Routing Evolution

| Aspect | Routing | Admission Control |
|--------|---------|-------------------|
| What it controls | Which instance handles a request | Whether a request enters the system at all |
| Fitness landscape | Unimodal (lower latency = better) | Multi-objective (reject more → better latency but worse utilization) |
| Search space | Stateless scoring math | Stateful strategies (rate trackers, per-tenant budgets) |
| Workload requirement | Balanced load (below capacity) | **Overload** (above capacity, forcing selective shedding) |
| File evolved | `sim/routing.go` | `sim/admission.go` |

---

## Design

### 1. Benchmark Directory Structure

```
benchmarks/blis_admission/
├── initial_program.go          # Full admission.go with AdaptiveAdmission + EVOLVE-BLOCK
├── evaluator.py                # Build BLIS, run overload workloads, return multi-objective score
├── config.yaml                 # SkyDiscover config
├── workloads/                  # Overload workload specs (v2 format)
│   ├── workload_v2_overload_mixed_slo.yaml
│   └── workload_v2_bursty_adversary.yaml
├── routing/
│   └── routing_policy.yaml     # Fixed routing (1:1 weighted), only admission evolves
├── inference-sim/              # Symlink → ../blis_router/inference-sim
└── scripts/
    ├── compare_results.py
    ├── plot_results.py
    ├── analyze_effort.py
    └── analyze_diffs.py
```

### 2. The Initial Program

Complete replacement for `sim/admission.go`. Must include all existing types (`AlwaysAdmit`, `TokenBucket`, `RejectAll`).

**Source**: `docs/study/admission_initial_program.go` (already drafted).

Key design decisions:
- **Pre-derived signals above the EVOLVE-BLOCK** (fixed, not mutable): `numInstances`, `totalInFlight`, `totalQueueDepth`, `maxKVUtil`, `avgKVUtil`, `minFreeKV`, `inputLen`, `sloClass`, `tenantID`, `clock`
- **Pre-provisioned mutable state on the struct**: `tenantTokens`, `tenantRequests`, `classCounters`, `windowStart`, `windowCount`, `totalAdmitted`, `totalRejected`, `lastClock`
- **Baseline behavior**: `return true, ""` (admit everything — identical to `always-admit`)
- **Factory trick**: The `"always-admit"` case in `NewAdmissionPolicy` is remapped to `return NewAdaptiveAdmission()`. YAML stays `policy: always-admit`, `bundle.go` untouched — **zero inference-sim changes required**.

### 3. The Evaluator

Same pattern as the routing evaluator. Key differences noted with **[DIFF]**.

```python
# benchmarks/blis_admission/evaluator.py
def evaluate(program_path: str) -> dict:
    # 1. Extract Go code from evolved program file
    # 2. [DIFF] Save original admission.go, write evolved code to inference-sim/sim/admission.go
    # 3. go build -o simulation_worker main.go
    # 4. [DIFF] For each (seed, workload) combination:
    #      Run: ./simulation_worker run --model <id> --hardware H100 --tp 1
    #                      --num-instances 4 --policy-config routing_policy.yaml
    #                      --workload-spec <workload.yaml> --seed <seed>
    #                      --snapshot-refresh-interval 5000000
    #                      --results-path <tmpfile.json>    # [DIFF] per-request output
    #                      --log info
    # 5. [DIFF] Parse from --results-path JSON:
    #    - injected_requests, completed_requests, per-request records (slo_class, tenant_id, e2e_ms)
    #    - Derive: rejected = num_requests (from workload YAML) - injected_requests (INV-1)
    # 6. [DIFF] Compute multi-objective score
    # 7. Restore original admission.go in finally block
    return {
        "combined_score": score,
        "slo_attainment": ...,
        "throughput": ...,
        "jain_fairness": ...,
        "avg_e2e_ms": ...,
        "avg_p95_ms": ...,
        "total_rejected": ...,
        "per_workload_scores": { ... },
    }
```

**Model/hardware**: Single-LLM by default (`BLIS_MULTI_LLM=0`, matching routing convention):

| Short name | Model ID | Hardware | TP |
|------------|----------|----------|----|
| `qwen_7b` | `qwen/qwen2.5-7b-instruct` | H100 | 1 |

Multi-LLM validation (`qwen_14b`) is a separate pass after promising policies are found.

**[DIFF] Fitness formula** (3 terms, all [0,1], higher = better):

```
score = 0.50 * slo_attainment
      + 0.30 * capped_throughput
      + 0.20 * jain_fairness
```

Where:
- **`slo_attainment`** = `(completed requests meeting SLO) / num_requests`. Rejected requests count as SLO misses. This naturally penalizes rejecting critical requests (they didn't meet their SLO) while rewarding shedding batch/background (whose SLOs would be missed under overload anyway).
- **`capped_throughput`** = `min(completed_requests / num_requests, THROUGHPUT_CAP) / THROUGHPUT_CAP`. The cap (default 0.85) means both `always-admit` (throughput=1.0) and a good shedding policy (throughput=0.85) score 1.0 on this term. This **eliminates the penalty for moderate rejection**, creating a smooth gradient from always-admit toward smart shedding. Only aggressive rejection (below 85% throughput) is penalized. The cap value is set to `1 - overload_fraction / (1 + overload_fraction)` — for 1.3x overload, this is ~0.77, rounded up to 0.85 for margin.
- **`jain_fairness`** = Jain index over per-tenant completion rates. `N = total tenants in workload spec` (not tenants with completions — this prevents "admit only one tenant" from scoring Jain=1.0). Formula: `(sum(xi))^2 / (N * sum(xi^2))` where `xi = completed_tenant_i / expected_tenant_i`. Guard: if all `xi=0`, `jain_fairness = 0.0`.

**Why 3 terms, not 5**: The original 5-term formula had `critical_rejection_rate` (subsumed by `slo_attainment` since rejected = SLO miss) and `normalized_p95` (noise on top of `slo_attainment`). Fewer terms = clearer gradient for the LLM = faster convergence.

**Why throughput cap**: Without the cap, `always-admit` scores throughput=1.0 and any rejection immediately hurts 2 of 3 terms (throughput drops, fairness may drop) before slo_attainment improves enough to compensate. This creates a shallow gradient that discourages the LLM from exploring rejection strategies. The cap removes this barrier — shedding up to ~15% of requests is "free" on the throughput term, so the LLM only needs slo_attainment to improve to see score gains.

**Degenerate strategy analysis** (at 1.3x overload):
- "Reject everything" → slo_attainment=0, throughput=0, fairness=0 → **score=0.0**
- "Admit everything" → slo_attainment=~0.4, throughput=1.0 (capped to 1.0), fairness=~0.9 → **score=~0.48**
- "Admit only critical" → slo_attainment=~0.2, throughput=0.2/0.85=0.24, fairness=~0.33 → **score=~0.24**
- "Smart shedding" (shed batch when overloaded) → slo_attainment=~0.7, throughput=0.85 (capped to 1.0), fairness=~0.8 → **score=~0.81**
- "Reject only batch" (40% rejected) → slo_attainment=~0.58, throughput=0.6/0.85=0.71, fairness=~0.67 → **score=~0.64**

The formula correctly ranks: smart shedding > reject-only-batch > always-admit > admit-only-critical > reject-all. The throughput cap widens the gap between always-admit (0.48) and smart shedding (0.81), giving the search a strong gradient.

**[DIFF] SLO targets** (calibrated during Phase 0 pilot):

| SLO Class | Target | Rationale |
|-----------|--------|-----------|
| critical | TBD (~P75 at 1.0x capacity) | Must be achievable with good admission |
| standard | TBD (~P90 at 1.0x capacity) | Generous but meaningful |
| sheddable | TBD | |
| batch | TBD | |
| background | TBD | Essentially no deadline |

**Phase 0 will measure actual latencies** at 1.0x capacity with `always-admit` and set targets accordingly. The routing experiment showed ~4300ms E2E at 40 QPS with 500-token requests — the admission workloads use smaller tokens (128-512), so latencies will be lower, but exact values must be measured.

**[DIFF] Extracting metrics from simulator output**:

**No sim changes required.** All metrics from `--results-path` JSON:
- Per-request records with `slo_class`, `tenant_id`, `e2e_ms` → compute `slo_attainment` and `jain_fairness` in Python
- `injected_requests` field → derive `rejected = num_requests - injected_requests` (INV-1)
- `num_requests` parsed from workload YAML (not in JSON — evaluator reads YAML separately)

**Edge case**: Reject-all → `completed=0`, `injected=0`, all metrics = 0, score = 0.0. Strong negative signal.

### 4. Policy Bundle

```yaml
# benchmarks/blis_admission/routing/routing_policy.yaml
admission:
  policy: always-admit        # Factory remapped to AdaptiveAdmission in initial_program.go
routing:
  policy: weighted
  scorers:
    - name: prefix-affinity
      weight: 1.0
    - name: load-balance
      weight: 1.0
priority:
  policy: constant
scheduler: fcfs
```

Routing, priority, and scheduling are **fixed**. Only admission is evolved.

### 5. Workloads

Both workloads must produce **overload** — arrival rate exceeding cluster capacity — to force non-trivial shedding. Under balanced load, `always-admit` is optimal.

**Overload rates are TBD** — calibrated in Phase 0 pilot. The routing experiments show the 4-instance qwen_7b/H100 cluster saturates at ~40-85 QPS with 200-500 token requests. With the smaller tokens in admission workloads (128-512), capacity is likely 60-150 QPS. Overload = 1.3x that.

#### Workload 1: `overload_mixed_slo`

**Tests**: Can the policy shed batch/sheddable to protect critical under sustained overload?

```yaml
version: "2"
seed: 42
category: language
aggregate_rate: TBD            # 1.3x capacity, calibrated in Phase 0
num_requests: TBD              # 60s worth at aggregate_rate

clients:
  - id: realtime-api
    tenant_id: tenant-rt
    slo_class: critical
    rate_fraction: 0.20
    streaming: true
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 128, std_dev: 32, min: 32, max: 256 } }
    output_distribution: { type: exponential, params: { mean: 64 } }

  - id: standard-service
    tenant_id: tenant-std
    slo_class: standard
    rate_fraction: 0.40
    streaming: false
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 256, std_dev: 64, min: 64, max: 512 } }
    output_distribution: { type: exponential, params: { mean: 128 } }

  - id: batch-pipeline
    tenant_id: tenant-batch
    slo_class: batch
    rate_fraction: 0.40
    streaming: false
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 512, std_dev: 128, min: 128, max: 1024 } }
    output_distribution: { type: exponential, params: { mean: 256 } }
```

#### Workload 2: `bursty_adversary`

**Tests**: Can the policy isolate critical tenant during burst events without over-rejecting during calm periods?

```yaml
version: "2"
seed: 42
category: language
aggregate_rate: TBD            # 1.1x capacity baseline, ~1.8x during bursts
num_requests: TBD

clients:
  - id: steady-critical
    tenant_id: tenant-critical
    slo_class: critical
    rate_fraction: 0.25
    streaming: true
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 128, std_dev: 32, min: 32, max: 256 } }
    output_distribution: { type: exponential, params: { mean: 64 } }

  - id: bulk-adversary
    tenant_id: tenant-bulk
    slo_class: sheddable
    rate_fraction: 0.75
    streaming: false
    arrival: { process: gamma, cv: 4.0 }   # extreme burstiness
    input_distribution: { type: gaussian, params: { mean: 768, std_dev: 256, min: 128, max: 2048 } }
    output_distribution: { type: exponential, params: { mean: 256 } }
```

### 6. SkyDiscover Config

```yaml
# benchmarks/blis_admission/config.yaml
language: go
file_suffix: .go
diff_based_generation: false     # MUST be false — true causes ~85% build errors with OpenEvolve
max_iterations: 50
checkpoint_interval: 5
max_solution_length: 40000

llm:
  models:
    - name: "aws/claude-sonnet-4-5"
      weight: 0.7
    - name: "aws/claude-opus-4-6"
      weight: 0.3
  api_base: "https://ete-litellm.ai-models.vpc-int.res.ibm.com"
  temperature: 1.0
  top_p: null
  max_tokens: 32000
  timeout: 120

prompt:
  system_message: |
    You are optimizing an admission control policy for a 4-instance LLM inference cluster.

    PROBLEM: Under overload, admitting everything causes queues to explode and critical
    tenants to be starved. Your job is to selectively reject low-priority requests to
    protect high-priority ones while maintaining throughput.

    GOAL: Maximize a multi-objective score:
      - SLO attainment (50%): fraction of ALL requests (including rejected) meeting latency targets.
        Rejected requests count as SLO misses. So rejecting critical hurts a lot.
      - Throughput (30%): completed / total requests, capped at 85%. Shedding up to 15% of
        requests is FREE — no throughput penalty. Only aggressive rejection (>15%) is penalized.
      - Jain fairness (20%): equal completion rates across tenants.

    BASELINE: Admit everything (always-admit). Under overload: SLO attainment ~40%,
    throughput at cap, fairness ~90%. Score ~0.48. You must beat this.

    STRATEGY HINT: Shed low-priority classes (batch, background) when cluster load is high.
    This improves SLO attainment (critical meets targets) at zero throughput cost (under cap).

    AVAILABLE SIGNALS — from request:
      - inputLen: number of input tokens (cost proxy)
      - sloClass: "critical", "standard", "sheddable", "batch", or "background"
      - tenantID: unique tenant identifier

    AVAILABLE SIGNALS — from cluster state:
      - numInstances: number of instances (4)
      - totalInFlight: sum of InFlightRequests across all instances (FRESH every call)
      - totalQueueDepth: sum of QueueDepth across instances (stale up to 5s)
      - maxKVUtil: highest KVUtilization across instances (stale up to 5s)
      - avgKVUtil: mean KVUtilization (stale up to 5s)
      - minFreeKV: fewest FreeKVBlocks on any instance (stale up to 5s)
      - clock: simulation clock in microseconds

    AVAILABLE STATE — persistent across calls (read/write via a.*):
      - a.tenantTokens[tenantID]: float64 budget per tenant
      - a.tenantRequests[tenantID]: int counter per tenant
      - a.classCounters[sloClass]: int counter per SLO class
      - a.windowStart, a.windowCount: sliding window tracking
      - a.totalAdmitted, a.totalRejected: lifetime counters
      - a.lastClock: previous call's clock value

    Return (true, "") to admit, (false, "reason") to reject.

    KEY INSIGHT from routing experiments: InFlightRequests is the best load signal
    (fresh, router-local). KVUtilization is useful as a safety filter (>90% = memory pressure).
    Stale signals (QueueDepth) should not be primary decision factors.

    RULES (compilation failures waste an iteration):
    1. Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
    2. Must be valid Go — guard all divisions to avoid divide-by-zero
    3. Must return (bool, string) — true to admit, false to reject
    4. Use the pre-provisioned state maps — don't add struct fields
    5. All map reads on tenantTokens/tenantRequests/classCounters are safe (pre-initialized)

    EVALUATION: 2 overload workloads with heterogeneous tenants.
      - overload_mixed_slo: sustained 1.3x overload, 3 SLO tiers (critical/standard/batch)
      - bursty_adversary: 1 steady critical tenant + 1 extremely bursty bulk tenant

    SLO TARGETS (E2E ms): calibrated from pilot data — see evaluator.

search:
  type: "adaevolve"
  num_context_programs: 4
  database:
    random_seed: 42

evaluator:
  timeout: 300
  cascade_evaluation: false
```

### 7. Baselines

| Baseline | Description | Expected weakness |
|----------|-------------|-------------------|
| `always-admit` | Admit everything | Queues explode under overload, critical starved |
| `priority-shedding` | Reject in inverse SLO-class order when `totalInFlight > threshold` | No per-tenant fairness; hard threshold = cliff behavior |
| `token-bucket` | capacity=TBD, refillRate=TBD | Rejects all classes equally; wastes capacity at low load |

`priority-shedding` is the naive version of what evolution should discover. If the evolved policy can't beat it, the search failed.

### 8. Comparison Metrics

| Metric | Description |
|--------|-------------|
| `combined_score` | Primary ranking (3-term formula) |
| `slo_attainment` | Fraction of all requests meeting SLO (rejected = miss) |
| `throughput` | completed / num_requests |
| `jain_fairness` | Jain index across tenant completion rates |
| `avg_e2e_ms` | Mean E2E for admitted requests |
| `total_rejected` | Absolute rejection count |
| Per-workload scores | Identifies specialization vs generalization |
| Iterations to best | Search efficiency |
| Build error rate | Framework reliability |

---

## Implementation Steps

### Phase 0: Capacity Calibration (mandatory, ~20 min)

**Purpose**: Determine actual cluster capacity and set overload rates + SLO targets from measured data.

**Tooling**: Write `benchmarks/blis_admission/scripts/calibrate_capacity.py` (~100 lines). The script:
- Builds the sim binary once
- Loops over rates × workloads, running sim with `--results-path` for per-request JSON
- Parses per-request records, computes **per-class** P50/P75/P90/P95
- Outputs a clean table and identifies saturation point
- Writes calibrated values to `calibration.json` for the evaluator to load

1. **Create benchmark skeleton**: directory, initial_program.go, config.yaml, workload YAMLs (with placeholder rates)
2. **Write `calibrate_capacity.py`** — standalone script that calls the sim binary directly
3. **Run rate sweep for BOTH workloads**: 20, 40, 60, 80, 100, 120, 160 QPS
   - 7 rates × 2 workloads × 1 seed = 14 simulation runs (~15 min)
   - For each run, record: per-class P50/P75/P90/P95 E2E, mean queue depth, injected vs num_requests
4. **Find saturation point** (per workload): The rate where P95 E2E exceeds 3× the P95 at the lowest rate (structural criterion, independent of SLO targets)
5. **Set overload rates**: Workload 1 = 1.3x saturation. Workload 2 = 1.1x saturation (gamma bursts push effective rate to ~1.8x)
6. **Set SLO targets** from **per-class** percentiles at 1.0x capacity:
   - critical = P75 E2E for critical-class requests at 1.0x
   - standard = P90 E2E for standard-class requests at 1.0x
   - sheddable = 2× P90 for sheddable at 1.0x
   - batch = 5× P90 for batch at 1.0x
   - background = 10× P90 for batch at 1.0x (very generous)
7. **Set THROUGHPUT_CAP**: `1 - overload_fraction / (1 + overload_fraction)`, rounded up. For 1.3x overload → ~0.85.
8. **Verify baseline scores**: Run `always-admit` at overload rate → score should be ~0.4-0.5 (room to improve)
9. **Verify token-bucket produces rejections**: Run token-bucket at overload → `injected_requests < num_requests`
10. **Calibrate fitness formula**: Verify all 3 components contribute meaningful variance between `always-admit` and `token-bucket`. Adjust weights if any component is constant.

### Phase 1: Setup & Pilot

9. **Finalize workload YAMLs** with calibrated rates from Phase 0
10. **Finalize SLO targets** in evaluator
11. **Pilot run**: `uv run skydiscover-run ... -s topk -i 3` to verify:
    - Go build succeeds with initial program
    - `--results-path` JSON parseable, per-request records have `slo_class`/`tenant_id`/`e2e_ms`
    - INV-1 derivation produces correct rejection count
    - Multi-objective score computed and in expected range
    - `admission.go` restored after evaluation
    - Iteration time is reasonable (~30-60s with single-LLM + single-seed + 2 workloads)

### Phase 2: Run Experiment

12. **Start with openevolve** (proven best in routing study): 50 iterations, single-seed, single-LLM
    - Expected: ~30-40 min total, ~30-60s/iter
13. **Monitor progress** every 2 minutes (same rules as routing — CLAUDE.md)
14. **Verify isolation**: `admission.go` restored, no leaked artifacts
15. **If openevolve succeeds**: Run adaevolve + evox for framework comparison (same config)

### Phase 3: Analysis

16. **Run analysis scripts** (adapted from blis_router)
17. **Robustness validation**: 3 seeds with best policy
18. **Write analysis.md** with multi-baseline comparison
19. **Deployment viability assessment**: Which signals are production-available?

### Phase 4: Extensions (if Phase 2 succeeds)

20. **Multi-LLM validation**: Run best policy with `qwen_14b`
21. **Diurnal workload**: Add `workload_v2_diurnal_heterogeneous.yaml` (4 tenants, time-varying load) — requires validating simulator timing first
22. **Joint evolution**: Evolve admission + routing simultaneously
23. **Transfer test**: Best routing policy + best admission policy together

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Overload rates wrong | Degenerate optimization | **Phase 0 calibration** (mandatory) |
| SLO targets unrealistic | slo_attainment = constant | **Phase 0 calibration** from real measurements |
| LLM generates reject-everything | Score = 0 | Formula strongly penalizes (throughput=0, slo_attainment=0) |
| LLM can't discover stateful strategies | Only finds threshold policies | Pre-provisioned state maps + system prompt examples |
| Multi-objective score noisy | Frameworks chase noise | Start single-seed; add seeds in robustness phase |
| Build errors with diff parsing | Wasted iterations | `diff_based_generation: false` (proven fix) |

---

## Resolved Questions

1. **Fitness formula**: 3 terms (slo_attainment 50%, capped_throughput 30%, fairness 20%). Rejected = SLO miss. Throughput capped at 0.85 to eliminate penalty for moderate shedding. Simpler than original 5-term proposal — clearer gradient, no degenerate strategies.
2. **Overload intensity**: Calibrated in Phase 0, not guessed.
3. **SLO targets**: Calibrated from Phase 0 pilot at 1.0x capacity, not from intuition.
4. **Sim changes**: None. Factory remapping + INV-1 + `--results-path` per-request JSON.
5. **Shared submodule**: Symlink `ln -s ../blis_router/inference-sim`.
6. **diff_based_generation**: `false` (routing proved ~85% build errors with `true`).
7. **MULTI_LLM default**: Off (single qwen_7b) for search. Multi-LLM is a validation pass.
8. **Jain fairness N**: `N = total tenants in workload spec`, not tenants with completions.
9. **Diurnal workload**: Deferred to Phase 4 — simulator supports it (CohortSpec + diurnalWindows), but 24-hour simulated horizon timing is unvalidated. Start with 2 simpler workloads.
10. **`num_requests` for INV-1**: Evaluator must parse workload YAML to get this (not in `--results-path` JSON). Guard: `assert num_requests > 0`.
11. **Throughput cap**: Set at 0.85 (calibrated from 1.3x overload fraction). Eliminates shallow gradient problem where `always-admit` throughput=1.0 punishes any rejection. Smart shedding now scores 0.81 vs always-admit 0.48 (wide gap = strong search signal).
12. **Phase 0 calibration script**: `calibrate_capacity.py` sweeps both workloads at 7 rate points (20-160 QPS), computes per-class percentiles, outputs calibration.json. ~100 lines of Python, ~20 min runtime.
13. **Saturation criterion**: P95 E2E exceeds 3x the P95 at lowest rate (structural, independent of SLO targets).
14. **`slo_class` omitempty**: `RequestMetrics.SLOClass` uses `omitempty` JSON tag. Evaluator must handle missing field (default to "standard").
15. **Binary name**: `simulation_worker` (matching routing evaluator's `-o` flag), not `blis`.

## Open Questions

1. **Per-class rejection tracking**: The evaluator treats rejected requests as SLO misses but can't distinguish "rejected critical" from "rejected batch" without per-class counters in the sim. The `slo_attainment` formula handles this implicitly (rejected = miss regardless of class), but if we need per-class rejection analytics, we'd need a minor sim change (add `rejected_requests` to `MetricsOutput`). Not blocking — the score formula works without it.
2. **Hopeless admits**: Under extreme overload, admitting a request that will definitely miss its SLO is strictly better than rejecting it (throughput credit, same slo_attainment). This incentivizes admitting hopeless requests. The externality (queue congestion hurts other requests) is captured indirectly but weakly. If this proves problematic, consider a queue-depth penalty term. Monitor during Phase 2.
