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
	// Oracle v10: Load-Overflow Router
	//
	// Key insight: The 1:1 baseline ALWAYS routes to the cached instance because
	// binary prefix scores (1.0 vs 0.0) dominate any 50/50 weighted combination.
	// With skewed traffic (e.g., 70% to one group), this creates an irrecoverable
	// hotspot on the cached instance.
	//
	// Strategy: Monitor load imbalance using InFlightRequests (fresh signal).
	// When the cached instance is significantly more loaded than the least-loaded
	// instance, OVERRIDE prefix affinity and route to the least-loaded instead.
	// This trades a cache miss for much better load balance.
	//
	// Decision rule:
	//   - Find the best prefix instance (highest prefix score)
	//   - Find the least-loaded instance (lowest InFlightRequests)
	//   - If they're the same → route there (no conflict)
	//   - If cached_inFlight > min_inFlight + overflow_threshold → route to least-loaded
	//   - Otherwise → route to cached (prefix hit is worth the slightly higher load)
	//
	// The threshold controls the cache-miss vs load-balance tradeoff.
	// Too low = too many unnecessary cache misses; too high = hotspot not resolved.

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

	// Find least loaded instance (by InFlightRequests, fresh signal)
	minInFlight := snapshots[0].InFlightRequests
	for _, snap := range snapshots {
		if snap.InFlightRequests < minInFlight {
			minInFlight = snap.InFlightRequests
		}
	}

	cachedInFlight := snapshots[bestPrefixIdx].InFlightRequests

	// Overflow threshold: how much extra load on cached instance before we spill
	// Setting this to 2 means: if cached instance has 2+ more in-flight requests
	// than the least loaded, prefer load balance over cache hit
	overflowThreshold := 2

	// Detect if prefix cache is warm for this request
	cacheWarm := bestPrefixScore > 0.3

	if cacheWarm && cachedInFlight > minInFlight+overflowThreshold {
		// OVERFLOW: cached instance is overloaded, route to least-loaded
		// Use pure load-based scoring (ignore prefix)
		for _, snap := range snapshots {
			freshLoad := 1.0 / (1.0 + float64(snap.InFlightRequests))
			staleTie := 0.001 / (1.0 + float64(snap.QueueDepth+snap.BatchSize))
			scores[snap.ID] = freshLoad + staleTie
		}
	} else {
		// NORMAL: use prefix affinity + fresh load (like v4)
		for _, snap := range snapshots {
			ps := prefixScores[snap.ID]
			freshLoad := 1.0 / (1.0 + float64(snap.InFlightRequests))
			scores[snap.ID] = 0.5*ps + 0.5*freshLoad
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
