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
	// Oracle v9: Warmup-Aware + Fresh InFlightRequests Router
	//
	// Key insight: The warmup phase (when prefix cache is cold) determines the
	// group-to-instance assignment. A bad warmup = bad performance for the rest.
	//
	// Phase 1 (WARMUP): When prefix cache is cold (no instance has >0.5 prefix score),
	// distribute requests using least-InFlightRequests to spread evenly across instances.
	// This creates a good initial cache assignment since groups arrive interleaved.
	//
	// Phase 2 (STEADY): Use prefix affinity + InFlightRequests load balancing (like v4).
	// The prefix scorer guides requests to cached instances; InFlightRequests prevents pile-on.

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// Detect cache warmth: is any instance a clear prefix match for this request?
	maxPrefix := 0.0
	for _, snap := range snapshots {
		if prefixScores[snap.ID] > maxPrefix {
			maxPrefix = prefixScores[snap.ID]
		}
	}
	cacheWarm := maxPrefix > 0.3

	if cacheWarm {
		// STEADY STATE: prefix + fresh load (same as v4, the best performer)
		for _, snap := range snapshots {
			ps := prefixScores[snap.ID]
			freshLoad := 1.0 / (1.0 + float64(snap.InFlightRequests))
			scores[snap.ID] = 0.5*ps + 0.5*freshLoad
		}
	} else {
		// WARMUP: spread requests evenly using InFlightRequests
		// This ensures different prefix groups land on different instances
		for _, snap := range snapshots {
			freshLoad := 1.0 / (1.0 + float64(snap.InFlightRequests))
			// Small stale tiebreaker
			staleTie := 0.001 / (1.0 + float64(snap.QueueDepth+snap.BatchSize))
			scores[snap.ID] = freshLoad + staleTie
		}
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
