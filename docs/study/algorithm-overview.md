# Search Algorithm Overview

This document explains four LLM-driven program evolution algorithms: **AdaEvolve**, **EvoX**, **OpenEvolve**, and **GEPA**. All share a common loop — ask an LLM to improve code, evaluate the result, keep the good ones — but differ in *how* they pick parents, guide the LLM, and handle getting stuck.

---

## Shared Basics

Every algorithm follows the same high-level pattern:

1. **User provides**: a starter program (with `EVOLVE-BLOCK` markers around the part the LLM can change), an evaluator script that scores programs, and a config file.
2. **Each iteration**: pick a parent program, build a prompt, ask the LLM to produce a better version, evaluate the child, decide whether to keep it.
3. **Output**: the best program found after N iterations.

The key differences are in parent selection, prompt construction, population management, and stagnation handling.

---

## 1. AdaEvolve

**Core idea**: Adaptive multi-island search that automatically shifts between exploration (try new ideas) and exploitation (refine what works) based on recent progress.

### Workflow

```
For each iteration:
  1. Pick an island using UCB (multi-armed bandit — favor islands that are improving)
  2. Check if globally stuck → if yes, generate "paradigm breakthrough" ideas via a separate LLM call
  3. Pick a parent from that island's archive (explore or exploit mode)
  4. Build prompt with: parent code + diverse context programs + mode label + evaluator feedback
  5. LLM generates child → evaluate → add to island's archive
  6. Periodically migrate top programs between islands (ring topology)
```

### Prompt / LLM

- The prompt includes an explicit **mode label**: either "explore different approaches" or "refine existing patterns", depending on how much progress the island has made recently.
- **Evaluator feedback** (diagnostics from the evaluator) is injected directly so the LLM knows *what's wrong*, not just the score.
- **Sibling context**: shows the LLM what mutations were already tried on this parent and whether they helped — prevents repeating failed approaches.
- **Paradigm breakthrough**: when globally stuck, a separate LLM call generates high-level algorithmic ideas (e.g., "try a completely different data structure"). These ideas are injected into subsequent prompts.

### Population & Selection

- **Multiple islands**, each with its own archive of programs.
- Archive keeps programs based on an **elite score** = weighted combo of fitness rank + novelty rank (how different a program is from others).
- When archive is full, the most similar non-protected program is replaced (deterministic crowding).
- **UCB island selection**: islands that have been improving get picked more often, but under-visited islands also get a chance (exploration bonus).

### Scoring

Standard evaluator — calls `evaluator.py`, gets a score. No LLM-as-judge.

### Pros & Cons

| Pros | Cons |
|------|------|
| Automatically adapts explore/exploit balance | Many hyperparameters to tune |
| Paradigm breakthrough can escape deep stagnation | Paradigm quality depends on LLM reasoning |
| Sibling context prevents repeating mistakes | Higher per-iteration overhead (UCB, diversity calcs) |
| Evaluator feedback closes the loop | Complex — harder to debug and understand |

---

## 2. EvoX

**Core idea**: Co-evolution — don't just evolve the *programs*, also evolve the *search strategy itself*. The LLM writes Python code for new selection/sampling strategies when the current one stops working.

### Workflow

```
Setup:
  1. Generate problem-specific variation operators (explore/exploit guidance) via one-time LLM call
  2. Load initial search strategy (a Python class that decides how to pick parents)

For each iteration:
  3. Use current search strategy to pick a parent → LLM mutates → evaluate child
  4. If stuck for too long (e.g., 10% of total iterations with no improvement):
     a. Score the current search strategy based on how much it improved solutions
     b. Ask the LLM to write a NEW search strategy (a Python database class)
     c. Validate the new strategy structurally
     d. Swap it in and migrate all programs to the new strategy
     e. If it crashes at runtime, fall back to the previous strategy
```

### Prompt / LLM

