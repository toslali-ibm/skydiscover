# Plan: Push BLIS Router Improvement Beyond 15%

**Goal**: Design workloads and evaluator changes that expose fundamental weaknesses in the baseline routing algorithm, enabling ADRS frameworks to achieve 50%+ improvement.

**Status**: Draft
**Date**: 2026-03-05

## 1. Root Cause Analysis: Why the Ceiling is ~10%

The 100-iteration experiment (20260304_182612) showed all four frameworks converging to 9.5–9.8% improvement. This ceiling exists because **the current workloads don't punish the baseline hard enough**. Three specific reasons:

### 1.1 The baseline is already decent on these workloads

The equal-weight baseline (1.0 prefix-affinity, 1.0 load-balance) happens to be a reasonable compromise:

| Workload | Optimal Strategy | Baseline Strategy | Gap |
|----------|-----------------|-------------------|-----|
| cache_warmup | Adaptive (load-aware + prefix tiebreak) | Equal-weight blend | Small — load component prevents prefix imbalance |
| load_spikes | Pure load-balance | Equal-weight blend | Small — load component saves it from prefix trap (+2% vs optimal) |
| multiturn | Pure prefix-affinity | Equal-weight blend | Tiny — prefix-affinity already dominates at 1:1 |

The baseline isn't naive round-robin — it's already a weighted combination of the two most important signals. Evolved solutions can only *refine* the weights, not discover a fundamentally different approach.

### 1.2 The simulation is too forgiving

Three simulator settings make routing decisions easy:

1. **Perfect signal freshness** (`snapshot-refresh-interval=0`): QueueDepth, BatchSize, KVUtilization are all synchronously fresh. In production, these come from Prometheus with 5–15s staleness. Fresh signals mean load-balance works perfectly — there's no need for clever heuristics.

2. **Effectively unconstrained KV cache**: The simulator loads model-trained defaults from `defaults.yaml` — for `meta-llama/llama-3.1-8b-instruct` on H100 TP=2, that's 132,139 blocks (~2.1M tokens per instance). With the current workloads (~1250 requests/instance at ~600 tokens each = 750K tokens needed), KV utilization stays around 36%. No instance ever approaches memory pressure, so the KVUtilization signal is never decision-critical and eviction never occurs.

3. **No routing latency** (`routing-latency=0`): Decisions are applied instantly. In production, network latency (0.5–2ms) means the state seen during routing differs from the state at injection time.

### 1.3 Unbalanced score contribution across workloads

The scoring formula averages raw milliseconds across workloads:

| Workload | Avg E2E (baseline) | % of Combined Score |
|----------|-------------------|-------------------|
| cache_warmup | 4422ms | ~56% |
| load_spikes | 3284ms | ~42% |
| multiturn | 161ms | **~2%** |

Multiturn — the workload with the most room for routing improvement via session affinity — contributes almost nothing to the final score. A 50% improvement in multiturn latency (save 80ms) moves the combined score by only 1%. The LLM has no incentive to optimize for it.

## 2. Strategy: Three Levers to Break the Ceiling

### Lever A: Design adversarial workloads where the baseline *catastrophically* fails

The current workloads were designed to *validate* routing — confirming that prefix-affinity and load-balance both matter. We need workloads designed to *break* the baseline: scenarios where equal-weight scoring makes disastrously bad decisions.

### Lever B: Make the simulation realistic (add staleness, memory pressure, latency)

Production routers operate with stale signals, constrained KV cache, and non-zero network latency. Adding these to the simulation widens the gap between "good enough" and "optimal" routing.

### Lever C: Fix the scoring formula so all workloads matter equally

Normalize per-workload scores before averaging so that multiturn improvements (in ms) count as much as cache_warmup improvements (in seconds).

## 3. Proposed Workloads (V3)

### 3.1 Workload: `kv_pressure` — KV Cache Cliff

