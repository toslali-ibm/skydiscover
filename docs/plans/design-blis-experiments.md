# Design: BLIS Router Discovery Experiments

## Goal

Run the BLIS router optimization task across **5 search frameworks** (OpenEvolve, GEPA, ShinkaEvolve, AdaEvolve, EvoX) via SkyDiscover's unified interface. Compare:

1. **Quality**: Which discovered routing algorithm delivers the best e2e latency, tail latency, and per-workload metrics?
2. **Cost**: How many LLM calls, tokens, wall-clock time did each framework consume?
3. **Convergence**: How quickly does each framework find good solutions?

---

## Background

### What We're Evolving

The `WeightedScoring.Route()` method in BLIS's `routing.go`. The baseline uses fixed equal weights `[1.0, 1.0]` on two scorers (prefix-affinity + load-balance). Evolution makes this adaptive based on request properties (`InputTokens`, `SLOClass`, `SessionID`) and instance state (`CacheHitRate`, `QueueDepth`, `KVUtilization`, etc.).

### Existing Work (OpenEvolve repo)

A working setup exists at `openevolve/examples/blis_router/`:
- `initial_program.py`: Full `routing.go` wrapped as Python string with EVOLVE-BLOCK markers around the scoring logic
- `evaluator.py`: Extracts Go code -> writes `routing.go` -> `go build` -> runs 3 workloads -> parses latency metrics -> returns score
- 3 workloads: `cache_warmup`, `load_spikes`, `multiturn` (each tests a different routing tension)
- `inference-sim/`: BLIS simulator as git submodule
- Scoring: `score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms` (higher = better)

### Key Constraint

Evaluation is **sequential** (not parallelizable) because each iteration writes to the same `routing.go`, rebuilds the Go binary, then runs simulations. ~30-60s per evaluation.

---

## Design

### 1. Benchmark Directory Structure

```
benchmarks/blis_router/
├── initial_program.py          # Full routing.go wrapped in Python with EVOLVE-BLOCK markers
├── evaluator.py                # Build BLIS, run workloads, return latency metrics (plain dict)
├── config.yaml                 # SkyDiscover config with system prompt
├── README.md
├── workloads/                  # Simulation workload specs
│   ├── workload_v2_cache_warmup.yaml
│   ├── workload_v2_load_spikes.yaml
│   └── workload_v2_multiturn.yaml
├── routing/                    # BLIS routing policy config
│   └── routing_policy.yaml     # prefix-affinity + load-balance, equal weights
├── inference-sim/              # Git submodule → github.com/inference-sim/inference-sim
└── scripts/
    ├── run_all_frameworks.sh   # Master experiment runner
    ├── compare_results.py      # Cross-framework comparison (tables + plots)
    ├── plot_convergence.py     # Score-over-iterations for each framework
    └── extract_costs.py        # LLM call counts, tokens, duration from logs
```

### 2. The Evaluator (single, shared by ALL frameworks)

SkyDiscover's key value: **one evaluator, one initial program** -- swap search algorithm via `--search` flag. All 5 frameworks use the exact same `evaluator.py`. No per-framework evaluator logic.

The evaluation logic is the same as in the OpenEvolve example (that was a reference for the build/run/score pattern), just adapted to SkyDiscover's interface (return plain `dict` instead of OpenEvolve's `EvaluationResult`):

```python
# benchmarks/blis_router/evaluator.py — used by ALL search frameworks
def evaluate(program_path: str) -> dict:
    # 1. Extract Go code from evolved program file
    # 2. Write to inference-sim/sim/routing.go
    # 3. go build
    # 4. Run 3 workloads (cache_warmup, load_spikes, multiturn)
    # 5. Parse cluster metrics from simulation output
    # 6. Return score + per-workload metrics
    return {
        "combined_score": score,          # required by SkyDiscover (higher = better)
        "avg_e2e_ms": avg_latency,
        "avg_p95_ms": avg_tail_latency,
        "cache_warmup_e2e_ms": ...,
        "load_spikes_e2e_ms": ...,
        "multiturn_e2e_ms": ...,
        "success_rate": ...,
        "num_successful": ...,
        "num_failed": ...,
        "artifacts": {                    # fed back to LLM as context
            "workload_results": workload_results,
            "suggestion": "...",
        },
    }
```