- **Variation operators** (generated once at startup): the LLM analyzes the problem and evaluator, then produces two guidance blocks — one for exploration ("try these different algorithmic approaches") and one for exploitation ("tune these specific parameters"). These are injected into mutation prompts.
- **Search strategy evolution**: when stagnating, the LLM is asked to *write a new Python class* that implements `sample()` and decides how parents are selected. The LLM sees stats about the current population and how well the current strategy performed.

### Population & Selection

- **Two populations**: (1) the actual programs being optimized, and (2) the search strategies (Python classes).
- How parents are picked depends entirely on the currently active search strategy — it could be anything the LLM wrote (best-of-k, diversity-based, tournament, etc.).
- When a new strategy is swapped in, all existing programs are migrated to it.

### Scoring

- Programs: standard evaluator.
- Search strategies: scored by `improvement * log_weight / sqrt(horizon)` — rewards strategies that make progress, especially from higher baselines where improvement is harder.

### Pros & Cons

| Pros | Cons |
|------|------|
| Search strategy adapts to the specific problem | Very complex (two nested evolution loops) |
| Variation operators are problem-specific | LLM-generated strategy code may have bugs |
| Only switches strategy when truly stuck | Hard to debug when things go wrong |
| Robust fallback if new strategy fails | Higher LLM cost (strategy generation calls) |

---

## 3. OpenEvolve

**Core idea**: MAP-Elites quality-diversity search with islands. Instead of just keeping the *best* programs, it maintains a grid of programs that are diverse along feature dimensions (e.g., code complexity vs. structural diversity). Each cell in the grid holds the best program for that combination of features.

### Workflow

```
For each iteration:
  1. Pick sampling mode: 70% exploit (from archive), 20% explore (random from island), 10% global random
  2. Pick a parent from the current island → rotate to next island (round-robin)
  3. Select context programs from nearby MAP-Elites cells
  4. LLM generates child → evaluate → compute feature coordinates
  5. Place child in the MAP-Elites grid: if cell is empty or child beats the occupant, it takes the cell
  6. Periodically migrate top programs between islands (ring topology)
```

### Prompt / LLM

- Uses the **default prompt template** — parent code + context programs + system message.
- **No mode-specific guidance**: unlike AdaEvolve, the prompt does not tell the LLM whether to explore or exploit. The diversity comes from the MAP-Elites grid structure, not from prompt engineering.
- Context programs are drawn from **nearby grid cells**, so the LLM sees structurally similar alternatives.

### Population & Selection

- **Multi-island**, each with its own MAP-Elites grid.
- The grid has configurable feature dimensions (default: code complexity and code diversity), each split into bins.
- Each cell holds exactly one program — the best one seen for that feature combination.
- A global archive keeps the top programs overall.

### Scoring

Standard evaluator. Feature dimensions (complexity, diversity) are computed from the code itself, not from evaluator metrics (though you can configure evaluator metrics as features).

### Pros & Cons

