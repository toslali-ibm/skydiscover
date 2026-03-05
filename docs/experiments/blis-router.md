# BLIS Router Experiment Guide

## Overview

This guide covers how to run BLIS router optimization experiments using SkyDiscover. The goal is to discover improved routing algorithms for a 4-instance LLM inference cluster by evolving the `WeightedScoring.Route()` method.

## Prerequisites

- Go 1.21+ (`go version`)
- Python 3.11+
- SkyDiscover installed with external backends: `uv sync --extra external`
- inference-sim submodule initialized: `git submodule update --init benchmarks/blis_router/inference-sim`
- BLIS builds: `cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go`

## Experiment Structure

Each experiment run creates a timestamped directory:

```
outputs/blis_router/<YYYYMMDD_HHMMSS>/
├── adaevolve/
│   ├── best/                  # Best discovered program
│   ├── logs/                  # SkyDiscover logs
│   ├── baseline_metrics.json  # Baseline scores for this experiment
│   └── run.log               # Full stdout/stderr
├── evox/
│   └── ...
├── openevolve/
│   └── ...
├── comparison_table.csv       # Cross-framework comparison
└── ...
```

## Running Experiments

### Single Framework

```bash
# Set output directory for artifacts
export BLIS_OUTPUT_DIR="outputs/blis_router/my_experiment/adaevolve"
export BLIS_SEED="42"
mkdir -p "$BLIS_OUTPUT_DIR"

uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s adaevolve \
  -i 50 \
  -o "$BLIS_OUTPUT_DIR" \
  -l INFO
```

### All Frameworks (Sequential)

```bash
# Usage: bash scripts/run_all_frameworks.sh [iterations] [seed]
bash benchmarks/blis_router/scripts/run_all_frameworks.sh 50 42
```

### Smoke Test (2 iterations)

```bash
export BLIS_OUTPUT_DIR="/tmp/blis_smoke"
mkdir -p "$BLIS_OUTPUT_DIR"
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s topk -i 2 -o "$BLIS_OUTPUT_DIR"
```

## Reproducibility

### Seeds

Reproducibility is guaranteed by three seed layers:

1. **BLIS simulation seed** (`BLIS_SEED` env var, default `42`): Controls workload generation (request arrivals, token counts). Passed as `--seed` to every BLIS simulation run. Also hardcoded in each workload YAML (`seed: 42`).

2. **SkyDiscover random seed** (`random_seed` in config.yaml, default `42`): Controls search algorithm randomness (island selection, mutation sampling, etc.).

3. **LLM temperature** (`temperature` in config.yaml): Controls LLM output diversity. Set to `1.0` for exploration; set to `0.0` for exact reproduction of LLM calls (requires same model version).

To reproduce an experiment exactly:
- Use the same seed values
- Use the same LLM model
- Use the same config.yaml
- Use the same inference-sim commit (check `git -C benchmarks/blis_router/inference-sim rev-parse HEAD`)

### Logging

Every experiment logs its configuration, seed, and inference-sim commit hash. Check `run.log` in the output directory.

## Experiment Isolation Guarantees

The evaluator enforces these isolation properties:

1. **routing.go is always restored**: After every `evaluate()` call, `routing.go` is restored to its original content via a `try/finally` block. No evaluation can corrupt the source tree.

2. **Artifacts go to output directory**: `baseline_metrics.json` and all evaluation artifacts are written to `BLIS_OUTPUT_DIR`, not the benchmark directory. Set this env var before every experiment.

3. **No cross-experiment data leaks**: The `run_all_frameworks.sh` script:
   - Sets `BLIS_OUTPUT_DIR` per framework
   - Cleans `__pycache__` between frameworks
   - Verifies `routing.go` checksum between runs
   - Restores `routing.go` from git if tampered

4. **Deterministic simulation**: Same seed + same routing code = same simulation output. The BLIS simulator is deterministic given the same seed.

## Guardrails for Claude Sessions

When a Claude session runs BLIS experiments, it MUST follow these rules:

### Before Starting

1. Run the pilot study to verify the pipeline: `python benchmarks/blis_router/scripts/pilot_study.py`
2. Verify `routing.go` matches the submodule: `cd benchmarks/blis_router/inference-sim && git diff sim/routing.go`
3. Ensure no stale artifacts exist: `ls benchmarks/blis_router/baseline_metrics.json` should fail

### During Experiments

4. Run frameworks **sequentially** (adaevolve → evox → openevolve → gepa → shinkaevolve). They share `routing.go` and cannot run in parallel.
5. Always set `BLIS_OUTPUT_DIR` to the experiment output directory before calling `skydiscover-run`.
6. Always set `BLIS_SEED` for reproducibility.
7. After each framework completes, verify:
   - `routing.go` is unchanged from the original (evaluator restores it, but verify)
   - No `baseline_metrics.json` in the benchmark directory
   - No `__pycache__` in the benchmark directory
   - Results exist in the expected output directory

### After Experiments

