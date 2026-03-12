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
	// Oracle v5: Composite Three-Regime Router
	//
	// Three distinct routing regimes based on cluster state:
	// 1. PREFIX regime: Near-maximal prefix affinity with minimal load pressure
	// 2. BURST regime: InFlightRequests-only exponential balancing
	// 3. STEADY-STATE regime: Size-aware KV+load blend
	//
	// Each regime uses exponential load decay (exp(-alpha*load)) instead of
	// 1/(1+load) for stronger preference toward lightly loaded instances.

	prefixScores := ws.scorers[0](req, snapshots)
	scores := make(map[string]float64, len(snapshots))

	// === REGIME DETECTION ===

	// 1. Prefix detection
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
	hasPrefix := maxPrefix > 0.1
	prefixConcentration := 0.0
	if hasPrefix {
		avgPrefix := sumPrefix / n
		if avgPrefix > 0 {
			prefixConcentration = maxPrefix / avgPrefix
		}
	}

	// 2. Burst detection (fresh signal only)
	totalInFlight := 0
	minInFlight := 1<<31 - 1
	maxInFlight := 0
	for _, snap := range snapshots {
		totalInFlight += snap.InFlightRequests
		if snap.InFlightRequests > maxInFlight {
			maxInFlight = snap.InFlightRequests
		}
		if snap.InFlightRequests < minInFlight {
			minInFlight = snap.InFlightRequests
		}
	}
	avgInFlight := float64(totalInFlight) / n
	inFlightSkew := float64(maxInFlight - minInFlight)
	isBurst := avgInFlight > 2.5 || inFlightSkew > 4.0

	// 3. Request size classification
	inputLen := len(req.InputTokens)
	isElephant := inputLen > 1500
	isMedium := inputLen > 500 && inputLen <= 1500

	// === SCORING ===

	for _, snap := range snapshots {
		inFlight := float64(snap.InFlightRequests)
		load := float64(snap.EffectiveLoad())
		kvFree := 1.0 - snap.KVUtilization
		pScore := prefixScores[snap.ID]

		if hasPrefix {
			// REGIME 1: Prefix-dominant
			prefixWeight := 0.80
			if prefixConcentration > 2.0 {
				prefixWeight = 0.90
			}

			var loadComponent float64
			if isBurst {
				loadComponent = math.Exp(-0.7 * inFlight)
			} else {
				loadComponent = math.Exp(-0.4 * load)
			}
			scores[snap.ID] = prefixWeight*pScore + (1.0-prefixWeight)*loadComponent

		} else if isBurst {
			// REGIME 2: Burst mode -- fresh signals only
			if isElephant {
				scores[snap.ID] = math.Exp(-1.0 * inFlight)
			} else {
				scores[snap.ID] = math.Exp(-0.6 * inFlight)
			}

		} else {
			// REGIME 3: Steady state -- full signal blend with exponential load
			expLoad := math.Exp(-0.5 * load)

			if isElephant {
				scores[snap.ID] = 0.35*kvFree + 0.65*expLoad
			} else if isMedium {
				scores[snap.ID] = 0.15*kvFree + 0.85*expLoad
			} else {
				scores[snap.ID] = 0.05*kvFree + 0.95*expLoad
			}
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
