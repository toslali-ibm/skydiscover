package sim

import (
	"fmt"
	"math/rand"
)

type RoutingSnapshot struct {
	ID               string
	QueueDepth       int
	BatchSize        int
	KVUtilization    float64
	FreeKVBlocks     int64
	CacheHitRate     float64
	InFlightRequests int
}

func (s RoutingSnapshot) EffectiveLoad() int {
	return s.QueueDepth + s.BatchSize + s.InFlightRequests
}

func NewRoutingSnapshot(id string) RoutingSnapshot {
	if id == "" {
		panic("NewRoutingSnapshot: id must not be empty")
	}
	return RoutingSnapshot{ID: id}
}

type RoutingDecision struct {
	TargetInstance string
	Reason         string
	Scores         map[string]float64
	Priority       float64
}

func NewRoutingDecision(target string, reason string) RoutingDecision {
	if target == "" {
		panic("NewRoutingDecision: target must not be empty")
	}
	return RoutingDecision{TargetInstance: target, Reason: reason}
}

func NewRoutingDecisionWithScores(target string, reason string, scores map[string]float64) RoutingDecision {
	if target == "" {
		panic("NewRoutingDecisionWithScores: target must not be empty")
	}
	return RoutingDecision{TargetInstance: target, Reason: reason, Scores: scores}
}

type RoutingPolicy interface {
	Route(req *Request, state *RouterState) RoutingDecision
}

type RoundRobin struct {
	counter int
}

func (rr *RoundRobin) Route(req *Request, state *RouterState) RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		panic("RoundRobin.Route: empty snapshots")
	}
	target := snapshots[rr.counter%len(snapshots)]
	rr.counter++
	return NewRoutingDecision(target.ID, fmt.Sprintf("round-robin[%d]", rr.counter-1))
}

type LeastLoaded struct {
	rng *rand.Rand
}

func (ll *LeastLoaded) Route(req *Request, state *RouterState) RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		panic("LeastLoaded.Route: empty snapshots")
	}
	minLoad := snapshots[0].EffectiveLoad()
	for i := 1; i < len(snapshots); i++ {
		if load := snapshots[i].EffectiveLoad(); load < minLoad {
			minLoad = load
		}
	}
	var tied []int
	for i, snap := range snapshots {
		if snap.EffectiveLoad() == minLoad {
			tied = append(tied, i)
		}
	}
	idx := tied[0]
	if len(tied) > 1 && ll.rng != nil {
		idx = tied[ll.rng.Intn(len(tied))]
	}
	return NewRoutingDecision(snapshots[idx].ID, fmt.Sprintf("least-loaded (load=%d)", minLoad))
}

type observerFunc func(req *Request, targetInstance string)

type WeightedScoring struct {
	scorers   []scorerFunc
	weights   []float64
	observers []observerFunc
	rng       *rand.Rand
}

