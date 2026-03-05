# SkyDiscover - Claude Session Guide

## What is this project?
SkyDiscover is a modular framework for AI-driven scientific and algorithmic discovery. It provides a unified interface for running and comparing optimization algorithms (AdaEvolve, EvoX, etc.) across ~200 benchmarks (math, systems, GPU, competitive programming, etc.).

## Doc Index - What to read for what

| I need to...                                    | Read this                          |
|-------------------------------------------------|------------------------------------|
| Understand the overall architecture              | [docs/codebase-notes/architecture.md](docs/codebase-notes/architecture.md) |
| Find a specific file or module                   | [docs/codebase-notes/codebase-map.md](docs/codebase-notes/codebase-map.md) |
| Understand how data flows through the system     | [docs/codebase-notes/data-flow.md](docs/codebase-notes/data-flow.md) |
| Learn how each search algorithm works            | [docs/codebase-notes/algorithms.md](docs/codebase-notes/algorithms.md) |
| Run an experiment or reproduce results           | [docs/codebase-notes/running-experiments.md](docs/codebase-notes/running-experiments.md) |
| Understand or modify configuration               | [docs/codebase-notes/configuration.md](docs/codebase-notes/configuration.md) |
| Add a new algorithm, benchmark, or component     | [docs/codebase-notes/extending.md](docs/codebase-notes/extending.md) |
| Run BLIS router experiments                       | [docs/experiments/blis-router.md](docs/experiments/blis-router.md) |
| BLIS router experiment design & execution plan    | [docs/plans/design-blis-experiments.md](docs/plans/design-blis-experiments.md) |

## Key Commands

```bash
# Install (uv must be available — install with: pip3 install uv)
uv sync

# Run a benchmark
uv run skydiscover-run <initial_program> <evaluator> -c <config.yaml> -s <algorithm> -i <iterations>

# Run tests
uv run pytest tests/

# Install benchmark-specific deps
uv sync --extra math          # math benchmarks
uv sync --extra adrs          # systems benchmarks
uv sync --extra frontier-cs   # competitive programming
uv sync --extra external      # external backends (OpenEvolve, GEPA, ShinkaEvolve)

# BLIS router pilot study (validates entire pipeline)
python benchmarks/blis_router/scripts/pilot_study.py
```

## Quick-Start: Run a BLIS Router Experiment

When asked to run a BLIS router experiment, use this exact recipe. **Do NOT use the `-m` flag** — the config.yaml already specifies models with the correct api_base.

```bash
# 1. Ensure uv is installed (check: which uv)
pip3 install uv 2>/dev/null  # no-op if already installed
uv sync                       # one-time: install project deps

# 2. Pre-flight checks (mandatory)
cd benchmarks/blis_router/inference-sim && git diff sim/routing.go  # must be empty
ls benchmarks/blis_router/baseline_metrics.json 2>/dev/null         # must not exist

# 3. Run a single framework (replace FRAMEWORK and ITERATIONS)
export BLIS_OUTPUT_DIR="outputs/blis_router/<EXPERIMENT_DIR>/<FRAMEWORK>"
export BLIS_SEED="42"
mkdir -p "$BLIS_OUTPUT_DIR"
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s <FRAMEWORK> \
  -i <ITERATIONS> \
  -o "$BLIS_OUTPUT_DIR" \
  -l INFO

# 4. Post-run verification
cd benchmarks/blis_router/inference-sim && git diff sim/routing.go  # must be empty
ls benchmarks/blis_router/baseline_metrics.json 2>/dev/null         # must not exist
```

**Available frameworks**: `adaevolve`, `evox`, `openevolve_native`, `gepa_native`, `topk`, `best_of_n`, `beam_search`

**Common pitfalls**:
- `uv` not found → `pip3 install uv`
- `Provider 'aws' requires api_base` → do NOT use `-m` flag; models are in config.yaml
- EvoX 401 errors on label generation → fixed in `search/evox/controller.py` (propagates parent LLM config to search controller). If this recurs, the search controller's LLM config is being loaded from `search/evox/config/search.yaml` which defaults to OpenAI — the fix is to propagate `self.config.llm` to `controller_input.config.llm` in `_init_search_evolution_controller()`.
- Experiments take ~30s per iteration (LLM call + Go build + 3 workload simulations)

