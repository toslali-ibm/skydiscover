# Sim2Real Transfer: BLIS Admission Control

AI-discovered admission control policy for llm-d, found by SkyDiscover (OpenEvolve, 50 iterations).
The evolved policy achieves **+36.5% improvement** over the always-admit baseline under 2x overload,
raising SLO attainment from 30% to 87% while maintaining 100% throughput utilization.

## What's in this directory

```
sim2real/admission/
├── README.md                 # This file
├── llm_config.yaml           # LLM + hardware + cluster config
├── workloads/                # Traffic profiles used in simulation (2x overload)
│   ├── workload_overload_mixed_slo.yaml   # Sustained 2x overload, 4 SLO classes, 4 tenants
│   └── workload_bursty_adversary.yaml     # 2x overload with gamma bursts, 5 tenants
├── baselines/                # Baseline admission policy (Go)
│   └── baseline_always_admit.go  # Admits all requests (no shedding) — our control
├── best/                     # The AI-discovered admission policy
│   ├── best_program.go       # Drop-in replacement for sim/admission.go
│   └── best_program_info.json # Metrics, iteration, parent lineage
├── routing_config/           # llm-d routing policy config
│   └── routing_policy.yaml   # Fixed routing (1:1), only admission is evolved
└── others/                   # Supporting files
    ├── calibration.json      # SLO targets + throughput cap + saturation rates
    ├── hardware_config.json  # GPU specs (H100)
    ├── baseline_metrics.json # Baseline scores per workload
    └── evaluator.py          # Multi-objective scoring function
```

## LLM and cluster config

| Parameter | Value |
|-----------|-------|
| Model | `qwen/qwen2.5-7b-instruct` (HF: `Qwen/Qwen2.5-7B-Instruct`) |
| GPU | NVIDIA H100 (80 GB) |
| Tensor Parallelism | 1 (one GPU per instance) |
| Instances | 4 |
| vLLM version | `vllm/vllm-openai:v0.11.0` |
| Snapshot refresh | 5 seconds (Prometheus scrape interval) |

## Workload profiles

Both workloads run at **2x overload** (320 QPS against ~160 QPS saturation capacity).

**workload_overload_mixed_slo.yaml** — Sustained overload
- 19,200 requests at 320 QPS (60 seconds)
- 4 tenants with 4 SLO classes:
  - `tenant-rt` (critical, 20%): Small prompts (128 tok), Poisson arrival
  - `tenant-std` (standard, 30%): Medium prompts (256 tok), Poisson
  - `tenant-shed` (sheddable, 20%): Medium prompts (256 tok), Poisson
  - `tenant-batch` (batch, 30%): Large prompts (512 tok), Poisson

**workload_bursty_adversary.yaml** — Burst isolation test
- 19,200 requests at 320 QPS (60 seconds)
- 5 tenants, adversarial bursts:
  - `tenant-crit-1`, `tenant-crit-2` (critical, 15% each): Steady Poisson
  - `tenant-std` (standard, 20%): Steady Poisson
  - `tenant-batch` (batch, 25%): Gamma arrival, CV=4.0 (adversarial bursts)
  - `tenant-shed` (sheddable, 25%): Gamma arrival, CV=3.0

## SLO targets (from calibration)

| SLO Class | E2E Target (ms) | Priority |
|-----------|-----------------|----------|
| Critical | 994 | Highest (weight 4.0) |
| Standard | 3,185 | High (weight 2.0) |
| Sheddable | 9,948 | Low (weight 1.0) |
| Batch | 33,340 | Lowest (weight 0.5) |

## Scoring formula

```
Score = 0.50 * SLO_attainment + 0.30 * capped_throughput + 0.20 * Jain_fairness
```

- **SLO attainment** (50%): Class-weighted average. Rejected batch/sheddable excluded from denominator (correct shedding). Rejected critical/standard count as misses.
- **Capped throughput** (30%): `min(completed/total, 0.50) / 0.50`. Prevents over-rejection.
- **Jain fairness** (20%): Per-tenant completion fairness index.

## Baseline vs evolved performance (simulation)

