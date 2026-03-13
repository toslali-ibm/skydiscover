#!/usr/bin/env python3
"""
Hypothesis-driven oracle exploration for BLIS admission control.

Tests three hypotheses about why the baseline is hard to beat:
  H1: Rejected sheddable/batch should NOT count as SLO misses
  H2: Workloads need more tenants (4+) for fair shedding
  H3: SLO targets need class-aware weighting (critical >> batch)

For each hypothesis, tests oracle policies at 2x overload and computes
the delta vs baseline. A positive delta means admission control wins.

Usage:
    python benchmarks/blis_admission/scripts/hypothesis_oracle.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).parent.parent
INFERENCE_SIM_DIR = SCRIPT_DIR / "inference-sim"
ADMISSION_GO_PATH = INFERENCE_SIM_DIR / "sim" / "admission.go"
POLICY_CONFIG = SCRIPT_DIR / "routing" / "routing_policy.yaml"
WORKLOADS_DIR = SCRIPT_DIR / "workloads"

SEED = "42"
MODEL_ID = "qwen/qwen2.5-7b-instruct"
MODEL_ARGS = ["--hardware", "H100", "--tp", "1"]
NUM_INSTANCES = os.environ.get("BLIS_NUM_INSTANCES", "4")
SNAPSHOT_REFRESH = "5000000"
SATURATION_RATE = 160


# ─── Oracle policies ─────────────────────────────────────────────────────────

ORACLE_POLICIES = {
    "always-admit": """
	return true, ""
""",
    "shed-batch": """
	if sloClass == "batch" {
		a.totalRejected++
		return false, "batch-rejected"
	}
	a.totalAdmitted++
	return true, ""
""",
    "shed-batch-sheddable": """
	if sloClass == "batch" || sloClass == "sheddable" {
		a.totalRejected++
		return false, "low-priority-rejected"
	}
	a.totalAdmitted++
	return true, ""
""",
    "shed-on-load": """
	perInstanceLoad := 0
	if numInstances > 0 {
		perInstanceLoad = totalInFlight / numInstances
	}
	if perInstanceLoad > 20 {
		if sloClass == "batch" || sloClass == "sheddable" {
			a.totalRejected++
			return false, "overload-shed"
		}
	}
	a.totalAdmitted++
	return true, ""
