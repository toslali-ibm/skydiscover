# Search Algorithms Deep Dive

## Algorithm Summary

| Algorithm | Complexity | Custom Controller | Key Idea |
|-----------|-----------|-------------------|----------|
| TopK | Simple | No | Keep top-K, refine one at random |
| Best-of-N | Simple | No | Generate N variants per round, keep best |
| Beam Search | Medium | No | Expand beam of top solutions with diversity |
| OpenEvolve Native | Medium | No | MAP-Elites quality-diversity grid + islands |
| GEPA Native | Medium | Yes | Pareto-based multi-objective + LLM merge |
| AdaEvolve | Complex | Yes | Multi-island UCB + adaptive intensity + paradigm breakthrough |
| EvoX | Complex | Yes | Co-evolves solutions AND search strategy itself |

---

## TopK (`search/topk/`)

**Files**: `database.py` (~56 lines)

**How it works**:
- Maintains all programs sorted by score
- `sample()`: Returns the best program as parent, next K-1 as context
- Uses default controller (no custom iteration logic)

**When to use**: Baseline comparisons, simple problems

---

## Best-of-N (`search/best_of_n/`)

**Files**: `database.py` (~85 lines)

**How it works**:
- Reuses the same parent for N consecutive iterations
- After N iterations, switches parent to current best
- Configurable via `best_of_n` parameter

**When to use**: When you want to explore many variants of a good solution before moving on

---

## Beam Search (`search/beam_search/`)

**Files**: `database.py` (~527 lines)

**How it works**:
- Maintains a beam of `beam_width` top solutions
- Selection strategies: `diversity_weighted`, `stochastic`, `round_robin`, `best`
- Diversity weighting balances exploitation (score) vs exploration (code diversity)
- Tracks lineage depth for optional depth penalties

**Key config**:
```yaml
beam_width: 5
beam_selection_strategy: "diversity_weighted"
beam_diversity_weight: 0.3
```

---

## OpenEvolve Native (`search/openevolve_native/`)

**Files**: `database.py` (~100+ lines)

**How it works**:
- MAP-Elites quality-diversity grid per island
- Multiple islands with ring-topology migration
- Three sampling modes: explore (random from island), exploit (archive elite), random
- Feature dimensions define the MAP-Elites grid (e.g., complexity, diversity)

**Key config**:
```yaml
num_islands: 5
exploration_ratio: 0.2
exploitation_ratio: 0.7
migration_interval: 10
```

---

## GEPA Native (`search/gepa_native/`)

**Files**: `controller.py`, `database.py`, `pareto_utils.py`

**How it works**:
- Multi-objective optimization using Pareto efficiency
- **Acceptance gating**: only accepts programs that improve the Pareto front
- **LLM-mediated merge**: asks LLM to intelligently combine two programs
- **Reflective prompting**: includes evaluation artifacts in next prompt

**Custom controller behavior**:
- After normal generation, may trigger merge operations
- Pareto dominance check before adding to database

---

## AdaEvolve (`search/adaevolve/`) - Flagship Algorithm

**Files**: `controller.py`, `database.py` (~1900 lines), `adaptation.py`, `archive/`, `paradigm/`

### Core Components

#### 1. Multi-Island System
- Population split into `num_islands` islands, each with `population_size` programs
- Islands evolve semi-independently, enabling parallel search in different regions
- **Dynamic island spawning**: new islands created when productivity exceeds threshold
- **Migration**: top programs copied between islands every `migration_interval` iterations

#### 2. Adaptive Search Intensity (adaptation.py)
- Tracks **accumulated improvement signal** `G` per island:
  - `delta` = normalized magnitude of fitness gain
  - `G = decay * G + delta^2` (exponential moving average)
- Search intensity = `f(G)` mapped to `[intensity_min, intensity_max]`
- High G (rapid improvement) -> higher intensity (more exploration)
- Low G (stagnation) -> lower intensity (more exploitation)

#### 3. UCB Island Selection
- Upper Confidence Bound balances exploration vs exploitation across islands
- UCB score = average reward + exploration bonus
- Islands with less exploration get higher bonus

#### 4. Paradigm Breakthrough (paradigm/)
- When global progress stalls (measured over `paradigm_window_size` iterations):
  - LLM generates novel high-level strategies ("paradigms")
  - New paradigms are tried as starting points
  - Limited retries (`paradigm_max_uses`) to prevent divergence
- Controlled by `paradigm_improvement_threshold`

#### 5. Error Retry
- Failed generations (syntax errors, runtime errors) are retried with error context injected into the prompt
- Up to `max_error_retries` attempts

#### 6. Unified Archive (archive/)
- Quality-diversity archive tracks elites by fitness + novelty
- Diversity strategies: `code` (code similarity), `metric` (metric similarity), `hybrid`
- Elite score = `fitness_weight * fitness + novelty_weight * novelty`

### Prompting Modes
- **Exploration** (high intensity): "Suggest creative, different approaches. Prioritize novelty."
- **Exploitation** (low intensity): "Refine existing patterns. Focus on small, targeted improvements."

### Key Config
```yaml
search:
  type: "adaevolve"
  database:
    population_size: 20
    num_islands: 2
    decay: 0.9
    intensity_min: 0.15
    intensity_max: 0.5
    migration_interval: 15
    use_paradigm_breakthrough: true
    use_adaptive_search: true
    use_ucb_selection: true
```

### Ablation Flags
Set any to `false` to disable for ablation studies:
- `use_adaptive_search` -> fixed intensity instead
- `use_ucb_selection` -> round-robin island selection
- `use_migration` -> no inter-island migration
- `use_unified_archive` -> simple list instead of QD archive
- `use_paradigm_breakthrough` -> no paradigm shifts
- `use_dynamic_islands` -> fixed number of islands

---

## EvoX (`search/evox/`) - Co-Evolution Algorithm

**Files**: `controller.py`, `database/`, `utils/`

### Core Idea
Co-evolves **solutions** and the **search algorithm itself**. The search strategy is a Python program that gets evolved alongside the solutions it helps find.

### How It Works

1. **Two populations maintained simultaneously**:
   - Solution population (the actual optimization targets)
   - Search strategy population (Python code defining sampling/selection logic)

2. **Each iteration**:
   - Current search strategy is executed to decide how to sample/select parents
   - Solutions are evolved normally using that strategy
   - Strategy quality is scored based on solution improvements it produces
   - Search strategy is also evolved via LLM

3. **Strategy scoring** (`utils/search_scorer.py`):
   - `LogWindowScorer` evaluates strategy quality
   - Metrics: improvement rate, final best score, convergence speed

4. **Variation operators** (`utils/variation_operator_generator.py`):
   - Auto-generated from evaluator feedback
   - LLM creates problem-specific mutation operators

### Key Config
```yaml
search:
  type: "evox"
  database:
    auto_generate_variation_operators: true
```

---

## External Backends

These wrap external packages to run through SkyDiscover's interface:

| Backend | Package | Flag |
|---------|---------|------|
| OpenEvolve | `codelion/openevolve` | `--search openevolve` |
| GEPA | `gepa-ai/gepa` | `--search gepa` |
| ShinkaEvolve | `SakanaAI/ShinkaEvolve` | `--search shinkaevolve` |

Install: `uv sync --extra external`

These bypass SkyDiscover's database/controller and use the external package's own search logic, but results are collected through SkyDiscover's reporting.
