# Evolve BLIS Router: 1:1 Weighted Foundation with Scorer Awareness

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Evolve BLIS routing algorithms that beat all known baselines (LLQ, LOR, Glia HRA, 1:1 weighted) by starting from the empirically strongest baseline (1:1 prefix-affinity + load-balance) and teaching the LLM about all existing BLIS scorers.

**Architecture:** The EVOLVE-BLOCK is inside `WeightedScoring.Route()` in routing.go. `initial_program.go` IS routing.go with EVOLVE-BLOCK markers. The default code iterates `ws.scorers`/`ws.weights` — which scorers and weights are active is controlled by `routing/routing_policy.yaml` (currently `prefix-affinity:1.0 + load-balance:1.0` = the 1:1 router). We enrich the EVOLVE-BLOCK with comments documenting all available scorers, raw signals, and strategies, and update the system prompt so the LLM knows what to optimize.

**Tech Stack:** Go (routing logic), Python (evaluator), BLIS inference-sim, SkyDiscover evolution frameworks

---

## 0. Empirical Evidence: Why 1:1 Is the Right Starting Point

From `outputs/blis_router/baseline_comparison.json` — averaged E2E latency across qwen_7b + qwen_14b.

**Evaluator workloads only** (glia_40qps + prefix_heavy — the two workloads the evaluator scores on):

| Router | prefix_heavy (ms) | glia_40qps (ms) |
|---|---|---|
| LLQ (stale QueueDepth only) | 247.4 | 11194.5 |
| LOR (fresh InFlightRequests only) | 125.8 | 6663.1 |
| Glia HRA (KV admission-aware) | 128.0 | 7111.2 |
| **1:1 (prefix-affinity + load-balance)** | **94.3** | **6684.7** |
| 3:2:2 (prefix:3 + QD:2 + KV:2) | 95.7 | 6694.7 |

> **Note:** glia_25qps exists in baseline_comparison.json but is NOT in the evaluator and is excluded from this table. All analysis uses only the 2 evaluator workloads.

**Key insights:**
1. **1:1 dominates on prefix_heavy** — 25% better than LOR (94ms vs 126ms) thanks to cache locality
2. **1:1 matches LOR on glia_40qps** — within noise (6685 vs 6663ms)
3. **Glia HRA is worse than LOR** on all workloads — KV admission overhead without benefit at these cache sizes
4. **3:2:2 ≈ 1:1** — adding queue-depth and kv-utilization scorers doesn't help at these weights
5. **1:1 is Pareto-optimal** — best on prefix, tied on load-balance

**Headroom analysis:** On glia_40qps, 1:1 is within noise of LOR — near zero headroom. On prefix_heavy, 1:1 is already 25% better than LOR but there may be further gains from adaptive weighting. The optimization surface is narrow on current workloads. If experiments plateau at ~0%, Phase 2 adds v3 workloads (Section 10) with cache_warmup and multiturn traffic patterns that may expose more differentiation.

## 1. How the Baseline Already Works

**No code changes needed to establish the 1:1 baseline.** The architecture is:

1. `initial_program.go` = `routing.go` with `// EVOLVE-BLOCK-START` / `// EVOLVE-BLOCK-END` markers (lines 177-219)
2. The code inside the markers is the default `WeightedScoring.Route()` logic: iterates `ws.scorers` and `ws.weights` to compute composite scores, then argmax + tie-break
3. `ws.scorers` and `ws.weights` are loaded from `routing/routing_policy.yaml`:
   ```yaml
   routing:
     policy: weighted
     scorers:
     - name: prefix-affinity
       weight: 1.0
     - name: load-balance
       weight: 1.0
   ```
   **Weight normalization:** `normalizeScorerWeights()` (routing_scorers.go) normalizes to sum=1.0, so `1.0 + 1.0` → `ws.weights = [0.5, 0.5]`. The LLM should know this: the default scoring is `0.5 × prefix + 0.5 × load`, not `1.0 × prefix + 1.0 × load`.
