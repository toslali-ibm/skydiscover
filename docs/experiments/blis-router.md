# BLIS Router Experiment Guide

## Overview

This guide covers how to run BLIS router optimization experiments using SkyDiscover. The goal is to discover improved routing algorithms for a 4-instance LLM inference cluster by evolving the `WeightedScoring.Route()` method.

## Prerequisites

- Go 1.21+ (`go version`)
- Python 3.11+
- SkyDiscover installed with external backends: `uv sync --extra external`
- inference-sim submodule initialized: `git submodule update --init benchmarks/blis_router/inference-sim`
- BLIS builds: `cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go`

## Naming Convention

Experiment directories follow this format:

```
outputs/blis_router/<YYMMDD>_<iters>i_<tag>/
```

| Component | Format | Example | Description |
|-----------|--------|---------|-------------|
| Date | `YYMMDD` | `260304` | When the experiment started |
| Iterations | `<N>i` | `100i` | Number of iterations per framework |
| Tag | short identifier | `seed42`, `sonnet`, `ablation` | Unique descriptor for the experiment |

**Examples:**
- `260304_100i_seed42` — 100 iterations, seed 42, default config
- `260305_20i_seed42` — 20 iterations, seed 42
- `260310_50i_opus_only` — 50 iterations, opus-only model ablation
- `260312_100i_temp0` — 100 iterations, temperature 0 reproduction test

**Rules:**
- Always include iteration count — this is the most important parameter for comparison
- Tags should be short (1-3 words, snake_case) and unique within the date
- When in doubt, use `seed<N>` as the tag
- Never reuse a directory name — if re-running the same config, add a suffix (`_v2`, `_rerun`)

**Legacy directories** (`YYYYMMDD_HHMMSS` format like `20260304_170300`) predate this convention and should not be renamed.

## Experiment Structure

Each experiment run creates a directory:

