# Design: BLIS Admission Control Discovery Experiments

## Goal

Discover adaptive admission control policies for multi-tenant LLM inference clusters using the same evolutionary framework used for routing optimization. Compare search frameworks on their ability to find policies that balance the **impossible triangle**:

1. **Utilization**: Maximize throughput — don't reject too aggressively
2. **Fairness**: Equal service quality across tenants (Jain fairness index)
3. **Isolation**: Protect critical tenants from bad actors and bursty bulk traffic

Static admission policies (always-admit, token-bucket) fail with heterogeneous tenants and dynamic traffic because they can't simultaneously optimize all three. An evolved adaptive policy observes cluster state and request metadata to make context-dependent admit/reject decisions.

---

## Background

### What We're Evolving

The `AdaptiveAdmission.Admit()` method in BLIS's `admission.go`. The baseline admits everything (identical to `always-admit`). Evolution makes this adaptive based on request properties (`SLOClass`, `TenantID`, `InputTokens`) and cluster state (`InFlightRequests`, `KVUtilization`, `QueueDepth`).

### Existing Work (BLIS Router Benchmark)

The routing benchmark (`benchmarks/blis_router/`) provides the proven pattern:
- `initial_program.go`: Full Go source file with EVOLVE-BLOCK markers, replaces `sim/routing.go` during evaluation
- `evaluator.py`: Extract Go → write file → `go build` → run simulations → parse metrics → return score
- Sequential evaluation (shared Go file), ~30-60s per iteration
- Scoring: `score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms`

This benchmark reuses the same infrastructure, replacing: (a) the file being evolved (`admission.go` instead of `routing.go`), (b) the fitness function (multi-objective instead of pure latency), and (c) the workloads (overload scenarios instead of balanced load).

### Key Differences from Routing Evolution

| Aspect | Routing | Admission Control |
|--------|---------|-------------------|
| What it controls | Which instance handles a request | Whether a request enters the system at all |
| Fitness landscape | Unimodal (lower latency = better) | Multi-objective with tension (reject more → better latency but worse utilization) |
| Search space | Stateless scoring math | Stateful strategies (rate trackers, per-tenant budgets, sliding windows) |
| Workload requirement | Balanced load (below capacity) | **Overload** (above capacity, forcing selective shedding) |
| File evolved | `sim/routing.go` | `sim/admission.go` |

### Key Constraint

Same as routing: evaluation is **sequential** because each iteration writes to the same `admission.go`, rebuilds the Go binary, then runs simulations. ~30-60s per evaluation.

---

## Design

### 1. Benchmark Directory Structure

```
benchmarks/blis_admission/
├── initial_program.go          # Full admission.go with AdaptiveAdmission + EVOLVE-BLOCK
├── evaluator.py                # Build BLIS, run overload workloads, return multi-objective score
├── config.yaml                 # SkyDiscover config with admission-specific system prompt
├── README.md
├── workloads/                  # Overload workload specs (v2 format)
│   ├── workload_v2_overload_mixed_slo.yaml
│   ├── workload_v2_bursty_adversary.yaml
│   └── workload_v2_diurnal_heterogeneous.yaml
├── routing/                    # Policy bundle — routing stays fixed, admission is "adaptive"
│   └── routing_policy.yaml
├── inference-sim/              # Git submodule (same as blis_router)
└── scripts/
    ├── run_all_frameworks.sh
    ├── compare_results.py
    ├── plot_results.py
    ├── analyze_effort.py
    └── analyze_diffs.py
```

### 2. The Initial Program

The initial program is a complete replacement for `sim/admission.go`. It must include all existing types (`AlwaysAdmit`, `TokenBucket`, `RejectAll`) because other code references them. It adds `AdaptiveAdmission` with pre-provisioned state and an EVOLVE-BLOCK.

**Source**: `docs/study/admission_initial_program.go` (already drafted).