4. The evaluator passes `--policy-config routing/routing_policy.yaml` to every simulation run
5. `get_or_compute_baseline()` (evaluator.py:315) reads `initial_program.go` to compute the baseline — since initial_program.go uses `ws.scorers`/`ws.weights`, the baseline IS the 1:1 weighted router

**The only changes needed are:**
- Add richer comments inside the EVOLVE-BLOCK (documenting scorers, signals, strategies)
- Update the system prompt in config.yaml
- Create reference scores script for multi-baseline monitoring
- Update docs/experiments/blis-router.md
- Fix evaluator.py docstring (says "3 workloads" / "12 sims")
- Update CLAUDE.md sim count if needed

## 2. EVOLVE-BLOCK: Enriched Comments

The current EVOLVE-BLOCK (initial_program.go:177-219) contains the default weighted scoring code with minimal comments. We add comprehensive documentation without changing the code logic:

```go
// EVOLVE-BLOCK-START
// Default 1:1 weighted router: prefix-affinity (1.0) + load-balance (1.0).
// After weight normalization: ws.weights = [0.5, 0.5] (sum-to-1).
// This is the strongest known baseline across all workloads.
// YOUR TASK: Discover adaptive weighting, additional signals, or novel
// combinations that beat this baseline on diverse workloads simultaneously.
//
// === COMPILATION CONTRACT ===
// Post-EVOLVE-BLOCK code requires these variables to be defined:
//   bestIdx (int)   — index into snapshots[], must satisfy 0 <= bestIdx < len(snapshots)
//   bestScore (float64) — score of the selected instance
//   scores (map[string]float64) — must have an entry for every snap.ID (used for logging)
// Failure to define any of these causes a compilation error.
//
// === BUILT-IN SCORER FUNCTIONS (same package, callable directly) ===
//
// 1. scoreLoadBalance(req *Request, snapshots []RoutingSnapshot) map[string]float64
//    Returns: 1/(1 + EffectiveLoad()) per instance. Higher = less loaded.
//    EffectiveLoad() = QueueDepth(stale) + BatchSize(stale) + InFlightRequests(fresh)
//
// 2. scoreQueueDepth(req *Request, snapshots []RoutingSnapshot) map[string]float64
//    Returns: min-max normalized effective load. (maxLoad - load) / (maxLoad - minLoad)
//    All-equal loads → all score 1.0.
//
// 3. scoreKVUtilization(req *Request, snapshots []RoutingSnapshot) map[string]float64
//    Returns: 1 - KVUtilization per instance. Higher = more KV cache headroom.
//    WARNING: KVUtilization is STALE (up to 5s old from Prometheus scrape).
//
// 4. ws.scorers[0] is prefix-affinity (proportional prefix match ratio, STATEFUL)
//    ws.scorers[1] is load-balance (1/(1+EffectiveLoad))
//    Configured via routing_policy.yaml. Observer fires AUTOMATICALLY after EVOLVE-BLOCK.
//
// === RAW SIGNALS PER INSTANCE (snap RoutingSnapshot) ===
//   FRESH (synchronous):  snap.InFlightRequests (int)
//   STALE (up to 5s):     snap.QueueDepth (int), snap.BatchSize (int),
//                          snap.KVUtilization (float64, 0-1), snap.FreeKVBlocks (int64),
//                          snap.CacheHitRate (float64, 0-1, from CachedSnapshotProvider)
//   DERIVED:              snap.EffectiveLoad() (int) = QueueDepth + BatchSize + InFlightRequests
//
// NOTE: snap.CacheHitRate is from CachedSnapshotProvider (periodic, stale up to 5s).
// The prefix-affinity SCORER (ws.scorers[0]) maintains its own fresh internal LRU —
// but the raw snap.CacheHitRate signal is stale. Use the scorer for cache-aware routing;
// use the raw signal only as a secondary indicator.
//
// === REQUEST SIGNALS ===
//   len(req.InputTokens) (int), req.SLOClass (string: "realtime"/"interactive"/"batch"),
//   req.SessionID (string, non-empty for multi-turn)
//
// === STRATEGIES TO EXPLORE ===
//   - Adaptive cache weight: boost prefix-affinity when max CacheHitRate >> avg
//   - KV-aware routing: penalize instances near KV saturation for large requests
//   - SLO differentiation: realtime→least-loaded, batch→most-cached
//   - Session stickiness: hash(SessionID) → preferred instance, override if overloaded
//   - Size-aware: large input requests need more KV headroom
//   - Burst detection: spike in InFlightRequests → back off

// Compute composite scores from all scorers
scores := make(map[string]float64, len(snapshots))
for i, scorer := range ws.scorers {
    dimScores := scorer(req, snapshots)
    for _, snap := range snapshots {
        s := dimScores[snap.ID]
        // Clamp to [0,1] per scorer contract
        if s < 0 {
            s = 0
        }
        if s > 1 {
            s = 1
        }
        scores[snap.ID] += s * ws.weights[i]
    }
}

// Argmax: select instance with highest composite score.
// Pass 1: find maximum score.
bestScore := -1.0
for _, snap := range snapshots {
    if scores[snap.ID] > bestScore {
        bestScore = scores[snap.ID]
    }
}

// Pass 2: collect all instances tied at maximum score.
var tied []int
for i, snap := range snapshots {
    if scores[snap.ID] == bestScore {
        tied = append(tied, i)
    }
}

// Random tie-breaking when rng is non-nil; positional (first) when nil.
bestIdx := tied[0]
if len(tied) > 1 && ws.rng != nil {
    bestIdx = tied[ws.rng.Intn(len(tied))]
}
// EVOLVE-BLOCK-END
```