Adaptations from the OpenEvolve example:
1. Remove `from openevolve.evaluation_result import EvaluationResult` -- return plain dict
2. Paths: `inference-sim/` relative to `benchmarks/blis_router/` (use `Path(__file__).parent`); workloads under `workloads/`, routing policy under `routing/`
3. Baseline caching: same pattern, cache to benchmark dir `baseline_metrics.json`

### 3. inference-sim as Submodule

Add inference-sim as a git submodule within the benchmark directory:

```bash
cd skydiscover
git submodule add https://github.com/inference-sim/inference-sim.git benchmarks/blis_router/inference-sim
```

**Why submodule (not copy)?**
- Keeps BLIS version tracked and reproducible
- Easy to update when BLIS evolves
- Same pattern as the openevolve repo

**Pre-build step**: The Go binary must be built before first evaluation. The evaluator handles this (builds on every iteration), but a one-time `go build` check in the README ensures the environment is correct.

### 4. SkyDiscover Config

```yaml
# benchmarks/blis_router/config.yaml
language: python
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
  top_p: null          # AWS Bedrock doesn't allow both temperature and top_p
  max_tokens: 32000
  timeout: 120

prompt:
  system_message: |
    You are optimizing a request router for a 4-instance LLM inference cluster.

    GOAL: Minimize end-to-end latency by improving how requests are routed to instances.

    BASELINE: Fixed equal weights [1.0, 1.0] on two scorers (prefix-affinity, load-balance).
    The weights never adapt to request or system state.

    AVAILABLE SIGNALS — from request (req):
      - len(req.InputTokens): input size
      - req.SLOClass: "realtime", "interactive", or "batch"
      - req.SessionID: non-empty for multi-turn sessions

    AVAILABLE SIGNALS — from instance snapshots (snap):
      - scores[snap.ID]: weighted scorer output (after the scorer loop)
      - snap.QueueDepth, snap.BatchSize, snap.PendingRequests
      - snap.EffectiveLoad(): QueueDepth + BatchSize + PendingRequests
      - snap.CacheHitRate: historical cache hit rate (0.0-1.0)
      - snap.KVUtilization: memory pressure (may be stale under high load)
      - snap.FreeKVBlocks: available memory blocks

    You can modify scores[snap.ID] after the scorer loop, add conditionals, or change
    the selection logic.

    RULES (compilation failures waste an iteration):
    1. Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
    2. Must be valid Go — guard all divisions to avoid NaN/Inf
    3. bestIdx must be valid: 0 <= bestIdx < len(snapshots)
    4. Don't zero scores — use a small multiplier instead
    5. Don't add new keys to scores[] — only modify existing instance IDs

    EVALUATION: Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (lower latency = higher score)
    Tested on 3 workloads: cache_warmup, load_spikes, multiturn.

search:
  type: "adaevolve"  # overridden per-run via --search flag
  num_context_programs: 4

evaluator:
  timeout: 300        # 5 min: build + 3 workloads
  cascade_evaluation: false
```

### 5. Running All Frameworks (just flip `--search`)

Same evaluator, same initial program, same config. The **only** difference is `--search <type>`:

| Framework | Flag | Notes |
|-----------|------|-------|
| AdaEvolve | `--search adaevolve` | Use default multi-island config (2 islands, UCB, migration) |
| EvoX | `--search evox` | Co-evolution of solution + search strategy |
| OpenEvolve | `--search openevolve` | External backend, needs `uv sync --extra external` |
| GEPA | `--search gepa` | External backend, Pareto-based |
| ShinkaEvolve | `--search shinkaevolve` | External backend, needs manual install |

Optional per-framework config overrides (if needed):
```
benchmarks/blis_router/
  config.yaml                    # shared base
  config_adaevolve.yaml          # optional: AdaEvolve-specific tuning
  config_evox.yaml               # optional: EvoX-specific tuning
```

