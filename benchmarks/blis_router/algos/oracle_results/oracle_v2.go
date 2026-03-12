package sim

import (
	"fmt"
	"math"
	"math/rand"
)

// RoutingSnapshot is a lightweight view of instance state for policy decisions.
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
	// Oracle v2: Exponential Load Decay + Aggressive Prefix + KV-Aware
	//
	// Key innovations over v1:
	// 1. Exponential load scoring: exp(-alpha * load/avgLoad) instead of 1/(1+load)
	//    Creates much stronger preference for lightly loaded instances
	// 2. Higher prefix weight (0.8) when cache signal exists
	// 3. KV-aware: penalize instances proportional to KV pressure for large requests
	// 4. InFlightRequests fresh-signal bonus: extra weight on the only fresh metric

	// Get prefix scores from the stateful scorer
	prefixScores := ws.scorers[0](req, snapshots)

	// Compute prefix statistics
	var maxPrefix, sumPrefix float64
	for _, snap := range snapshots {
		ps := prefixScores[snap.ID]
		if ps > maxPrefix {
			maxPrefix = ps
		}
		sumPrefix += ps
	}
	n := float64(len(snapshots))
	avgPrefix := sumPrefix / n
	hasPrefixSignal := maxPrefix > 0.05

	// Compute load statistics
	totalLoad := 0
	minLoad := math.MaxInt32
	for _, snap := range snapshots {
		load := snap.EffectiveLoad()
		totalLoad += load
		if load < minLoad {
			minLoad = load
		}
	}
	avgLoad := float64(totalLoad) / n

	// Compute scores
	scores := make(map[string]float64, len(snapshots))

	for _, snap := range snapshots {
		load := float64(snap.EffectiveLoad())

		// Exponential load score: strong preference for less loaded
		var loadScore float64
		if avgLoad > 0 {
			loadScore = math.Exp(-2.0 * load / avgLoad)
		} else {
			loadScore = 1.0
		}

		// Fresh signal bonus: InFlightRequests is the only fresh metric
		// Penalize instances with many in-flight (pile-on indicator)
		inflightPenalty := 0.0
		if snap.InFlightRequests > 0 {
			inflightPenalty = 0.05 * float64(snap.InFlightRequests)
		}

		// KV pressure: penalize proportional to utilization
		kvPenalty := 0.0
		if snap.KVUtilization > 0.7 {
			kvPenalty = (snap.KVUtilization - 0.7) * 0.5
		}

		// Combine based on whether prefix signal exists
		var composite float64
		if hasPrefixSignal {
			ps := prefixScores[snap.ID]
			// Strong prefix affinity when cache is concentrated
			prefixBonus := 0.0
			if ps > avgPrefix && avgPrefix > 0 {
				prefixBonus = (ps - avgPrefix) * 0.2
			}
			composite = 0.8*ps + 0.2*loadScore + prefixBonus - inflightPenalty - kvPenalty
		} else {
			// No prefix: pure load-based with exponential decay
			composite = loadScore - inflightPenalty - kvPenalty
		}

		scores[snap.ID] = composite
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