**What changed vs current:** Only the comment block at the top. The Go code is identical to routing.go's default. This ensures the baseline metrics are unchanged.

## 3. System Prompt (config.yaml system_message)

Replace the current system_message with:

```
You are optimizing a routing algorithm for a 4-instance LLM inference cluster.
Your goal: minimize end-to-end latency across ALL workloads simultaneously.

STARTING POINT: 1:1 weighted router (prefix-affinity + load-balance with equal weights).
After normalization, weights are [0.5, 0.5]. This is already the strongest known
baseline — it beats pure load-balancing (LOR) by 25% on prefix-heavy traffic while
matching LOR on general traffic. It also beats Glia HRA (KV-admission-aware routing
from the Glia paper) on all tested workloads.

YOUR TASK: Beat the 1:1 baseline. Possible approaches:
- Adaptive weighting: increase prefix weight when cache is unevenly distributed,
  increase load weight during traffic bursts
- Additional signals: KV utilization, request size, SLO class, session ID
- Novel scorer combinations the existing fixed-weight scorers can't capture
- Request-aware logic: different routing strategies for different request types

WORKLOADS (your algorithm is evaluated on ALL of these, equally weighted):

Workload 1: glia_40qps — HIGH-THROUGHPUT GENERAL TRAFFIC
  40 QPS, bursty (gamma CV=7.3), no prefix groups, ~500-token prompts
  Key challenge: load-balancing under bursty arrivals with stale signals
  1:1 baseline: ~6685ms E2E. Load-balance component dominates here.
  Headroom is small — 1:1 matches the best load-balancer (LOR). Focus on not regressing.

Workload 2: glia_prefix_heavy — PREFIX-INTENSIVE TRAFFIC
  120 QPS, 4 prefix groups × 6144-token shared prefixes, tiny per-request input (60-120 tokens)
  Key challenge: route requests to instances that have their prefix cached
  1:1 baseline: ~94ms E2E. Prefix-affinity provides 25% win over LOR here.
  Opportunity: adaptive weighting could push further. When one instance has
  a hot prefix (high scorer output), boost its score for matching requests.

AVAILABLE SCORER FUNCTIONS (callable directly in the EVOLVE-BLOCK):
  scoreLoadBalance(req, snapshots)       → map[string]float64  [1/(1+EffectiveLoad)]
  scoreQueueDepth(req, snapshots)        → map[string]float64  [min-max normalized load]
  scoreKVUtilization(req, snapshots)     → map[string]float64  [1 - KVUtilization]
  ws.scorers[0](req, snapshots)          → map[string]float64  [prefix match ratio, STATEFUL]
  ws.scorers[1](req, snapshots)          → map[string]float64  [load-balance = 1/(1+EffectiveLoad)]
    (prefix-affinity observer fires automatically after EVOLVE-BLOCK — do NOT call manually)

AVAILABLE RAW SIGNALS:
  FRESH: snap.InFlightRequests (int)
  STALE (5s): snap.QueueDepth (int), snap.BatchSize (int), snap.KVUtilization (float64, 0-1),
              snap.FreeKVBlocks (int64), snap.CacheHitRate (float64, 0-1)
  DERIVED: snap.EffectiveLoad() (int) = QueueDepth + BatchSize + InFlightRequests
  REQUEST: len(req.InputTokens) (int), req.SLOClass (string: "realtime"/"interactive"/"batch"),
           req.SessionID (string, non-empty for multi-turn)

SIGNAL FRESHNESS MATTERS:
  InFlightRequests is updated synchronously before each routing call — always accurate.
  ALL other snapshot signals (including CacheHitRate) come from CachedSnapshotProvider's
  periodic refresh — up to 5s stale. The prefix-affinity SCORER (ws.scorers[0]) maintains
  its own fresh internal LRU, so prefer the scorer output over raw snap.CacheHitRate.
  At 40-120 QPS, 200-600 requests arrive per staleness window.
  Lean on InFlightRequests for load decisions. Use stale signals as secondary tiebreakers.

WARNINGS (common failure modes — avoid these):
  - Removing load-balance entirely causes starvation on general workloads
  - Fixed high prefix weight causes pile-on when all requests match one prefix
  - QueueDepth as primary load signal is stale and causes oscillation at high QPS
  - Guard all divisions against zero denominators (div-by-zero crashes the simulation)

RULES (compilation failures waste an iteration):
1. Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
2. Must be valid Go — guard all divisions to avoid NaN/Inf
3. bestIdx must be valid: 0 <= bestIdx < len(snapshots)
4. scores[snap.ID] must be set for every instance (used for logging)
5. bestScore must be defined before EVOLVE-BLOCK-END

EVALUATION: Score = mean percentage improvement over 1:1 baseline, averaged
equally across both workloads. Baseline = 0%. Positive = better.
```

