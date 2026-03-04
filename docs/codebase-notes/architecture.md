# Architecture Overview

## High-Level Design

SkyDiscover follows a **modular, pluggable pipeline architecture**. Every major component (search algorithm, prompt builder, evaluator, LLM backend) can be swapped independently.

```
CLI (cli.py) / Python API (api.py)
         |
         v
     Runner (runner.py)
     - Loads config, creates database, manages checkpoints
         |
         v
     Discovery Controller (search/default_discovery_controller.py)
     - Core loop: sample -> prompt -> LLM -> evaluate -> store
     - Subclasses: AdaEvolveController, CoEvolutionController, GEPANativeController
         |
    +---------+-----------+-----------+
    |         |           |           |
    v         v           v           v
 Database  Context     Evaluator    LLM Pool
 (stores   Builder    (runs user's  (calls
 programs) (builds     eval func)   LLM APIs)
            prompts)
```

## Layer Responsibilities

### Entry Layer
- **`cli.py`**: Parses CLI args, builds config, calls Runner
- **`api.py`**: `run_discovery()` and `discover_solution()` wrap Runner for Python usage
- **`__init__.py`**: Re-exports public API

### Orchestration Layer
- **`runner.py`**: Top-level orchestrator. Loads config, creates the database + controller pair, seeds the initial program (iteration 0), runs the main loop, manages checkpoints and monitoring

### Search Layer
- **`search/base_database.py`**: `Program` dataclass + `ProgramDatabase` ABC. All algorithms store programs as `Program` objects
- **`search/default_discovery_controller.py`**: Default discovery loop. Each iteration: sample parent from DB -> build prompt via ContextBuilder -> call LLM -> parse code from response -> evaluate -> add to DB
- **`search/registry.py`**: Factory pattern - registries mapping algorithm names to (database_class, controller_class, program_class) tuples
- **`search/route.py`**: Wires all algorithm implementations into the registries

### Algorithm Layer
Each algorithm lives in its own subdirectory under `search/`:
- `topk/`, `best_of_n/`, `beam_search/` - Simple algorithms (database only, use default controller)
- `adaevolve/` - Complex: custom database + controller + adaptation + paradigm breakthrough
- `evox/` - Complex: custom database + controller + co-evolution logic
- `gepa_native/` - Custom controller with Pareto-based acceptance
- `openevolve_native/` - MAP-Elites database, uses default controller

### Prompt Layer
- **`context_builder/base.py`**: `ContextBuilder` ABC
- **`context_builder/default/`**: Default prompt builder + templates
- **`context_builder/adaevolve/`**, **`evox/`**, **`gepa_native/`**: Algorithm-specific prompt builders
- **`context_builder/utils.py`**: `TemplateManager` loads `.txt` template files, formats artifacts

### Evaluation Layer
- **`evaluation/evaluator.py`**: Runs user's `evaluate(program_path)` in subprocess with timeout, retries, and optional cascade evaluation
- **`evaluation/llm_judge.py`**: Optional secondary scoring via LLM-as-judge

### LLM Layer
- **`llm/base.py`**: `LLMInterface` ABC + `LLMResponse` dataclass
- **`llm/openai.py`**: Implementation via LiteLLM (supports OpenAI, Gemini, Anthropic, local models)
- **`llm/llm_pool.py`**: Weighted random sampling from multiple models
- **`llm/agentic_generator.py`**: Multi-turn agent that can read files and search codebase before generating

### Extras
- **`extras/monitor/`**: Live HTTP dashboard for real-time visualization
- **`extras/external/`**: Wrappers for external backends (OpenEvolve, GEPA, ShinkaEvolve packages)

## Key Design Patterns

1. **Registry + Factory**: Algorithm implementations register themselves; `route.py` wires them. Runner uses `create_database()` / `create_controller()` factories
2. **Template Method**: `DefaultDiscoveryController` defines the iteration loop; algorithm-specific controllers override specific steps
3. **Strategy Pattern**: Database implementations define different sampling/storage strategies
4. **Async Pipeline**: Core loop is async for concurrent LLM calls and evaluations via `AsyncTaskPool`
5. **Configuration-as-code**: YAML configs are parsed into typed dataclasses (`config.py` ~800 lines) with CLI overrides
