# Extending SkyDiscover

## Adding a New Search Algorithm

### 1. Create directory
```
skydiscover/search/my_algorithm/
  __init__.py
  database.py       # Required: ProgramDatabase subclass
  controller.py     # Optional: only if you need custom iteration logic
```

### 2. Implement database

```python
# database.py
from skydiscover.search.base_database import ProgramDatabase, Program

class MyAlgorithmDatabase(ProgramDatabase):
    def __init__(self, config):
        super().__init__(config)
        # Initialize your data structures

    def add(self, program: Program) -> None:
        # Store a new evaluated program
        ...

    def sample(self) -> tuple[Program, list[Program]]:
        # Return (parent_program, context_programs)
        # parent = the program to mutate
        # context = additional examples for LLM context
        ...

    def get_best(self) -> Program:
        # Return best program found so far
        ...
```

### 3. (Optional) Implement custom controller

Only needed if you want to change the iteration loop itself (not just sampling/storage):

```python
# controller.py
from skydiscover.search.default_discovery_controller import DefaultDiscoveryController

class MyAlgorithmController(DefaultDiscoveryController):
    async def run_iteration(self, iteration: int) -> Program | None:
        # Custom iteration logic
        # Can call self.generate_and_evaluate() for the standard sub-steps
        ...
```

### 4. Register in route.py

```python
# In skydiscover/search/route.py, add:
from skydiscover.search.my_algorithm.database import MyAlgorithmDatabase
# If custom controller:
from skydiscover.search.my_algorithm.controller import MyAlgorithmController

register_database("my_algorithm", MyAlgorithmDatabase)
register_controller("my_algorithm", MyAlgorithmController)  # or omit for default
```

### 5. (Optional) Add config dataclass

In `skydiscover/config.py`, add a new database config:

```python
@dataclass
class MyAlgorithmDatabaseConfig:
    my_param: int = 10
```

And wire it into `DatabaseConfig`.

### 6. (Optional) Add context builder

If your algorithm needs special prompt formatting:

```
skydiscover/context_builder/my_algorithm/
  builder.py       # Subclass of ContextBuilder
  templates/       # .txt prompt templates
```

## Adding a New Benchmark

### 1. Create benchmark directory

```
benchmarks/my_domain/my_task/
  initial_program.py   # Optional: starting solution
  evaluator.py         # Required: evaluate(program_path) -> dict
  config.yaml          # Optional: task-specific config
  requirements.txt     # Optional: extra dependencies
```

### 2. Write evaluator

```python
# evaluator.py
def evaluate(program_path):
    """Score a candidate program.

    Args:
        program_path: Path to the generated .py file

    Returns:
        dict with "combined_score" (float, higher=better)
        and optional "artifacts" (dict, fed back to LLM)
    """
    # Load and execute the candidate
    import importlib.util
    spec = importlib.util.spec_from_file_location("solution", program_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.solve(test_input)
    score = compute_quality(result)

    return {
        "combined_score": score,
        "artifacts": {
            "feedback": f"Achieved {score:.4f}. Try optimizing the inner loop.",
        },
    }
```

### 3. Write initial program (optional)

```python
# initial_program.py
import numpy as np

# EVOLVE-BLOCK-START
def solve(input_data):
    """Baseline solution - SkyDiscover will improve this."""
    return input_data
# EVOLVE-BLOCK-END
```

The `EVOLVE-BLOCK-START` / `EVOLVE-BLOCK-END` markers define the mutable region. Everything outside is preserved. If omitted, the entire file is mutable.

### 4. Write config (optional)

```yaml
# config.yaml
prompt:
  system_message: |
    You are an expert at [your domain].
    [Constraints, hints, domain knowledge]
evaluator:
  timeout: 120
search:
  type: "adaevolve"
```

### 5. Run

```bash
uv run skydiscover-run \
  benchmarks/my_domain/my_task/initial_program.py \
  benchmarks/my_domain/my_task/evaluator.py \
  -c benchmarks/my_domain/my_task/config.yaml \
  -s adaevolve -i 100
```

## Adding a New LLM Provider

LLM providers are handled via LiteLLM. If your provider is LiteLLM-compatible, just use the right `provider/model` format. No code changes needed.

For a truly custom provider, implement `LLMInterface` from `skydiscover/llm/base.py`.

## Adding a New External Backend

1. Create wrapper in `skydiscover/extras/external/my_backend.py`
2. Register in `skydiscover/extras/external/__init__.py`
3. The wrapper should accept SkyDiscover's config and translate it to the external package's API