Key design decisions:
- **Pre-derived signals above the EVOLVE-BLOCK** (fixed, not mutable): `numInstances`, `totalInFlight`, `totalQueueDepth`, `maxKVUtil`, `avgKVUtil`, `minFreeKV`, `inputLen`, `sloClass`, `tenantID`, `clock`
- **Pre-provisioned mutable state on the struct**: `tenantTokens`, `tenantRequests`, `classCounters`, `windowStart`, `windowCount`, `totalAdmitted`, `totalRejected`, `lastClock`
- **Baseline behavior**: `return true, ""` (admit everything — identical to `always-admit`)
- **Factory trick**: No new policy name needed. The `"always-admit"` case in `NewAdmissionPolicy` is remapped to `return NewAdaptiveAdmission()`. Since the baseline EVOLVE-BLOCK is `return true, ""`, behavior is identical to the original `AlwaysAdmit`. The YAML stays `policy: always-admit`, and `bundle.go` is untouched — **zero inference-sim changes required**.

The LLM can only mutate the block between `EVOLVE-BLOCK-START` and `EVOLVE-BLOCK-END`. Everything else is frozen.

### 3. The Evaluator

Same pattern as the routing evaluator. Key differences noted with **[DIFF]**.

```python
# benchmarks/blis_admission/evaluator.py
def evaluate(program_path: str) -> dict:
    # 1. Extract Go code from evolved program file
    # 2. [DIFF] Save original admission.go, write evolved code to inference-sim/sim/admission.go
    # 3. go build -o blis main.go
    # 4. [DIFF] For each (model, seed, workload) combination:
    #      Run: ./blis run --model <id> --hardware H100 --tp 1
    #                      --num-instances 4 --policy-config routing_policy.yaml
    #                      --workload-spec <workload.yaml> --seed <seed>
    #                      --snapshot-refresh-interval 5000000
    #                      --results-path <tmpfile.json>    # [DIFF] enables per-request output
    #                      --log info
    # 5. [DIFF] Parse ALL metrics from --results-path JSON file (not stdout).
    #    The file is always written even when CompletedRequests==0 (reject-all edge case).
    #    Extract: injected_requests, completed_requests, e2e stats, per-request records.
    #    Derive: rejected = num_requests (from workload YAML) - injected_requests (INV-1).
    # 6. [DIFF] Compute multi-objective score from per-request records (slo_class, tenant_id, e2e_ms)
    # 7. [DIFF] Clean up tmpfile after parsing
    # 8. [DIFF] Restore original admission.go in finally block
    return {
        "combined_score": score,
        # --- Multi-objective components ---
        "slo_attainment": ...,          # fraction of per-class SLO targets met [0,1]
        "jain_fairness": ...,           # JainFairnessIndex across tenants [0,1]
        "utilization": ...,             # completed / (completed + rejected) [0,1]
        "critical_rejection_rate": ..., # 1 - (completed_critical / expected_critical) [0,1]
        "avg_e2e_ms": ...,              # mean E2E for admitted requests
        "avg_p95_ms": ...,              # P95 E2E for admitted requests
        # --- Per-workload breakdown ---
        "overload_mixed_slo_score": ...,
        "bursty_adversary_score": ...,
        "diurnal_heterogeneous_score": ...,
        # --- Diagnostics ---
        "total_rejected": ...,
        "success_rate": ...,
        "artifacts": { ... },
    }
```

**[DIFF] Model/hardware configuration**: Same as the routing evaluator — two models evaluated by default:

| Short name | Model ID | Hardware | TP |
|------------|----------|----------|----|
| `qwen_7b` | `qwen/qwen2.5-7b-instruct` | H100 | 1 |
| `qwen_14b` | `qwen/qwen3-14b` | H100 | 1 |

Controlled by `BLIS_MULTI_LLM` env var (default: on). Same `BLIS_SEED`, `BLIS_NUM_INSTANCES` env vars as routing evaluator.

**[DIFF] Fitness formula** (multi-objective, higher = better):

```
score = 0.30 * slo_attainment
      + 0.25 * jain_fairness
      + 0.20 * utilization
      - 0.15 * critical_rejection_rate
      - 0.10 * normalized_p95
```