| Pros | Cons |
|------|------|
| Principled diversity maintenance (MAP-Elites) | No adaptive explore/exploit — ratios are fixed |
| Simple to configure and understand | No prompt guidance (LLM doesn't know the mode) |
| Nearby-cell context is relevant for refinement | Default features (complexity, diversity) may not capture meaningful differences |
| Benefits from framework improvements automatically | Each grid cell holds only one program — limited |

---

## 4. GEPA

**Core idea**: Quality over quantity — strict acceptance gating (children must beat their parent), reflective prompting (show the LLM its past failures), and LLM-mediated merge (combine two programs with complementary strengths).

### Workflow

```
For each iteration:
  1. Pick parent (epsilon-greedy: 90% best program, 10% random from elite pool)
  2. Build prompt with: parent code + context + rejection history (recent failed attempts)
  3. LLM generates child → evaluate
  4. Acceptance gate: child score > parent score?
     - YES: add to elite pool, schedule a merge for next iteration
     - NO: add to rejection history (shown in future prompts), do NOT add to population
  5. If stuck for 15+ iterations: attempt a merge
     - Pick two programs that excel on different metrics
     - Ask LLM to combine their strengths into one program
     - Accept merge only if it beats BOTH parents
```

### Prompt / LLM

- **Reflective prompting**: the last ~5 rejected mutations are included in the prompt — their code, scores, and error messages. The LLM is told "these were tried and rejected" and asked to learn from them.
- **Merge prompt**: a separate focused prompt showing two programs side-by-side with per-metric comparison ("Program A is better at X, Program B is better at Y — combine the best of both").

### Population & Selection

- **Single elite pool**, sorted by fitness, capped at population size.
- No islands, no diversity mechanism — purely fitness-driven.
- Best program and initial program are always protected from eviction.
- **Pareto-aware** option: can sample parents from the Pareto front across individual metrics.

### Scoring

Standard evaluator. Acceptance gate uses the primary score. Merge acceptance requires beating *both* parents.

### Pros & Cons

| Pros | Cons |
|------|------|
| Population stays lean and high-quality | Strict gating may reject useful stepping-stone programs |
| Reflective prompting learns from failures | No diversity mechanism — population can converge |
| Merge exploits complementary strengths | Small merge budget (10 attempts) |
| Simple population management | No islands — limits parallel exploration |

---

## Comparison

| Aspect | AdaEvolve | EvoX | OpenEvolve | GEPA |
|--------|-----------|------|------------|------|
| **Search philosophy** | Adaptive explore/exploit | Co-evolve the search itself | Quality-diversity grid | Strict quality filter + merge |
| **Population** | Multi-island archives | Single DB (but DB class evolves) | Multi-island MAP-Elites grids | Single elite pool |
| **Parent selection** | UCB island + adaptive mode | Defined by evolved strategy | 70/20/10 exploit/explore/random | Epsilon-greedy or Pareto |
| **Prompt guidance** | Mode label + feedback + paradigm + siblings | Problem-specific variation operators | None (default template) | Rejection history (reflective) |
| **Diversity mechanism** | Novelty scores + island migration | Depends on evolved strategy | MAP-Elites feature grid | None |
| **Stagnation escape** | Paradigm breakthrough (LLM ideas) | Evolve a new search strategy | None explicit | LLM-mediated merge |
| **Acceptance policy** | Add all, archive may evict weak | Add all | Grid cell replacement (better wins) | Strict: must beat parent |
| **Complexity** | High (many moving parts) | Very high (two nested loops) | Medium | Low-medium |
| **LLM calls/iteration** | 1 + occasional paradigm | 1 + occasional strategy gen | 1 | 1 + occasional merge |
| **Best for** | Problems needing adaptive exploration | Problems where optimal search strategy is unknown | Problems where diversity matters | Problems where incremental refinement works |

### Key Takeaways

- **AdaEvolve** is the most feature-rich — it adapts everything (explore/exploit balance, island selection, stagnation escape). Best when you want a general-purpose algorithm that handles diverse problem landscapes. Trade-off: complexity and tuning burden.

- **EvoX** is the most ambitious — it evolves the search algorithm itself. Best when you suspect the right search strategy varies by problem and you want the system to figure it out. Trade-off: very complex, hard to debug, and the LLM-generated strategies may not always work.

- **OpenEvolve** is the most principled for diversity — MAP-Elites ensures the population covers different regions of the feature space. Best when maintaining diverse solutions matters (e.g., you want multiple qualitatively different good solutions). Trade-off: no adaptive behavior, and the default features may not be meaningful for all problems.

- **GEPA** is the simplest and most quality-focused — strict gating keeps the population clean, reflective prompting leverages failure history, and merge combines complementary strengths. Best for problems where steady incremental improvement works and you want a lean, interpretable search. Trade-off: no diversity mechanism, so it can converge early.
