# BLIS vs Vidur: Comparative Algorithm Discovery Experiment

## Goal

Compare SkyDiscover's algorithm discovery on BLIS (Go DES) vs Vidur (Python DES) to show that **BLIS's richer simulator fidelity — prefix caching, vLLM-style scheduling, realistic signal surface — enables discovery of better routing algorithms**.

Both simulators start from LOR (Least Outstanding Requests). We measure **% improvement over each simulator's own LOR baseline**. This is the only fair comparison since the simulators use different LLMs, different latency models, and produce different absolute numbers.

## Key Claim

BLIS discovers algorithms with greater relative improvement over LOR than Vidur, because:
1. **Prefix caching modeled**: BLIS rewards routing that exploits prefix locality; Vidur has no prefix model
2. **Richer signal surface**: BLIS exposes KVUtilization, CacheHitRate, FreeKVBlocks, InFlightRequests, SLOClass, SessionID; Vidur exposes only `num_pending_requests` and `memory_usage_percent`
3. **Per-request routing**: BLIS routes one request at a time with fresh signals (matches llm-d production); Vidur routes batches with stale state
4. **vLLM-style scheduling**: BLIS models continuous batching and iteration-level preemption; Vidur uses Sarathi scheduling

## Modeling Decisions

What each simulator models, simplifies, or omits — and what that means for the comparison.

| Component | BLIS | Vidur | Impact on Comparison |
|-----------|------|-------|---------------------|
| Prefix caching | Modeled (block-level LRU) | Omitted | HIGH: BLIS can discover prefix-exploiting strategies; Vidur cannot. Core claim. |
| KV cache pressure | Modeled (KVUtilization, FreeKVBlocks) | Simplified (memory_usage_percent, 0-100) | MEDIUM: BLIS has finer-grained memory signals |
| Per-request routing | Modeled (Route() per request) | Simplified (batch schedule()) | MEDIUM: Affects what routing logic is expressible |
| Token-weighted load | Omitted (EffectiveLoad counts requests) | Accessible (num_prefill_tokens, pd_ratio) | Vidur advantage — can weight by request size |
| Continuous batching | Modeled (vLLM-style) | Simplified (Sarathi chunked-prefill) | LOW: Affects latency model, not routing interface |
| LLM models | Qwen-7B/14B on H100 | Llama-2-7B/3-8B on A100 | Mitigated: relative metrics normalize this away |
| Request arrival | Same distributions, different formats | Same distributions, CSV traces | Mitigated: matched workload design in Phase 1 |

**Key simplification**: We accept different LLMs and hardware because we compare **relative improvement over each simulator's own LOR baseline**, not absolute latency numbers. The assumption is: if BLIS's richer signals enable better routing, the relative improvement should be larger regardless of the underlying LLM.

## Simulator Comparison

| Aspect | BLIS | Vidur |
|--------|------|-------|
| Language | Go | Python |
| Routing interface | Per-request: `Route(req, state)` | Per-batch: `schedule(queue, replicas, n)` |
| LLMs | Qwen-7B, Qwen-14B (H100, TP=1) | Llama-2-7B, Llama-3-8B (A100, TP=1) |
| Prefix caching | Yes (block-level LRU, hit rate tracking) | No |
| KV cache signal | KVUtilization, FreeKVBlocks, CacheHitRate | memory_usage_percent only |
| Request signals | InputTokens, SLOClass, SessionID | num_prefill_tokens, num_decode_tokens, pd_ratio |
| Scheduling model | vLLM continuous batching | Sarathi chunked-prefill |
| Cluster size | 4 instances | 4 replicas |
| Scoring | `-0.5 * avg_e2e - 0.5 * avg_p95` | `-0.5 * avg_e2e - 0.5 * avg_p95` |
| Starting point | LOR via `initial_program_lor.go` | LOR via `initial_program.py` |

**Baseline clarification**: BLIS starts from `initial_program_lor.go` (pure LOR — picks lowest EffectiveLoad, ignores prefix/cache signals), NOT `initial_program.go` (which uses prefix-affinity:1.0 + load-balance:1.0 weighted scoring). This ensures both simulators start from equivalent pure load-balancing baselines. The LOR baseline is also each simulator's control — improvement is measured against it.

## Problem: Finding the Right Load Regime

### What We Know