Where:
- `slo_attainment` = fraction of completed requests meeting per-class E2E targets. Computed in Python from per-request JSON (via `--results-path`): for each request, `e2e_ms <= slo_target[slo_class]`.
- `jain_fairness` = Jain index across tenant throughputs. Computed in Python: group completed requests by `tenant_id`, count per-tenant, apply `(sum(xi))^2 / (N * sum(xi^2))`.
- `utilization` = `completed_requests / (completed_requests + rejected_requests)`. Both fields from stdout JSON.
- `critical_rejection_rate` = `1 - (completed_critical / expected_critical)` where `expected_critical = num_requests * critical_rate_fraction` (proxy — rejected requests have no per-request records)
- `normalized_p95` = weighted average of per-class `p95_e2e_ms / class_slo_target_ms`, clamped to [0,1]. Uses the per-class SLO targets above as denominators.

**[DIFF] SLO targets** (E2E latency, ms):

| SLO Class | Target |
|-----------|--------|
| critical | 500 |
| standard | 2000 |
| sheddable | 5000 |
| batch | 10000 |
| background | 30000 |

**[DIFF] Extracting new metrics from simulator output**:

**No sim changes required.** All metrics are derived from existing output:

- **Primary data source**: `--results-path <tmpfile>` JSON file. This is always written (even when `CompletedRequests == 0`), unlike stdout JSON which is suppressed when no requests complete. The file contains both aggregate metrics (`injected_requests`, `completed_requests`, latency stats) and per-request records (`slo_class`, `tenant_id`, `e2e_ms`). Tmpfile is cleaned up after parsing.
- **`rejected_requests`**: Derived via INV-1: `rejected = num_requests - injected_requests`. `num_requests` is from the workload YAML; `injected_requests` is in the `--results-path` JSON.
- **`utilization`**: `completed_requests / (completed_requests + rejected_requests)`. Both values available from the JSON file.

**Evaluator strategy** (all computed in Python from `--results-path` data):
- `slo_attainment`: For each completed request in the `requests` array, check `e2e_ms <= slo_target[slo_class]`, take fraction.
- `jain_fairness`: Group completed requests by `tenant_id`, count per-tenant throughput, apply Jain formula `(sum(xi))^2 / (N * sum(xi^2))`.
- `critical_rejection_rate`: `1 - (completed_critical / expected_critical)` where `expected_critical = num_requests * critical_rate_fraction` (from workload spec).
- `utilization`: `completed_requests / (completed_requests + rejected_requests)` using INV-1 derivation above.

**Edge case**: If the evolved policy rejects everything, `CompletedRequests == 0`, `injected_requests == 0`, `utilization == 0`, `slo_attainment == 0`, `jain_fairness == 0`. The combined score will be very low (~0.0), providing strong negative signal to the search framework.

### 4. Policy Bundle

```yaml
# benchmarks/blis_admission/routing/routing_policy.yaml
admission:
  policy: always-admit        # Factory remapped in initial_program.go to AdaptiveAdmission
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

Routing, priority, and scheduling are **fixed** at their defaults. Only admission is evolved.

**No sim changes required**: The factory remapping in the initial program handles policy creation. The YAML uses `always-admit` which passes existing validation.

### 5. Workloads

All three workloads must produce **overload** — aggregate arrival rate exceeding cluster capacity — to force the admission policy to make non-trivial shedding decisions. Under balanced load, `always-admit` is optimal and there's nothing to evolve.

#### Workload 1: `overload_mixed_slo`

**Tests**: Can the policy shed batch/sheddable to protect critical under sustained overload?

```yaml
version: "2"
seed: 42
category: language
aggregate_rate: 450        # ~1.5x capacity of 4-instance cluster (~300 req/s)
num_requests: 27000        # 60s at 450 req/s

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
aggregate_rate: 400        # ~1.3x capacity at baseline, ~2x during bursts
num_requests: 24000

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

#### Workload 3: `diurnal_heterogeneous`

