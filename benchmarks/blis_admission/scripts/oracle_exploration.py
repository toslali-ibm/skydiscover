#!/usr/bin/env python3
"""
Oracle Exploration: Find workload conditions where admission control beats always-admit.

Tests multiple overload levels × oracle policies to identify the scoring conditions
where intelligent admission control clearly outperforms the baseline.

Usage:
    python benchmarks/blis_admission/scripts/oracle_exploration.py
"""

import json
import os
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

# Saturation rate from calibration
SATURATION_RATE = 160

# Overload levels to test
OVERLOAD_FACTORS = [1.3, 1.5, 1.8, 2.0, 2.5]

# Oracle admission policies (name -> Go EVOLVE-BLOCK code)
ORACLE_POLICIES = {
    "always-admit": """
	return true, ""
""",
    "shed-batch-always": """
	// Always reject batch class
	if sloClass == "batch" {
		a.totalRejected++
		return false, "batch-rejected"
	}
	a.totalAdmitted++
	return true, ""
""",
    "shed-batch-sheddable-always": """
	// Always reject batch and sheddable
	if sloClass == "batch" || sloClass == "sheddable" {
		a.totalRejected++
		return false, "low-priority-rejected"
	}
	a.totalAdmitted++
	return true, ""
""",
    "shed-batch-on-load": """
	// Reject batch when in-flight load is high
	perInstanceLoad := 0
	if numInstances > 0 {
		perInstanceLoad = totalInFlight / numInstances
	}
	if sloClass == "batch" && perInstanceLoad > 20 {
		a.totalRejected++
		return false, "batch-load-shed"
	}
	a.totalAdmitted++
	return true, ""
""",
    "shed-low-priority-on-load": """
	// Reject batch+sheddable when load is high, only batch when moderate
	perInstanceLoad := 0
	if numInstances > 0 {
		perInstanceLoad = totalInFlight / numInstances
	}
	if perInstanceLoad > 30 {
		if sloClass == "batch" || sloClass == "sheddable" {
			a.totalRejected++
			return false, "overload-shed"
		}
	} else if perInstanceLoad > 20 {
		if sloClass == "batch" {
			a.totalRejected++
			return false, "moderate-load-shed"
		}
	}
	a.totalAdmitted++
	return true, ""
""",
    "class-priority-cascade": """
	// Cascading priority: shed progressively based on cluster load
	perInstanceLoad := 0
	if numInstances > 0 {
		perInstanceLoad = totalInFlight / numInstances
	}

	// Level 1: Light load - shed only batch with large inputs
	if perInstanceLoad > 15 && sloClass == "batch" && inputLen > 256 {
		a.totalRejected++
		return false, "large-batch-shed"
	}
	// Level 2: Moderate load - shed all batch
	if perInstanceLoad > 25 && sloClass == "batch" {
		a.totalRejected++
		return false, "batch-shed"
	}
	// Level 3: High load - shed sheddable too
	if perInstanceLoad > 35 && sloClass == "sheddable" {
		a.totalRejected++
		return false, "sheddable-shed"
	}
	a.totalAdmitted++
	return true, ""
""",
}

# Template for admission.go with EVOLVE-BLOCK replaced
ADMISSION_GO_TEMPLATE = None  # Loaded from file


def load_admission_template():
    """Load the admission.go template from initial_program.go."""
    initial = (SCRIPT_DIR / "initial_program.go").read_text()
    return initial


def create_oracle_admission_go(template: str, oracle_code: str) -> str:
    """Replace the EVOLVE-BLOCK with oracle code."""
    import re
    pattern = r"(// EVOLVE-BLOCK-START\n)(.*?)(	// EVOLVE-BLOCK-END)"
    replacement = f"// EVOLVE-BLOCK-START\n{oracle_code}\n\t// EVOLVE-BLOCK-END"
    return re.sub(pattern, replacement, template, flags=re.DOTALL)


def build_sim():
    """Build the simulator binary."""
    result = subprocess.run(
        ["go", "build", "-o", "simulation_worker", "main.go"],
        cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"Build failed: {result.stderr[:500]}")
        return False
    return True


def create_temp_workload(workload_name: str, rate: int) -> tuple[Path, int]:
    """Create a temporary workload YAML with the given aggregate rate.
    Returns (path, num_requests)."""
    wl_file = f"workload_{workload_name}.yaml"
    wl_path = WORKLOADS_DIR / wl_file
    with open(wl_path) as f:
        spec = yaml.safe_load(f)

    spec["aggregate_rate"] = rate
    num_requests = rate * 60
    spec["num_requests"] = num_requests

    tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    yaml.dump(spec, tmp, default_flow_style=False)
    tmp.close()
    return Path(tmp.name), num_requests