**Thesis**: When KV cache is constrained, routing to a nearly-full instance triggers eviction cascades that destroy latency. The baseline ignores KV utilization (it's not in the default scorer config: only prefix-affinity + load-balance). An evolved router must use `FreeKVBlocks` and `KVUtilization` to route *around* memory-saturated instances.

**Design**:
- **4 instances, constrained KV cache** (`--total-kv-blocks 15000` per instance, ~240K tokens). The model default is 132,139 blocks — we reduce to ~11% of that to force memory pressure with the larger request sizes in this workload.
- **Rate**: 500 req/s, 3000 requests, 6 seconds
- **Traffic mix**:
  - 35% "long-context RAG" — input 2000±500 tokens, output 200±80 tokens, prefix 1024. These are the cache killers: each request needs ~140 blocks (~2200 tokens / 16 tokens/block), and with 500 req/s × 35% × 1/4 instances ≈ 44 concurrent on one instance → ~6000 blocks. That's 40% of an instance's 15K-block budget from this one client type alone.
  - 30% "short Q&A" — input 100±30 tokens, output 30±10 tokens, realtime SLO. Tiny footprint (~8 blocks), latency-sensitive — should go to instances with free KV
  - 20% "summarization" — input 4000±1000 tokens, output 100±30 tokens, batch SLO. Massive input (~250 blocks each). Just 15 concurrent summarization requests consume 3750 blocks (25% of instance KV). One of these on a nearly-full instance triggers eviction.
  - 15% "code completion" — input 500±100 tokens, output 50±20 tokens, interactive, prefix 512

**Why baseline fails**: Equal-weight (prefix-affinity + load-balance) doesn't consider KV utilization at all. When 2 instances hit 85%+ KV utilization, the baseline keeps sending large requests there because they have matching prefixes or slightly lower queue depth. Eviction cascades cause 3–5x latency spikes.

**Expected improvement opportunity**: 40–60%. The evolved router can:
- Hard-avoid instances above 80% KV utilization for large requests
- Use `FreeKVBlocks` as a routing signal (proportional to request size)
- Route small requests to saturated instances (they fit), large requests to free instances
- Set Priority for realtime requests on congested instances

### 3.2 Workload: `size_segregation` — Head-of-Line Blocking

**Thesis**: When tiny latency-critical requests share instances with huge batch requests, head-of-line blocking destroys tail latency. The baseline treats all requests equally regardless of size.

**Design**:
- **4 instances, moderate KV cache** (`--total-kv-blocks 30000`, ~23% of model default 132K — enough that good routing avoids pressure, but bad routing hits eviction with the 6000-token document requests)
- **Rate**: 800 req/s, 4000 requests, 5 seconds
- **Traffic mix**:
  - 40% "API micro-requests" — input 30±10 tokens, output 10±5 tokens, realtime SLO, streaming. Sub-50ms target
  - 25% "document processing" — input 6000±2000 tokens, output 500±200 tokens, batch SLO. These take 2–5 seconds and block everything behind them
  - 20% "conversational" — input 300±80 tokens, output 60±20 tokens, interactive SLO, prefix 256
  - 15% "long generation" — input 200±50 tokens, output 2000±500 tokens, batch SLO. Long decode time (13+ seconds at ~150 tok/s), occupies a batch slot for a long time

**Why baseline fails**: Equal-weight scoring doesn't differentiate by request size. A 6000-token document request routed to an instance with 40 micro-requests in the queue will: (a) consume massive KV, (b) slow batch iteration speed (prefill dominates), and (c) cause head-of-line blocking for queued micro-requests. The baseline has no mechanism to segregate traffic by size or SLO.

**Expected improvement opportunity**: 50–70%. The evolved router can:
- Route large requests to dedicated "batch" instances (detected via low InFlightRequests + high FreeKVBlocks)
- Keep instances with many small realtime requests free from batch pollution
- Use `len(req.InputTokens)` and `req.SLOClass` to implement size-aware routing
- Set high `Priority` on realtime requests to jump the queue

### 3.3 Workload: `stale_signals` — Decision-Making Under Uncertainty

**Thesis**: With stale monitoring signals (realistic production scenario), load-balance scoring becomes unreliable. The baseline weights stale signals equally with fresh ones (InFlightRequests is always synchronous). An evolved router should rely more on InFlightRequests during staleness windows and less on QueueDepth/BatchSize.

**Design**:
- **8 instances** (larger cluster = more complex decision space)
- **Snapshot refresh interval**: 100ms (`--snapshot-refresh-interval 100000` microseconds)
- **Rate**: 2000 req/s, 10000 requests, 5 seconds
- **Traffic mix**:
  - 30% "chat" — input 200±60 tokens, output 40±15 tokens, interactive SLO, prefix 512
  - 30% "API" — input 80±20 tokens, output 20±8 tokens, realtime SLO, no prefix
  - 25% "batch analysis" — input 1000±300 tokens, output 150±50 tokens, batch SLO, prefix 256
  - 15% "code gen" — input 400±100 tokens, output 300±100 tokens, interactive SLO, prefix 1024

**Why baseline fails**: With 100ms staleness on QueueDepth/BatchSize/KVUtilization, the load-balance scorer operates on stale data. At 2000 req/s, 200 requests arrive per staleness interval. Load-balance makes decisions based on state that's 200 requests out of date. The baseline treats this stale signal with equal weight. An evolved router should:
- Lean heavily on InFlightRequests (always fresh) during high-rate periods
- Discount load-balance score when staleness is high (detectable via rapid score changes between evaluations)
- Use prefix-affinity as a tiebreaker rather than equal contributor

**Expected improvement opportunity**: 30–50%.

### 3.4 Workload: `session_thrash` — Session Affinity Under Contention

**Thesis**: The current multiturn workload runs at 150 req/s — so low that there's barely any contention. If sessions compete for the same prefix-affinity slots at high rates, the baseline can't maintain affinity AND balance load simultaneously.

**Design**:
- **4 instances**
- **Rate**: 600 req/s, 6000 requests, 10 seconds
- **Traffic mix**:
  - 50% "high-value coding sessions" — input 80±20, output 15, prefix 4096, 8 rounds, think_time 20ms, accumulate context. 8 rounds means by round 8 context is ~4800 tokens. Cache miss cost: 85ms (47% of ~180ms total)
  - 25% "competing coding sessions" — SAME prefix group as above (coding-system-prompt, prefix 4096), 6 rounds. These compete for the same prefix-affinity slot, forcing the router to spread sessions across instances even though prefix-affinity says "send all to one instance"
  - 25% "burst batch" — input 2000±600, output 300±100, batch SLO, gamma CV=4.0 (extreme bursts). These burst requests periodically overwhelm instances, disrupting session affinity

**Why baseline fails**: With 75% of traffic sharing the SAME prefix group at 600 req/s, prefix-affinity concentrates 450 req/s on one instance (should be ~150 req/s per instance). The baseline's equal-weight load-balance partially corrects this, but the bursty batch traffic periodically creates chaos — when a burst lands, the router must decide whether to break session affinity to avoid the overloaded instance or maintain affinity and accept queuing. Equal weights can't make this tradeoff dynamically.

**Expected improvement opportunity**: 40–60%. The evolved router can:
- Use SessionID to track individual sessions, not just prefix groups
- Implement session-sticky routing within a load budget (route session to its instance unless load exceeds threshold)
- During bursts, temporarily override affinity for new sessions while preserving existing sessions
- Use InFlightRequests as burst detector (sudden spike = back off from that instance)

## 4. Evaluator & Scoring Changes

### 4.1 Evaluator: Expose simulation knobs

The current evaluator hardcodes `--num-instances 4` and uses model-trained KV defaults (~132K blocks for llama-3.1-8b on H100 TP=2, which is unconstrained for current workload sizes). To support the new workloads, we need to allow per-workload simulation parameters.

**Proposal**: Extend the workload YAML format to include simulation overrides:

```yaml
# In each workload YAML
simulation:
  num_instances: 8                    # Override default 4
  total_kv_blocks: 8000              # Constrain KV cache
  snapshot_refresh_interval: 100000  # 100ms staleness
  routing_latency: 1000             # 1ms routing latency
  max_num_running_reqs: 64          # Smaller batches
```

The evaluator reads these from the workload YAML and passes them as CLI flags to the simulation worker. This keeps each workload self-contained and doesn't require multiple config files.

**Implementation**: Modify `_build_sim_cmd()` in `evaluator.py` to accept a `sim_overrides: dict` parameter parsed from the workload YAML.

### 4.2 Scoring: Normalize per-workload before averaging

**Current**: `score = -0.5 * avg(e2e_mean across workloads) - 0.5 * avg(p95 across workloads)`

**Problem**: Workloads with high absolute latency dominate. Multiturn (161ms) contributes 2% vs cache_warmup (4422ms) at 56%.

**Proposed**: Normalize each workload's metrics relative to its baseline, then average:

```python
# Per workload: compute % improvement vs baseline
for workload in workloads:
    pct_improvement_e2e = (baseline_e2e - evolved_e2e) / baseline_e2e
    pct_improvement_p95 = (baseline_p95 - evolved_p95) / baseline_p95

# Average % improvements across workloads (each workload contributes equally)
avg_pct_e2e = mean(pct_improvements_e2e)
avg_pct_p95 = mean(pct_improvements_p95)

# Combined normalized score (higher = better, 0 = baseline)
normalized_score = 0.5 * avg_pct_e2e + 0.5 * avg_pct_p95
```

This ensures each workload has equal influence on the final score. A 20% improvement on multiturn counts the same as a 20% improvement on cache_warmup.

**Backward compatibility**: Keep the raw-millisecond score for comparison with the existing 100-iteration results. Add the normalized score as a new metric. Use the normalized score as the primary optimization target.

### 4.3 System prompt update

Update `config.yaml`'s system message to inform the LLM about the new workload characteristics:

```
NEW SIGNALS TO EXPLOIT:
- KV pressure: snap.KVUtilization > 0.8 means eviction risk. snap.FreeKVBlocks < 1000 is critical.
- Request size: len(req.InputTokens) varies 30-6000. Large requests need instances with free KV.
- SLO class: req.SLOClass "realtime" needs sub-50ms. Route away from congested instances.
- Session affinity: req.SessionID persists across turns. Cache miss cost = 40-85ms per session bounce.
- Signal staleness: QueueDepth/BatchSize may be 100ms stale. InFlightRequests is always fresh.
  Rely on InFlightRequests when other signals seem inconsistent.
```

## 5. Implementation Plan

### Phase 1: Evaluator changes (no new workloads yet)

1. **Add per-workload sim overrides to evaluator.py** — parse `simulation:` block from workload YAML, pass as CLI flags to `_build_sim_cmd()`
2. **Add normalized scoring** — compute per-workload % improvement vs baseline alongside raw score. Use normalized score as optimization target.
3. **Validate**: Run baseline on existing V2 workloads with the new evaluator. Scores must match existing results (no regression).

### Phase 2: New workloads (V3)

4. **Create workload YAMLs**: `workload_v3_kv_pressure.yaml`, `workload_v3_size_segregation.yaml`, `workload_v3_stale_signals.yaml`, `workload_v3_session_thrash.yaml`
5. **Validate each workload independently**:
   - Run baseline → record metrics
   - Run sabotaged (round-robin) → confirm routing matters (expect 100%+ worse)
   - Run oracle/manual variants → confirm large improvement is achievable
   - **Acceptance criterion**: Sabotaged/baseline gap > 100% on at least 3 of 4 workloads
6. **Update WORKLOADS list in evaluator.py** — replace V2 with V3 (or make configurable)

### Phase 3: Prompt & config tuning

7. **Update config.yaml system prompt** — add V3 workload descriptions and new signal hints
8. **Increase EVOLVE-BLOCK scope** — consider expanding the mutable region to allow the LLM to add new scorer functions or modify admission logic (currently limited to the scoring loop + argmax)
9. **Consider adding Priority to the search space** — the RoutingDecision.Priority field is available but the LLM doesn't know about it. Adding a hint could enable SLO-aware priority boosting.

### Phase 4: Full experiment

10. **Pilot study** — 20-iteration run of adaevolve on V3 workloads. Target: >20% improvement.
11. **Full experiment** — 100-iteration run of all 4 frameworks. Target: 50%+ improvement for at least one framework.
12. **Comparison** — Run analysis scripts, write analysis.md, compare V2 vs V3 results.

## 6. Expected Impact

| Workload | Baseline Weakness | Expected Improvement |
|----------|-------------------|---------------------|
| kv_pressure | No KV utilization awareness | 40–60% |
| size_segregation | No size/SLO-aware routing | 50–70% |
| stale_signals | Equal weight on stale signals | 30–50% |
| session_thrash | Can't balance affinity vs load dynamically | 40–60% |
| **Average across V3** | | **40–60%** |

With normalized scoring, each workload contributes equally, and the combined improvement should reach **40–60%** — well above the 10% ceiling of V2.

## 7. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| New workloads too hard — even evolved solutions can't improve | No improvement | Validate oracle/manual variants first (Phase 2 step 5). If oracle can't beat baseline by 30%+, the workload is flawed, not the router. |
| Simulation doesn't support `--total-kv-blocks` or `--snapshot-refresh-interval` correctly | Workloads don't stress what we think | Test each flag independently on a simple workload before designing V3. |
| LLM can't discover KV-aware or size-aware routing in the EVOLVE-BLOCK | Small improvement despite large opportunity | Expand EVOLVE-BLOCK to include more of the routing logic. Add stronger hints in the system prompt. Consider seeding the initial population with hand-crafted solutions that demonstrate KV-awareness. |
| Normalized scoring introduces instability | Score oscillates | Use baseline caching (already implemented) and add smoothing if needed. |
| Expanded EVOLVE-BLOCK causes more build errors | Lower success rate | Monitor build error rate in Phase 4 pilot. If >50%, constrain the block. |

## 8. Success Criteria

- **Minimum**: At least one framework achieves 30%+ normalized improvement on V3 workloads
- **Target**: At least one framework achieves 50%+ normalized improvement
- **Stretch**: Multiple frameworks achieve 50%+, demonstrating that the improvement comes from workload design, not framework luck
- **Validation**: Hand-crafted oracle solution achieves 60%+ on V3 workloads (confirms the improvement ceiling is real and reachable)