**Tests**: Can the policy adapt across load levels — permissive at trough, selective at peak — while maintaining fairness across 4 tenants?

```yaml
version: "2"
seed: 42
category: language
aggregate_rate: 500        # peak rate; diurnal modulates this
num_requests: 30000

cohorts:
  - id: enterprise-critical
    tenant_id: tenant-enterprise
    slo_class: critical
    population: 1
    rate_fraction: 0.15
    streaming: true
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 128, std_dev: 32, min: 32, max: 256 } }
    output_distribution: { type: exponential, params: { mean: 64 } }
    diurnal: { peak_hour: 14, peak_to_trough_ratio: 2.5 }

  - id: saas-standard
    tenant_id: tenant-saas
    slo_class: standard
    population: 1
    rate_fraction: 0.30
    streaming: false
    arrival: { process: poisson }
    input_distribution: { type: gaussian, params: { mean: 256, std_dev: 64, min: 64, max: 512 } }
    output_distribution: { type: exponential, params: { mean: 128 } }
    diurnal: { peak_hour: 10, peak_to_trough_ratio: 3.0 }

  - id: research-batch
    tenant_id: tenant-research
    slo_class: batch
    population: 1
    rate_fraction: 0.35
    streaming: false
    arrival: { process: gamma, cv: 2.5 }
    input_distribution: { type: gaussian, params: { mean: 512, std_dev: 128, min: 128, max: 1024 } }
    output_distribution: { type: exponential, params: { mean: 256 } }
    diurnal: { peak_hour: 2, peak_to_trough_ratio: 2.0 }

  - id: background-indexing
    tenant_id: tenant-infra
    slo_class: background
    population: 1
    rate_fraction: 0.20
    streaming: false
    arrival: { process: constant }
    input_distribution: { type: gaussian, params: { mean: 1024, std_dev: 256, min: 256, max: 2048 } }
    output_distribution: { type: exponential, params: { mean: 512 } }
```

### 6. SkyDiscover Config

```yaml
# benchmarks/blis_admission/config.yaml
language: go
file_suffix: .go
diff_based_generation: true
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

    PROBLEM: Static admission control fails with heterogeneous tenants and dynamic traffic.
    You can't simultaneously maintain utilization, enforce fairness, and isolate bad actors
    with fixed thresholds. Your job is to discover an adaptive policy.

    GOAL: Maximize a multi-objective score balancing:
      - SLO attainment (30%): fraction of requests meeting per-class latency targets
      - Jain fairness (25%): equal throughput across tenants
      - Utilization (20%): don't reject too much — maximize completed requests
      - Critical protection (15%): never reject critical-class requests unnecessarily
      - Tail latency (10%): keep P95 E2E low for admitted requests

    BASELINE: Admit everything (always-admit). Under overload, queues explode,
    critical tenants are starved, and tail latency blows up.

    AVAILABLE SIGNALS — from request:
      - inputLen: number of input tokens (cost proxy)
      - sloClass: "critical", "standard", "sheddable", "batch", or "background"
      - tenantID: unique tenant identifier

    AVAILABLE SIGNALS — from cluster state:
      - numInstances: number of instances in the cluster
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

    RULES (compilation failures waste an iteration):
    1. Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
    2. Must be valid Go — guard all divisions to avoid divide-by-zero
    3. Must return (bool, string) — true to admit, false to reject
    4. Use the pre-provisioned state maps — don't add struct fields
    5. All map reads on tenantTokens/tenantRequests/classCounters are safe (pre-initialized)

    EVALUATION: 3 overload workloads with heterogeneous tenants.
      - overload_mixed_slo: sustained 1.5x overload, 3 SLO tiers
      - bursty_adversary: 1 steady critical tenant + 1 extremely bursty bulk tenant
      - diurnal_heterogeneous: 4 tenants with time-varying load patterns

    SLO TARGETS (E2E ms): critical=500, standard=2000, sheddable=5000, batch=10000, background=30000

search:
  type: "adaevolve"
  num_context_programs: 4
  database:
    random_seed: 42

evaluator:
  timeout: 300
  cascade_evaluation: false
```

