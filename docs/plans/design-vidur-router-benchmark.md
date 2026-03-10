# Plan: Vidur Router Benchmark for SkyDiscover

## Context

The NSDI paper already has a BLIS router case study where SkyDiscover evolves routing algorithms in inference-sim and transfers them to LLMD. We want to **also** evolve routing in Microsoft's Vidur simulator to show SkyDiscover **generalizes across simulators with different architectures** (Go event-driven vs Python discrete-event), different fidelity models, and different signal surfaces.

**Paper framing**: This is a **generality** experiment, not a head-to-head. We expect smaller gains in Vidur (reduced signal surface — no prefix caching, no SLO classes) and that's the correct narrative: richer signals in BLIS enable richer routing improvements, while Vidur validates the approach works even with limited signals.

The Vidur codebase is already at `benchmarks/vidur_router/vidur/`. We need to build the benchmark scaffold around it.

**Constraints**:
- Do NOT modify any BLIS-related files
- Do NOT modify the Vidur codebase (`benchmarks/vidur_router/vidur/`) — use it as-is

## Design Decisions (from user)

- **Start from**: LOR (Least Outstanding Requests) — Vidur's strongest built-in scheduler
- **Workloads**: Match BLIS's 3 workloads via trace conversion (exact same request patterns)
- **Replicas**: 4 (match BLIS's 4 instances)
- **Multi-LLM**: `meta-llama/Llama-2-7b-hf` + `meta-llama/Meta-Llama-3-8B` on A100

---

## Step 1: Create benchmark directory structure

```
benchmarks/vidur_router/
├── __init__.py
├── evaluator.py              # Core: inject scheduler → run Vidur → extract metrics → score
├── initial_program.py        # LOR schedule() with EVOLVE-BLOCK markers
├── config.yaml               # SkyDiscover config (LLM, search params, system prompt)
├── workloads/                # 3 trace CSVs generated from BLIS workload specs
│   ├── cache_warmup.csv
│   ├── load_spikes.csv
│   ├── multiturn.csv
│   └── README.md
├── scripts/
│   ├── generate_traces.py    # BLIS YAML → Vidur CSV converter
│   ├── pilot_study.py        # End-to-end validation (6+ checks)
│   ├── compare_results.py    # Cross-framework comparison + CSV
│   ├── plot_results.py       # 3 charts
│   ├── analyze_effort.py     # Search efficiency + 4 charts + JSON
│   ├── analyze_diffs.py      # Code diff analysis + LLM explanations
│   ├── validate_robustness.py # Multi-seed robustness validation
│   └── run_all_frameworks.sh  # Sequential runner with isolation checks
├── vidur/                    # (already exists — Vidur submodule)
└── README.md
```

---

## Step 2: Create `initial_program.py`

Standalone Python file with LOR scheduler extracted as a function with EVOLVE-BLOCK markers. Signal documentation above the block acts as the LLM's API reference.

```python
"""Vidur Global Scheduler — Evolved Routing Algorithm.
...signal documentation...
"""
from typing import List, Tuple, Dict, Any

# EVOLVE-BLOCK-START
def schedule(request_queue: list, replica_schedulers: dict, num_replicas: int) -> List[Tuple[int, Any]]:
    """Route queued requests to replicas. Returns list of (replica_id, request) tuples."""
    request_queue.sort(key=lambda r: r.arrived_at)
    request_mapping = []
    pending_requests_map = {
        rs.replica_id: rs.num_pending_requests
        for rs in replica_schedulers.values()
    }
    while request_queue:
        request = request_queue.pop(0)
        replica_id = min(pending_requests_map.items(), key=lambda x: x[1])[0]
        pending_requests_map[replica_id] += 1
        request_mapping.append((replica_id, request))
    return request_mapping
# EVOLVE-BLOCK-END
```

Key choices:
- Standalone function (not class method) — simpler for LLM
- Explicit parameters — LLM sees available signals
- Docstring documents request fields, replica state, and cluster info
- Start from LOR — meaningful baseline

---

## Step 3: Create `evaluator.py`

### Code injection: Monkey-patch on scheduler instance

After `Simulator.__init__()`, patch `schedule()` directly on the scheduler instance:

```python
def _patch_scheduler(simulator, schedule_fn):
    original = simulator._scheduler
    def patched_schedule():
        queue_copy = list(original._request_queue)
        result = schedule_fn(queue_copy, original._replica_schedulers, original._num_replicas)
        original._request_queue.clear()
        return result
    original.schedule = patched_schedule
```

This avoids registry/enum modifications. Add **replica_id validation** in the wrapper to catch invalid IDs before they hit Vidur's internals:

```python
def _patch_scheduler(simulator, schedule_fn):
    original = simulator._scheduler
    valid_ids = set(original._replica_schedulers.keys())
    def patched_schedule():
        queue_copy = list(original._request_queue)
        result = schedule_fn(queue_copy, original._replica_schedulers, original._num_replicas)
        # Validate replica_ids before returning to Vidur
        for replica_id, req in result:
            if replica_id not in valid_ids:
                raise ValueError(f"Invalid replica_id {replica_id}, valid: {valid_ids}")
        original._request_queue.clear()
        return result
    original.schedule = patched_schedule
```

### Critical: Vidur runtime issues to handle (no Vidur modifications)

All fixes are in our evaluator code — we never modify Vidur itself.

1. **`write_metrics` MUST be True**: The `if_write_metrics` decorator in `metrics_store.py` gates ALL metric recording. If False, DataSeries will be empty. Configure:
   ```python
   MetricsConfig(write_metrics=True, store_plots=False, enable_chrome_trace=False,
                 write_json_trace=False, wandb_project=None, store_request_metrics=True,
                 store_operation_metrics=False, store_utilization_metrics=False,
                 store_batch_metrics=False)
   ```

2. **`atexit` handler cleanup**: Every `Simulator()` calls `atexit.register(self._write_output)`. Fix: `atexit.unregister(simulator._write_output)` after each `run()`.

3. **Entity ID counter reset**: Use `BaseEntity.__subclasses__()` to catch all entity types (Request, Batch, Replica, Cluster, BatchStage, ExecutionTime):
   ```python
   from vidur.entities.base_entity import BaseEntity
   for cls in BaseEntity.__subclasses__():
       cls._id = -1
   ```

4. **Exception handling**: Wrap `simulator.run()` in try/except → return error sentinel on crash.

5. **MetricsConfig creates timestamped dirs**: `MetricsConfig.__post_init__` creates `output_dir/<timestamp>/` on every construction. Use `tempfile.mkdtemp()` as output_dir and clean up after metrics extraction. This prevents 600+ dirs per experiment.

6. **GC between simulations**: Explicitly `del simulator` and `gc.collect()` after each simulation to prevent memory accumulation (12 sims per eval, each holding MetricsStore with DataSeries).

7. **max_tokens clipping**: Vidur's `TraceRequestGeneratorConfig.max_tokens` defaults to 4096 and silently clips prefill tokens. Set to 8192 (multiturn workload has prefills up to ~4600 tokens after context accumulation).

8. **Completion verification**: After simulation, verify `len(e2e_series._data_series)` matches expected request count. If requests didn't complete (timeout/bad routing), flag in artifacts. Set `SimulationConfig.time_limit` to a reasonable value (300s) and treat incomplete sims as errors.

### Metrics extraction

```python
from vidur.metrics.constants import RequestMetricsTimeDistributions
e2e_series = simulator.metric_store._request_metrics_time_distributions[
    RequestMetricsTimeDistributions.REQUEST_E2E_TIME
]
df = e2e_series._to_df()
mean_e2e_s = df[e2e_series._y_name].mean()
p95_e2e_s = df[e2e_series._y_name].quantile(0.95)
# Convert to ms for scoring
mean_e2e_ms = mean_e2e_s * 1000
p95_e2e_ms = p95_e2e_s * 1000
```

Also extract TTFT from `PREFILL_TIME_E2E`.

### Scoring formula (match BLIS)

```python
score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms
```

### Return format (match BLIS evaluator parity)

Success:
```python
{
    "combined_score": float,
    "avg_e2e_ms": float, "avg_p95_ms": float, "avg_ttft_ms": float,
    "cache_warmup_e2e_ms": float, "cache_warmup_p95_ms": float,
    "load_spikes_e2e_ms": float, "load_spikes_p95_ms": float,
    "multiturn_e2e_ms": float, "multiturn_p95_ms": float,
    "success_rate": float, "num_successful": int, "num_failed": int,
    "artifacts": { "workload_results": {...}, "success_rate": "100%", ... }
}
```

Error (sentinel score `-100000.0`):
```python
{
    "combined_score": -100000.0, "avg_e2e_ms": float("inf"), "avg_p95_ms": float("inf"),
    "error": str, "artifacts": { "error_type": str, "error_message": str, "suggestion": str }
}
```

Partial failure: average over successful workloads only, report `failed_workloads` in artifacts.

### Baseline caching with seed-change detection

Cache at `$VIDUR_OUTPUT_DIR/baseline_metrics.json`. On load, check `cached["seeds"] == sorted(seeds)` and `cached["models"] == sorted(model_names)` — recompute if changed. Include `per_model` breakdown.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VIDUR_OUTPUT_DIR` | `.eval_artifacts` (with warning) | Experiment output directory |
| `VIDUR_SEED` | `"42,456"` | Trace generation seeds (comma-separated) |
| `VIDUR_MULTI_LLM` | `"1"` | Enable multi-LLM (Llama-2-7B + Llama-3-8B) |
| `VIDUR_NUM_REPLICAS` | `"4"` | Number of replicas |

### How seeds work with trace replay

Since we use trace replay (not Vidur's synthetic generator), the Vidur simulation is **deterministic** for a given trace. Seeds control **trace generation**:
- `generate_traces.py` produces per-seed trace files: `cache_warmup_seed42.csv`, `cache_warmup_seed456.csv`, etc.
- The evaluator selects trace files based on `VIDUR_SEED`
- No Vidur modification needed — each "seed" is just a different input trace

### Eval loop

```
for seed in seeds:
    for model in models:
        for workload in workloads:
            trace_file = f"workloads/{workload}_seed{seed}.csv"
            config = build_simulation_config(model, trace_file, num_replicas, temp_output_dir)
            simulator = Simulator(config)
            patch_scheduler(simulator, schedule_fn)
            try:
                simulator.run()
                verify_completion_count(simulator)
                extract_metrics(simulator)
            except Exception as e:
                record_error(e)
            finally:
                atexit.unregister(simulator._write_output)
                del simulator
                gc.collect()
                reset_entity_ids()
                cleanup_temp_dir()
average all results → combined_score
```

### Evaluator `__main__` block

Include standalone testing (matching BLIS pattern):
```python
if __name__ == "__main__":
    result = evaluate(sys.argv[1] if len(sys.argv) > 1 else "initial_program.py")
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["combined_score"] > -100000 else 1)
```

### Concurrency warning

NOT safe for concurrent execution. Multiple `evaluate()` calls MUST be serialized — Vidur uses module-level state.

### Key files to reference
- BLIS evaluator: `benchmarks/blis_router/evaluator.py`
- Vidur simulator: `benchmarks/vidur_router/vidur/vidur/simulator.py`
- Vidur base scheduler: `benchmarks/vidur_router/vidur/vidur/scheduler/global_scheduler/base_global_scheduler.py`
- Vidur metrics store: `benchmarks/vidur_router/vidur/vidur/metrics/metrics_store.py`
- Vidur data series: `benchmarks/vidur_router/vidur/vidur/metrics/data_series.py`
- Vidur config: `benchmarks/vidur_router/vidur/vidur/config/config.py`
- Vidur entities: `benchmarks/vidur_router/vidur/vidur/entities/base_entity.py` (ID counter)
- Vidur model configs: `benchmarks/vidur_router/vidur/vidur/config/model_config.py` (exact names)
- Vidur profiling data: `benchmarks/vidur_router/vidur/data/profiling/compute/a100/meta-llama/`

---

## Step 4: Create workload traces from BLIS workload specs

Vidur supports trace replay via CSV with columns `arrived_at`, `num_prefill_tokens`, `num_decode_tokens`.

### `scripts/generate_traces.py`

Reads BLIS workload YAMLs, generates **per-seed** Vidur CSVs:
1. Parse each BLIS YAML (client mix, arrival process, token distributions)
2. Per client group, per seed: generate arrivals and sample tokens:
   - **Poisson arrivals**: inter-arrival = `Exponential(1 / (aggregate_rate × rate_fraction))`
   - **Gamma arrivals (CV=3)**: shape = `1/CV² = 1/9`, scale = `mean × CV² = mean × 9`
   - **Input tokens**: `Gaussian(mean, std_dev)` clipped to `[min, max]`, floor at 1
   - **Output tokens**: `Exponential(mean)`, floor at 1
3. **Prefill mapping**: `num_prefill_tokens = prefix_length + input_tokens`
4. **Multi-turn flattening**: Each session of `max_rounds` rounds becomes `max_rounds` independent requests:
   - Round 0: `arrived_at = session_start, prefill = prefix_length + new_input, decode = sampled_output`
   - Round N: `arrived_at = prev_arrival + think_time_us/1e6 + prev_decode/1e6`, `prefill = prefix_length + accumulated_context + new_input`, `decode = sampled_output`
   - Context accumulates: `accumulated = sum of prior (new_input + output) tokens`
5. Merge all client groups, sort by `arrived_at`, truncate to `num_requests`
6. Write CSV per seed: `{workload}_seed{seed}.csv`
7. Use `numpy.random.default_rng(seed)` with per-client isolated RNGs for full determinism

Output: `workloads/{workload}_seed{seed}.csv` for each workload × seed combination

### What maps and what doesn't

| BLIS concept | Vidur mapping | Notes |
|---|---|---|
| `prefix_length + input_tokens` | `num_prefill_tokens` | Prefix = extra prefill (no caching in Vidur) |
| `output_distribution` | `num_decode_tokens` | Direct mapping |
| Arrival process | `arrived_at` timestamps | Direct generation |
| `slo_class`, `prefix_group`, `streaming`, `multi_turn` | Not mapped | Vidur has none of these concepts |

### Load calibration (mandatory pilot step)

BLIS workload rates (200/300/150 req/s) may not be appropriate for Vidur's execution time model. Each workload may need a **different** `time_scale_factor`. During pilot study:
1. Run each trace at original QPS → check cluster utilization via replica memory/busy metrics
2. If utilization < 50% or > 95%, adjust `time_scale_factor` per workload in the evaluator
3. Target ~70-80% utilization so routing decisions matter
4. Record calibrated `time_scale_factor` values in workloads/README.md

### Eval speed concern

Vidur is Python DES — expect 15-60s per simulation depending on request count. With 12 sims/eval, worst case ~12 minutes per iteration. Options:
- Add `VIDUR_TRACE_SCALE` env var to sample first N% of rows from each trace (e.g., 25% for fast search, 100% for final eval)
- Default to scaled traces during search, full traces for robustness validation

### Generated files
```
benchmarks/vidur_router/workloads/
├── cache_warmup_seed42.csv    # 12,000 requests
├── cache_warmup_seed456.csv
├── load_spikes_seed42.csv     # 18,000 requests
├── load_spikes_seed456.csv
├── multiturn_seed42.csv       # 9,000 requests (flattened)
├── multiturn_seed456.csv
└── README.md                  # Documents mapping, caveats, calibration
```

### Caveat for paper

Document explicitly: Vidur doesn't model prefix caching, SLO classes, or sessions. This **reduces the optimization surface** — LOR is already near-optimal for simple load balancing. Smaller improvements are expected and consistent with the reduced signal surface.

---

## Step 5: Create `config.yaml`

```yaml
language: python
file_suffix: .py
diff_based_generation: true
max_iterations: 50
checkpoint_interval: 5
max_solution_length: 20000