def run_sim(workload_path: Path) -> dict | None:
    """Run a single simulation and return the results JSON."""
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

        result = subprocess.run(
            cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            print(f"  Sim failed (rc={result.returncode}): {result.stderr[:200]}")
            return None

        with open(results_path) as f:
            return json.load(f)
    except Exception as exc:
        print(f"  Sim error: {exc}")
        return None
    finally:
        try:
            os.unlink(results_path)
        except OSError:
            pass


def compute_score(requests: list, num_requests: int, workload_path: Path, completed: int, slo_targets: dict, throughput_cap: float) -> dict:
    """Compute the 3-term score: SLO attainment + capped throughput + Jain fairness."""
    # SLO attainment
    meeting_slo = 0
    for req in requests:
        e2e_ms = req.get("e2e_ms", 0)
        if e2e_ms <= 0:
            continue
        slo_class = req.get("slo_class", "standard")
        target = slo_targets.get(slo_class, slo_targets.get("standard", 3000))
        if e2e_ms <= target:
            meeting_slo += 1
    slo_att = meeting_slo / num_requests if num_requests > 0 else 0

    # Capped throughput
    raw_thr = completed / num_requests if num_requests > 0 else 0
    capped_thr = min(raw_thr, throughput_cap) / throughput_cap

    # Jain fairness
    with open(workload_path) as f:
        spec = yaml.safe_load(f)
    tenant_fracs = {}
    for client in spec.get("clients", []):
        tid = client.get("tenant_id", "")
        frac = client.get("rate_fraction", 0.0)
        if tid:
            tenant_fracs[tid] = tenant_fracs.get(tid, 0) + frac

    completed_per_tenant = defaultdict(int)
    for req in requests:
        tid = req.get("tenant_id", "")
        if tid and req.get("e2e_ms", 0) > 0:
            completed_per_tenant[tid] += 1

    n = len(tenant_fracs)
    xi_values = []
    for tid, frac in tenant_fracs.items():
        expected = frac * num_requests
        xi = completed_per_tenant.get(tid, 0) / expected if expected > 0 else 0
        xi_values.append(xi)

    sum_xi = sum(xi_values)
    sum_xi_sq = sum(x * x for x in xi_values)
    fairness = (sum_xi ** 2) / (n * sum_xi_sq) if sum_xi_sq > 0 else 0.0

    score = 0.50 * slo_att + 0.30 * capped_thr + 0.20 * fairness

    # Per-class SLO attainment
    per_class_meeting = defaultdict(int)
    per_class_total = defaultdict(int)
    for req in requests:
        slo_class = req.get("slo_class", "standard")
        per_class_total[slo_class] += 1
        e2e_ms = req.get("e2e_ms", 0)
        if e2e_ms > 0:
            target = slo_targets.get(slo_class, 3000)
            if e2e_ms <= target:
                per_class_meeting[slo_class] += 1

    per_class_slo = {}
    for cls in sorted(set(list(per_class_total.keys()))):
        total = per_class_total[cls]
        met = per_class_meeting[cls]
        per_class_slo[cls] = met / total if total > 0 else 0

    # Per-class P95
    per_class_latencies = defaultdict(list)
    for req in requests:
        e2e = req.get("e2e_ms", 0)
        if e2e > 0:
            per_class_latencies[req.get("slo_class", "standard")].append(e2e)

    per_class_p95 = {}
    for cls, vals in sorted(per_class_latencies.items()):
        per_class_p95[cls] = float(np.percentile(vals, 95))

    return {
        "score": score,
        "slo_attainment": slo_att,
        "capped_throughput": capped_thr,
        "jain_fairness": fairness,
        "completed": completed,
        "num_requests": num_requests,
        "rejected": num_requests - (len(requests)),
        "per_class_slo": per_class_slo,
        "per_class_p95": per_class_p95,
        "raw_throughput": raw_thr,
    }


def main():
    # Load calibration for SLO targets
    cal_path = SCRIPT_DIR / "calibration.json"
    if cal_path.exists():
        with open(cal_path) as f:
            cal = json.load(f)
        slo_targets = cal.get("slo_targets", {})
    else:
        slo_targets = {"critical": 1243, "standard": 3055, "sheddable": 16107, "batch": 30297}

    print(f"SLO targets: {slo_targets}")

    template = load_admission_template()
    original_admission = ADMISSION_GO_PATH.read_text()

    workload_names = ["overload_mixed_slo", "bursty_adversary"]

    # Build once with always-admit first
    print("\nBuilding simulator...")
    ADMISSION_GO_PATH.write_text(template)
    if not build_sim():
        ADMISSION_GO_PATH.write_text(original_admission)
        sys.exit(1)

    all_results = {}

    try:
        for overload_factor in OVERLOAD_FACTORS:
            rate = int(SATURATION_RATE * overload_factor)
            throughput_cap = round(1 - (overload_factor - 1) / overload_factor, 3)
            throughput_cap = max(0.50, min(0.95, throughput_cap))  # Clamp

            print(f"\n{'='*70}")
            print(f"OVERLOAD: {overload_factor}x ({rate} QPS), throughput_cap={throughput_cap}")
            print(f"{'='*70}")

            for policy_name, oracle_code in ORACLE_POLICIES.items():
                print(f"\n  --- Policy: {policy_name} ---")

                # Write oracle admission.go
                oracle_go = create_oracle_admission_go(template, oracle_code)
                ADMISSION_GO_PATH.write_text(oracle_go)
                if not build_sim():
                    print(f"  BUILD FAILED for {policy_name}")
                    continue

                policy_results = {}
                for wl_name in workload_names:
                    tmp_wl, num_req = create_temp_workload(wl_name, rate)
                    try:
                        data = run_sim(tmp_wl)
                    finally:
                        os.unlink(str(tmp_wl))

                    if data is None:
                        print(f"    {wl_name}: FAILED")
                        continue

                    completed = data.get("completed_requests", 0)
                    injected = data.get("injected_requests", 0)
                    requests = data.get("requests", [])

                    result = compute_score(
                        requests, num_req, WORKLOADS_DIR / f"workload_{wl_name}.yaml",
                        completed, slo_targets, throughput_cap,
                    )
                    policy_results[wl_name] = result

                    print(f"    {wl_name}: score={result['score']:.4f} "
                          f"(slo={result['slo_attainment']:.3f}, thr={result['capped_throughput']:.3f}, "
                          f"fair={result['jain_fairness']:.3f}) "
                          f"completed={completed}/{num_req} rejected={result['rejected']}")
                    for cls, pct in sorted(result.get("per_class_slo", {}).items()):
                        p95 = result.get("per_class_p95", {}).get(cls, 0)
                        print(f"      {cls:12s}: SLO met {pct*100:.1f}%, P95={p95:.0f}ms")

                if policy_results:
                    avg_score = sum(r["score"] for r in policy_results.values()) / len(policy_results)
                    key = (overload_factor, policy_name)
                    all_results[f"{overload_factor}x_{policy_name}"] = {
                        "overload_factor": overload_factor,
                        "rate": rate,
                        "throughput_cap": throughput_cap,
                        "policy": policy_name,
                        "avg_score": avg_score,
                        "workloads": policy_results,
                    }
                    print(f"    >> AVG SCORE: {avg_score:.4f}")

    finally:
        # Restore original admission.go
        ADMISSION_GO_PATH.write_text(original_admission)
        print("\n[Restored admission.go]")

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY: Score by Overload × Policy")
    print(f"{'='*70}")
    print(f"{'Overload':>10} {'Policy':>30} {'Score':>8} {'SLO':>7} {'Thr':>7} {'Fair':>7} {'vs Baseline':>12}")
    print("-" * 85)

    for factor in OVERLOAD_FACTORS:
        baseline_key = f"{factor}x_always-admit"
        baseline_score = all_results.get(baseline_key, {}).get("avg_score", 0)

        for policy_name in ORACLE_POLICIES:
            key = f"{factor}x_{policy_name}"
            if key not in all_results:
                continue
            r = all_results[key]
            avg_slo = sum(w["slo_attainment"] for w in r["workloads"].values()) / len(r["workloads"])
            avg_thr = sum(w["capped_throughput"] for w in r["workloads"].values()) / len(r["workloads"])
            avg_fair = sum(w["jain_fairness"] for w in r["workloads"].values()) / len(r["workloads"])
            delta = r["avg_score"] - baseline_score
            marker = " ***" if delta > 0.01 else (" **" if delta > 0 else "")
            print(f"{factor:>8.1f}x {policy_name:>30} {r['avg_score']:>8.4f} {avg_slo:>7.3f} "
                  f"{avg_thr:>7.3f} {avg_fair:>7.3f} {delta:>+10.4f}{marker}")
        print()

    # Find best oracle per overload level
    print("\nBEST ORACLE PER OVERLOAD LEVEL:")
    for factor in OVERLOAD_FACTORS:
        best_key = None
        best_score = -1
        baseline_score = 0
        for policy_name in ORACLE_POLICIES:
            key = f"{factor}x_{policy_name}"
            if key in all_results:
                if policy_name == "always-admit":
                    baseline_score = all_results[key]["avg_score"]
                if all_results[key]["avg_score"] > best_score:
                    best_score = all_results[key]["avg_score"]
                    best_key = key
        if best_key:
            r = all_results[best_key]
            delta = best_score - baseline_score
            print(f"  {factor}x: {r['policy']:>30} = {best_score:.4f} (baseline={baseline_score:.4f}, delta={delta:+.4f})")

    # Save results
    output_path = SCRIPT_DIR / "oracle_exploration_results.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