### 7. Running All Frameworks

Same evaluator, same initial program, same config. Only `--search` changes.

| Framework | Flag | Notes |
|-----------|------|-------|
| AdaEvolve | `--search adaevolve` | Multi-island, UCB exploration |
| EvoX | `--search evox` | Co-evolution of solution + search strategy |
| OpenEvolve | `--search openevolve` | External backend, fuzzy diff matching |

Start with 3 frameworks (the ones proven in the routing study). Add GEPA later if needed.

### 8. Comparison Metrics

#### 8.1 Quality Metrics (per framework, from best discovered program)

| Metric | Description | Source |
|--------|-------------|--------|
| `combined_score` | Multi-objective composite | Primary ranking metric |
| `slo_attainment` | Fraction meeting SLO targets | Higher = better |
| `jain_fairness` | Jain index across tenants | Higher = better (1.0 = perfect) |
| `utilization` | completed / total | Higher = better |
| `critical_rejection_rate` | % critical requests rejected | Lower = better (0 = ideal) |
| `avg_e2e_ms` | Mean E2E for admitted requests | Lower = better |
| `avg_p95_ms` | P95 E2E for admitted requests | Lower = better |
| Per-workload scores | Score on each of the 3 workloads | Identifies specialization vs generalization |

#### 8.2 Cost Metrics (same as routing study)

| Metric | Description |
|--------|-------------|
| Total LLM calls | API call count |
| Total tokens | Input + output token consumption |
| Wall-clock time | Experiment duration |
| Successful evaluations | Programs that compiled + ran |
| Failed evaluations | Build failures, timeouts |
| Iterations to best | When the best solution was found |

#### 8.3 Pareto Analysis

Because admission control has a multi-objective fitness, we should also plot the **Pareto frontier** of discovered policies across:
- Utilization vs SLO attainment
- Utilization vs Jain fairness
- SLO attainment vs Jain fairness

This reveals whether different frameworks find different tradeoff points.

### 9. Baselines to Compare Against

In addition to cross-framework comparison, each evolved policy should be compared to these static baselines:

| Baseline | Config | Expected weakness |
|----------|--------|-------------------|
| `always-admit` | Default | Queues explode under overload, critical starved |
| `token-bucket-conservative` | capacity=1000, refillRate=200 | Rejects critical and batch equally; wastes capacity at low load |
| `token-bucket-aggressive` | capacity=500, refillRate=150 | Better latency but low utilization |

Baseline scores should be computed by the evaluator once and cached (same pattern as routing baseline).

---

## Implementation Steps

### Phase 1: Setup

**No inference-sim changes required.** The factory remapping in the initial program and INV-1 arithmetic in the evaluator eliminate all sim-side changes.

