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
	// Oracle v1: Adaptive Prefix-Load Router
	// Key insight: dynamically adjust prefix vs load weights based on cluster state.
	// When prefix scores have high variance (cache is concentrated), boost prefix weight.
	// When load is imbalanced, boost load weight. Also add overload penalty.

	// Step 1: Get raw scores from built-in scorers
	prefixScores := ws.scorers[0](req, snapshots) // prefix-affinity
	loadScores := ws.scorers[1](req, snapshots)    // load-balance

	// Step 2: Compute cluster state statistics
	var maxPrefix, sumPrefix float64
	var minLoad, maxLoad, sumLoad float64
	minLoad = math.MaxFloat64
	for _, snap := range snapshots {
		ps := prefixScores[snap.ID]
		ls := loadScores[snap.ID]
		if ps > maxPrefix {
			maxPrefix = ps
		}
		sumPrefix += ps
		if ls < minLoad {
			minLoad = ls
		}
		if ls > maxLoad {
			maxLoad = ls
		}
		sumLoad += ls
	}
	n := float64(len(snapshots))
	avgPrefix := sumPrefix / n
	prefixSpread := maxPrefix - avgPrefix // How concentrated cache is

	// Step 3: Adaptive weighting
	// Base: 50/50 (like 1:1 baseline)
	// Boost prefix when cache is concentrated (spread > 0.2 means some instance has much better cache)
	// Boost load when load is very imbalanced
	prefixWeight := 0.5
	loadWeight := 0.5

	if prefixSpread > 0.3 {
		// Strong cache signal: boost prefix to 0.75
		prefixWeight = 0.75
		loadWeight = 0.25
	} else if prefixSpread > 0.15 {
		// Moderate cache signal: boost prefix to 0.65
		prefixWeight = 0.65
		loadWeight = 0.35
	}

	// If load is very imbalanced, ensure we don't ignore it
	loadRange := maxLoad - minLoad
	if loadRange > 0.5 {
		// Very imbalanced: ensure at least 40% load weight
		if loadWeight < 0.4 {
			loadWeight = 0.4
			prefixWeight = 0.6
		}
	}

	// Step 4: Compute composite scores with overload penalty
	scores := make(map[string]float64, len(snapshots))
	avgLoad := 0
	for _, snap := range snapshots {
		avgLoad += snap.EffectiveLoad()
	}
	avgLoadF := float64(avgLoad) / n

	for _, snap := range snapshots {
		ps := prefixScores[snap.ID]
		ls := loadScores[snap.ID]
		composite := ps*prefixWeight + ls*loadWeight

		// Overload penalty: if this instance has much more load than average, penalize
		load := float64(snap.EffectiveLoad())
		if avgLoadF > 0 && load > avgLoadF*1.5 {
			penalty := (load - avgLoadF*1.5) / (avgLoadF + 1.0) * 0.3
			composite -= penalty
		}

		// KV saturation penalty: avoid instances near KV exhaustion
		if snap.KVUtilization > 0.9 {
			composite *= 0.5
		} else if snap.KVUtilization > 0.8 {
			composite *= 0.8
		}

		scores[snap.ID] = composite
	}

	// Argmax
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
