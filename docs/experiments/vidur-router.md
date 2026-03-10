# Vidur Router Experiment Guide

## Overview

This guide covers how to run Vidur router optimization experiments using SkyDiscover. The goal is to discover improved routing algorithms for a 4-replica LLM inference cluster simulated by Microsoft's Vidur (Python discrete-event simulator).

**Paper framing**: This is a **generality** experiment demonstrating SkyDiscover works across simulators with different architectures (Go event-driven BLIS vs Python DES Vidur), fidelity models, and signal surfaces. Smaller gains are expected due to Vidur's reduced signal surface (no prefix caching, no SLO classes, no sessions).

## Prerequisites

- Python 3.10+
- SkyDiscover installed: `uv sync`
- Vidur dependencies: `uv sync --extra vidur` (or install manually: numpy, pandas, scikit-learn, matplotlib, plotly_express)
- Trace CSVs generated: `python benchmarks/vidur_router/scripts/generate_traces.py`
- Pilot study passed: `python benchmarks/vidur_router/scripts/pilot_study.py`

## Naming Convention

```
outputs/vidur_router/<YYMMDD>_<iters>i_<tag>/
```

Same convention as BLIS. Examples:
- `260310_50i_seed42` — 50 iterations, seed 42
- `260312_100i_multi_llm` — 100 iterations, multi-LLM

## Running Experiments

### Single Framework

```bash
export VIDUR_OUTPUT_DIR="outputs/vidur_router/<EXPERIMENT>/<FRAMEWORK>"
export VIDUR_SEED="42"  # or "42,456" for multi-seed
# export VIDUR_MULTI_LLM="0"  # default is "1" (both models)
mkdir -p "$VIDUR_OUTPUT_DIR"

uv run skydiscover-run \
  benchmarks/vidur_router/initial_program.py \
  benchmarks/vidur_router/evaluator.py \
  -c benchmarks/vidur_router/config.yaml \
  -s <FRAMEWORK> \
  -i <ITERATIONS> \
  -o "$VIDUR_OUTPUT_DIR" \
  -l INFO
```

### All Frameworks (sequential)

```bash
./benchmarks/vidur_router/scripts/run_all_frameworks.sh <experiment_name> <iterations>
```

**Available frameworks**: `adaevolve`, `evox`, `openevolve`, `gepa_native`, `topk`, `best_of_n`, `beam_search`

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `VIDUR_OUTPUT_DIR` | `.eval_artifacts` (with warning) | Experiment output directory |
| `VIDUR_SEED` | `"42,456"` | Trace generation seeds (comma-separated) |
| `VIDUR_MULTI_LLM` | `"1"` | Enable multi-LLM (Llama-2-7B + Llama-3-8B) |
| `VIDUR_NUM_REPLICAS` | `"4"` | Number of replicas |
| `VIDUR_TRACE_SCALE` | `"1.0"` | Fraction of trace to use (0-1, for faster search) |

## Workloads

Three workloads converted from BLIS via `scripts/generate_traces.py`:

| Workload | Requests | Original QPS | Description |
|---|---|---|---|
| `cache_warmup` | 12,000 | 200 | Prefix diversity across 4 groups |
| `load_spikes` | 18,000 | 300 | Heavy-hitter prefix with bursty arrival |
| `multiturn` | ~9,000 | 150 | Multi-turn sessions (flattened) |

### What doesn't map from BLIS

- **Prefix caching**: Vidur doesn't model prefix caches — prefix tokens just become extra prefill
- **SLO classes**: No SLO-aware scheduling in Vidur
- **Session affinity**: No session tracking
- **Streaming**: Not modeled as a routing signal

This reduces the optimization surface. LOR is already near-optimal for simple load balancing.

## Scoring Formula

```python
score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms
```

Higher (less negative) is better. Same formula as BLIS.

## Multi-LLM Evaluation

Default evaluates both models:
- `meta-llama/Llama-2-7b-hf` on A100
- `meta-llama/Meta-Llama-3-8B` on A100