## 4. Evaluator Configuration

### 4.1 Workloads (unchanged — 2 workloads, not 3)
- `glia_40qps` → `workload_glia_40qps.yaml` — load-balance anchor
- `prefix_heavy` → `workload_glia_prefix_heavy.yaml` — prefix optimization target

### 4.2 LLM Models in evaluator (unchanged)
- `qwen_7b` — qwen/qwen2.5-7b-instruct, H100, TP=1
- `qwen_14b` — qwen/qwen3-14b, H100, TP=1

### 4.3 Seeds (unchanged)
Default dual-seed: `BLIS_SEED="42,456"`

### 4.4 Search LLMs in config.yaml (unchanged)
- `aws/claude-sonnet-4-5` (70%) + `aws/claude-opus-4-6` (30%)

### 4.5 Scoring Formula (unchanged)
```
Per-workload: candidate_val = 0.5 * e2e + 0.5 * p95
              improvement = 1 - candidate_val / baseline_val
Score = mean(improvements across workloads) × 100  (percentage points)
```
0% = matches 1:1, positive = better, negative = worse.

### 4.6 Simulation count
- 2 seeds × 2 models × 2 workloads = **8 simulations per evaluation** (not 12)
- ~50-60s per iteration

## 5. Pre-Experiment Validation

### 5.1 Compute Reference Scores

Create `benchmarks/blis_router/scripts/compute_reference_scores.py` that:
1. Reads `outputs/blis_router/baseline_comparison.json`
2. Uses the evaluator scoring formula (per-workload `0.5*e2e + 0.5*p95`, then `1 - candidate/baseline`)
3. Computes scores for LLQ, LOR, Glia vs 1:1 baseline for the active workloads (glia_40qps + prefix_heavy)
4. Maps workload names correctly: `baseline_comparison.json` uses `glia_prefix_heavy` → evaluator uses `prefix_heavy`
5. Averages across both models (qwen_7b + qwen_14b)
6. Outputs `outputs/blis_router/reference_scores.json` + prints summary table

