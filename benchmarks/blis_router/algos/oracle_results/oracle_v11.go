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
	// Oracle v11: Adaptive Load-Overflow Router
	//
	// Improves on v10 by using a RATIO-based overflow instead of absolute difference.
	// This prevents degenerating to pure load-balancing on heavily skewed workloads
	// while still preventing hotspots on moderately skewed ones.
	//
	// Strategy:
	//   - Normal mode: prefix_affinity + InFlightRequests (like v4)
	//   - Overflow mode: when cached_inFlight > overflowRatio * avg_inFlight,
	//     reduce prefix weight dynamically based on how overloaded the cached instance is
	//   - This creates a smooth transition from prefix-dominant to load-dominant routing
	//   - Small overflows: slight prefix weight reduction (still mostly follows cache)
	//   - Large overflows: nearly pure load balancing (emergency spill)

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// Find best prefix instance
	bestPrefixIdx := 0
	bestPrefixScore := prefixScores[snapshots[0].ID]
	for i, snap := range snapshots {
		if prefixScores[snap.ID] > bestPrefixScore {
			bestPrefixScore = prefixScores[snap.ID]
			bestPrefixIdx = i
		}
	}

	// Compute average InFlightRequests across all instances
	totalInFlight := 0
	for _, snap := range snapshots {
		totalInFlight += snap.InFlightRequests
	}
	avgInFlight := float64(totalInFlight) / float64(len(snapshots))

	cachedInFlight := float64(snapshots[bestPrefixIdx].InFlightRequests)

	// Detect if prefix cache is warm
	cacheWarm := bestPrefixScore > 0.3

	// Compute dynamic prefix weight based on load imbalance
	prefixWeight := 0.5 // default: same as 1:1

	if cacheWarm && avgInFlight > 0.5 {
		// How overloaded is the cached instance relative to average?
		// ratio = 1.0 means average, 2.0 means 2x average, etc.
		overloadRatio := cachedInFlight / avgInFlight

		if overloadRatio > 1.5 {
			// Reduce prefix weight proportionally to overload
			// At 1.5x: prefixWeight = 0.5 (unchanged)
			// At 2.0x: prefixWeight = 0.25
			// At 3.0x: prefixWeight = 0.0 (pure load balancing)
			reduction := (overloadRatio - 1.5) / 1.5 // 0 at 1.5x, 1.0 at 3.0x
			if reduction > 1.0 {
				reduction = 1.0
			}
			prefixWeight = 0.5 * (1.0 - reduction)
		}
	}

	loadWeight := 1.0 - prefixWeight

	for _, snap := range snapshots {
		ps := prefixScores[snap.ID]
		freshLoad := 1.0 / (1.0 + float64(snap.InFlightRequests))
		scores[snap.ID] = prefixWeight*ps + loadWeight*freshLoad
	}

	// Argmax with random tie-breaking
	bestScore := -1.0
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