| Rate | BLIS routing signal | Vidur routing signal | Notes |
|------|-------------------|---------------------|-------|
| ~17 QPS (current v3) | <1% improvement | 22% improvement | BLIS too idle; Vidur has DES queueing |
| ~50 QPS | Unknown (needs testing) | 2.8s E2E, 100% completion (cache_warmup) | Vidur sweet spot for cache_warmup |
| ~37 QPS | Unknown (needs testing) | 2.2s E2E, 59% completion (load_spikes) | Vidur borderline for load_spikes |
| 200-300 QPS (v2) | 63% improvement | Cannot run | BLIS deep overload, unrealistic latencies |

### Vidur Calibration Data (from calibration_results.json, seed 42, Llama-2-7B on A100)

These numbers are empirical snapshots from the current simulator versions. Phase 0 re-validates before committing to rates.

**cache_warmup** (BLIS base: 200 QPS):
| Rate Scale | Effective QPS | E2E (ms) | P95 (ms) | Completion | Viable? |
|-----------|--------------|----------|----------|------------|---------|
| 4.0 | ~50 | 2,778 | 8,866 | 100% | Yes (recommended) |
| 6.0 | ~33 | 2,177 | 7,021 | 82% | Marginal |
| 8.0 | ~25 | 1,966 | 6,339 | 61% | No |
| 12.0 (current) | ~17 | ~1,300 | ~4,000 | 100% | Too idle |

**load_spikes** (BLIS base: 300 QPS):
| Rate Scale | Effective QPS | E2E (ms) | P95 (ms) | Completion | Viable? |
|-----------|--------------|----------|----------|------------|---------|
| 6.0 | ~50 | 3,327 | 10,677 | 79% | Marginal |
| 8.0 | ~38 | 2,185 | 7,273 | 59% | No |
| 18.0 (current) | ~17 | ~1,300 | ~4,000 | 100% | Too idle |

**multiturn** (BLIS base: 150 QPS):
| Rate Scale | Effective QPS | E2E (ms) | Completion | Viable? |
|-----------|--------------|----------|------------|---------|
| All scales | ~19 QPS | Varies | ~66% | Context-limited, not QPS-limited |

## Experiment Design

### Phase 0: Rate Calibration (prerequisite)

**Objective**: Find the highest QPS where both BLIS and Vidur run reliably (>90% completion on Vidur, E2E in 2-5 second range on BLIS where routing decisions create measurable differentiation).

**Calibration runs must use deterministic seeds** (seed 42) and record: simulator version/commit, seed, workload parameters, E2E, P95, completion %, simulated duration.