llm:
  models:
    - name: "aws/claude-sonnet-4-5"
      weight: 0.7
    - name: "aws/claude-opus-4-6"
      weight: 0.3
  api_base: "https://ete-litellm.ai-models.vpc-int.res.ibm.com"
  temperature: 1.0
  max_tokens: 16000
  timeout: 120

prompt:
  system_message: |
    You are optimizing a request routing algorithm for an LLM inference cluster
    simulated by Vidur (discrete-event simulator). The cluster has 4 replicas,
    each running the same LLM model on A100 GPUs.

    GOAL: Minimize end-to-end request latency and tail latency.
    Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (higher/less negative = better)

    AVAILABLE SIGNALS PER REQUEST:
    - arrived_at (float): arrival timestamp in seconds
    - num_prefill_tokens (int): input tokens (larger = longer first-token time)
    - num_decode_tokens (int): output tokens to generate
    - total_tokens (int): prefill + decode
    - pd_ratio (float): prefill / decode ratio (high = compute-heavy prefill)

    AVAILABLE SIGNALS PER REPLICA:
    - num_pending_requests (int): queue depth (requests waiting + in-progress)
    - num_allocated_blocks (int): KV-cache memory blocks in use
    - memory_usage_percent (float): 0-100% memory utilization
    - is_empty() (bool): no pending work

    KNOWN WEAKNESS OF BASELINE (LOR):
    LOR only considers queue depth. It ignores request size — routing a 4096-token
    prefill to a replica with 1 pending request is suboptimal if another replica
    with 2 small pending requests would finish sooner. It also ignores memory
    pressure — a replica at 90% memory may preempt requests, increasing latency.

    SIGNAL FRESHNESS:
    All signals are real-time (simulation state). In production, queue depth and
    memory would have ~5s staleness from Prometheus scraping. Algorithms that
    are robust to stale signals are preferred.

    NOTE ON MEMORY SIGNALS:
    Memory signals (num_allocated_blocks, memory_usage_percent) start at 0 for
    all replicas and only become informative after requests have been processed.
    Early scheduling rounds should rely on queue depth (num_pending_requests).

    RULES:
    - Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
    - Must be valid Python — no syntax errors
    - Must return List[Tuple[int, Request]] — (replica_id, request) pairs
    - replica_id values MUST come from replica_schedulers.keys()
    - Must consume all requests from request_queue
    - Only import typing and math (no external packages)
    - Guard all divisions against zero

