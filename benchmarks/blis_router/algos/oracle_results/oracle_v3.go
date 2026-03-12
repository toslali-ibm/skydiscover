package sim

import (
	"fmt"
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
	// Oracle v3: Two-Tier Filtered Routing
	//
	// Strategy: Filter instances by load, then pick best prefix match among acceptable ones.
	// This prevents cache-chasing from creating tail latency, while maximizing cache hits
	// among load-balanced candidates.
	//
	// Tier 1: Accept instances with load <= minLoad + slack
	// Tier 2: Among accepted, pick highest prefix score
	// Fallback: If no prefix signal, pick least InFlightRequests (fresh signal)

	// Get prefix scores
	prefixScores := ws.scorers[0](req, snapshots)

	// Find minimum load (using InFlightRequests as primary, it's fresh)
	minInFlight := snapshots[0].InFlightRequests
	minEffective := snapshots[0].EffectiveLoad()
	for _, snap := range snapshots {
		if snap.InFlightRequests < minInFlight {
			minInFlight = snap.InFlightRequests
		}
		if snap.EffectiveLoad() < minEffective {
			minEffective = snap.EffectiveLoad()
		}
	}

	// Check if prefix signal exists
	maxPrefix := 0.0
	for _, snap := range snapshots {
		if prefixScores[snap.ID] > maxPrefix {
			maxPrefix = prefixScores[snap.ID]
		}
	}
	hasPrefixSignal := maxPrefix > 0.05

	// Load slack: how many extra requests we tolerate for cache hits
	// Adaptive: tighter slack when load is high, looser when load is low
	slack := 3
	if minEffective > 10 {
		slack = 2
	}
	if minEffective > 20 {
		slack = 1
	}

	scores := make(map[string]float64, len(snapshots))

	if hasPrefixSignal {
		// Two-tier: filter by load, then maximize prefix
		for _, snap := range snapshots {
			loadOk := snap.EffectiveLoad() <= minEffective+slack
			ps := prefixScores[snap.ID]

			if loadOk {
				// Accepted: score = prefix affinity + small load bonus
				loadBonus := 0.01 * float64(minEffective+slack-snap.EffectiveLoad())
				scores[snap.ID] = ps + loadBonus
			} else {
				// Rejected: very low score (only chosen if all others also rejected)
				scores[snap.ID] = -1.0 + ps*0.01
			}
		}
	} else {
		// No prefix: use InFlightRequests (fresh) + small EffectiveLoad tiebreaker
		for _, snap := range snapshots {
			// Primary: minimize InFlightRequests (fresh signal)
			inflightScore := 1.0 / (1.0 + float64(snap.InFlightRequests))
			// Secondary: minimize EffectiveLoad (stale but comprehensive)
			loadScore := 0.1 / (1.0 + float64(snap.EffectiveLoad()))
			// Tertiary: prefer instances with more KV headroom
			kvScore := 0.01 * (1.0 - snap.KVUtilization)
			scores[snap.ID] = inflightScore + loadScore + kvScore
		}
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