**BLIS calibration** (not yet done — we've only tested 17 QPS and 200+ QPS):
- Test at candidate rates: 30, 40, 50, 60, 70 QPS
- Use `initial_program_lor.go` as baseline
- Target: E2E in 2-5 seconds (routing matters but not deep overload)
- Record: seed, rate, E2E, P95, completion %

**Vidur calibration** (partially done — see table above):
- cache_warmup at rate_scale 4.0 (~50 QPS): 2.8s E2E, 100% completion — **viable**
- Re-validate with new workloads (Phase 0 runs AFTER workload design)

**Rate equivalence**: "QPS" means request arrival rate (requests entering the system per second of simulated time). Both simulators measure this the same way. Effective QPS (requests completed / total simulated time) may differ due to queueing — we calibrate on arrival rate, not throughput.

**Deliverable**: A single target arrival rate per workload that both simulators handle well. Saved to `outputs/comparison_calibration/rate_sweep.json` with full provenance.

**Go/no-go gate**: Both simulators must show routing signal at the calibrated rate:
- Vidur: >90% completion on at least 3 of 4 workloads
- BLIS: >2% score difference between LOR and round-robin on at least 2 workloads
- Vidur: >2% score difference between LOR and round-robin on at least 1 workload (verifies Vidur's routing decisions also matter at this rate)
If these conditions are not met, fall back to "each at own stress point" comparison (see Risks).

**LOR baseline equivalence check**: At the calibrated rate, verify both simulators show similar round-robin → LOR improvement direction. If one shows large improvement (>10%) while the other shows none, the baselines are not comparable and the rate must be adjusted.

**Seed variance check**: Run LOR baseline with seeds 42, 456, 789 on both simulators. Report standard deviation of combined score across seeds. If Vidur's std-dev > 2× BLIS's std-dev, increase to 3 seeds for Phase 2.

**Execution order**: Phase 0 and Phase 1 are interleaved, not sequential:
1. Design workloads with placeholder rates (Phase 1a)
2. Run rate sweep on both simulators using placeholder workloads (Phase 0a)
3. Update workload rates based on calibration results (Phase 1b)
4. Re-validate final workloads at chosen rates (Phase 0b)

### Phase 1: Workload Design (purpose-built comparison workloads)

Design **new workloads from scratch** optimized for this comparison. Do NOT reuse v2/v3 distributions — create workloads that highlight BLIS's advantages (prefix caching, richer signals).

**Four workloads** (each designed to test a specific BLIS capability):

1. **prefix_heavy**: Large shared prefixes (8K+ tokens), few prefix groups (2-3), high prefix-to-input ratio. BLIS should discover prefix-affinity routing; Vidur has nothing to exploit.
   - BLIS: 3 clients with `prefix_group` and `prefix_length: 8192`, plus 1 client without prefix
   - Vidur: Same arrival/token distributions but prefix info is invisible to the router

2. **load_imbalance** (CONTROL): Heterogeneous request sizes (mix of tiny 40-token and large 2000-token requests), bursty gamma arrivals. Tests whether discovery finds token-aware or size-aware load balancing.
   - Both simulators see this — but BLIS has KVUtilization and FreeKVBlocks as signals
   - **Control workload pass/fail**: BLIS and Vidur should show similar improvement (within 5% of each other). If BLIS shows >5% more improvement than Vidur on this workload, it suggests BLIS's advantage comes from general signal richness, not just prefix/SLO/session signals. If both show similar improvement, it confirms the advantage is signal-specific.

3. **session_affinity**: Multi-turn sessions with 4-6 rounds, large accumulated context. Tests whether BLIS discovers session stickiness via SessionID signal.
   - BLIS: SessionID available; Vidur: no session concept

4. **mixed_slo**: Realtime + batch + interactive clients with different latency requirements. Tests SLO-aware routing.
   - BLIS: SLOClass signal available; Vidur: no SLO concept

**BLIS format**: YAML workload specs with `aggregate_rate` set from Phase 0 calibration.
**Vidur format**: CSV traces generated from the same underlying distributions (arrival times, token counts). Prefix/SLO/session metadata is BLIS-only — Vidur traces contain only `arrived_at, num_prefill_tokens, num_decode_tokens`.

**Key constraint**: Each workload produces ~30-60 seconds of simulated time at the calibrated rate.

**Workload matching**: Workloads are generated from the same statistical distributions but are NOT identical traces. Seeds control reproducibility within each simulator, but cross-simulator workloads are independent samples from the same distribution. This is acceptable because we compare relative improvement, not absolute latency.

### Workload Version Switching

**Evaluator configuration**: Add `BLIS_WORKLOAD_SET` env var (default: `"v3"`, set to `"comparison"` for this experiment). The evaluator reads the workload list from a versioned mapping:

```
v3 → [workload_v3_cache_warmup.yaml, workload_v3_load_spikes.yaml, workload_v3_multiturn.yaml]
comparison → [workload_cmp_prefix_heavy.yaml, workload_cmp_load_imbalance.yaml, workload_cmp_session_affinity.yaml, workload_cmp_mixed_slo.yaml]
```

Same pattern for Vidur: `VIDUR_WORKLOAD_SET` env var.

**Baseline cache invalidation**: Baseline metrics are keyed by workload set + seeds. Changing `BLIS_WORKLOAD_SET` forces baseline recomputation. The baseline file includes a `workload_set` field for verification.

### Phase 2: SkyDiscover Runs (50 iterations)

**BLIS** (4 framework runs):
```bash
EXPERIMENT="260312_50i_comparison"
for FW in openevolve evox gepa_native adaevolve; do
  export BLIS_OUTPUT_DIR="outputs/blis_router/${EXPERIMENT}/${FW}"
  export BLIS_SEED="42,456"
  export BLIS_WORKLOAD_SET="comparison"
  mkdir -p "$BLIS_OUTPUT_DIR"
  uv run skydiscover-run \
    benchmarks/blis_router/initial_program_lor.go \
    benchmarks/blis_router/evaluator.py \
    -c benchmarks/blis_router/config.yaml \
    -s $FW -i 50 -o "$BLIS_OUTPUT_DIR" -l INFO
done
```

**Vidur** (4 framework runs):
```bash
EXPERIMENT="260312_50i_comparison_vidur"
for FW in openevolve evox gepa_native adaevolve; do
  export VIDUR_OUTPUT_DIR="outputs/vidur_router/${EXPERIMENT}/${FW}"
  export VIDUR_SEED="42,456"
  export VIDUR_WORKLOAD_SET="comparison"
  mkdir -p "$VIDUR_OUTPUT_DIR"
  uv run skydiscover-run \
    benchmarks/vidur_router/initial_program.py \
    benchmarks/vidur_router/evaluator.py \
    -c benchmarks/vidur_router/config.yaml \
    -s $FW -i 50 -o "$VIDUR_OUTPUT_DIR" -l INFO
done
```

**Starting point**: LOR for both (default, configurable).

**Pre-run validation**: Before each SkyDiscover run, verify:
- Baseline computes successfully with the comparison workload set
- LOR baseline score is non-zero and finite
- All workload files exist and are parseable
- routing.go (BLIS) is clean: `git diff sim/routing.go` shows no changes

**Timing estimate**:
- BLIS: ~60s/iteration × 50 iterations × 4 frameworks = ~3.3 hours
- Vidur: ~120-180s/iteration × 50 iterations × 4 frameworks = ~6.7-10 hours
- Total: ~10-13 hours (sequential), ~5-7 hours (2 parallel streams)

### Phase 3: Evaluation & Comparison

**Primary metric**: % improvement over LOR baseline (relative, not absolute).

```
improvement = (best_score - LOR_score) / abs(LOR_score) × 100
```

Guard: if `LOR_score == 0`, report as N/A. Both scores are negative (lower latency = higher score), so `abs(LOR_score)` is always positive.

**Pre-comparison validation**:
- Verify all 8 runs (4 BLIS + 4 Vidur) completed successfully
- Verify each run's best program compiles/runs (BLIS: Go build succeeds; Vidur: Python import succeeds)
- Verify baseline metrics exist and include `workload_set: "comparison"` field
- Log any frameworks where best_score == baseline_score (no improvement found)

**Comparison table** (the main result):

| Framework | BLIS % over LOR | Vidur % over LOR | BLIS advantage |
|-----------|----------------|-----------------|----------------|
| openevolve | ? | ? | ? |
| evox | ? | ? | ? |
| gepa_native | ? | ? | ? |
| adaevolve | ? | ? | ? |

**Per-workload breakdown**: Show which workloads BLIS wins on. Expect:
- `prefix_heavy`: BLIS's biggest advantage (Vidur blind to prefix)
- `session_affinity`: BLIS advantage (SessionID signal)
- `mixed_slo`: BLIS advantage (SLOClass signal)
- `load_imbalance`: Smaller or no advantage (both have load signals)

**Signal attribution analysis** (quantitative + qualitative):
1. **Automated signal scan**: For each discovered algorithm, grep for signal variable names (CacheHitRate, KVUtilization, FreeKVBlocks, SLOClass, SessionID for BLIS; num_pending_requests, memory_usage_percent, num_prefill_tokens for Vidur). Report which signals each algorithm references.
2. **Signal ablation** (optional, time permitting): For the best BLIS algorithm, zero out each signal one at a time and re-evaluate. If score drops >2% when CacheHitRate is zeroed, the algorithm meaningfully depends on prefix caching.
3. **Code diff review**: Manually inspect the top-scoring BLIS and Vidur algorithms to confirm the automated scan results and identify routing logic patterns.

**Comparison script**: `benchmarks/scripts/compare_blis_vs_vidur.py`
- Reads `best_program_info.json` from each framework's output directory
- Computes % improvement over LOR for each
- Generates comparison tables, per-workload breakdowns
- Output schema: `{ "blis": { "<framework>": { "score", "lor_score", "improvement_pct", "per_workload": {...} } }, "vidur": { ... } }`
- Outputs: `outputs/comparison/blis_vs_vidur_results.json`, `comparison_table.csv`

### Phase 4: Analysis & Writeup

**analysis.md** in output directory should include:

1. **Rate calibration results**: What rates were chosen, why, and the calibration evidence
2. **Aggregate comparison table**: % improvement over LOR for each framework × simulator
3. **Per-workload E2E table**: Where does BLIS win? Which workloads show prefix/SLO/session advantage?
4. **Algorithm diff analysis**: What signals do BLIS-discovered algorithms use that Vidur cannot?
5. **Search space analysis**: BLIS can start from prefix-aware programs; Vidur cannot
6. **Convergence comparison**: Which simulator's discovery converges faster?
7. **Deployment viability**: BLIS algorithms map more directly to llm-d (per-request, prefix-aware)

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| No rate exists where both show signal | Experiment invalid | Fall back to "each at own stress point" with relative comparison. Document the rate gap as a finding. |
| BLIS shows <5% improvement even at moderate load | Weak claim | Increase to 8 instances (more routing surface), use larger prefixes (16K+), or increase rate within BLIS's safe range |
| Vidur improvement from batch-routing, not signal surface | Confounded result | Analyze discovered algorithms qualitatively — check if Vidur finds batch-specific vs signal-specific strategies |
| Different LLMs confound comparison | Unfair comparison | Use relative metrics only; both run multi-LLM for robustness. LLM difference affects absolute numbers, not relative improvement |
| Iteration time too slow for Vidur | Practical blocker | Use `VIDUR_TRACE_SCALE=0.5` to halve trace size, or reduce to single-seed |
| Baseline cache stale after workload switch | Invalid scores | Baseline keyed by workload_set + seeds; auto-recomputes on mismatch |
| Workload design biased toward BLIS | Unfair comparison | Include `load_imbalance` workload where both simulators have comparable signals (control workload) |

## File Inventory

### New files to create
- `benchmarks/scripts/calibrate_comparison_rates.py` — Rate sweep for both simulators
- `benchmarks/scripts/compare_blis_vs_vidur.py` — Cross-simulator comparison
- `benchmarks/blis_router/workloads/workload_cmp_*.yaml` — Purpose-built BLIS comparison workloads
- Vidur comparison traces (generated by updated `generate_traces.py`)

### Existing files to modify
- `benchmarks/blis_router/evaluator.py` — Add `BLIS_WORKLOAD_SET` env var for workload version switching; add `workload_set` field to baseline cache
- `benchmarks/vidur_router/evaluator.py` — Add `VIDUR_WORKLOAD_SET` env var for workload version switching
- `benchmarks/vidur_router/scripts/generate_traces.py` — Support custom rate_scale and comparison workload generation

### No changes needed
- Initial programs (use existing `initial_program_lor.go` and `initial_program.py`)
- Config files (LLM configs stay the same)
- SkyDiscover core (search algorithms unchanged)

## Execution Order

```
Phase 0+1 (interleaved):           (~2-3 hours)
  ├→ 1a. Design 4 workloads with placeholder rates
  ├→ 1b. Update evaluator.py for workload version switching
  ├→ 0a. Rate sweep on both simulators using placeholder workloads
  ├→ 0b. LOR baseline equivalence check + seed variance check
  ├→ 1c. Update workload rates from calibration
  └→ 0c. Re-validate final workloads at chosen rates (go/no-go gate)

Phase 2: SkyDiscover runs          (~10-13 hours total)
  ├→ BLIS: 4 frameworks × 50i      (~3.3 hours, sequential)
  └→ Vidur: 4 frameworks × 50i     (~6.7-10 hours, sequential)
  (Can run BLIS and Vidur in parallel on different machines/terminals)

Phase 3: Evaluation                 (~30 min)
  ├→ Validate all runs completed with valid outputs
  └→ Run comparison script, generate tables

Phase 4: Analysis                   (~1-2 hours)
  └→ Write analysis.md, signal attribution, deployment viability
```

## Success Criteria

The experiment succeeds if:
1. Both simulators run at the calibrated rate without breaking (>90% Vidur completion, reasonable BLIS E2E, both show >2% LOR-vs-round-robin differentiation)
2. BLIS-discovered algorithms show **>5% improvement** over LOR (meaningful signal)
3. BLIS improvement % > Vidur improvement % on at least 3 of 4 workloads (BLIS advantage demonstrated). This is necessary but not sufficient — criterion #4 provides the causal evidence.
4. Signal attribution confirms BLIS algorithms reference prefix/cache/SLO/session signals that Vidur algorithms cannot access (automated signal scan + optional ablation)
5. At least one workload (expect `prefix_heavy`) shows clear BLIS advantage attributable to prefix caching
6. Control workload (`load_imbalance`) shows BLIS and Vidur within 5% of each other, confirming advantage is signal-specific not general

If criteria 1-2 fail, the experiment still produces useful findings about simulator limitations and the rate regime where each simulator is useful. If criterion 6 fails (BLIS wins even on control), the conclusion shifts from "signal-specific advantage" to "general signal richness advantage."