8. Run `python benchmarks/blis_router/scripts/compare_results.py <output_dir>` to generate comparison tables.
9. Run `python benchmarks/blis_router/scripts/plot_results.py <output_dir>` to generate accuracy plots (saved to `<output_dir>/plots/`). Plots include a baseline bar and annotate each framework with % improvement vs baseline. Reads `baseline_metrics.json` from the first framework subdirectory.
10. Run `python benchmarks/blis_router/scripts/analyze_effort.py <output_dir>` to generate effort/cost analysis (see [Post-Experiment Analysis](#post-experiment-analysis) below).
11. Record the exact experiment configuration (seed, iterations, model, inference-sim commit) in the output directory.
12. **Write `analysis.md`** in the output directory summarizing both accuracy AND effort/cost results. It must include:
    - Accuracy comparison table (scores, % improvement vs baseline)
    - Effort summary table (iterations, wall time, avg/median iteration time, population size, unique scores, generation depth)
    - Search efficiency table (% improvement per wall-clock minute)
    - Convergence observations (iterations to best, % wasted compute)
    - AdaEvolve island details (if applicable)
    - Population quality comparison (spread, diversity ratio)
    - Key takeaways comparing accuracy vs cost tradeoffs
    - Experiment configuration (seed, model, inference-sim commit)
13. Do NOT delete or modify output directories — they are the permanent record.

## Scoring

```
score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms
```

- Higher is better (less negative = lower latency)
- Tested on 3 workloads: cache_warmup, load_spikes, multiturn
- Baseline score with equal weights: approximately -4278

## Workloads

| Workload | Tests | Typical Baseline E2E |
|----------|-------|---------------------|
| cache_warmup | Prefix cache effectiveness under warming | ~4400ms |
| load_spikes | Routing under bursty arrivals | ~3300ms |
| multiturn | Session-aware routing for multi-turn conversations | ~160ms |

## Post-Experiment Analysis

After all frameworks complete, run **three** analysis scripts to get a complete picture of both accuracy and cost/effort:

```bash
RESULTS_DIR="outputs/blis_router/<YYYYMMDD_HHMMSS>"

# 1. Accuracy comparison (existing)
python benchmarks/blis_router/scripts/compare_results.py "$RESULTS_DIR"
python benchmarks/blis_router/scripts/plot_results.py "$RESULTS_DIR"

# 2. Effort/cost analysis (NEW)
python benchmarks/blis_router/scripts/analyze_effort.py "$RESULTS_DIR"
```

### What `analyze_effort.py` produces

**Console output**: Summary table with per-framework effort metrics:
- **Iterations**: Number of completed iterations
- **Wall time**: Total wall-clock time (minutes)
- **Avg/Median iteration time**: Duration per generate-evaluate cycle (seconds)
- **Population size**: Final archive/population size at end of run
- **Unique scores**: Number of distinct scores in final population (diversity measure)
- **Max generation**: Deepest mutation depth (genealogy depth)
- **Best iteration**: Which iteration found the best program
- **Score + % improvement**: Best score vs baseline

For AdaEvolve, additionally reports per-island details:
- Per-island population, improvements, total evaluations, productivity (improvement rate)
- Search intensity per island (exploration vs exploitation balance)
- Diversity strategy, stagnation state, active paradigms

**Output files** (saved to `<results_dir>/`):
| File | Contents |
|------|----------|
| `effort_analysis.csv` | Per-framework effort metrics (machine-readable) |
| `effort_analysis.json` | Full analysis with island details, trajectories, etc. |
| `plots/iteration_duration_boxplot.png` | Box plot of iteration durations across frameworks |
| `plots/convergence_curves.png` | Best score over iterations (with baseline reference) |
| `plots/effort_vs_improvement.png` | Scatter: wall time vs score improvement (bubble = population size) |
| `plots/search_efficiency.png` | Bar chart: % improvement per wall-clock minute |

### Metrics available per framework

| Metric | Source | All frameworks | AdaEvolve only |
|--------|--------|:-:|:-:|
| Iteration duration (avg, median, min, max, stddev) | Log files | ✓ | ✓ |
| Total wall time | Log files | ✓ | ✓ |
| Final population size | Checkpoint programs | ✓ | ✓ |
| Unique score count (diversity) | Checkpoint programs | ✓ | ✓ |
| Max/avg mutation generation depth | Checkpoint programs | ✓ | ✓ |
| Best score + iteration found | best_program_info.json | ✓ | ✓ |
| Score convergence trajectory | Iteration stats JSONL | | ✓ |
| Number of islands | AdaEvolve metadata | | ✓ |
| Per-island productivity | Iteration stats JSONL | | ✓ |
| Per-island search intensity | Iteration stats JSONL | | ✓ |
| UCB selection stats | Iteration stats JSONL | | ✓ |
| Paradigm breakthrough state | Iteration stats JSONL | | ✓ |
| Diversity strategy type | AdaEvolve metadata | | ✓ |

### Interpreting results

**Efficiency**: The `search_efficiency.png` plot shows which algorithm gets the most improvement per wall-clock minute. A framework with lower absolute improvement but much less wall time may be more practical.

**Convergence**: The `convergence_curves.png` plot reveals whether a framework is still improving at the end of the run (suggesting more iterations would help) or has plateaued (diminishing returns).

**Effort vs. improvement**: The scatter plot (`effort_vs_improvement.png`) lets you compare the cost-effectiveness of different approaches. Frameworks in the upper-left quadrant (high improvement, low cost) are ideal.

**Diversity**: Low `unique_scores` relative to `population_size` suggests the search is converging to similar solutions. High diversity with low improvement may indicate the search is too exploratory.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `uv` not found | Run `pip3 install uv` to install it |
| `Provider 'aws' requires api_base` | Do NOT use `-m` flag — models are defined in config.yaml with api_base |
| EvoX 401 on label generation (`gpt-5-mini`) | Fixed: `search/evox/controller.py` now propagates parent LLM config to the search controller. The root cause was that EvoX's internal `search.yaml` defaults to OpenAI models/api_base, and these weren't overridden by the user's config. |
| `go build` fails | Check Go version, run `go version`. Ensure inference-sim submodule is initialized. |
| All workloads fail | Check `stderr` in workload results. Usually a Go compilation error in evolved code. |
| Score is -100000 | Error sentinel — check `error` and `artifacts.error_type` in result dict. |
| `routing.go` corrupted | Run `cd benchmarks/blis_router/inference-sim && git checkout sim/routing.go` |
| Baseline not computed | Ensure `BLIS_OUTPUT_DIR` is set and writable. |
