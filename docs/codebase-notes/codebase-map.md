# Codebase Map

File-by-file reference for the SkyDiscover codebase. Use this to find where specific functionality lives.

## Core Entry Points

| File | Purpose |
|------|---------|
| `skydiscover/__init__.py` | Public API exports: `run_discovery`, `discover_solution`, `Runner` |
| `skydiscover/api.py` | High-level APIs wrapping Runner |
| `skydiscover/cli.py` | CLI entry point (`skydiscover-run` command) |
| `skydiscover/runner.py` | Top-level orchestrator: loads config, creates DB/controller, runs discovery loop, checkpoints |
| `skydiscover/config.py` | All config dataclasses (~800 lines): `Config`, `LLMConfig`, `SearchConfig`, `EvaluatorConfig`, etc. |

## Search Algorithms

### Infrastructure
| File | Purpose |
|------|---------|
| `skydiscover/search/base_database.py` | `Program` dataclass + `ProgramDatabase` ABC |
| `skydiscover/search/default_discovery_controller.py` | Default iteration loop: sample -> prompt -> LLM -> evaluate -> store |
| `skydiscover/search/registry.py` | Factory functions + registries for database/controller/program classes |
| `skydiscover/search/route.py` | Wires all algorithm implementations to registries |

### Simple Algorithms (database only, use default controller)
| File | Purpose |
|------|---------|
| `skydiscover/search/topk/database.py` | TopK: selects top-K solutions, picks one to refine |
| `skydiscover/search/best_of_n/database.py` | Best-of-N: generates N variants per round, keeps best |
| `skydiscover/search/beam_search/database.py` | Beam search with diversity-weighted selection |
| `skydiscover/search/openevolve_native/database.py` | MAP-Elites + island-based evolutionary search |

### AdaEvolve (complex, custom controller + database)
| File | Purpose |
|------|---------|
| `skydiscover/search/adaevolve/controller.py` | AdaEvolve controller: UCB island selection, error retry |
| `skydiscover/search/adaevolve/database.py` | Multi-island database (~1900 lines): populations, migration, spawning |
| `skydiscover/search/adaevolve/adaptation.py` | `AdaptiveState`: accumulated improvement signal, search intensity |
| `skydiscover/search/adaevolve/archive/unified_archive.py` | Quality-diversity archive management |
| `skydiscover/search/adaevolve/archive/diversity.py` | Diversity metrics (code, metric, hybrid) |
| `skydiscover/search/adaevolve/paradigm/generator.py` | LLM-driven paradigm breakthrough generation |
| `skydiscover/search/adaevolve/paradigm/tracker.py` | Paradigm state tracking |

### EvoX (complex, custom controller + database)
| File | Purpose |
|------|---------|
| `skydiscover/search/evox/controller.py` | Co-evolution controller: alternates solution + search strategy evolution |
| `skydiscover/search/evox/database/search_strategy_db.py` | Search strategy database |
| `skydiscover/search/evox/database/search_strategy_evaluator.py` | Evaluates search strategy quality |
| `skydiscover/search/evox/database/initial_search_strategy.py` | Default starting search strategy |
| `skydiscover/search/evox/utils/search_scorer.py` | `LogWindowScorer` for scoring strategy performance |
| `skydiscover/search/evox/utils/variation_operator_generator.py` | Auto-generates variation operators from evaluator |
| `skydiscover/search/evox/utils/coevolve_logging.py` | Co-evolution logging |

### GEPA Native
| File | Purpose |
|------|---------|
| `skydiscover/search/gepa_native/controller.py` | Pareto-based controller with acceptance gating + LLM merge |
| `skydiscover/search/gepa_native/database.py` | Pareto-efficient program storage |
| `skydiscover/search/gepa_native/pareto_utils.py` | Pareto front utilities |

### Search Utilities
| File | Purpose |
|------|---------|
| `skydiscover/search/utils/checkpoint_manager.py` | Checkpoint save/load/resume |
| `skydiscover/search/utils/discovery_utils.py` | Serialization, database helpers |
| `skydiscover/search/utils/logging_utils.py` | Colored console formatting, JSON logging |

## Context Builders (Prompt Generation)