## Project Structure (top-level)

```
skydiscover/          # Main package
  api.py              # Python API: run_discovery(), discover_solution()
  cli.py              # CLI entry point: skydiscover-run
  runner.py           # Orchestrator
  config.py           # All config dataclasses
  search/             # Search algorithms (adaevolve, evox, topk, beam_search, etc.)
  context_builder/    # Prompt generation for each algorithm
  evaluation/         # Evaluator, LLM judge
  llm/                # LLM backends (OpenAI/LiteLLM, agentic generator)
  utils/              # Code parsing, metrics, async utilities
  extras/             # Monitor dashboard, external backends
benchmarks/           # ~200 optimization tasks
  blis_router/        # BLIS router optimization benchmark (see below)
configs/              # YAML config templates
scripts/reproduce/    # Reproduction scripts for paper results
tests/                # Pytest tests
docs/experiments/     # Experiment guides
docs/plans/           # Design docs and implementation plans
docs/codebase-notes/  # Codebase documentation
```

## Conventions

- **Entry points**: `skydiscover-run` CLI or `run_discovery()` / `discover_solution()` Python API
- **Search algorithms** are registered in `search/route.py` via `search/registry.py`
- **Config** is YAML-driven; CLI flags override YAML values
- **Evaluator pattern**: every benchmark has `evaluator.py` with `evaluate(program_path) -> dict`
- **EVOLVE-BLOCK markers**: `# EVOLVE-BLOCK-START` / `# EVOLVE-BLOCK-END` in initial programs mark mutable regions
- **LLM providers**: any LiteLLM-compatible model via `provider/model` format
- **Async-first**: core loop uses asyncio for concurrent LLM calls and evaluations

## BLIS Router Experiment Rules

**READ [docs/experiments/blis-router.md](docs/experiments/blis-router.md) BEFORE running any BLIS experiment.**

When running BLIS router experiments, Claude sessions MUST follow these rules:

### Mandatory Pre-flight

1. **Run pilot study first**: `python benchmarks/blis_router/scripts/pilot_study.py` — all tests must pass
2. **Verify clean state**: No `baseline_metrics.json` or `.eval_artifacts` in `benchmarks/blis_router/`
3. **Verify routing.go**: `cd benchmarks/blis_router/inference-sim && git diff sim/routing.go` must show no changes

### Mandatory During Experiments

4. **Sequential execution only**: Run frameworks one at a time (adaevolve → evox → openevolve → gepa → shinkaevolve). They share `routing.go` and CANNOT run in parallel.
5. **Always set `BLIS_OUTPUT_DIR`**: Every experiment's artifacts (baseline, logs) go to its own output directory. NEVER write to the benchmark directory.
6. **Always set `BLIS_SEED`**: For reproducibility. Default is 42. Record the seed with results.
7. **Verify isolation after each framework**:
   - `routing.go` unchanged (evaluator restores it via try/finally)
   - No artifacts leaked to benchmark directory
   - Results exist in the expected output directory

### Mandatory Post-experiment

8. **Record configuration**: Save seed, iterations, model, inference-sim commit hash with results
9. **Run comparison**: `python benchmarks/blis_router/scripts/compare_results.py <output_dir>`
10. **Plot accuracy results**: `python benchmarks/blis_router/scripts/plot_results.py <output_dir>` (saves PNGs to `<output_dir>/plots/`). Plots include baseline as a bar and annotate each framework bar with % improvement vs baseline.
11. **Run effort/cost analysis**: `python benchmarks/blis_router/scripts/analyze_effort.py <output_dir>` — produces effort metrics table, CSV, JSON, and plots (iteration duration boxplot, convergence curves, effort-vs-improvement scatter, search efficiency bar chart). See [Post-Experiment Analysis](docs/experiments/blis-router.md#post-experiment-analysis) for full details.
12. **Write `analysis.md`** in output dir with both accuracy AND effort/cost results: accuracy tables, effort summary (wall time, iteration times, population size, diversity), search efficiency (%/min), convergence behavior, population quality, and key accuracy-vs-cost takeaways. See `outputs/blis_router/20260304_182612/analysis.md` as the reference template.
13. **Never delete output directories** — they are the permanent experimental record