### 6. Master Experiment Script

```bash
#!/usr/bin/env bash
# scripts/run_all_frameworks.sh
set -euo pipefail

ITERATIONS=${1:-50}
MODEL="aws/claude-sonnet-4-5"
BASE_DIR="benchmarks/blis_router"
OUTPUT_BASE="outputs/blis_router"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Ensure deps
uv sync --extra external

# Verify Go + BLIS build
(cd "$BASE_DIR/inference-sim" && go build -o simulation_worker main.go) || {
  echo "FATAL: BLIS build failed. Install Go and check inference-sim submodule."
  exit 1
}

# Run each framework sequentially (they share the same routing.go file)
for SEARCH in adaevolve evox openevolve gepa shinkaevolve; do
  echo "=========================================="
  echo "Running: $SEARCH ($ITERATIONS iterations)"
  echo "=========================================="

  OUTPUT_DIR="${OUTPUT_BASE}/${TIMESTAMP}/${SEARCH}"

  # Use framework-specific config if it exists, else base config
  CONFIG="$BASE_DIR/config.yaml"
  [[ -f "$BASE_DIR/config_${SEARCH}.yaml" ]] && CONFIG="$BASE_DIR/config_${SEARCH}.yaml"

  # Clean baseline cache so each framework starts fresh
  rm -f "$OUTPUT_DIR/baseline_metrics.json"

  uv run skydiscover-run \
    "$BASE_DIR/initial_program.py" \
    "$BASE_DIR/evaluator.py" \
    -c "$CONFIG" \
    -s "$SEARCH" \
    -m "$MODEL" \
    -i "$ITERATIONS" \
    -o "$OUTPUT_DIR" \
    -l INFO \
    2>&1 | tee "${OUTPUT_DIR}/run.log"

  echo "$SEARCH complete. Results in $OUTPUT_DIR"
done

echo "All frameworks complete. Results in ${OUTPUT_BASE}/${TIMESTAMP}/"
echo "Run: python $BASE_DIR/scripts/compare_results.py ${OUTPUT_BASE}/${TIMESTAMP}"
```

**Why sequential, not parallel?** All frameworks write to the same `inference-sim/sim/routing.go` and rebuild the same binary. Parallel runs would corrupt each other. To parallelize, each would need its own `inference-sim/` clone -- possible future optimization.

### 7. Comparison Metrics

#### 7.1 Quality Metrics (per framework)

From each framework's best discovered program, re-run evaluation and collect:

| Metric | Description | Source |
|--------|-------------|--------|
| `combined_score` | -0.5 * avg_e2e - 0.5 * avg_p95 | Primary ranking metric |
| `avg_e2e_ms` | Mean e2e latency across 3 workloads | Lower = better |
| `avg_p95_ms` | Mean p95 tail latency across 3 workloads | Lower = better |
| `cache_warmup_e2e_ms` | Per-workload: cache warmup | Lower = better |
| `load_spikes_e2e_ms` | Per-workload: load spikes | Lower = better |
| `multiturn_e2e_ms` | Per-workload: multi-turn sessions | Lower = better |
| `% improvement vs baseline` | `(baseline - best) / baseline * 100` | Percentage gain |

#### 7.2 Cost Metrics (per framework)

Extracted from SkyDiscover logs and output:

| Metric | Description | How to collect |
|--------|-------------|----------------|
| Total LLM calls | Number of LLM API calls | Count from logs |
| Total tokens (input + output) | Token consumption | Sum from logs |
| Wall-clock time | Total experiment duration | Start/end timestamps |
| Avg time per iteration | Wall-clock / iterations | Computed |
| Successful evaluations | Programs that compiled + ran | Count from logs |
| Failed evaluations | Build failures, timeouts | Count from logs |
| Iterations to best | Which iteration found the best solution | From best_program_info.json |

#### 7.3 Convergence Analysis

Track `combined_score` over iterations for each framework. Plot:
- Score vs iteration (line chart, one line per framework)
- Time to reach X% of final improvement
- Score improvement rate (delta per iteration, moving average)

### 8. Comparison Scripts

