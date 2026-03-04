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
| BLIS router experiment design & execution plan    | [docs/plans/design-blis-experiments.md](docs/plans/design-blis-experiments.md) |

## Key Commands

```bash
# Install
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
```

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
configs/              # YAML config templates
scripts/reproduce/    # Reproduction scripts for paper results
tests/                # Pytest tests
```

## Conventions

- **Entry points**: `skydiscover-run` CLI or `run_discovery()` / `discover_solution()` Python API
- **Search algorithms** are registered in `search/route.py` via `search/registry.py`
- **Config** is YAML-driven; CLI flags override YAML values
- **Evaluator pattern**: every benchmark has `evaluator.py` with `evaluate(program_path) -> dict`
- **EVOLVE-BLOCK markers**: `# EVOLVE-BLOCK-START` / `# EVOLVE-BLOCK-END` in initial programs mark mutable regions
- **LLM providers**: any LiteLLM-compatible model via `provider/model` format
- **Async-first**: core loop uses asyncio for concurrent LLM calls and evaluations