### 5.2 Pre-Flight Checks

```bash
# 1. Verify inference-sim is clean and on expected branch
cd benchmarks/blis_router/inference-sim
git status                    # must be clean
git diff sim/routing.go       # must be empty
cd -

# 2. Verify routing_policy.yaml is 1:1
cat benchmarks/blis_router/routing/routing_policy.yaml
# Should show prefix-affinity:1.0, load-balance:1.0

# 3. No stale artifacts
test ! -f benchmarks/blis_router/baseline_metrics.json && echo "OK" || echo "ERROR"

# 4. Build test
cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go && echo "OK" && cd -

# 5. Pilot study
python benchmarks/blis_router/scripts/pilot_study.py

# 6. Reference scores
python benchmarks/blis_router/scripts/compute_reference_scores.py
```

### 5.3 Smoke Test (2 iterations)

```bash
export BLIS_OUTPUT_DIR="/tmp/blis_smoke_1_1"
export BLIS_SEED="42"
export BLIS_MULTI_LLM="0"
mkdir -p "$BLIS_OUTPUT_DIR"
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.go \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s topk -i 2 -o "$BLIS_OUTPUT_DIR" -l INFO

# Verify baseline matches 1:1 from baseline_comparison.json
# prefix_heavy e2e ~57ms (qwen_7b, seed 42)
# glia_40qps e2e ~4314ms (qwen_7b, seed 42)
cat "$BLIS_OUTPUT_DIR/baseline_metrics.json" | python3 -m json.tool

# Noise floor check: verify prefix_heavy P95 values are stable across seeds.
# If P95 varies >20% between seed 42 and seed 456, the evaluator's 0.5*P95 weight
# may cause noise-dominated scoring on that workload. Note this in experiment log.
```

## 6. Monitoring: Multi-Baseline Comparison

### 6.1 Monitoring Commands (every 2 minutes)

```bash
EXPERIMENT="<EXPERIMENT_NAME>"
FRAMEWORK="<FRAMEWORK>"
LOG=$(ls outputs/blis_router/${EXPERIMENT}/${FRAMEWORK}/logs/*.log 2>/dev/null | head -1)

echo "=== PROGRESS ==="
grep -c "Iteration" "$LOG" 2>/dev/null || echo "0"
echo "=== BEST SCORE (% improvement vs 1:1 baseline) ==="
grep "best program score" "$LOG" | tail -3
echo "=== ERRORS ==="
grep -c "BuildError\|error\|Error\|BUILD FAILED" "$LOG" 2>/dev/null || echo "0"
echo "=== LAST LINES ==="
tail -5 "$LOG"
```

### 6.2 Multi-Baseline Reporting Format

Every monitoring update MUST include:

```
Update at ~X min (iter N/TOTAL):

VALIDITY:
- Successful: XX, Build errors: XX, Auth errors: XX

PROGRESS vs 1:1 BASELINE (evaluator score):
- Current best: +X.X% vs 1:1 baseline (found at iter N)

CONTEXT vs OTHER BASELINES (from reference_scores.json):
- LLQ would score:  ~XX% vs 1:1 (stale QueueDepth, pile-on prone)
- LOR would score:  ~XX% vs 1:1  (good load-balance, no cache awareness)
- Glia would score: ~XX% vs 1:1  (KV-aware but worse than LOR here)

TIMING:
- Elapsed: X min, Avg: X.Xs/iter, ETA: ~X min remaining

TREND:
- Last new best at iter N (X iters ago)
- [Still improving / Plateaued / Regressing]
```

**Correctness rules:**
1. Score from log is ALWAYS vs 1:1 baseline — never describe it as vs LLQ or LOR
2. Reference scores are pre-computed constants from `reference_scores.json` — label with "~"
3. Never compute cross-baseline percentages by hand
4. If score is negative, the evolved algorithm is WORSE than 1:1

