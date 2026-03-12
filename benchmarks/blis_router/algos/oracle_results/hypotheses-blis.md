# BLIS Inference-Sim Hypothesis Experiments: Complete Catalog

44 validated hypothesis experiments from `inference-sim/hypotheses/`, organized by family.
Each experiment follows the ED-1 through ED-6 rigor framework with multi-seed validation,
controlled comparisons, and root cause verification.

---

## Catalog Summary

| Family | Count | Confirmed | Refuted | Partial |
|--------|:-----:|:---------:|:-------:|:-------:|
| Scheduler Invariants | 4 | 4 | 0 | 0 |
| Structural Model | 10 | 5 | 2 | 3 |
| Robustness / Failure-Mode | 7 | 5 | 1 | 1 |
| Performance Regime | 6 | 3 | 2 | 1 |
| Workload / Arrival | 3 | 2 | 0 | 1 |
| Cross-Policy Comparative | 8 | 5 | 1 | 2 |
| **Total** | **44** | **30** | **6** | **8** |

Note: "Confirmed with nuance" and "Confirmed with wrong mechanism" are counted as Confirmed.
"Reclassified" counted as Partial.

---

## 1. Scheduler Invariants

### H12: Request Conservation Across All Policies
- **Status:** CONFIRMED (with bug discovery)
- **Hypothesis:** `injected == completed + queued + running + dropped` holds for all policy configurations.
- **Finding:** 67/67 conservation checks pass. Discovered preemption-path panic bug in the process.
- **Invariant:** INV-1 validated universally.

### H13: Deterministic Replay
- **Status:** CONFIRMED
- **Hypothesis:** Same seed produces byte-identical output across all policy configurations.
- **Finding:** INV-6 holds for 5 policy configurations. Bit-exact reproducibility confirmed.

### H-Liveness: Scheduler Liveness Under Admissible Load
- **Status:** CONFIRMED
- **Hypothesis:** All schedulers complete all requests when load < capacity.
- **Finding:** 45/45 configurations pass (rate=100–280 req/s). Zero still_queued or still_running at termination. SJF provides 31% lower E2E under constrained batching (token budget=2048).

### H25: Full Policy Stack Integration Stress
- **Status:** CONFIRMED
- **Hypothesis:** Full stack (weighted routing + token-bucket + tiered KV + priority-FCFS + tracing) maintains conservation.
- **Finding:** All invariants (INV-1, INV-6, INV-5) hold under combined policy stress. Token-bucket rejects 99.2% at 2000 req/s (Config A). Multi-turn chat achieves 34.96% cache hit rate (Config B). Zero counterfactual regret under low utilization.

---

## 2. Structural Model

### H3: Signal Freshness — Queue-Depth vs KV-Utilization
- **Status:** CONFIRMED
- **Hypothesis:** Queue-depth distributes more evenly than kv-utilization at high request rates.
- **Finding:** At rate=5000, kv-utilization produces:
  - **1.7–2.8× worse TTFT mean** (2259–3644ms vs 1290–1319ms)
  - **3.0–4.7× worse TTFT P99** (7870–12285ms vs 2532–2604ms)
  - **200×+ worse distribution uniformity** (StdDev 142–226 vs 0.7–1.0)
- **Mechanism:** DES event ordering at `cluster.go:160` processes routing decisions *before* batch formation. `PendingRequests` updates synchronously (fresh); `KVUtilization` only updates after batch formation (stale). At rate=5000 (200μs inter-arrival), hundreds of routing decisions fire before any KV state change.
- **Rate dependence:** Effect scales with rate. At rate=100: 1.07× ratio. At rate=2000: 2.13×. At rate=5000: 1.71×.
- **Snapshot refresh compounding:** At 2ms refresh interval, 830/1000 requests pile onto a single instance (vs 423 with immediate refresh).
- **Composite mitigation:** Even `kv:5,qd:1` (KV-dominant) produces perfect balance because the fresh QD signal dominates when KV is uniformly stale.
- **Routing implication:** Never use kv-utilization as sole routing scorer at high rates. Always include queue-depth for stability.

