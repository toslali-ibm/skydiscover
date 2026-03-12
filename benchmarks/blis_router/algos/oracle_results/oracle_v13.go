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
	// Oracle v13: Composite Hypothesis Oracle
	//
	// Combines insights from hypothesis-oracle-blis.md:
	//   H1: Fresh InFlightRequests (primary load signal)
	//   H2: SLO-adaptive prefix weight (batch=heavy prefix, realtime=heavy load)
	//   H3: Quadratic load penalty for better differentiation at high load
	//   H4: KV cache awareness — avoid near-saturated instances
	//   H6: Size-aware routing — elephants need more load sensitivity
	//
	// Architecture:
	//   Phase 1: KV viability filter (hard cutoff at 92%)
	//   Phase 2: SLO + size-adaptive weights
	//   Phase 3: Prefix + quadratic fresh load scoring with KV gradient

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// --- Phase 1: KV Viability Filter (H4) ---
	viable := make([]bool, len(snapshots))
	anyViable := false
	for i, snap := range snapshots {
		viable[i] = true
		// Hard filter: skip instances near KV saturation
		if snap.KVUtilization > 0.92 {
			viable[i] = false
		}
		if viable[i] {
			anyViable = true
		}
	}
	if !anyViable {
		// Fallback: all viable if none pass
		for i := range viable {
			viable[i] = true
		}
	}

	// --- Phase 2: Adaptive Weights (H2 + H6) ---
	prefixWeight := 0.30 // Default: lower prefix allows natural overflow/spread
	loadWeight := 0.70

	// SLO adaptation
	if req.SLOClass == "batch" {
		prefixWeight = 0.50 // Batch can tolerate queueing for cache hits
		loadWeight = 0.50
	} else if req.SLOClass == "realtime" {
		prefixWeight = 0.10 // Realtime needs lowest latency
		loadWeight = 0.90
	}

	// Size adaptation (H6): large requests need stronger load avoidance
	inputLen := float64(len(req.InputTokens))
	if inputLen > 2000 {
		prefixWeight *= 0.5 // Halve prefix weight for elephants
		loadWeight = 1.0 - prefixWeight
	}

	// --- Phase 3: Scoring (H1 + H3 + H4 gradient) ---
	for i, snap := range snapshots {
		if !viable[i] {
			scores[snap.ID] = -1e9
			continue
		}

		ps := prefixScores[snap.ID]
		f := float64(snap.InFlightRequests)

		// H1 + H3: Fresh signal with moderate quadratic penalty
		// quadratic gives better differentiation at high load:
		// linear: 1/(1+10)=0.091, 1/(1+20)=0.048 (1.9x ratio)
		// quadratic: 1/(1+0.15*100)=0.063, 1/(1+0.15*400)=0.016 (3.9x ratio)
		loadScore := 1.0 / (1.0 + 0.15*f*f)

		// H4: KV pressure gradient (soft layer on top of hard filter)
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