## 7. Updates to docs/experiments/blis-router.md

### 7.1 Fixes for existing inaccuracies

| Line | Current (wrong) | Should be |
|---|---|---|
| 73, 95 | `initial_program.py` | `initial_program.go` |
| 218 | stale `multiturn` reference | Remove or update |
| 314 | Wrong scoring formula (raw fallback) | Baseline-normalized formula: `mean(1 - candidate/baseline) × 100` |
| 318 | `cache_warmup, load_spikes, multiturn` | `glia_40qps, prefix_heavy` |
| 320-321 | Baseline score ~-4487 / ~-7121 | Update with 1:1 baseline scores |
| 336-337 | `llama_8b`, `mixtral_8x7b` | `qwen_7b` (qwen/qwen2.5-7b-instruct), `qwen_14b` (qwen/qwen3-14b) |
| 340 | "3 workloads" / "12 sims" | "2 workloads" / "8 sims" (2 seeds × 2 models × 2 workloads) |
| 388-393 | Workload table references old workloads | Update to glia_40qps + prefix_heavy |

### 7.2 New/updated sections

**A. "Initial Program" section** explaining that initial_program.go = routing.go with markers, behavior controlled by routing_policy.yaml (currently 1:1).

**B. Updated "Scoring" section** with 1:1 as baseline, reference scores for LLQ/LOR/Glia, and correct formula.

**C. Updated "Workloads" section** with glia_40qps + prefix_heavy descriptions and 1:1 baseline E2E.

**D. Updated monitoring format** in "Guardrails for Claude Sessions" with the multi-baseline format from Section 6.2.

**E. Updated "Multi-LLM Evaluation" section** with correct model names (qwen_7b, qwen_14b) and correct sim count (8, not 12).

### 7.3 Fix evaluator.py docstring

Line 8: `"4. Running simulations on N seeds × N LLMs × 3 workloads (default: 2 seeds × 2 LLMs × 3 = 12 sims)"`
Should be: `"4. Running simulations on N seeds × N LLMs × 2 workloads (default: 2 seeds × 2 LLMs × 2 = 8 sims)"`

### 7.4 Update CLAUDE.md experiment timing

The CLAUDE.md "Common pitfalls" section says:
`"Experiments take ~60s per iteration by default (LLM call + Go build + 12 simulations: 2 seeds × 2 LLMs × 3 workloads)"`
Should reference 8 simulations: 2 seeds × 2 LLMs × 2 workloads.

## 8. Implementation Tasks

> **Task dependency:** Tasks 1-4 are prep work (can be parallelized). Task 5 (validation) depends on Tasks 1-3 completing. Task 6 (experiment) MUST NOT start until Task 5 passes.

### Task 1: Add enriched comments to initial_program.go

**Files:**
- Modify: `benchmarks/blis_router/initial_program.go` (lines 177-219, inside EVOLVE-BLOCK)

Add the scorer/signal documentation comments from Section 2 above the existing code. **Do NOT change any Go code** — only add comments between EVOLVE-BLOCK-START and the first line of code.

Key changes from v3:
- CacheHitRate moved from FRESH to STALE with explanatory note
- Added "COMPILATION CONTRACT" section documenting bestIdx/bestScore/scores requirements
- Added weight normalization note (1.0+1.0 → [0.5, 0.5])

**Verify:** `cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go` still succeeds.

### Task 2: Create compute_reference_scores.py

**Files:**
- Create: `benchmarks/blis_router/scripts/compute_reference_scores.py`

The script must:
- Read `outputs/blis_router/baseline_comparison.json`
- Map workload names: `glia_prefix_heavy` (in JSON) → `prefix_heavy` (in evaluator)
- Use the EXACT evaluator scoring formula from evaluator.py:497-520 (per-workload `0.5*e2e + 0.5*p95`, improvement `= 1 - candidate_val/baseline_val`, then average × 100)
- Only use workloads active in the evaluator: `glia_40qps` and `prefix_heavy`
- Average across both models (qwen_7b + qwen_14b)
- Score LLQ, LOR, Glia against 1:1 baseline
- Write `outputs/blis_router/reference_scores.json`
- Print human-readable summary