func (ws *WeightedScoring) Route(req *Request, state *RouterState) RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		panic("WeightedScoring.Route: empty snapshots")
	}

	// EVOLVE-BLOCK-START
	// Oracle v14: Anti-Glia Composite Oracle
	//
	// Tuned to maximize advantage over Glia while maintaining 10-15% over 1:1.
	// Key change: slightly higher prefix weight (0.35) than v11/v12 (0.30).
	// This gets more cache hits (widening Glia gap) at slight cost to load balance
	// (narrowing 1:1 gap, which is acceptable per requirements).
	//
	// Combines:
	//   - Adaptive prefix weight: 0.35 base, reduced under overload
	//   - Fresh InFlightRequests with moderate quadratic penalty
	//   - KV viability filter + soft pressure gradient
	//   - Overload detection: when cached instance is >1.8x average, spill

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// --- Phase 1: KV Viability Filter ---
	viable := make([]bool, len(snapshots))
	anyViable := false
	for i, snap := range snapshots {
		viable[i] = true
		if snap.KVUtilization > 0.92 {
			viable[i] = false
		}
		if viable[i] {
			anyViable = true
		}
	}
	if !anyViable {
		for i := range viable {
			viable[i] = true
		}
	}

	// --- Phase 2: Overload-Adaptive Prefix Weight ---
	// Find best prefix instance and compute load stats
	bestPrefixIdx := 0
	bestPrefixScore := prefixScores[snapshots[0].ID]
	totalInFlight := 0
	for i, snap := range snapshots {
		if prefixScores[snap.ID] > bestPrefixScore {
			bestPrefixScore = prefixScores[snap.ID]
			bestPrefixIdx = i
		}
		totalInFlight += snap.InFlightRequests
	}
	avgInFlight := float64(totalInFlight) / float64(len(snapshots))
	cachedInFlight := float64(snapshots[bestPrefixIdx].InFlightRequests)

	cacheWarm := bestPrefixScore > 0.3

	// Base prefix weight: 0.35 (higher than v11/v12's 0.30 for more cache hits)
	prefixWeight := 0.35
	loadWeight := 0.65

	if cacheWarm && avgInFlight > 0.5 {
		overloadRatio := cachedInFlight / avgInFlight
		if overloadRatio > 1.8 {
			// Smoothly reduce prefix weight under overload
			reduction := (overloadRatio - 1.8) / 1.5
			if reduction > 1.0 {
				reduction = 1.0
			}
			prefixWeight = 0.35 * (1.0 - reduction)
			loadWeight = 1.0 - prefixWeight
		}
	}

	// --- Phase 3: Scoring ---
	for i, snap := range snapshots {
		if !viable[i] {
			scores[snap.ID] = -1e9
			continue
		}

		ps := prefixScores[snap.ID]
		f := float64(snap.InFlightRequests)

		// Moderate quadratic: better differentiation than 1/(1+f)
		loadScore := 1.0 / (1.0 + 0.12*f*f)

		// KV pressure gradient
		kvMult := 1.0
		if snap.KVUtilization > 0.80 {
			kvMult = 1.0 - (snap.KVUtilization-0.80)*3.0
			if kvMult < 0.3 {
				kvMult = 0.3
			}
		}

		scores[snap.ID] = (prefixWeight*ps + loadWeight*loadScore) * kvMult
	}

	// Argmax with random tie-breaking
	bestScore := -1e18
	for _, snap := range snapshots {
		if scores[snap.ID] > bestScore {
			bestScore = scores[snap.ID]
		}
	}
	var tied []int
	for i, snap := range snapshots {
		if scores[snap.ID] == bestScore {
			tied = append(tied, i)
		}
	}
	bestIdx := tied[0]
	if len(tied) > 1 && ws.rng != nil {
		bestIdx = tied[ws.rng.Intn(len(tied))]
	}
	// EVOLVE-BLOCK-END

	for _, obs := range ws.observers {
		obs(req, snapshots[bestIdx].ID)
	}

	return NewRoutingDecisionWithScores(
		snapshots[bestIdx].ID,
		fmt.Sprintf("weighted-scoring (score=%.3f)", bestScore),
		scores,
	)
}

type AlwaysBusiest struct{}

func (ab *AlwaysBusiest) Route(_ *Request, state *RouterState) RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		panic("AlwaysBusiest.Route: empty snapshots")
	}
	maxLoad := snapshots[0].EffectiveLoad()
	target := snapshots[0]
	for i := 1; i < len(snapshots); i++ {
		load := snapshots[i].EffectiveLoad()
		if load > maxLoad {
			maxLoad = load
			target = snapshots[i]
		}
	}
	return NewRoutingDecision(target.ID, fmt.Sprintf("always-busiest (load=%d)", maxLoad))
}

func NewRoutingPolicy(name string, scorerConfigs []ScorerConfig, blockSize int64, rng *rand.Rand) RoutingPolicy {
	if !IsValidRoutingPolicy(name) {
		panic(fmt.Sprintf("unknown routing policy %q", name))
	}
	switch name {
	case "", "round-robin":
		return &RoundRobin{}
	case "least-loaded":
		return &LeastLoaded{rng: rng}
	case "weighted":
		if len(scorerConfigs) == 0 {
			scorerConfigs = DefaultScorerConfigs()
		}
		scorers := make([]scorerFunc, len(scorerConfigs))
		var observers []observerFunc
		for i, cfg := range scorerConfigs {
			scorer, obs := newScorerWithObserver(cfg.Name, int(blockSize))
			scorers[i] = scorer
			if obs != nil {
				observers = append(observers, obs)
			}
		}
		weights := normalizeScorerWeights(scorerConfigs)
		return &WeightedScoring{scorers: scorers, weights: weights, observers: observers, rng: rng}
	case "always-busiest":
		return &AlwaysBusiest{}
	default:
		panic(fmt.Sprintf("unhandled routing policy %q", name))
	}
}