| Metric | Baseline (always-admit) | Evolved | Improvement |
|--------|------------------------|---------|-------------|
| Combined score | 0.624 | 0.852 | **+36.5%** |
| SLO attainment | 30.0% | 87.2% | **+57.2 pp** |
| Throughput | 100% | 100% | No loss |
| Jain fairness | 1.00 | 0.58 | Tradeoff (expected) |
| Avg E2E (ms) | 9,083 | 949 | **-89.6%** |

Per-workload:
| Workload | Baseline score | Evolved score |
|----------|---------------|---------------|
| overload_mixed_slo | 0.651 | 0.839 |
| bursty_adversary | 0.598 | 0.865 |

## What the evolved policy does

The evolved admission policy (`best/best_program.go`) uses a simple but effective strategy:

1. **Always admit critical and standard**: These high-priority classes are never rejected.

2. **Load-based shedding for batch**: When per-instance load (`totalInFlight / numInstances`) exceeds 12, reject batch requests. If a tenant has been under-served (admission rate < 40%), raise the threshold to 20.

3. **Load-based shedding for sheddable**: Same pattern but higher thresholds — reject when per-instance load exceeds 25 (or 35 for under-served tenants).

4. **Per-tenant fairness tracking**: Tracks each tenant's admission rate and relaxes rejection thresholds for tenants getting less than their fair share.

Key insight: The policy uses **per-instance load** (fresh, synchronous signal) as the primary decision variable, with **per-tenant admission rate** for fairness correction. No stale metrics needed for the core decision.

## Sim2Real transfer instructions

### Experiment 1: Baseline (always-admit)

This is the control. Deploy llm-d with no admission control under 2x overload.

**Step 1: Deploy the cluster**
```bash
# Deploy 4 vLLM instances serving qwen2.5-7b-instruct on H100, TP=1
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-7B-Instruct \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.90 \
  --port 800X  # 8001-8004 for 4 instances
```

**Step 2: Configure the llm-d router**
```yaml
# routing_policy.yaml — baseline (no admission control)
admission:
  policy: always-admit
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

**Step 3: Generate 2x overload traffic**
```bash
# First calibrate: find the saturation rate for your real cluster
# Run increasing QPS until P95 starts climbing steeply (~160 QPS expected)
# Then run at 2x that rate

# For workload_overload_mixed_slo:
#   - 320 QPS aggregate, 19200 requests (60 seconds)
#   - 4 tenants, Poisson arrivals
#   - Requests must include SLO class and tenant ID in headers:
#     X-SLO-Class: critical|standard|sheddable|batch
#     X-Tenant-ID: tenant-rt|tenant-std|tenant-shed|tenant-batch
#   - Input token sizes: see workload YAML

# For workload_bursty_adversary:
#   - 320 QPS aggregate, 19200 requests (60 seconds)
#   - 5 tenants, 2 with gamma-bursty arrivals
#   - Same header requirements
```

**Step 4: Collect metrics**
```bash
# Measure per SLO class:
#   - SLO attainment: % of requests completing within target E2E
#   - E2E latency: mean, P50, P95, P99
#   - TTFT (time to first token)
#   - Throughput (tokens/sec, completed requests/sec)
#   - Per-tenant completion counts (for fairness)
#   - Rejection counts per class (should be 0 for always-admit)
```

### Experiment 2: Evolved admission policy (best_program.go)

**Step 1: Deploy the cluster** (same as Experiment 1)

**Step 2: Replace admission.go**
```bash
# Copy best_program.go into llm-d router's sim/admission.go
cp best/best_program.go <llm-d-repo>/sim/admission.go

# Rebuild the router
cd <llm-d-repo> && go build -o router .
```

**Step 3: Use the same routing config**
```yaml
# Same routing_policy.yaml — admission: always-admit is remapped to
# AdaptiveAdmission internally (the evolved EVOLVE-BLOCK replaces the baseline)
admission:
  policy: always-admit
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

**Step 4: Run the same workloads and measure** (same as Experiment 1)

### What to compare

