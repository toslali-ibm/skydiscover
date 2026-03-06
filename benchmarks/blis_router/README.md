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

# 3. Run pilot study (validates entire pipeline)
python benchmarks/blis_router/scripts/pilot_study.py

# 4. Run with a single framework (2 iterations for smoke test)
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s topk -i 2 -o outputs/blis_router/smoke_test
```

## Multi-LLM Evaluation

By default, the evaluator tests against a single LLM (Llama 8B, blackbox). Set `BLIS_MULTI_LLM=1` to also test against Mixtral 8x7B MoE (crossmodel estimator), which validates that discovered algorithms generalize across dense and MoE architectures.

```bash
export BLIS_MULTI_LLM=1
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s adaevolve -i 50 -o outputs/blis_router/multi_llm_test
```

| Short Name | Model | Estimator | Hardware |
|------------|-------|-----------|----------|
| `llama_8b` | `meta-llama/llama-3.1-8b-instruct` | blackbox (trained coefficients) | H100, TP=1 |
| `mixtral_8x7b` | `mistralai/mixtral-8x7b-instruct-v0.1` | crossmodel (physics-informed, MoE-aware) | H100, TP=2 |

When multi-LLM is enabled, the combined score averages across all models and workloads. Baseline metrics include per-model breakdowns; evaluation artifacts remain per-workload (averaged across models).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BLIS_SEED` | `42` | Random seed for reproducibility |
| `BLIS_OUTPUT_DIR` | (required) | Experiment output directory for isolation |
| `BLIS_NUM_INSTANCES` | `4` | Number of simulated instances per cluster |
| `BLIS_MULTI_LLM` | `0` | Set to `1` to evaluate against multiple LLMs |

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
With `BLIS_MULTI_LLM=1`, averaged across 2 models x 3 workloads.