""",
}


# ─── Workload definitions ────────────────────────────────────────────────────

# Original workloads (from files)
# New workloads for H2: more tenants, better class distribution

WORKLOAD_H2_PRIORITY_MIX = {
    "version": "1",
    "seed": 42,
    "category": "language",
    "aggregate_rate": 320,  # 2x overload
    "num_requests": 19200,
    "clients": [
        {
            "id": "rt-api",
            "tenant_id": "tenant-rt",
            "slo_class": "critical",
            "rate_fraction": 0.20,
            "streaming": True,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 128, "std_dev": 32, "min": 32, "max": 256}},
            "output_distribution": {"type": "exponential", "params": {"mean": 64}},
        },
        {
            "id": "standard-svc",
            "tenant_id": "tenant-std",
            "slo_class": "standard",
            "rate_fraction": 0.30,
            "streaming": False,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 256, "std_dev": 64, "min": 64, "max": 512}},
            "output_distribution": {"type": "exponential", "params": {"mean": 128}},
        },
        {
            "id": "sheddable-svc",
            "tenant_id": "tenant-shed",
            "slo_class": "sheddable",
            "rate_fraction": 0.20,
            "streaming": False,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 256, "std_dev": 64, "min": 64, "max": 512}},
            "output_distribution": {"type": "exponential", "params": {"mean": 128}},
        },
        {
            "id": "batch-pipeline",
            "tenant_id": "tenant-batch",
            "slo_class": "batch",
            "rate_fraction": 0.30,
            "streaming": False,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 512, "std_dev": 128, "min": 128, "max": 1024}},
            "output_distribution": {"type": "exponential", "params": {"mean": 256}},
        },
    ],
}

WORKLOAD_H2_BURSTY = {
    "version": "1",
    "seed": 42,
    "category": "language",
    "aggregate_rate": 320,  # 2x overload
    "num_requests": 19200,
    "clients": [
        {
            "id": "critical-steady-1",
            "tenant_id": "tenant-crit-1",
            "slo_class": "critical",
            "rate_fraction": 0.15,
            "streaming": True,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 128, "std_dev": 32, "min": 32, "max": 256}},
            "output_distribution": {"type": "exponential", "params": {"mean": 64}},
        },
        {
            "id": "critical-steady-2",
            "tenant_id": "tenant-crit-2",
            "slo_class": "critical",
            "rate_fraction": 0.15,
            "streaming": True,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 128, "std_dev": 32, "min": 32, "max": 256}},
            "output_distribution": {"type": "exponential", "params": {"mean": 64}},
        },
        {
            "id": "standard-svc",
            "tenant_id": "tenant-std",
            "slo_class": "standard",
            "rate_fraction": 0.20,
            "streaming": False,
            "arrival": {"process": "poisson"},
            "input_distribution": {"type": "gaussian", "params": {"mean": 256, "std_dev": 64, "min": 64, "max": 512}},
            "output_distribution": {"type": "exponential", "params": {"mean": 128}},
        },
        {
            "id": "batch-adversary",
            "tenant_id": "tenant-batch",
            "slo_class": "batch",
            "rate_fraction": 0.25,
            "streaming": False,
            "arrival": {"process": "gamma", "cv": 4.0},
            "input_distribution": {"type": "gaussian", "params": {"mean": 768, "std_dev": 256, "min": 128, "max": 2048}},
            "output_distribution": {"type": "exponential", "params": {"mean": 256}},
        },
        {
            "id": "sheddable-bulk",
            "tenant_id": "tenant-shed",
            "slo_class": "sheddable",
            "rate_fraction": 0.25,
            "streaming": False,
            "arrival": {"process": "gamma", "cv": 3.0},
            "input_distribution": {"type": "gaussian", "params": {"mean": 512, "std_dev": 128, "min": 128, "max": 1024}},
            "output_distribution": {"type": "exponential", "params": {"mean": 192}},
        },
    ],
}


# ─── Scoring functions ────────────────────────────────────────────────────────

def _parse_workload_tenants(spec: dict) -> dict:
    """Extract per-tenant fractions from workload spec."""
    fracs = defaultdict(float)
    for c in spec.get("clients", []):
        tid = c.get("tenant_id", "")
        if tid:
            fracs[tid] += c.get("rate_fraction", 0)
    return dict(fracs)


def _parse_workload_class_fractions(spec: dict) -> dict:
    """Extract per-SLO-class fractions from workload spec."""
    fracs = defaultdict(float)
    for c in spec.get("clients", []):
        cls = c.get("slo_class", "standard")
        fracs[cls] += c.get("rate_fraction", 0)
    return dict(fracs)


def score_original(requests, num_requests, spec, completed, slo_targets, throughput_cap):
    """Original scoring: rejected = SLO miss for all classes."""
    # SLO attainment
    meeting = 0
    for req in requests:
        e2e = req.get("e2e_ms", 0)
        if e2e <= 0:
            continue
        target = slo_targets.get(req.get("slo_class", "standard"), 3000)
        if e2e <= target:
            meeting += 1
    slo_att = meeting / num_requests if num_requests > 0 else 0

    # Throughput
    raw_thr = completed / num_requests if num_requests > 0 else 0
    capped_thr = min(raw_thr, throughput_cap) / throughput_cap

    # Fairness
    tenant_fracs = _parse_workload_tenants(spec)
    comp_per_tenant = defaultdict(int)
    for req in requests:
        tid = req.get("tenant_id", "")
        if tid and req.get("e2e_ms", 0) > 0:
            comp_per_tenant[tid] += 1
    n = len(tenant_fracs)
    xi = []
    for tid, frac in tenant_fracs.items():
        expected = frac * num_requests
        xi.append(comp_per_tenant.get(tid, 0) / expected if expected > 0 else 0)
    sum_xi = sum(xi)
    sum_xi2 = sum(x*x for x in xi)
    fairness = (sum_xi**2) / (n * sum_xi2) if sum_xi2 > 0 else 0

    score = 0.50 * slo_att + 0.30 * capped_thr + 0.20 * fairness
    return {"score": score, "slo_att": slo_att, "capped_thr": capped_thr, "fairness": fairness}


def score_h1_shed_tolerant(requests, num_requests, spec, completed, slo_targets, throughput_cap):
    """H1: Rejected sheddable/batch = NOT counted as SLO miss.

    SLO denominator = total - rejected_sheddable - rejected_batch.
    This treats shedding as a correct decision, not a failure.
    """
    class_fracs = _parse_workload_class_fractions(spec)

    # Count injected (completed + in-progress) per class
    injected_per_class = defaultdict(int)
    for req in requests:
        injected_per_class[req.get("slo_class", "standard")] += 1

    # Count expected per class
    expected_per_class = {}
    for cls, frac in class_fracs.items():
        expected_per_class[cls] = int(frac * num_requests)

    # Rejected per class = expected - injected
    rejected_sheddable = max(0, expected_per_class.get("sheddable", 0) - injected_per_class.get("sheddable", 0))
    rejected_batch = max(0, expected_per_class.get("batch", 0) - injected_per_class.get("batch", 0))
    shed_free = rejected_sheddable + rejected_batch

    # SLO attainment: denominator excludes shed requests
    denom = num_requests - shed_free
    meeting = 0
    for req in requests:
        e2e = req.get("e2e_ms", 0)
        if e2e <= 0:
            continue
        target = slo_targets.get(req.get("slo_class", "standard"), 3000)
        if e2e <= target:
            meeting += 1
    slo_att = meeting / denom if denom > 0 else 0

    # Throughput (same)
    raw_thr = completed / num_requests if num_requests > 0 else 0
    capped_thr = min(raw_thr, throughput_cap) / throughput_cap

    # Fairness (same)
    tenant_fracs = _parse_workload_tenants(spec)
    comp_per_tenant = defaultdict(int)
    for req in requests:
        tid = req.get("tenant_id", "")
        if tid and req.get("e2e_ms", 0) > 0:
            comp_per_tenant[tid] += 1
    n = len(tenant_fracs)
    xi = []
    for tid, frac in tenant_fracs.items():
        expected = frac * num_requests
        xi.append(comp_per_tenant.get(tid, 0) / expected if expected > 0 else 0)
    sum_xi = sum(xi)
    sum_xi2 = sum(x*x for x in xi)
    fairness = (sum_xi**2) / (n * sum_xi2) if sum_xi2 > 0 else 0

    score = 0.50 * slo_att + 0.30 * capped_thr + 0.20 * fairness
    return {"score": score, "slo_att": slo_att, "capped_thr": capped_thr, "fairness": fairness}


def score_h3_weighted_class(requests, num_requests, spec, completed, slo_targets, throughput_cap):
    """H3: Class-weighted SLO attainment.

    critical=4, standard=2, sheddable=1, batch=0.5
    SLO attainment per class = (met) / (expected). Rejected = SLO miss.
    Overall = weighted avg of per-class attainments.
    """
    CLASS_WEIGHTS = {"critical": 4.0, "standard": 2.0, "sheddable": 1.0, "batch": 0.5}

    class_fracs = _parse_workload_class_fractions(spec)

    # Per-class: expected, met
    expected = {}
    for cls, frac in class_fracs.items():
        expected[cls] = int(frac * num_requests)

    met_per_class = defaultdict(int)
    for req in requests:
        e2e = req.get("e2e_ms", 0)
        if e2e <= 0:
            continue
        cls = req.get("slo_class", "standard")
        target = slo_targets.get(cls, 3000)
        if e2e <= target:
            met_per_class[cls] += 1

    # Weighted average
    total_weight = 0
    weighted_sum = 0
    for cls, exp in expected.items():
        if exp > 0:
            w = CLASS_WEIGHTS.get(cls, 1.0)
            att = met_per_class[cls] / exp
            weighted_sum += w * att
            total_weight += w

    slo_att = weighted_sum / total_weight if total_weight > 0 else 0

    # Throughput (same)
    raw_thr = completed / num_requests if num_requests > 0 else 0
    capped_thr = min(raw_thr, throughput_cap) / throughput_cap

    # Fairness (same)
    tenant_fracs = _parse_workload_tenants(spec)
    comp_per_tenant = defaultdict(int)
    for req in requests:
        tid = req.get("tenant_id", "")
        if tid and req.get("e2e_ms", 0) > 0:
            comp_per_tenant[tid] += 1
    n = len(tenant_fracs)
    xi = []
    for tid, frac in tenant_fracs.items():
        expected_t = frac * num_requests
        xi.append(comp_per_tenant.get(tid, 0) / expected_t if expected_t > 0 else 0)
    sum_xi = sum(xi)
    sum_xi2 = sum(x*x for x in xi)
    fairness = (sum_xi**2) / (n * sum_xi2) if sum_xi2 > 0 else 0

    score = 0.50 * slo_att + 0.30 * capped_thr + 0.20 * fairness
    return {"score": score, "slo_att": slo_att, "capped_thr": capped_thr, "fairness": fairness}


def score_combined_h1h3(requests, num_requests, spec, completed, slo_targets, throughput_cap):
    """H1+H3: Shed-tolerant + class-weighted SLO.

    Rejected sheddable/batch = excluded from denominator.
    SLO uses class weights: critical=4, standard=2, sheddable=1, batch=0.5.
    """
    CLASS_WEIGHTS = {"critical": 4.0, "standard": 2.0, "sheddable": 1.0, "batch": 0.5}

    class_fracs = _parse_workload_class_fractions(spec)

    injected_per_class = defaultdict(int)
    for req in requests:
        injected_per_class[req.get("slo_class", "standard")] += 1

    expected_per_class = {}
    for cls, frac in class_fracs.items():
        expected_per_class[cls] = int(frac * num_requests)

    met_per_class = defaultdict(int)
    for req in requests:
        e2e = req.get("e2e_ms", 0)
        if e2e <= 0:
            continue
        cls = req.get("slo_class", "standard")
        target = slo_targets.get(cls, 3000)
        if e2e <= target:
            met_per_class[cls] += 1

    # For sheddable/batch: use injected (not expected) as denominator
    # Rejected sheddable/batch are simply not counted
    total_weight = 0
    weighted_sum = 0
    for cls, exp in expected_per_class.items():
        if exp <= 0:
            continue
        w = CLASS_WEIGHTS.get(cls, 1.0)
        if cls in ("sheddable", "batch"):
            denom = injected_per_class.get(cls, 0)
            if denom > 0:
                att = met_per_class[cls] / denom
            else:
                att = 1.0  # All rejected = correct decision → 100%
        else:
            # Critical/standard: rejected = SLO miss
            att = met_per_class[cls] / exp
        weighted_sum += w * att
        total_weight += w

    slo_att = weighted_sum / total_weight if total_weight > 0 else 0

    # Throughput
    raw_thr = completed / num_requests if num_requests > 0 else 0
    capped_thr = min(raw_thr, throughput_cap) / throughput_cap

    # Fairness
    tenant_fracs = _parse_workload_tenants(spec)
    comp_per_tenant = defaultdict(int)
    for req in requests:
        tid = req.get("tenant_id", "")
        if tid and req.get("e2e_ms", 0) > 0:
            comp_per_tenant[tid] += 1
    n = len(tenant_fracs)
    xi = []
    for tid, frac in tenant_fracs.items():
        expected_t = frac * num_requests
        xi.append(comp_per_tenant.get(tid, 0) / expected_t if expected_t > 0 else 0)
    sum_xi = sum(xi)
    sum_xi2 = sum(x*x for x in xi)
    fairness = (sum_xi**2) / (n * sum_xi2) if sum_xi2 > 0 else 0

    score = 0.50 * slo_att + 0.30 * capped_thr + 0.20 * fairness
    return {"score": score, "slo_att": slo_att, "capped_thr": capped_thr, "fairness": fairness}


# ─── Simulation runner ────────────────────────────────────────────────────────

def build_sim():
    result = subprocess.run(
        ["go", "build", "-o", "simulation_worker", "main.go"],
        cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0


def create_oracle_go(template: str, oracle_code: str) -> str:
    pattern = r"(// EVOLVE-BLOCK-START\n)(.*?)(	// EVOLVE-BLOCK-END)"
    return re.sub(pattern, f"// EVOLVE-BLOCK-START\n{oracle_code}\n\t// EVOLVE-BLOCK-END", template, flags=re.DOTALL)


def run_sim(workload_path: Path) -> dict | None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        results_path = tmp.name
    try:
        cmd = [
            "./simulation_worker", "run",
            "--model", MODEL_ID,
            "--num-instances", NUM_INSTANCES,
            "--policy-config", str(POLICY_CONFIG),
            "--workload-spec", str(workload_path),
            "--snapshot-refresh-interval", SNAPSHOT_REFRESH,
            "--results-path", results_path,
            "--log", "warn",
            "--seed", SEED,
        ] + MODEL_ARGS
        result = subprocess.run(cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return None
        with open(results_path) as f:
            return json.load(f)
    except Exception:
        return None
    finally:
        try:
            os.unlink(results_path)
        except OSError:
            pass


def write_temp_workload(spec: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    yaml.dump(spec, tmp, default_flow_style=False)
    tmp.close()
    return Path(tmp.name)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    cal_path = SCRIPT_DIR / "calibration.json"
    with open(cal_path) as f:
        cal = json.load(f)
    slo_targets = cal.get("slo_targets", {})
    print(f"SLO targets: {slo_targets}")

    template = (SCRIPT_DIR / "initial_program.go").read_text()
    original_admission = ADMISSION_GO_PATH.read_text()

    # Tighter SLO targets for H3 testing
    tight_slo_targets = {
        "critical": 800,     # Tight: must respond fast
        "standard": 2000,    # Moderate
        "sheddable": 8000,   # Lenient
        "batch": 15000,      # Very lenient
    }

    # Overload levels to test
    overload_factors = [1.5, 2.0, 2.5]

    # ─── Workload sets ────────────────────────────────────────────────────
    # Original workloads (from YAML files)
    orig_mixed = yaml.safe_load(open(WORKLOADS_DIR / "workload_overload_mixed_slo.yaml"))
    orig_bursty = yaml.safe_load(open(WORKLOADS_DIR / "workload_bursty_adversary.yaml"))

    workload_sets = {
        "original": {
            "mixed_slo": orig_mixed,
            "bursty": orig_bursty,
        },
        "H2_redesigned": {
            "mixed_slo": WORKLOAD_H2_PRIORITY_MIX,
            "bursty": WORKLOAD_H2_BURSTY,
        },
    }

    scoring_fns = {
        "original": score_original,
        "H1_shed_tolerant": score_h1_shed_tolerant,
        "H3_weighted_class": score_h3_weighted_class,
        "H1+H3_combined": score_combined_h1h3,
    }

    all_results = []

    try:
        for wl_set_name, wl_set in workload_sets.items():
            for factor in overload_factors:
                rate = int(SATURATION_RATE * factor)
                throughput_cap = round(max(0.50, min(0.95, 1 - (factor - 1) / factor)), 3)

                print(f"\n{'='*80}")
                print(f"  {wl_set_name} @ {factor}x ({rate} QPS), throughput_cap={throughput_cap}")
                print(f"{'='*80}")

                for policy_name, oracle_code in ORACLE_POLICIES.items():
                    # Build with oracle
                    oracle_go = create_oracle_go(template, oracle_code)
                    ADMISSION_GO_PATH.write_text(oracle_go)
                    if not build_sim():
                        print(f"  BUILD FAILED: {policy_name}")
                        continue

                    # Run on both workloads
                    wl_results = {}
                    for wl_name, wl_spec in wl_set.items():
                        spec = dict(wl_spec)
                        spec["aggregate_rate"] = rate
                        spec["num_requests"] = rate * 60

                        tmp_path = write_temp_workload(spec)
                        try:
                            data = run_sim(tmp_path)
                        finally:
                            os.unlink(str(tmp_path))

                        if data is None:
                            continue

                        completed = data.get("completed_requests", 0)
                        requests = data.get("requests", [])
                        num_req = spec["num_requests"]

                        # Score with all scoring functions
                        scores = {}
                        for score_name, score_fn in scoring_fns.items():
                            targets = tight_slo_targets if "tight" in score_name else slo_targets
                            scores[score_name] = score_fn(
                                requests, num_req, spec, completed, slo_targets, throughput_cap,
                            )

                        # Also score with tight SLOs
                        for score_name, score_fn in scoring_fns.items():
                            scores[f"{score_name}_tight"] = score_fn(
                                requests, num_req, spec, completed, tight_slo_targets, throughput_cap,
                            )

                        wl_results[wl_name] = {
                            "completed": completed,
                            "num_requests": num_req,
                            "scores": scores,
                        }

                    if wl_results:
                        row = {
                            "wl_set": wl_set_name,
                            "factor": factor,
                            "policy": policy_name,
                            "workloads": wl_results,
                        }
                        all_results.append(row)

    finally:
        ADMISSION_GO_PATH.write_text(original_admission)

    # ─── Analysis ─────────────────────────────────────────────────────────
    print(f"\n\n{'='*80}")
    print("HYPOTHESIS RESULTS — Oracle delta vs baseline (positive = oracle wins)")
    print(f"{'='*80}")

    # Group by (wl_set, factor, scoring_fn)
    for score_name in list(scoring_fns.keys()) + [f"{k}_tight" for k in scoring_fns.keys()]:
        print(f"\n{'─'*80}")
        print(f"  SCORING: {score_name}")
        print(f"{'─'*80}")
        print(f"{'WL Set':>18} {'Factor':>7} {'Policy':>25} {'Score':>7} {'Delta':>8} {'SLO':>6} {'Thr':>6} {'Fair':>6}")

        for wl_set_name in workload_sets:
            for factor in overload_factors:
                # Find baseline
                baseline_scores = {}
                for row in all_results:
                    if row["wl_set"] == wl_set_name and row["factor"] == factor and row["policy"] == "always-admit":
                        for wl_name, wl_data in row["workloads"].items():
                            baseline_scores[wl_name] = wl_data["scores"].get(score_name, {}).get("score", 0)

                if not baseline_scores:
                    continue

                baseline_avg = sum(baseline_scores.values()) / len(baseline_scores)

                for policy_name in ORACLE_POLICIES:
                    for row in all_results:
                        if row["wl_set"] == wl_set_name and row["factor"] == factor and row["policy"] == policy_name:
                            policy_scores = {}
                            policy_slo = {}
                            policy_thr = {}
                            policy_fair = {}
                            for wl_name, wl_data in row["workloads"].items():
                                s = wl_data["scores"].get(score_name, {})
                                policy_scores[wl_name] = s.get("score", 0)
                                policy_slo[wl_name] = s.get("slo_att", 0)
                                policy_thr[wl_name] = s.get("capped_thr", 0)
                                policy_fair[wl_name] = s.get("fairness", 0)

                            if policy_scores:
                                avg = sum(policy_scores.values()) / len(policy_scores)
                                delta = avg - baseline_avg
                                avg_slo = sum(policy_slo.values()) / len(policy_slo)
                                avg_thr = sum(policy_thr.values()) / len(policy_thr)
                                avg_fair = sum(policy_fair.values()) / len(policy_fair)
                                marker = " *** WIN" if delta > 0.01 else (" * win" if delta > 0 else "")
                                print(f"{wl_set_name:>18} {factor:>5.1f}x {policy_name:>25} "
                                      f"{avg:>7.4f} {delta:>+8.4f} {avg_slo:>6.3f} {avg_thr:>6.3f} {avg_fair:>6.3f}{marker}")

    # ─── Find best combinations ───────────────────────────────────────────
    print(f"\n\n{'='*80}")
    print("BEST ORACLE CONFIGURATIONS (positive delta, sorted)")
    print(f"{'='*80}")

    winners = []
    for score_name in list(scoring_fns.keys()) + [f"{k}_tight" for k in scoring_fns.keys()]:
        for wl_set_name in workload_sets:
            for factor in overload_factors:
                baseline_avg = 0
                for row in all_results:
                    if row["wl_set"] == wl_set_name and row["factor"] == factor and row["policy"] == "always-admit":
                        scores = [wl["scores"].get(score_name, {}).get("score", 0) for wl in row["workloads"].values()]
                        baseline_avg = sum(scores) / len(scores) if scores else 0

                for policy_name in ORACLE_POLICIES:
                    if policy_name == "always-admit":
                        continue
                    for row in all_results:
                        if row["wl_set"] == wl_set_name and row["factor"] == factor and row["policy"] == policy_name:
                            scores = [wl["scores"].get(score_name, {}).get("score", 0) for wl in row["workloads"].values()]
                            avg = sum(scores) / len(scores) if scores else 0
                            delta = avg - baseline_avg
                            if delta > 0:
                                winners.append({
                                    "scoring": score_name,
                                    "workloads": wl_set_name,
                                    "overload": factor,
                                    "policy": policy_name,
                                    "score": avg,
                                    "baseline": baseline_avg,
                                    "delta": delta,
                                })

    winners.sort(key=lambda x: x["delta"], reverse=True)
    for i, w in enumerate(winners[:20]):
        print(f"  {i+1:2d}. {w['scoring']:>25} | {w['workloads']:>15} | {w['overload']:.1f}x | "
              f"{w['policy']:>25} | score={w['score']:.4f} | baseline={w['baseline']:.4f} | delta={w['delta']:+.4f}")

    if not winners:
        print("  NO WINNING COMBINATIONS FOUND")
        print("  The scoring formula fundamentally cannot reward admission control with current parameters.")

    # Save
    output_path = SCRIPT_DIR / "hypothesis_results.json"
    with open(output_path, "w") as f:
        json.dump({"results": all_results, "winners": winners}, f, indent=2, default=str)
    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