Set `VIDUR_MULTI_LLM=0` for single-model (Llama-2-7B only).

## Guardrails for Claude Sessions

### Mandatory Pre-flight

1. **Run pilot study**: `python benchmarks/vidur_router/scripts/pilot_study.py` — all checks must pass
2. **Verify clean state**: No `baseline_metrics.json` or `.eval_artifacts` in `benchmarks/vidur_router/`
3. **Verify traces exist**: `ls benchmarks/vidur_router/workloads/*.csv`

### Mandatory During Experiments

4. **Sequential execution only**: Vidur uses module-level state — frameworks CANNOT run in parallel
5. **Always set `VIDUR_OUTPUT_DIR`**: Never write to the benchmark directory
6. **Monitor every 2 minutes** while a framework is running:
   ```bash
   LOG=$(ls outputs/vidur_router/<EXPERIMENT>/<FRAMEWORK>/logs/*.log | head -1)
   echo "=== PROGRESS ===" && grep -c "Iteration" "$LOG"
   echo "=== BEST ===" && grep "best program score" "$LOG" | tail -3
   echo "=== ERRORS ===" && grep -c "error\|Error\|RuntimeError\|SyntaxError\|TypeError" "$LOG"
   echo "=== LAST LINES ===" && tail -5 "$LOG"
   ```
   Each update MUST include: validity, progress, timing, improvement trend.
7. **Verify isolation after each framework**: No artifacts in benchmark directory

### After Experiments (run ALL steps — even when adding one framework to an existing experiment)

8. **Record configuration**: Save seed, iterations, model, commit hash with results
9. **Run ALL four analysis scripts**:
   ```bash
   RESULTS_DIR="outputs/vidur_router/<EXPERIMENT>"
   python benchmarks/vidur_router/scripts/compare_results.py "$RESULTS_DIR"
   python benchmarks/vidur_router/scripts/plot_results.py "$RESULTS_DIR"
   python benchmarks/vidur_router/scripts/analyze_effort.py "$RESULTS_DIR"
   python benchmarks/vidur_router/scripts/analyze_diffs.py "$RESULTS_DIR"
   ```
10. **Write or update `analysis.md`** in output dir. **Every number MUST come from script output or JSON files — never compute numbers manually.** Required sections:
    - Configuration (seed, iterations, models, replicas)
    - Combined Score table (from `comparison_table.csv`)
    - Per-Workload E2E table
    - Per-Workload P95 table
    - Effort Analysis (from `effort_analysis.json`)
    - Search Efficiency
    - Convergence
    - Key Takeaways
    - Comparison with BLIS results (% improvement only, NOT absolute scores)
11. **Merge baseline metrics into best_program_info.json**: Copy `baseline_metrics.json` contents into `best/best_program_info.json` as `"baseline_metrics"` key
12. **Robustness validation** (recommended):
    ```bash
    python benchmarks/vidur_router/scripts/validate_robustness.py "$RESULTS_DIR"
    ```
13. **Never delete output directories**

### Concurrency Warning

Vidur evaluations are NOT safe for concurrent execution. Multiple `evaluate()` calls MUST be serialized.

## analysis.md Template

```markdown
# Vidur Router Experiment: <EXPERIMENT_NAME>

## Configuration
- Seed(s): ...
- Iterations: ...
- Models: Llama-2-7B, Llama-3-8B
- Replicas: 4
- Trace scale: 1.0

## Combined Scores
(from comparison_table.csv)

## Per-Workload E2E Latency
(from comparison_table.csv)

## Per-Workload P95 Latency
(from comparison_table.csv)

## Effort Analysis
(from effort_analysis.json)

## Search Efficiency
(from effort_analysis.json)

## Convergence
(from convergence plot)

## Key Takeaways

## Comparison with BLIS
(% improvement only — never compare absolute scores across simulators)
```

## Deployment Viability

Not applicable for Vidur (this is a generality experiment, not a deployment pipeline).