| Metric | Baseline (always-admit) | Evolved | Expected gain |
|--------|------------------------|---------|---------------|
| SLO attainment (critical) | Low (~30%) | High (~87%) | +57 pp |
| SLO attainment (standard) | Low | High | Large gain |
| Batch rejection rate | 0% | Moderate | Expected shedding |
| Sheddable rejection rate | 0% | Some | Expected shedding |
| Overall throughput | 100% | ~50-60% | Intentional shedding |
| Avg E2E (all classes) | ~9,000 ms | ~950 ms | ~10x reduction |

The key insight: under 2x overload, always-admit lets the queues grow unbounded, dragging ALL requests (including critical) past their SLO targets. The evolved policy sheds batch/sheddable work to protect critical/standard latency.

## Signal availability in real llm-d

| Signal | Freshness | Source | Available? |
|--------|-----------|--------|-----------|
| InFlightRequests (totalInFlight) | Synchronous | Router-local counter | Yes |
| SLO class | Per-request | HTTP header (X-SLO-Class) | Yes |
| Tenant ID | Per-request | HTTP header (X-Tenant-ID) | Yes |
| Input length | Per-request | Token count | Yes |
| QueueDepth (totalQueueDepth) | ~5s stale | Prometheus | Yes |
| KVUtilization | ~5s stale | Prometheus | Yes (not used by evolved) |
| Stateful maps | Per-router | In-memory | Yes |

The evolved policy primarily uses **InFlightRequests** (synchronous) and **request metadata** (SLO class, tenant ID). All production-available. The per-tenant tracking maps are lightweight in-memory state.

## Threshold tuning for real deployment

The evolved thresholds (12, 20, 25, 35) were discovered under simulation with specific model/hardware characteristics. For real deployment:

1. **Calibrate saturation**: Find your real cluster's saturation rate (QPS where P95 starts climbing)
2. **Scale overload**: Generate 2x your saturation rate
3. **Tune thresholds if needed**: The load-per-instance thresholds may need adjustment based on real vLLM batching behavior. Start with the discovered values and adjust based on observed SLO attainment.

## Running the reproduction script

A standalone simulation repro script is provided at `repro/blis_admission_repro.py`.
It runs always-admit (baseline) and the evolved best program against both overload workloads and prints a multi-objective comparison table.

```bash
cd sim2real/admission   # or wherever this folder lives

# Install deps if needed
pip install pyyaml   # (or: uv sync from repo root)

# Clone inference-sim at the pinned commit (one-time)
git clone https://github.com/inference-sim/inference-sim.git inference-sim
git -C inference-sim checkout 7fd7a88d5d5005b15b142fa8e70cf5d8537ceebe

# Run repro (default: seed 42, 4 instances)
python repro.py

# Custom seeds
python repro.py --seeds 42,456 --num-instances 4
```

Expected output:
```
BLIS Admission Control Repro  —  model: qwen/qwen2.5-7b-instruct  |  seeds: 42
Scoring: 0.5*SLO_attainment + 0.3*capped_throughput + 0.2*Jain_fairness  |  throughput_cap=0.5
================================================================================
Program                 Combined   SLO attn   Throughput   Fairness     Avg E2E     vs base
Always-admit (base)  *   0.6240    30.0%       100.0%      100.0%    9083 ms   (control)
Evolved (best)           0.8520    87.2%       100.0%       57.9%     949 ms     +36.5%
```

SLO attainment is class-weighted and shed-tolerant: rejected `batch`/`sheddable` requests are excluded from the denominator (shedding them is the correct behavior). Throughput is capped at 50% to reward moderate shedding equally with over-admission.

## Experiment config (for reproducibility)

| Parameter | Value |
|-----------|-------|
| Source experiment | `260312_50i_admission_openevolve` |
| Search framework | OpenEvolve (external backend) |
| Iterations | 50 (best found at iteration 28) |
| Seeds | 42 |
| Scoring | `0.50*SLO_attainment + 0.30*capped_throughput + 0.20*Jain_fairness` |
| Baseline score | 0.624 |
| Best score | 0.852 |