### H29: Stale Routing Snapshots Degrade Tail Latency
- **Status:** CONFIRMED
- **Hypothesis:** Increasing snapshot refresh from 1ms to 100ms degrades TTFT P99 by ≥20% at >80% saturation.
- **Finding:** Degradation far exceeds 20% threshold:

  | Refresh Interval | TTFT P99 | Change vs 0μs |
  |-----------------|---------|--------------|
  | 0μs (immediate) | 64.00ms | baseline |
  | 1ms | 64.00ms | +0.0% |
  | 5ms | 64.00ms | +0.0% **(safe zone)** |
  | 10ms | 72.97ms | **+14.0% (threshold)** |
  | 50ms | 229.57ms | **+258.7%** |
  | 100ms | 414.76ms | **+548.1%** |
  | 500ms | 1163.03ms | **+1717.3%** |

- **Instance distribution:** Fresh (1ms): Jain FI=0.9986–0.9997. Stale (100ms): FI=0.9952, all seeds show deterministic 120/120/120/140 bias.
- **Negative control:** queue-depth:1 scorer shows **0.0% difference** (byte-identical across 3 seeds). Effect is specific to stale KV signal path.
- **Composite mitigation:** kv:2,qd:2 reduces impact from +354% to **+3.8%** (99% mitigation).
- **Mechanism:** Routing herding — stale signals → multiple requests see same "least loaded" instance → burst queueing on herded instance → feedback loop.
- **Erratum (2026-03-09, PR #467):** Post-fix, ALL Prometheus-sourced signals (QueueDepth, BatchSize, KVUtilization) become Periodic when `--snapshot-refresh-interval > 0`. Only `InFlightRequests` remains unconditionally fresh.

### H26: Admission Latency Causal Ordering
- **Status:** CONFIRMED
- **Hypothesis:** Adding admission latency delays E2E by exactly that amount at low load.
- **Finding:** Exact additive offset. TTFT, E2E, and SchedulingDelay all shift linearly. Validates event pipeline causality (INV-5).

### H-Phase-Structure: TTFT/Decode Linearity
- **Status:** CONFIRMED
- **Hypothesis:** TTFT is linear in input tokens; decode time is linear in output tokens.
- **Finding:** R² = 1.000000 for both relationships. Slopes match α/β predictions within 0.01%. Raw TTFT contaminated by Poisson queueing at rate=0.01 (one seed hit blocking); adjusted TTFT (minus scheduling delay) is perfectly clean.

### H-MMK: DES vs M/M/k Analytical Model
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** DES matches M/M/k analytical predictions.
- **Finding:** After work-conserving bug fix: within 3.3% at ρ≤0.3. Diverges 47–71% at ρ≥0.5. Little's Law holds universally at 0.0% error.
- **Root cause of divergence:** Alpha/beta split — server-blocking `beta` (step time) vs non-blocking `alpha` (queueing overhead). Alpha doesn't advance simulation clock, creating hidden service time that M/M/k doesn't model.

### H-Step-Quantum: Step-Time Quantum Effect
- **Status:** REFUTED
- **Hypothesis:** Reducing step-time quantum reduces DES/M/M/1 divergence.
- **Finding:** Divergence *increases* when reducing beta (47% → 98%). **Wrong mental model** — the divergence comes from alpha/beta split, not step quantization. Alpha=0 control confirms: divergence drops to 0.3–14%.

### H19: Roofline vs Blackbox Policy Rankings
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** Roofline latency model preserves blackbox policy rankings.
- **Finding:** Mean rankings preserved (6/6 match). P99 rankings diverge (1/6 match). Roofline's deterministic step times mask variance that blackbox captures via fitted coefficients.

### H30: CrossModel Request-Level Fidelity
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** BLIS replay with crossmodel coefficients matches real vLLM within 25% relative error.
- **Finding:** Throughput ±2.5% (passes). TTFT systematically underpredicted: **-25.9% mean |RE|**. Both crossmodel AND per-model blackbox show same gap — it's a scheduling model limitation (BLIS has zero inter-step overhead; real vLLM has 50–300μs CPU overhead per step per layer, accumulating to 50–400ms per request).

### H31: CrossModel Generalization to Near-Saturation
- **Status:** REFUTED
- **Hypothesis:** For codellama-34b reasoning, BLIS achieves ≥90% completion with TTFT/E2E MAPE <30%/<25%.
- **Finding:** Completion rate passes (98%) but TTFT/E2E fail by **2000×**. A 22% throughput overestimate shifts ρ from 124% (unstable, queues grow linearly) to 102% (stable, bounded). M/G/1 phase transition: E[W] → ∞ as ρ → 1⁺.

### H32: CrossModel Aggregate Capacity Planning
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** Crossmodel achieves TTFT p50/p99/throughput targets for codellama but fails for mixtral reasoning.
- **Finding:** Codellama: TTFT p50 RE 15.7%, p99 RE 16.3% (both pass). Cross-profile generalization works (codegen/roleplay unseen profiles within targets). Capacity planning: ρ < 0.8 usable (±16–25%); 0.8–1.0 unreliable; ρ > 1.0 catastrophic.

---

## 3. Robustness / Failure-Mode

### H5: Token-Bucket Admission Smooths Bursts
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Token-bucket admission smooths TTFT under Gamma CV=3.5 arrivals.
- **Finding:** 56–69× TTFT improvement, but via **96% load shedding**, not burst smoothing. The token bucket rejects most requests during bursts; survivors see empty queues. The mechanism is admission control (capacity limiting), not traffic shaping.

### H14: Pathological Template Anomalies
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** Pathological configurations (always-busiest, inverted-SLO) produce detectably worse behavior.
- **Finding:** 4.5× worse TTFT confirmed. Three detector bugs discovered in the process. Anomaly detection framework catches pathological configurations but with different sensitivity than expected.

### H21: Extreme Scorer Weights (100:1)
- **Status:** REFUTED
- **Hypothesis:** `prefix-affinity:100,queue-depth:1` behaves like single prefix-affinity scorer.
- **Finding:** Dramatically different. Pure prefix-affinity sends ALL 200 requests to instance 0 (149.33ms TTFT). 100:1 ratio distributes [160,13,14,13] (24.38ms TTFT) — **6.1× better**.
- **Mechanism (4 steps):**
  1. Weight normalization: 100:1 → PA=0.9901, QD=0.0099
  2. Cold-start tie: prefix cache empty → all scores = 0.0
  3. Observer feedback: first request seeds cache on instance 0 → cascade begins
  4. Queue-depth tiebreaker breaks cascade: even 0.0099 weight detects load imbalance, seeds cache on other instances
- **Key finding:** Normalization makes 100:1 and 100000:1 produce **byte-identical output**. Weight magnitude is irrelevant — only relative ratios matter. And even tiny ratios suffice for tiebreaking.
- **Routing implication:** Never use prefix-affinity as sole scorer. Even 1% queue-depth weight prevents degenerate concentration.

### H22: Zero KV Blocks Error Handling
- **Status:** CONFIRMED
- **Hypothesis:** Zero/negative KV configurations produce clean CLI errors.
- **Finding:** All boundary conditions caught cleanly. Defense-in-depth: CLI `logrus.Fatalf` + library `panic()` backup.

### H24: Combined Pathological Anomalies
- **Status:** CONFIRMED
- **Hypothesis:** Always-busiest + inverted-SLO produces maximum anomalies.
- **Finding:** 4.9× TTFT P99 degradation. Super-additive interaction: combined inversions (9,963 anomalies) > sum of individual parts (5,017). Routing dominates ~95% of degradation; scheduling contributes ~5%.

### H-Overload: 10× Overload Robustness
- **Status:** CONFIRMED
- **Hypothesis:** 10× overload produces no panics or deadlocks; conservation holds.
- **Finding:** 10/10 configurations exit cleanly. 84/84 conservation checks pass across 1–10× saturation. Token-bucket rejection: 0% at 1× to 70% at 10×.

### H-Overload-KV: Combined Overload + KV Pressure
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Combined overload + KV pressure maintains conservation.
- **Finding:** 72/72 conservation checks pass. Sharp cliff between 500–2000 KV blocks: 500 blocks causes timeout (cascading preemptions); 2000 blocks has zero preemptions. Tiered config has no effect when GPU blocks sufficient.

---

## 4. Performance Regime

### H7: Horizontal Scaling
- **Status:** CONFIRMED (super-linear)
- **Hypothesis:** Doubling instances 4→8 halves TTFT P99 at saturation.
- **Finding:** **7.4× improvement** (not 2×).

  | Instances | Utilization | TTFT P99 |
  |-----------|:-----------:|:--------:|
  | 2 | 4.35× | 1787ms |
  | 4 | 2.18× | 476ms |
  | 8 | 1.09× | **56ms** |

- **Mechanism:** Queue growth rate per instance = (λ/k) − μ. At k=4: 67.6 req/s excess. At k=8: 5.1 req/s excess (**92.5% reduction**, not 50%). Theoretical ratio 13.3×; measured 7.4× due to finite workload, alpha overhead, and queue draining.
- **Sub-saturation control:** 4→8 gives only 1.064× improvement (within noise). No queue buildup → no scaling benefit.
- **E2E insensitive:** 4→8 ratio = 1.058× (5.8%). Decode dominates E2E.
- **Routing implication:** Routing quality has most impact **near saturation** where small excess rate changes cause large latency changes.

### H8: KV Cache Pressure Cliff
- **Status:** CONFIRMED
- **Hypothesis:** Reducing KV blocks increases preemption frequency.
- **Finding:** **Binary threshold**, not gradual degradation.

  | KV Blocks | Preempt Rate | TTFT P99 | vs Baseline |
  |-----------|:----------:|:--------:|:-----------:|
  | 5000 | 0.0% | 460.6ms | 1.00× |
  | 3000 | 0.0% | 460.6ms | 1.00× |
  | 2200 | 0.0% | 460.6ms | 1.00× |
  | **2100** | **11.2%** | **2174.6ms** | **4.72×** |
  | **2000** | **51.0%** | **3048.0ms** | **6.62×** |

- **Cascade mechanism:** Preempted requests restart from ProgressIndex=0 (full re-prefill). Creates feedback: preemption → requeue → re-prefill → consumes blocks → more preemptions. At 2000 blocks: 28–66% of requests preempted at least once.
- **Livelock finding:** Blocks < 1000 causes simulation livelock (no circuit breaker in preemption loop).
- **Routing implication:** No graceful degradation zone. A KV-blind router can push instances past the cliff in a single decision.

### H11: Token Budget Throughput vs ITL Tradeoff
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Larger token budgets improve throughput but worsen ITL.
- **Finding:** Throughput +27%, ITL P99 worsens 5.8×. Prefill-decode co-batching creates ITL spikes when large prefills share decode batches.

### H27: Chunked Prefill Reduces Short-Request TTFT
- **Status:** CONFIRMED
- **Hypothesis:** Chunked prefill (threshold=256) reduces TTFT P99 for short requests by ≥30% in bimodal workloads.
- **Finding:** **46–58% improvement** (avg 51.9%), far exceeding 30% threshold.
- **Tradeoff:** P50 worsens 8–21%. Long-request TTFT degrades 60–69%. E2E insensitive (<4%).
- **Mechanism:** Chunking breaks long prefills into 256-token chunks interleaved with decode steps. Short requests waiting behind a long prefill see first-token sooner.

### H28: Chunked Prefill Improves Decode ITL
- **Status:** REFUTED
- **Hypothesis:** Chunked prefill improves mean ITL by >15% for concurrent decode requests.
- **Finding:** ITL improvement ~-0.5% (essentially zero). **Wrong mental model:** decode steps (~255 of 256 total steps) dominate; the one prefill step being split has negligible ITL impact. Chunking only helps TTFT, not ITL.

### H-Reasoning-KV: Reasoning Context KV Cliff
- **Status:** REFUTED (primary hypothesis)
- **Hypothesis:** Multi-turn reasoning triggers preemption cliff shift.
- **Finding:** Cliff shift NOT detected (1.09×, below 20% threshold). Mean concurrent demand identical, so preemption cliff location unchanged.
- **Positive finding:** **63.8% prefix cache hit rate** for reasoning workloads. TTFT growth only 32% (128→1664 tokens) despite 3.6× input growth, thanks to prefix caching.

---

## 5. Workload / Arrival

### H16: Gamma vs Poisson Tail Latency
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Bursty (Gamma CV=3.5) arrivals produce worse tail latency than Poisson.
- **Finding:**

  | Load Level | Gamma/Poisson TTFT P99 Ratio |
  |-----------|:---------------------------:|
  | Sub-saturation (ρ≈0.59) | **1.56–1.81× (avg 1.66×)** |
  | Overload (ρ≈3×, 500 reqs) | **1.09–1.41× (avg 1.25×)** |
  | Sustained overload (2000 reqs) | 1.02× (effect vanishes) |

- **Surprise:** Effect **stronger at sub-saturation** (1.66× vs 1.25×). Low baseline TTFT makes relative burst spikes proportionally larger. At sustained overload, queues grow linearly and burst signal becomes noise.
- **E2E insensitive:** ~2% variation across all experiments. Decode dominates.
- **Routing implication:** Bursty arrivals amplify routing quality differences. Test workloads should use moderate load with high CV for maximum differentiation.

### H20: Heavy-Tailed Input Distributions
- **Status:** REFUTED
- **Hypothesis:** ParetoLogNormal distributions produce more preemptions than Gaussian.
- **Finding:** ParetoLN produces **29% FEWER preemptions** (avg 238 vs 336).
- **Root cause:** **Distribution median drives KV pressure**, not mean or tail.
  - ParetoLN: 70% of samples at ~79 tokens (5 blocks). Fast cycling, releases blocks quickly.
  - Gaussian (mean=256): median ~256 tokens (16 blocks). Uniformly high occupancy.
  - At-median block ratio: Gaussian/ParetoLN = **3.2× higher for Gaussian**.
- **Sub-saturation control:** Zero preemptions for both. ParetoLN TTFT P99 is 2.8× higher (intrinsic prefill cost from heavy tail, not HOL blocking).
- **Routing implication:** Elephant-mouse mixtures can be KV-friendlier than uniform traffic. Size-aware routing should consider occupancy duration, not just input length.

### H-Arrival-Generators: Arrival Sampler CDF Validation
- **Status:** CONFIRMED (with design limitation)
- **Hypothesis:** Poisson/Gamma/Weibull samplers match theoretical CDFs.
- **Finding:** Poisson passes perfectly. Gamma/Weibull CV≤1.5 pass. **High-CV samplers fail:** Gamma CV=3.5 and Weibull CV=3.5 fail KS test due to int64 microsecond clamping (42–43% and 6.8% truncation respectively). Clamping is intentional (prevents zero/negative IATs) but undocumented.

---

## 6. Cross-Policy Comparative

### Prefix-Affinity: Cache-Aware Routing
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Prefix-aware routing outperforms load-only for prefix-heavy workloads.
- **Finding:** **2.45× better TTFT** than queue-depth for multi-turn chat (28.2ms vs 69.0ms).

  | Config | TTFT Mean | TTFT P99 | Cache Hit |
  |--------|-----------|----------|-----------|
  | prefix-affinity:3,qd:2 | 28.2ms | 71.0ms | 55.7% |
  | queue-depth:1 | 69.0ms | 167.1ms | 23.3% |
  | round-robin | 21.8ms | 35.9ms | 62.9% |

- **Round-robin surprise:** Best at low load due to cyclic pattern: 5-round sessions × 4 instances → round 5 returns to same instance as round 1, creating accidental partial cache reuse with zero concentration.
- **High-load crossover (rate=5000):** Prefix-affinity 205.2ms vs queue-depth 892.0ms (**4.3× better**). At high load, full cache reuse outweighs concentration overhead.
- **Shared prompt caveat:** Heavy prefix weight (pa:5,qd:1) **hurts** at low load — sends all 200 to one instance (24.4ms vs even distribution at 16.8ms).
- **Routing implication:** Prefix affinity is multiplicatively valuable when prefix overlap exists. But must be balanced with load signal; pure prefix routing concentrates traffic.

### H1-SJF: Shortest-Job-First Scheduling
- **Status:** CONFIRMED
- **Hypothesis:** SJF reduces TTFT for short requests in bimodal workloads.
- **Finding:** **94% TTFT reduction** for 32-token requests in bimodal 32:1024 workload. SJF prioritizes short requests in batch formation, giving them first access to compute.

### H2: Priority-FCFS with SLO-Based Priority
- **Status:** REFUTED
- **Hypothesis:** SLO-based priority reduces realtime TTFT.
- **Finding:** SLO-based priority is **mathematically equivalent to FCFS** — the SLO priority function is age-based (oldest request gets highest priority), which is exactly FCFS ordering. No TTFT differentiation observed.

### H4: Round-Robin vs Least-Loaded Equivalence
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Round-robin matches least-loaded for uniform workloads.
- **Finding:** Mean metrics equivalent. But TTFT P99 is **12–21% worse for least-loaded** due to routing decision overhead and synchronization effects.
- **Load equivalence:** Requires workload heterogeneity. Uniform workloads don't differentiate policies even at high load because prefix-affinity = least-loaded when 100% cache-miss.

### H6: Counterfactual Regret
- **Status:** CONFIRMED (with wrong mechanism)
- **Hypothesis:** Round-robin has higher counterfactual regret than weighted routing.
- **Finding:** RR regret higher, but mechanism is different: RR regret is structurally zero by design (no alternative decisions possible). Higher regret does NOT imply worse TTFT.

### H15: Fitness Evaluation Ranks Prefix-Affinity
- **Status:** CONFIRMED (with nuance)
- **Hypothesis:** Fitness function correctly ranks prefix-affinity above load-only.
- **Finding:** +4.4% fitness advantage for prefix-aware (TTFT-heavy weights). But normalization **compresses** real improvements: 26–38% raw TTFT P99 improvement maps to only 2.6–8.2% fitness score difference.
- **Sensitivity:** TTFT-heavy weights: +4.4%. Throughput-heavy weights: +0.7%.
- **Non-prefix control:** 0.000000 difference (byte-identical). Perfect control — prefix scorer contributes zero when no prefix overlap.
- **Routing implication:** Fitness evaluation correctly discriminates but normalizes out magnitude. Weight vector selection matters — TTFT-heavy weights amplify differentiation.

### H17: Multi-Scorer Pareto Frontier
- **Status:** RECLASSIFIED (no within-workload frontier)
- **Hypothesis:** Different scorer weight configurations produce Pareto-optimal tradeoffs.
- **Finding:** **No within-workload Pareto frontier.** Cache-heavy (pa:5,qd:1) **dominates all metrics** when prefix overlap exists:

  | Config | TTFT Mean | TTFT P99 | Cache Hit |
  |--------|-----------|----------|-----------|
  | cache-heavy (pa:5,qd:1) | **21.2ms** | **31.2ms** | **39.7%** |
  | llm-d default (pa:3,qd:2,kv:2) | 26.5ms | 59.8ms | 29.4% |
  | load-balance (qd:3,kv:3) | 38.8ms | 115.0ms | 10.7% |

  On independent workloads (no prefix): all configs equivalent except kv-heavy (33% worse, temporal micro-bursting from slow KV signal changes).
- **Cross-workload frontier:** Select weights per workload type — cache-heavy for prefix, load-heavy for non-prefix. No single config is universally optimal.

### H-Cross-Model: Findings Generalization Across Architectures
- **Status:** PARTIALLY CONFIRMED
- **Hypothesis:** Behavioral findings generalize from Llama-2/Mixtral to Qwen/Qwen2.5-7B.
- **Finding:** **12/15 findings generalize**. Cache-related findings (Prefix-Affinity, H9, H10) are parameter-dependent. Invariants (INV-1, INV-6) model-agnostic. H14 pathological effect actually **stronger** with Qwen (6.5× vs 4.5×) due to higher β₂.

---

## Cross-Cutting Insights for Routing Policy Design

### Signal Architecture

1. **Fresh signals dominate stale ones.** InFlightRequests (synchronous) > QueueDepth (periodic) > KVUtilization (delayed). At high rates, kv-utilization alone causes 200× distribution imbalance (H3). Snapshot staleness at 100ms = +548% P99 degradation (H29).

2. **Composite scoring provides 99% staleness mitigation.** Adding queue-depth to kv-utilization (kv:2,qd:2) reduces staleness impact from +354% to +3.8% (H29). The default `pa:3,qd:2,kv:2` profile is inherently resilient.

3. **Post-PR #467, only InFlightRequests is unconditionally fresh.** All Prometheus-sourced signals (QueueDepth, BatchSize, KVUtilization) become periodic when snapshot-refresh-interval > 0. This makes InFlightRequests the only reliable fresh signal for routing decisions.

### Capacity & Memory

4. **KV cache is a binary cliff, not a gradient.** Zero preemptions at 2200 blocks → 51% preemption rate at 2000 blocks (H8). No graceful degradation. Preemption cascades via ProgressIndex=0 restart create feedback loops.

5. **Distribution median drives KV pressure, not mean or tail.** ParetoLN (median ~79 tokens) is 3.2× more KV-efficient than Gaussian (median ~256) despite similar means (H20). Size-aware routing should consider occupancy duration.

6. **Prefix caching provides 63.8% hit rate for reasoning, 96% TTFT reduction at maximum reuse** (H9, H-Reasoning-KV). The single highest-impact optimization for prefix-heavy workloads.

### Load & Scaling

7. **Near-saturation routing has super-linear impact.** 4→8 instances = 7.4× TTFT improvement (not 2×) because excess rate reduction is non-linear (H7). Routing quality matters most at 70–90% utilization.

8. **Bursty arrivals amplify routing quality differences.** Gamma CV=3.5 causes 1.66× worse P99 at sub-saturation but only 1.25× at overload (H16). Effect vanishes at sustained overload. Test workloads should target moderate load + high CV for maximum differentiation.

9. **Chunked prefill helps TTFT (52%) but not ITL** (H27, H28). Decode steps dominate request lifetime (>99%); prefill chunking only helps time-to-first-token.

### Weight Configuration

10. **Never use a single scorer.** Pure prefix-affinity sends all traffic to one instance (H21). Even 1% load-balancing weight prevents degenerate concentration. Weight magnitude is irrelevant — only ratios matter (100:1 ≡ 100000:1).

11. **No within-workload Pareto frontier.** Cache-heavy dominates on prefix workloads; configs are equivalent on non-prefix workloads (H17). Optimal weights depend on workload type, not a universal tradeoff.

12. **Fitness normalization compresses real improvements.** 26–38% raw TTFT P99 improvement maps to 2.6–8.2% fitness score (H15). Weight vector selection in fitness functions matters for search algorithm sensitivity.

### Invariants (Universal)

13. **INV-1 (Request Conservation):** Validated in every experiment across all policy combinations.
14. **INV-6 (Determinism):** Byte-identical output from same seed, preserved across all optimizations.
15. **INV-5 (Causality):** arrival ≤ enqueue ≤ schedule ≤ completion. Validated via admission latency injection (H26).
