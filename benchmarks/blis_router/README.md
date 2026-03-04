# BLIS Router Benchmark

Discover improved routing algorithms for the BLIS LLM inference simulator using SkyDiscover's search frameworks.

## What This Evolves

The `WeightedScoring.Route()` method in BLIS's `routing.go`. The baseline uses fixed equal weights on two scorers (prefix-affinity + load-balance). Evolution makes the scoring adaptive based on request properties and instance state.

## Prerequisites

- Go 1.21+ (`go version`)
- Python 3.11+ with uv
- SkyDiscover installed: `uv sync --extra external`

## Quick Start

```bash
# 1. Initialize submodule
git submodule update --init benchmarks/blis_router/inference-sim

# 2. Verify BLIS builds
cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go && cd -

# 3. Run with a single framework (2 iterations for smoke test)
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s topk -i 2 -o outputs/blis_router/smoke_test
```

## Run All Frameworks

```bash
bash benchmarks/blis_router/scripts/run_all_frameworks.sh 50
```

## Compare Results

```bash
python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>/
```

## Directory Structure

```
benchmarks/blis_router/
├── initial_program.py    # routing.go wrapped in Python with EVOLVE-BLOCK markers
├── evaluator.py          # Build + run + score (shared by all frameworks)
├── config.yaml           # SkyDiscover config
├── workloads/            # 3 simulation workload specs
├── routing/              # Routing policy config (prefix-affinity + load-balance)
├── inference-sim/        # BLIS simulator (git submodule)
└── scripts/              # Experiment runners and comparison tools
```

## Scoring

`score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms` (higher is better)

Tested on 3 workloads: cache_warmup, load_spikes, multiturn.
