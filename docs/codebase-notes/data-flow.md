# Data Flow

## Execution Flow

```
USER INPUT
  - initial_program.py (optional, with EVOLVE-BLOCK markers)
  - evaluator.py (required: evaluate(program_path) -> dict)
  - config YAML or CLI args
       |
       v
  Runner.__init__()
  - Loads config (YAML + CLI overrides)
  - Creates ProgramDatabase (algorithm-specific)
  - Loads initial program file
  - Sets up logging
       |
       v
  Runner.run()
  - Creates DiscoveryController (algorithm-specific)
  - Evaluates initial program (iteration 0)
  - Adds initial program to database
  - Starts monitor server (if enabled)
       |
       v
  MAIN DISCOVERY LOOP (for each iteration 1..max_iterations)
       |
       v
  1. Database.sample()
     - Algorithm-specific parent selection
     - Returns: parent Program + list of context Programs
       |
       v
  2. ContextBuilder.build_prompt()
     - Formats parent code, metrics, artifacts, context programs
     - Applies exploration/exploitation guidance (AdaEvolve)
     - Returns: (system_message, user_message) strings
       |
       v
  3. LLMPool.generate()  (or AgenticGenerator if --agentic)
     - Selects model via weighted sampling
     - Calls LLM API with system + user message
     - Returns: LLMResponse with generated text
       |
       v
  4. Parse LLM response
     - extract_diffs() if diff_based_generation=true
     - parse_full_rewrite() otherwise
     - Applies diff/rewrite to parent code
     - Returns: new program source code string
       |
       v
  5. Evaluator.evaluate_program()
     - Writes code to temp file
     - Runs evaluate(program_path) in subprocess with timeout
     - Cascade evaluation: quick check -> full eval (if enabled)
     - LLM judge scoring (if enabled)
     - Returns: EvaluationResult (metrics + artifacts)
       |
       v
  6. Database.add()
     - Stores Program with metrics
     - Algorithm-specific: update islands, Pareto front, beam, etc.
     - Updates best-ever tracking
       |
       v
  7. Checkpoint (every N iterations)
     - Saves database state + best program to disk
       |
       v
  (repeat loop)
       |
       v
  OUTPUT
  - best/best_program.py          (best solution code)
  - best/best_program_info.json   (metadata: id, score, parent, timestamp)
  - checkpoints/checkpoint_N/     (resumable state)
  - logs/                          (iteration logs)
  - Returns DiscoveryResult object
```

## Key Data Structures

### Program (search/base_database.py)
```python
@dataclass
class Program:
    id: str                    # UUID
    solution: str              # Source code or text
    metrics: dict              # {"combined_score": float, ...domain-specific...}
    parent_id: str | None      # UUID of parent program
    iteration_found: int       # Which iteration produced this
    artifacts: dict | None     # Evaluation feedback for LLM context
    generation_metadata: dict  # LLM model used, tokens, etc.
```

### EvaluationResult (evaluation/evaluation_result.py)
```python
@dataclass
class EvaluationResult:
    metrics: dict              # {"combined_score": float, ...}
    artifacts: dict | None     # Feedback text, partial results, etc.
```

### DiscoveryResult (api.py)
```python
@dataclass
class DiscoveryResult:
    best_program: Program      # Best Program object found
    best_score: float          # Primary metric value
    best_solution: str         # Actual source code
    metrics: dict              # Final metrics dict
    output_dir: str            # Where results were saved
```

### LLMResponse (llm/base.py)
```python
@dataclass
class LLMResponse:
    content: str               # Generated text
    model: str                 # Model that generated it
    usage: dict                # Token usage stats
```

## Output Directory Structure

```
<output_dir>/
  best/
    best_program.py              # Final best solution
    best_program_info.json       # Metadata
  checkpoints/
    checkpoint_10/               # Every checkpoint_interval iterations
      database.json              # Full database state (resumable)
      best_program_info.json
    checkpoint_20/
    ...
  logs/
    search.log                   # Iteration-by-iteration log
    adaevolve_signals.json       # (AdaEvolve only) Island adaptation metrics
```

## Scoring Convention

- `combined_score` is the primary optimization target (maximized)
- If `combined_score` is not in the evaluator's return dict, SkyDiscover averages all numeric values
- `artifacts` dict entries are injected into the next LLM prompt as context feedback
- Higher score = better (always maximized)