```
outputs/blis_router/<experiment_name>/
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

1. **BLIS simulation seed** (`BLIS_SEED` env var, default `42,456` — two seeds): Controls workload generation (request arrivals, token counts). Passed as `--seed` to every BLIS simulation run. By default, each evaluation runs against two seeds: `42` (normal behavior) and `456` (exposes baseline load-balance saturation under bursty traffic). Set `BLIS_SEED=42` for single-seed backward compatibility, or `BLIS_SEED=42,456,789` for custom multi-seed. Results are averaged across all seeds.

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

4. Run frameworks **sequentially** (openevolve → evox → adaevolve → gepa_native). They share `routing.go` and cannot run in parallel. **Use `openevolve` (external backend), NOT `openevolve_native`** — the native reimplementation has a strict diff parser that produces ~85% Go build errors.
5. Always set `BLIS_OUTPUT_DIR` to the experiment output directory before calling `skydiscover-run`.
6. Always set `BLIS_SEED` for reproducibility. Optionally set `BLIS_MULTI_LLM=1` to test against multiple LLMs. Set `BLIS_NUM_INSTANCES` to override the default 4 instances.
7. **Monitor every 2 minutes** while a framework is running. Set up a background `sleep 120` loop and check the framework's log file after each interval. Every update to the user MUST include:

   **Validity check:**
   - Count of successful evaluations vs build errors vs other failures
   - Any 401/auth errors, crashes, or unexpected exceptions
   - Whether the run is still active (new log lines appearing)

   **Progress & improvement:**
   - Iterations completed / total (e.g., "35/100")
   - Current best score and % improvement vs baseline (-4278.20)
   - When the last new best was found (iteration number)
   - Whether it's still improving or has plateaued

   **Timing stats:**
   - Elapsed wall time since start
   - Average seconds per iteration
   - Estimated time remaining = (total - completed) × avg_iter_time

   **Example monitoring command** (run as background task with `sleep 120`):
   ```bash
   LOG=$(ls outputs/blis_router/<EXPERIMENT>/<FRAMEWORK>/logs/*.log 2>/dev/null | head -1)
   echo "=== PROGRESS ===" && grep -c "Iteration" "$LOG"
   echo "=== BEST SCORES ===" && grep "best program score" "$LOG" | tail -3
   echo "=== NEW BESTS ===" && grep "New best" "$LOG" | tail -3
   echo "=== ERRORS ===" && grep -c "BuildError\|error\|Error" "$LOG"
   echo "=== LAST LINES ===" && tail -5 "$LOG"
   ```

   **Format example** for user update:
   ```
   Update at ~12 min (iter 35/100):
   - Validity: 25 successful, 10 build errors, 0 auth errors — healthy
   - Best: -3920.45 (+8.4% vs baseline), found at iter 28
   - Timing: 12.3 min elapsed, 34.2s/iter avg, ~37 min remaining
   - Trend: last improvement 7 iters ago, still exploring
   ```

8. After each framework completes, verify:
   - `routing.go` is unchanged from the original (evaluator restores it, but verify)
   - No `baseline_metrics.json` in the benchmark directory
   - No `__pycache__` in the benchmark directory
   - Results exist in the expected output directory

### After Experiments (run ALL steps — even when adding one framework to an existing experiment)

8. Run **all four** analysis scripts in order:
   ```bash
   RESULTS_DIR="outputs/blis_router/<YYYYMMDD_HHMMSS>"
   python benchmarks/blis_router/scripts/compare_results.py "$RESULTS_DIR"
   python benchmarks/blis_router/scripts/plot_results.py "$RESULTS_DIR"
   python benchmarks/blis_router/scripts/analyze_effort.py "$RESULTS_DIR"
   python benchmarks/blis_router/scripts/analyze_diffs.py "$RESULTS_DIR"
   ```
   This produces **7 plots** in `<RESULTS_DIR>/plots/`:
   - `combined_scores.png` — bar chart of combined score per framework + baseline
   - `latency_comparison.png` — grouped bars for avg E2E vs avg P95
   - `per_workload_latency.png` — per-workload E2E breakdown
   - `iteration_duration_boxplot.png` — box plot of iteration durations
   - `convergence_curves.png` — best score over iterations (line chart)
   - `effort_vs_improvement.png` — scatter: wall time vs improvement (bubble = population)
   - `search_efficiency.png` — bar chart: % improvement per wall-clock minute

   Plus data files:
   - `comparison_table.csv` — **includes baseline row + per-workload E2E and P95 columns** (authoritative source for analysis.md tables)
   - `effort_analysis.csv`, `effort_analysis.json` — effort metrics per framework
   - `diff_explanations.md` — diffs with LLM explanations
   - Per-framework `best/best_vs_initial.diff` files

9. Record the exact experiment configuration (seed, iterations, model, inference-sim commit) in the output directory.
10. **Write or update `analysis.md`** in the output directory.

    **DATA SOURCING RULE**: Every number in `analysis.md` MUST come from script output or JSON files — never compute or estimate numbers manually. Specifically:
    - Aggregate scores → `comparison_table.csv` or `compare_results.py` console output
    - Per-workload E2E and P95 → `comparison_table.csv` (includes baseline row and per-workload columns)
    - Effort metrics → `effort_analysis.json`
    - Per-model baseline → `compare_results.py` console output (Multi-LLM baseline breakdown section)
    - If a number isn't in any script output, add it to the script first — do NOT fabricate it.

    The analysis MUST include ALL of:
    - Accuracy comparison table (scores, % improvement vs baseline) — **all frameworks including any newly added**
    - Per-workload E2E latency table (baseline + all frameworks) — **copy from `compare_results.py` output**
    - Per-workload P95 latency table (baseline + all frameworks) — **copy from `compare_results.py` output**
    - Key findings
    - Convergence comparison (20-iter vs 100-iter scores, if both exist)
    - Best iteration found per framework
    - Effort summary table (iterations, wall time, avg/median/min/max/stddev iteration time, population size, unique scores, diversity ratio, generation depth)
    - Search efficiency table (% improvement per wall-clock minute)
    - Convergence behavior (iterations to best, % wasted compute)
    - AdaEvolve island details (if applicable)
    - Population quality comparison (pop size, best/median/worst scores, spread)
    - Key takeaways comparing accuracy vs cost tradeoffs
    - Experiment configuration (seed, model, api_base, temperature, inference-sim commit)

    **CRITICAL**: When adding a framework to an existing experiment, update ALL tables and findings in `analysis.md` — do not leave stale data from before the new framework was added.

11. **Merge baseline metrics into best_program_info.json**: For each framework, copy the contents of `<framework>/baseline_metrics.json` into `<framework>/best/best_program_info.json` as a new top-level `"baseline_metrics"` key. This makes each best program self-contained with both its own metrics and the baseline it improved upon. Use a script like:
    ```bash
    RESULTS_DIR="outputs/blis_router/<EXPERIMENT_DIR>"
    python3 -c "
    import json, pathlib
    for fw in pathlib.Path('$RESULTS_DIR').iterdir():
        if not fw.is_dir(): continue
        bl = fw / 'baseline_metrics.json'
        bp = fw / 'best' / 'best_program_info.json'
        if bl.exists() and bp.exists():
            baseline = json.loads(bl.read_text())
            info = json.loads(bp.read_text())
            info['baseline_metrics'] = baseline
            bp.write_text(json.dumps(info, indent=2) + '\n')
            print(f'  Updated {fw.name}/best/best_program_info.json')
    "
    ```
12. **Robustness validation** (recommended before transferring algorithms): Re-evaluate best programs across multiple seeds to detect overfitting:
    ```bash
    python benchmarks/blis_router/scripts/validate_robustness.py "$RESULTS_DIR"
    # Runs both single-LLM and multi-LLM by default (3 seeds × 5 programs × 2 modes)
    # Options:
    #   --seeds 42,123,456,789    (custom seeds)
    #   --single-llm-only         (faster: skip multi-LLM)
    #   --multi-llm-only          (only multi-LLM)
    ```
    Output goes to `<results_dir>/robustness/` with JSON + CSV. Look for frameworks where cross-seed stddev is low and mean improvement remains high.
13. Do NOT delete or modify output directories — they are the permanent record.

## Scoring

```
score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms
```

- Higher is better (less negative = lower latency)
- Tested on 3 workloads × N seeds (default 2 seeds: 42, 456): cache_warmup, load_spikes, multiturn
- With `BLIS_MULTI_LLM=1`: averaged across N seeds × 2 models × 3 workloads
- Baseline score (single LLM): approximately -4487
- Baseline score (multi-LLM): approximately -7121

## Multi-LLM Evaluation

By default, the evaluator tests against two LLMs (qwen_7b + qwen_14b) to validate that discovered algorithms generalize across model sizes. Set `BLIS_MULTI_LLM=0` to use only qwen_7b for faster iterations.

```bash
# Multi-LLM is ON by default. To disable:
export BLIS_MULTI_LLM=0
export BLIS_OUTPUT_DIR="outputs/blis_router/${EXPERIMENT}/<FRAMEWORK>"
export BLIS_SEED="42"
```

| Short Name | Model | Estimator | Hardware |
|------------|-------|-----------|----------|
| `llama_8b` | `meta-llama/llama-3.1-8b-instruct` | blackbox (trained coefficients) | H100, TP=1 |
| `mixtral_8x7b` | `mistralai/mixtral-8x7b-instruct-v0.1` | crossmodel (physics-informed, MoE-aware) | H100, TP=2 |

With default settings (multi-LLM ON, two seeds):
- Each evaluation runs 12 simulations (2 seeds × 2 models × 3 workloads)
- Evaluation time: ~24-48s per iteration depending on system load
- With single-LLM (`BLIS_MULTI_LLM=0`): 6 sims, ~12-24s
- `baseline_metrics.json` includes a `per_model` key with full per-model per-workload breakdowns
- The combined score and per-workload values in the evaluate return dict are averaged across models
- Analysis scripts work unchanged — they read the same top-level fields

## ShinkaEvolve Setup

ShinkaEvolve is supported but **currently blocked** because it requires an embedding model (`text-embedding-3-small`) for code deduplication, and the IBM LiteLLM proxy does not serve embedding models.

**To unblock ShinkaEvolve**, one of:
1. Add an embedding model endpoint to LiteLLM proxy config, OR
2. Modify `shinkaevolve_default.yaml` to set `code_embed_sim_threshold: 1.0` (disables embedding-based dedup — may produce more duplicate solutions but otherwise functional)

**BLIS-specific config requirements** (must be set before running):
- `max_parallel_jobs: 1` — BLIS evaluations write to a shared `routing.go` and CANNOT run in parallel. The default of 4 will corrupt results.
- `embedding_model` — must point to a working embedding endpoint, or dedup must be disabled (see above)

**Example run** (once unblocked):
```bash
# Ensure shinka package is installed
uv sync --extra external

# Run with BLIS router
EXPERIMENT="$(date +%y%m%d)_100i_shinka"
export BLIS_OUTPUT_DIR="outputs/blis_router/${EXPERIMENT}/shinkaevolve"
export BLIS_SEED="42"
mkdir -p "$BLIS_OUTPUT_DIR"
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s shinkaevolve \
  -i 100 \
  -o "$BLIS_OUTPUT_DIR" \
  -l INFO
```

**ShinkaEvolve key features** (from `shinkaevolve_default.yaml`):
- Multi-patch evolution: diff (60%), full (30%), cross-pollination (10%)
- UCB1 dynamic LLM selection across temperature variants (0.0, 0.7, 1.0)
- 5 islands with migration every 5 generations
- Meta-model guidance every 10 generations
- Code-embedding deduplication (threshold 0.995) — requires embedding model

## Workloads

| Workload | Tests |
|----------|-------|
| cache_warmup | Load balance vs prefix-affinity when 3 prefix groups create imbalance across 4 instances |
| load_spikes | Routing under bursty arrivals when one prefix group gets 50% of traffic |
| multiturn | Session stickiness for multi-turn conversations with large prefix caches |

Baseline latencies vary by model configuration — run `compare_results.py` after an experiment for exact numbers.

## Post-Experiment Analysis

After all frameworks complete, run **four** analysis scripts to get a complete picture of accuracy, cost/effort, and code changes:

```bash
RESULTS_DIR="outputs/blis_router/<YYYYMMDD_HHMMSS>"

# 1. Accuracy comparison
python benchmarks/blis_router/scripts/compare_results.py "$RESULTS_DIR"
python benchmarks/blis_router/scripts/plot_results.py "$RESULTS_DIR"

# 2. Effort/cost analysis
python benchmarks/blis_router/scripts/analyze_effort.py "$RESULTS_DIR"

# 3. Diff analysis (diffs + LLM explanations)
python benchmarks/blis_router/scripts/analyze_diffs.py "$RESULTS_DIR"
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