**Run and verify:** `python benchmarks/blis_router/scripts/compute_reference_scores.py`

### Task 3: Update config.yaml system_message

**Files:**
- Modify: `benchmarks/blis_router/config.yaml` (system_message field)

Replace with the prompt from Section 3. Key changes from v3:
- Added RULES section (compilation contract: bestIdx, bestScore, scores)
- Fixed CacheHitRate freshness (STALE, not FRESH)
- Added weight normalization note
- Softened anti-patterns to "WARNINGS" with explanatory context
- Added headroom note for glia_40qps

**Verify:** `python3 -c "import yaml; yaml.safe_load(open('benchmarks/blis_router/config.yaml'))"`

### Task 4: Update docs and fix docstrings

**Files:**
- Modify: `docs/experiments/blis-router.md` — apply all fixes from Section 7.1 + 7.2
- Modify: `benchmarks/blis_router/evaluator.py` — fix line 8 docstring (Section 7.3)
- Modify: `CLAUDE.md` — fix sim count in experiment timing (Section 7.4)

**Verify:** All file paths referenced in docs exist; all commands are syntactically correct.

### Task 5: Pre-experiment validation

**Depends on:** Tasks 1, 2, 3 must be complete.

Run the full pre-flight checklist from Section 5.2 + smoke test from Section 5.3. Verify:
- Baseline metrics match `baseline_comparison.json` 1:1 values
- Reference scores are generated and look reasonable
- Build succeeds with enriched comments
- P95 noise floor check (prefix_heavy stability across seeds)

### Task 6: Run experiment

**Depends on:** Task 5 must pass.

```bash
EXPERIMENT="$(date +%y%m%d)_50i_1_1_foundation"
export BLIS_OUTPUT_DIR="outputs/blis_router/${EXPERIMENT}/openevolve"
export BLIS_SEED="42,456"
mkdir -p "$BLIS_OUTPUT_DIR"
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.go \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  -s openevolve -i 50 \
  -o "$BLIS_OUTPUT_DIR" -l INFO
```

Monitor every 2 minutes per Section 6. After completion, run full post-experiment analysis per CLAUDE.md/blis-router.md checklist.

### Task 7: Post-experiment analysis

Follow existing post-experiment checklist (CLAUDE.md steps 9-14). The `analysis.md` must include a **multi-baseline comparison table** with all numbers from script output:

| Router | Score vs 1:1 | Avg E2E (ms) | Avg P95 (ms) |
|---|---|---|---|
| LLQ | ~XX% | ~XX | ~XX |
| LOR | ~XX% | ~XX | ~XX |
| Glia HRA | ~XX% | ~XX | ~XX |
| **1:1 baseline** | **0%** | **~XX** | **~XX** |
| Best evolved | +X% | Y | Z |

> Fill all "~XX" values from `reference_scores.json` output. Never hardcode numbers in the plan.

**Note:** `run_lor_baseline.py` and `compare_lor.py` are NOT needed. All baseline data (LLQ, LOR, Glia, 1:1, 3:2:2) is already in `outputs/blis_router/baseline_comparison.json`, generated by `benchmarks/blis_router/scripts/compare_baselines.py`. The `compute_reference_scores.py` script (Task 2) reads this JSON to produce evaluator-normalized reference scores. The CLAUDE.md post-experiment step 9 (LOR baseline) can be skipped — the reference scores provide the same 3-way comparison context.

## 9. Risk Mitigation