1. **Create `benchmarks/blis_admission/`** with the directory structure above
2. **Symlink inference-sim**: `ln -s ../blis_router/inference-sim benchmarks/blis_admission/inference-sim` (shared submodule — the two benchmarks already can't run concurrently since they share a Go build)
3. **Create initial_program.go**: From `docs/study/admission_initial_program.go`, with `"always-admit"` factory case remapped to `NewAdaptiveAdmission()`
4. **Create evaluator.py**: Adapt from `blis_router/evaluator.py`:
   - Change file target: save/restore `sim/admission.go` (not `routing.go`)
   - Add `--results-path <tmpfile>` to sim command for per-request JSON
   - Compute `rejected_requests` via INV-1: `num_requests - injected_requests`
   - Parse per-request JSON to compute `slo_attainment`, `jain_fairness`, `critical_rejection_rate`
   - Multi-objective scoring formula
5. **Create workload YAMLs**: The 3 overload scenarios defined above
6. **Create routing_policy.yaml**: With `admission: { policy: always-admit }`
7. **Create config.yaml**: With admission-specific system prompt
8. **Pilot run**: `uv run skydiscover-run ... -s topk -i 2` to verify full pipeline:
   - Verify Go build succeeds with the initial program
   - Verify overload workloads produce rejections (check `injected_requests < num_requests` when baseline is evolved)
   - Verify per-request JSON is written and parseable via `--results-path`
   - Verify `admission.go` is restored after evaluation
   - Verify multi-objective score is computed and reasonable

### Phase 2: Run Experiments

12. **Run 3 frameworks**: adaevolve, evox, openevolve — sequentially, 50 iterations each
13. **Monitor progress**: Check logs every 2 minutes (same CLAUDE.md rules as routing)
14. **Verify isolation after each framework**: `admission.go` restored, no leaked artifacts

### Phase 3: Analysis

15. **Run comparison scripts**: Adapt from blis_router scripts for multi-objective metrics
16. **Plot Pareto frontiers**: Utilization vs fairness vs SLO attainment
17. **Compare to static baselines**: always-admit, token-bucket variants
18. **Document findings**: Write `analysis.md` in output directory

### Phase 4: Optional Extensions

19. **Joint evolution**: Evolve admission + routing simultaneously (both EVOLVE-BLOCKs in one file)
20. **Longer runs**: 100+ iterations for frameworks still improving at 50
21. **Transfer test**: Apply best routing policy from routing study + best admission policy from this study

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Multi-objective score is noisy | Frameworks chase noise instead of real improvement | Use 2 seeds, average scores; tune weights on pilot data |
| LLM generates policies that reject everything | Score tanks on utilization term | Fitness formula penalizes over-rejection; `always-admit` baseline provides floor |
| Overload workloads too aggressive | All policies fail equally | Start with 1.3x overload, increase gradually; include calm periods |
| LLM can't discover stateful strategies | Only finds threshold-based policies | Pre-provisioned state maps lower the barrier; system prompt documents state usage |
| Workload rates don't actually produce overload (capacity depends on model/hardware) | Nothing to evolve — always-admit wins | Run pilot with always-admit first; verify queue buildup and >0 rejected requests with token-bucket |
| `JainFairnessIndex` or `SLOAttainment` not in JSON output | Evaluator can't compute score | Use `--results-path` for per-request JSON, compute in Python. Derive `rejected_requests` via INV-1 (`num_requests - injected_requests`). Zero sim changes. |

---

## Resolved Questions

1. **Fitness weights**: Start with 30/25/20/15/10. Calibrate during pilot (Phase 1, step 11) by checking that all components contribute meaningfully to score variance across `always-admit` vs `token-bucket` baselines. Adjust if any component is constant.
2. **Overload intensity**: Pilot run (step 11) with `always-admit` will reveal the actual saturation point. If 450 req/s doesn't produce queue buildup, increase. If queues grow unbounded, decrease. Target: 30-50% of requests experience SLO violations under `always-admit`.
3. **Sim changes required**: None. The factory remapping trick (remap `"always-admit"` → `NewAdaptiveAdmission()` in the replaced `admission.go`) avoids needing a new policy name. `rejected_requests` is derived via INV-1 (`num_requests - injected_requests`) in Python. SLO attainment and Jain fairness are computed from `--results-path` per-request JSON.
4. **Shared submodule**: Use symlink (`ln -s ../blis_router/inference-sim`). No sim changes needed, so the routing benchmark is completely unaffected. Both benchmarks already can't run concurrently (shared Go build directory).

## Open Questions

1. **Per-class rejection tracking**: The `critical_rejection_rate` proxy (`1 - completed_critical / expected_critical`) conflates rejection with in-progress requests at simulation end. If this proves noisy, consider adding per-SLO-class rejection counters to the sim as a Phase 2 enhancement.
2. **Diurnal workload simulation time**: With `CohortSpec` diurnal modulation creating 24 one-hour lifecycle windows, the simulation horizon may be very long. Need to verify during pilot that diurnal workload completes in reasonable time (~120s). If too slow, reduce to a 6-hour simulated window or use only workloads 1 and 2 initially.