| File | Purpose |
|------|---------|
| `skydiscover/context_builder/base.py` | `ContextBuilder` ABC |
| `skydiscover/context_builder/utils.py` | `TemplateManager`, artifact formatting helpers |
| `skydiscover/context_builder/human_feedback.py` | Human feedback integration |
| `skydiscover/context_builder/default/builder.py` | Default prompt builder |
| `skydiscover/context_builder/default/templates/` | Default `.txt` prompt templates |
| `skydiscover/context_builder/adaevolve/builder.py` | AdaEvolve prompts (exploration vs exploitation) |
| `skydiscover/context_builder/evox/builder.py` | EvoX co-evolution prompts |
| `skydiscover/context_builder/evox/formatters.py` | EvoX-specific formatting |
| `skydiscover/context_builder/gepa_native/builder.py` | GEPA reflective prompting |

## Evaluation

| File | Purpose |
|------|---------|
| `skydiscover/evaluation/evaluator.py` | Runs `evaluate(program_path)` with timeout, retries, cascade |
| `skydiscover/evaluation/evaluation_result.py` | `EvaluationResult` dataclass |
| `skydiscover/evaluation/llm_judge.py` | LLM-as-judge secondary scoring |

## LLM Backends

| File | Purpose |
|------|---------|
| `skydiscover/llm/base.py` | `LLMInterface` ABC + `LLMResponse` dataclass |
| `skydiscover/llm/openai.py` | OpenAI/LiteLLM implementation (supports all providers) |
| `skydiscover/llm/llm_pool.py` | Weighted sampling from multiple LLM models |
| `skydiscover/llm/agentic_generator.py` | Multi-turn agent loop with file read + search tools |
| `skydiscover/llm/tool_schemas/` | JSON schemas for agentic tool calling |

## Utilities

| File | Purpose |
|------|---------|
| `skydiscover/utils/code_utils.py` | Diff extraction, full rewrite parsing, language detection, code extraction |
| `skydiscover/utils/metrics.py` | Score calculation: `combined_score` or average of numeric values |
| `skydiscover/utils/async_utils.py` | `AsyncTaskPool` for concurrent async execution |
| `skydiscover/utils/prepare.py` | File preparation utilities |

## Extras

| File | Purpose |
|------|---------|
| `skydiscover/extras/monitor/server.py` | Live monitoring HTTP/WebSocket server |
| `skydiscover/extras/monitor/callback.py` | Callback for pushing program updates to monitor |
| `skydiscover/extras/monitor/viewer.py` | CLI for replaying completed runs |
| `skydiscover/extras/monitor/dashboard.html` | Web UI dashboard |
| `skydiscover/extras/external/__init__.py` | External backend registry + dynamic loader |
| `skydiscover/extras/external/openevolve_backend.py` | OpenEvolve package wrapper |
| `skydiscover/extras/external/gepa_backend.py` | GEPA package wrapper |
| `skydiscover/extras/external/shinkaevolve_backend.py` | ShinkaEvolve package wrapper |

## Benchmarks

```
benchmarks/
  math/              # 14 tasks: circle_packing, erdos_min_overlap, heilbronn_triangle, etc.
  ADRS/              # 5 tasks: cloudcast, eplb, prism, etc.
  gpu_mode/          # 4 tasks: vecadd, grayscale, trimul, mla_decode
  frontier-cs-eval/  # 172 competitive programming tasks
  arc_benchmark/     # ARC-AGI visual reasoning
  ale_bench/         # 10 algorithmic problems
  image_gen/         # 1 image evolution task
  prompt_optimization/ # 1 HotPotQA task
```

Each benchmark has: `initial_program.py` (optional), `evaluator.py` (required), `config.yaml`, optionally `requirements.txt`.

## Config Templates

```
configs/
  default.yaml           # TopK, minimal template
  adaevolve.yaml         # AdaEvolve full config
  evox.yaml              # EvoX co-evolution
  openevolve_native.yaml # OpenEvolve Native
  llm_judge.yaml         # LLM-as-judge demo
  human_in_the_loop.yaml # Monitor + human feedback
```

## Scripts

```
scripts/
  run_cp.sh              # Quick-start script
  reproduce/
    run_all.sh           # All benchmarks
    math.sh, adrs.sh, gpu.sh, frontier_cs.sh, ale_bench.sh, arc.sh, prompt_opt.sh
```