| Risk | Impact | Mitigation |
|---|---|---|
| LLM removes scorers, regresses to raw QueueDepth | Score drops heavily | Prompt warns against removing load-balance; evaluator penalizes regression |
| LLM can't improve beyond 1:1 on current workloads | 0% improvement | Expected given narrow headroom. Move to Phase 2 (v3 workloads) per Section 10 |
| ws.scorers index assumption (0=prefix, 1=load) breaks | Wrong scorer called | Verify in smoke test; document in comments; LLM can also call standalone functions |
| Reference scores don't match runtime baseline | Misleading monitoring | Label as approximate (~); final analysis uses exact script output |
| Scorer function signatures change in inference-sim | Build errors | Pin to tested inference-sim commit; verify build in pre-flight |
| LLM produces code missing bestIdx/bestScore/scores | Build error | RULES section in prompt + COMPILATION CONTRACT in comments explicitly document requirements |
| Prefix_heavy P95 is noisy (high variance across seeds) | Score noise | Noise floor check in Task 5; if >20% variance, note in experiment log and consider 3-seed |

## 10. Phase 2: Expanding Optimization Surface (if Phase 1 plateaus)

If experiments on current workloads plateau at ~0% improvement over 1:1 (which is likely given the narrow headroom), expand with v3 workloads:

**Candidate workloads** (already defined as YAML files):
- `workload_v3_cache_warmup.yaml` — cold-start traffic with cache warming dynamics
- `workload_v3_multiturn.yaml` — multi-turn sessions with session stickiness opportunities
- `workload_v3_load_spikes.yaml` — sudden traffic spikes testing robustness

**Adding workloads to the evaluator:**
1. Add to `evaluator.py:WORKLOADS` list
2. Update sim count in docs (8 → 12 per eval)
3. Recompute reference scores
4. Re-run smoke test to get new baseline metrics

These workloads expose more differentiation between routers (session stickiness, burst handling, cache warming) and give the LLM more optimization surface.

## 11. Open Questions

1. **Phase 2 timing:** Should we run Phase 1 (50 iterations) first and assess, or jump directly to expanded workloads? Recommendation: run Phase 1 first — even a 0% result is informative (confirms 1:1 is near-optimal for current traffic patterns).

2. **Should the LLM be told about `routing_policy.yaml`?** Currently the prompt documents `ws.scorers[0]` = prefix-affinity and `ws.scorers[1]` = load-balance. This is sufficient — the LLM doesn't need to know about the YAML, just the scorer array.

3. **Should we also try Glia as starting point for comparison?** Running one experiment from Glia HRA initial program would show whether evolution can improve on a different starting algorithm. Lower priority — try 1:1 first.

---

## Appendix: Changes from v3 (reviewer fixes applied)

| Issue | Category | Fix |
|---|---|---|
| C1: Missing compilation rules in prompt | CRITICAL | Added RULES section to system prompt (Section 3) + COMPILATION CONTRACT to EVOLVE-BLOCK comments (Section 2) |
| C2: Near-zero headroom on current workloads | CRITICAL | Added headroom analysis to Section 0, explicit "Headroom is small" note in prompt, promoted Phase 2 to full section (Section 10) |
| C3: run_lor_baseline.py workload defaults | CRITICAL | Added note about auto-detection + fallback behavior in Task 7 |
| C4: Missing CLAUDE.md/evaluator.py doc updates | CRITICAL | Added Task 4 scope: evaluator.py docstring (Section 7.3) + CLAUDE.md timing (Section 7.4) |
| C5: Missing task dependency (Task 3 → Task 6) | CRITICAL | Added explicit dependency chain at top of Section 8 |
| I1: snap.CacheHitRate classified as FRESH | IMPORTANT | Moved to STALE in both EVOLVE-BLOCK comments and system prompt, added explanatory note |
| I2: Weight normalization undocumented | IMPORTANT | Added normalization note to Section 1 and EVOLVE-BLOCK comments |
| I3: Section 0 table includes non-evaluator workload | IMPORTANT | Removed glia_25qps column, table now shows only evaluator workloads |
| I4: Anti-patterns too prescriptive | IMPORTANT | Renamed to "WARNINGS" with explanatory context |
| I5: No noise floor check for P95 | IMPORTANT | Added to smoke test (Section 5.3) and risk table |
| I7-I12: blis-router.md inaccuracies | IMPORTANT | Expanded Section 7.1 with all line-level fixes including scoring formula, sim count |