#### `compare_results.py`

```
Input:  outputs/blis_router/<timestamp>/
        ├── adaevolve/best/best_program_info.json
        ├── evox/best/best_program_info.json
        ├── openevolve/best/best_program_info.json
        ├── gepa/best/best_program_info.json
        └── shinkaevolve/best/best_program_info.json

Output:
  - comparison_table.csv          (framework x metric matrix)
  - comparison_chart.png          (grouped bar chart: frameworks x metrics)
  - per_workload_breakdown.png    (per-workload latency comparison)
  - winner_summary.txt            (which framework won on which metric)
```

#### `plot_convergence.py`

```
Input:  outputs/blis_router/<timestamp>/*/logs/
Output: convergence.png            (score vs iteration, all frameworks overlaid)
```

#### `extract_costs.py`

```
Input:  outputs/blis_router/<timestamp>/*/logs/
Output: cost_table.csv             (framework x cost metric matrix)
        cost_chart.png             (LLM calls, tokens, time comparison)
```

### 9. Final Evaluation Protocol

After all frameworks complete, re-evaluate each best program on a **fresh BLIS build** to ensure reproducibility:

```bash
# For each framework's best program:
# 1. Extract Go code
# 2. Write to routing.go
# 3. Build fresh
# 4. Run all 3 workloads
# 5. Also run on held-out workloads (if available) to test generalization
```

This guards against evaluation noise from shared build artifacts.

---

## Implementation Steps

### Phase 1: Setup (one-time)

1. **Create benchmark directory**: `benchmarks/blis_router/`
2. **Add inference-sim submodule**: `git submodule add ...`
3. **Port initial_program.py**: Copy from openevolve, verify EVOLVE-BLOCK markers match latest inference-sim
4. **Port evaluator.py**: Adapt return type (dict instead of EvaluationResult), fix paths
5. **Copy workload files + routing_policy.yaml** from openevolve
6. **Write config.yaml**: SkyDiscover format with the system prompt
7. **Test locally**: `uv run skydiscover-run ... -s topk -i 2` (quick sanity check)

### Phase 2: Run Experiments

8. **Install external backends**: `uv sync --extra external` + ShinkaEvolve manual install
9. **Run all 5 frameworks**: `bash scripts/run_all_frameworks.sh 50`
10. **Monitor progress**: Check logs, ensure no build failures or timeouts

### Phase 3: Analysis

11. **Run comparison scripts**: Generate tables, charts, convergence plots
12. **Re-evaluate best programs**: Fresh builds for each winner
13. **Document findings**: Which framework won? On which metrics? At what cost?

### Phase 4: Optional Extensions

14. **Held-out workloads**: Test generalization on new workload patterns
15. **Longer runs**: 100+ iterations for frameworks that are still improving at 50
16. **Model ablation**: Try different LLMs (gpt-5, gemini, etc.) with the best framework
17. **Parallel inference-sim clones**: Each framework gets its own copy for true parallel runs

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Go build failures dominate iterations | Wasted LLM calls | Error retry in AdaEvolve; GEPA and EvoX may need explicit retry config |
| Sequential runs take too long | 5 * 50 * ~45s = ~3 hours | Acceptable for 50 iters; use checkpoints to resume |
| inference-sim version drift | Different baselines | Pin submodule to specific commit; cache baseline per run |
| External backends have different evaluator contracts | Broken evaluations | GEPA/ShinkaEvolve adapters handle file-based -> string-based conversion internally |
| Frameworks not directly comparable (different sampling budgets) | Unfair comparison | Fix iteration count AND track actual LLM calls; report both |

---

## Open Questions

1. **Parallelization**: Should we clone `inference-sim/` per framework for parallel runs? Adds disk but saves ~4x wall-clock time.
2. **Iteration budget**: 50 iterations per framework, or should some get more? AdaEvolve and EvoX may need more iterations due to exploration phases.
3. **LLM model**: Same model for all frameworks (fair comparison) vs each framework's recommended model (best results)?
4. **Baseline consistency**: Should the baseline be computed once and shared, or independently per framework run?
