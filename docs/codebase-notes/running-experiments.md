# Running Experiments

## Prerequisites

```bash
# Python >= 3.10, uv installed
uv sync
export OPENAI_API_KEY="sk-..."
# For Gemini: export GEMINI_API_KEY="..."
# For Anthropic: export ANTHROPIC_API_KEY="..."
```

## Quick Start - Single Benchmark

```bash
# Pattern:
uv run skydiscover-run [INITIAL_PROGRAM] EVALUATOR [OPTIONS]

# Example: circle packing with AdaEvolve
uv sync --extra math
uv run skydiscover-run \
  benchmarks/math/circle_packing/initial_program.py \
  benchmarks/math/circle_packing/evaluator.py \
  --config benchmarks/math/circle_packing/config.yaml \
  --search adaevolve \
  --iterations 100

# Without initial program (LLM starts from scratch):
uv run skydiscover-run evaluator.py --search evox --model gpt-5 --iterations 100
```

## Python API

```python
from skydiscover import run_discovery

result = run_discovery(
    initial_program="benchmarks/math/circle_packing/initial_program.py",
    evaluator="benchmarks/math/circle_packing/evaluator.py",
    config="benchmarks/math/circle_packing/config.yaml",
    search="adaevolve",
    model="gpt-5",
    iterations=100,
)
print(result.best_score, result.best_solution)
```

## Reproducing Paper Results

All reproduction scripts are in `scripts/reproduce/`:

```bash
# All benchmarks
bash scripts/reproduce/run_all.sh

# By domain
bash scripts/reproduce/math.sh              # Math (14 tasks), needs: uv sync --extra math
bash scripts/reproduce/adrs.sh              # Systems (5 tasks), needs: uv sync --extra adrs
bash scripts/reproduce/gpu.sh               # GPU (4 tasks)
bash scripts/reproduce/frontier_cs.sh       # Competitive programming (172 tasks), needs: uv sync --extra frontier-cs
bash scripts/reproduce/ale_bench.sh         # Algorithmic (10 tasks)
bash scripts/reproduce/arc.sh               # ARC-AGI
bash scripts/reproduce/prompt_opt.sh        # HotPotQA, needs: uv sync --extra prompt-optimization
```

## Comparing Algorithms on a Single Task

```bash
for algo in topk beam_search best_of_n adaevolve evox gepa_native openevolve_native; do
  uv run skydiscover-run \
    benchmarks/math/circle_packing/initial_program.py \
    benchmarks/math/circle_packing/evaluator.py \
    --config benchmarks/math/circle_packing/config.yaml \
    --search $algo \
    --iterations 100 \
    --output results/${algo}
done
```

## Key CLI Flags

| Flag | Description | Example |
|------|-------------|---------|
| `-c, --config` | YAML config file | `-c configs/adaevolve.yaml` |
| `-i, --iterations` | Number of iterations | `-i 100` |
| `-m, --model` | LLM model (overrides config) | `-m gpt-5` or `-m gemini/gemini-3-pro-preview` |
| `-s, --search` | Search algorithm | `-s adaevolve` |
| `-o, --output` | Output directory | `-o results/my_run` |
| `--api-base` | Override LLM API endpoint | `--api-base http://localhost:11434/v1` |
| `--checkpoint` | Resume from checkpoint | `--checkpoint results/my_run/checkpoints/checkpoint_50` |
| `--agentic` | Enable agentic mode | `--agentic` |
| `-l, --log-level` | Log verbosity | `-l DEBUG` |

## Using Local Models

```bash
# Ollama
uv run skydiscover-run evaluator.py \
  --model ollama/llama3 \
  --api-base http://localhost:11434/v1 \
  --search topk -i 50

# vLLM
uv run skydiscover-run evaluator.py \
  --model vllm/my-model \
  --api-base http://localhost:8000/v1 \
  --search adaevolve -i 100
```

## Multi-Model Pools

In YAML config:
```yaml
llm:
  models:
    - name: "gpt-5"
      weight: 0.7
    - name: "gemini/gemini-2.0-flash"
      weight: 0.3
```

## Live Monitoring

Add to config:
```yaml
monitor:
  enabled: true
  port: 8765
```

Dashboard URL prints at run start. Replay a completed run:
```bash
uv run skydiscover-viewer /path/to/checkpoints/checkpoint_100
```

## Resuming from Checkpoint

```bash
uv run skydiscover-run \
  benchmarks/math/circle_packing/initial_program.py \
  benchmarks/math/circle_packing/evaluator.py \
  --checkpoint results/my_run/checkpoints/checkpoint_50 \
  --iterations 100  # continues from iteration 50 to 100
```

## Writing Your Own Benchmark

### 1. Evaluator (required)
```python
# evaluator.py
def evaluate(program_path):
    # Load and run the candidate program
    exec(open(program_path).read())
    score = ...  # compute quality metric
    return {
        "combined_score": score,        # primary metric (maximized)
        "artifacts": {                  # optional - fed back to LLM
            "feedback": "Off by one in loop boundary",
        },
    }
```

### 2. Initial Program (optional)
```python
# initial_program.py
import numpy as np

# EVOLVE-BLOCK-START
def solve(input_data):
    return input_data  # baseline
# EVOLVE-BLOCK-END
```

### 3. Config (optional)
```yaml
# config.yaml
prompt:
  system_message: |
    You are an expert at solving [your problem].
    [Domain-specific hints and constraints]
search:
  type: "adaevolve"
evaluator:
  timeout: 60
```

### 4. Run
```bash
uv run skydiscover-run initial_program.py evaluator.py -c config.yaml -s adaevolve -i 100
```

## Output Structure

After a run completes:
```
<output_dir>/
  best/
    best_program.py              # Best solution found
    best_program_info.json       # Score, metadata
  checkpoints/
    checkpoint_10/               # Resumable state
    checkpoint_20/
    ...
  logs/
    search.log                   # Full iteration log
```
