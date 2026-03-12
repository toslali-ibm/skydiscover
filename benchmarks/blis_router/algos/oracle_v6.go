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
	// Oracle v6: Adaptive Prefix + Size-Aware Fresh-Signal Router
	//
	// Combines three key innovations:
	// 1. From v1: Adaptive prefix weighting based on prefix spread (not fixed weights)
	//    This triggers DIFFERENT routing decisions than 1:1 only when load differs enough
	// 2. From v4: InFlightRequests as primary load signal (fresh, not stale)
	// 3. New: Size-dependent load sensitivity (elephants get much stronger load avoidance)
	//
	// The goal: match or beat v1's prefix_heavy gain (+1.1%) while adding glia_40qps gain

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// Classify request by size
	inputLen := len(req.InputTokens)
	// Size-dependent load sensitivity: larger requests suffer more from queueing
	loadAlpha := 0.5 // default
	if inputLen > 2000 {
		loadAlpha = 1.2 // elephant: VERY aggressive load avoidance
	} else if inputLen > 800 {
		loadAlpha = 0.8 // medium: moderate load sensitivity
	} else if inputLen < 200 {
		loadAlpha = 0.3 // small: mild load preference
	}

	// Compute prefix statistics
	maxPrefix := 0.0
	sumPrefix := 0.0
	for _, snap := range snapshots {
		ps := prefixScores[snap.ID]
		if ps > maxPrefix {
			maxPrefix = ps
		}
		sumPrefix += ps
	}
	n := float64(len(snapshots))
	avgPrefix := sumPrefix / n
	hasPrefix := maxPrefix > 0.05

	// Compute load statistics using InFlightRequests (fresh)
	totalInFlight := 0
	minInFlight := 1<<31 - 1
	for _, snap := range snapshots {
		totalInFlight += snap.InFlightRequests
		if snap.InFlightRequests < minInFlight {
			minInFlight = snap.InFlightRequests
		}
	}
	avgInFlight := float64(totalInFlight) / n

	for _, snap := range snapshots {
		inFlight := float64(snap.InFlightRequests)

		// Fresh load score with size-dependent decay
		loadScore := math.Exp(-loadAlpha * inFlight)

		if hasPrefix {
			ps := prefixScores[snap.ID]

			// Adaptive prefix weight based on how concentrated the cache is
			// AND how loaded the cached instance is
			prefixWeight := 0.55 // slightly above baseline 0.5

			// If prefix is concentrated (clear winner), boost prefix
			prefixSpread := maxPrefix - avgPrefix
			if prefixSpread > 0.3 {
				prefixWeight = 0.65
			}

			// BUT if the cached instance is much more loaded than average,
			// reduce prefix weight to avoid hot-spotting
			if ps > 0.5 && inFlight > avgInFlight+2.0 {
				prefixWeight = 0.40 // override: load is critical
			}

			scores[snap.ID] = prefixWeight*ps + (1.0-prefixWeight)*loadScore

		} else {
			// No prefix: pure size-aware load balancing
			// Small KV headroom bonus as tiebreaker
			kvBonus := 0.01 * (1.0 - snap.KVUtilization)
			scores[snap.ID] = loadScore + kvBonus
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