search:
  type: "adaevolve"
  num_context_programs: 4
  database:
    random_seed: 42

evaluator:
  timeout: 600
  cascade_evaluation: false
```

---

## Step 6: Create analysis scripts

### `scripts/pilot_study.py` (6+ checks, matching BLIS depth)
1. Verify Vidur imports (plotly_express, wandb, etc.)
2. Verify profiling data exists for both models
3. Verify trace CSVs exist and parse correctly
4. Run baseline (LOR) on 1 workload × 1 seed × 1 model → verify finite score, P95 >= mean
5. Run round-robin on same config → verify different (worse) score
6. Verify no artifacts leaked to benchmark directory
7. Verify EVOLVE-BLOCK markers present in initial_program.py
8. Estimate single-eval wall time → report for experiment planning

### `scripts/compare_results.py`
- Read `best_program_info.json` + `baseline_metrics.json` per framework
- Produce `comparison_table.csv` with baseline row + per-workload E2E + P95 columns
- Print console summary with % improvement
- Include per-model baseline breakdown

### `scripts/plot_results.py`
3 charts: `combined_scores.png`, `latency_comparison.png`, `per_workload_latency.png`

### `scripts/analyze_effort.py`
4 charts + `effort_analysis.csv` + `effort_analysis.json`:
- `iteration_duration_boxplot.png`, `convergence_curves.png`
- `effort_vs_improvement.png`, `search_efficiency.png`
- Per-framework: iterations, wall time, avg/median/min/max iter time, population, unique scores

### `scripts/analyze_diffs.py`
- Per-framework diffs (best vs initial)
- LLM explanations of algorithmic changes
- `diff_explanations.md` output

### `scripts/validate_robustness.py`
- Re-evaluate best programs across 3+ seeds
- Detect overfitting to specific seed
- Output JSON + CSV to `<results_dir>/robustness/`

### `scripts/run_all_frameworks.sh`
Sequential runner with isolation checks (matching BLIS pattern):
- Run frameworks one at a time (adaevolve → evox → openevolve → gepa_native)
- `__pycache__` cleanup between frameworks
- `baseline_metrics.json` leak check after each
- Record `experiment_config.json` metadata
- Track failed frameworks

### Per-evaluation diff logging (in evaluator.py)
Log EVOLVE-BLOCK diff on every `evaluate()` call (matching BLIS `print_diff()`):
```python
logger.info(f"Eval diff: +{added}/-{removed} lines vs initial")
```

---

## Step 7: Create `docs/experiments/vidur-router.md`

Comprehensive experiment guide (parallel to blis-router.md) including:
- Overview, prerequisites (Python 3.10+, Vidur deps installed, trace CSVs generated), naming convention
- Quick-start recipe with exact commands
- Environment variables reference (VIDUR_OUTPUT_DIR, VIDUR_SEED, VIDUR_MULTI_LLM, VIDUR_NUM_REPLICAS, VIDUR_TRACE_SCALE)
- Workload descriptions + mapping from BLIS + caveats (no prefix caching, no SLO)
- Scoring formula
- Multi-LLM evaluation (Llama-2-7B + Llama-3-8B on A100)
- **Guardrails for Claude sessions**: pre-flight, monitoring every 2 min, post-experiment checklist
- **Monitoring commands** (SkyDiscover log patterns work the same as BLIS):
  ```bash
  LOG=$(ls outputs/vidur_router/<EXPERIMENT>/<FRAMEWORK>/logs/*.log | head -1)
  echo "=== PROGRESS ===" && grep -c "Iteration" "$LOG"
  echo "=== BEST ===" && grep "best program score" "$LOG" | tail -3
  echo "=== ERRORS ===" && grep -c "error\|Error\|RuntimeError\|SyntaxError\|TypeError" "$LOG"
  echo "=== LAST LINES ===" && tail -5 "$LOG"
  ```
- Post-experiment analysis steps (all scripts)
- **`analysis.md` template** with data sourcing rule: "every number MUST come from script output or JSON files — never compute manually." Required sections: Configuration, Combined Score table, Per-Workload E2E table, Per-Workload P95 table, Multi-LLM Baseline Breakdown, Effort Analysis, Search Efficiency, Convergence, Population Quality, Key Takeaways, Experiment Config
- Merge baseline into best_program_info.json step
- Comparison with BLIS results (% improvement only, not absolute scores)
- Concurrency warning
- Note: deployment viability assessment N/A for Vidur (generality experiment)

---

## Step 8: Update project docs

- Add vidur-router entry to `CLAUDE.md` doc index table
- Add quick-start recipe to `CLAUDE.md`
- Do NOT modify any BLIS-related files

---

## Step 9: Add Vidur dependencies

Add `vidur` extra to `pyproject.toml` (or document manual install):
- numpy, pandas, scikit-learn, wandb, plotly_express, kaleido, ddsketch, matplotlib, seaborn, fasteners

---

## Implementation Order

1. `benchmarks/vidur_router/__init__.py` (empty)
2. `benchmarks/vidur_router/initial_program.py`
3. `benchmarks/vidur_router/scripts/generate_traces.py` + generate CSVs
4. `benchmarks/vidur_router/evaluator.py` (core — most complex, address all Vidur runtime issues)
5. `benchmarks/vidur_router/config.yaml`
6. `benchmarks/vidur_router/scripts/pilot_study.py`
7. **Run pilot study** — validate traces, metrics extraction, scoring, load calibration
8. `benchmarks/vidur_router/scripts/compare_results.py`
9. `benchmarks/vidur_router/scripts/plot_results.py`
10. `benchmarks/vidur_router/scripts/analyze_effort.py`
11. `benchmarks/vidur_router/scripts/analyze_diffs.py`
12. `benchmarks/vidur_router/scripts/validate_robustness.py`
13. `docs/experiments/vidur-router.md`
14. Update `CLAUDE.md` doc index + quick-start
15. Add Vidur deps to pyproject.toml

---

## Verification

1. **Pilot study passes**: `python benchmarks/vidur_router/scripts/pilot_study.py` — all checks green
2. **Smoke test** (2 iterations):
   ```bash
   export VIDUR_OUTPUT_DIR="/tmp/vidur_smoke"
   export VIDUR_SEED="42"
   export VIDUR_MULTI_LLM="0"
   mkdir -p "$VIDUR_OUTPUT_DIR"
   uv run skydiscover-run \
     benchmarks/vidur_router/initial_program.py \
     benchmarks/vidur_router/evaluator.py \
     -c benchmarks/vidur_router/config.yaml \
     -s topk -i 2 -o "$VIDUR_OUTPUT_DIR"
   ```
3. **Baseline sanity**: LOR score is finite, negative, P95 >= mean
4. **Multi-LLM**: Both models produce valid metrics with profiling data
5. **Error handling**: Intentionally bad schedule() returns error sentinel, doesn't crash
6. **Isolation**: No artifacts in `benchmarks/vidur_router/` after evaluation
7. **Analysis scripts**: Run all 5 on smoke test output, verify CSV + plots + JSON

---

## Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `write_metrics=False` blocks all metrics | **High** | Keep True, disable plots individually (Step 3) |
| `atexit` handlers accumulate (12 per eval) | **High** | Unregister after each `simulator.run()` |
| Entity ID counters never reset | **Medium** | Reset class-level `_id` between simulations |
| Vidur deps conflict with SkyDiscover | **Low** | Add as separate extra in pyproject.toml |
| Workload QPS doesn't stress cluster | **Medium** | Load calibration in pilot study (Step 4) |
| Small improvement dismissed as noise | **Medium** | Multi-seed with stddev/CI reporting |
| Evolved schedule() crashes Vidur mid-sim | **Medium** | try/except wrapper → error sentinel |
| Profiling data missing for model | **Low** | Verified: exists for both Llama-2-7B and Llama-3-8B on A100 |

## Paper Framing Notes

- Report **% improvement over simulator-specific baseline** — never compare absolute scores across BLIS and Vidur
- Expected: smaller gains in Vidur (3-5% vs 10-15%+ in BLIS) due to reduced signal surface
- Frame as: "richer simulation fidelity + richer signals → richer discovered algorithms"
- Acknowledge stripped features (prefix caching, SLO, sessions) explicitly
- **Success criteria**: statistically significant improvement in ≥2/3 workloads across seeds
