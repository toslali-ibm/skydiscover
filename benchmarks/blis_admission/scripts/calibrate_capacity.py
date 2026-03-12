#!/usr/bin/env python3
"""
Phase 0: Capacity Calibration for BLIS Admission Control Experiments

Sweeps both admission workloads at multiple QPS rates to:
1. Find the saturation point (where P95 exceeds 3x the P95 at lowest rate)
2. Set SLO targets from per-class percentiles at 1.0x capacity
3. Set overload rates at 1.3x saturation
4. Output calibration.json for the evaluator

Usage:
    python benchmarks/blis_admission/scripts/calibrate_capacity.py

Output:
    benchmarks/blis_admission/calibration.json
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
POLICY_CONFIG = SCRIPT_DIR / "routing" / "routing_policy.yaml"

# Sweep configuration
RATE_GRID = [20, 40, 60, 80, 100, 120, 160]
SEED = "42"
MODEL_ID = "qwen/qwen2.5-7b-instruct"
MODEL_ARGS = ["--hardware", "H100", "--tp", "1"]
NUM_INSTANCES = os.environ.get("BLIS_NUM_INSTANCES", "4")
SNAPSHOT_REFRESH = "5000000"

WORKLOADS = [
    ("overload_mixed_slo", "workload_overload_mixed_slo.yaml"),
    ("bursty_adversary", "workload_bursty_adversary.yaml"),
]


def build_sim():
    """Build the simulator binary."""
    print("Building simulator...")
    result = subprocess.run(
        ["go", "build", "-o", "simulation_worker", "main.go"],
        cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"Build failed: {result.stderr[:500]}")
        sys.exit(1)
    print("Build OK")


def create_temp_workload(original_path: Path, rate: int) -> Path:
    """Create a temporary workload YAML with the given aggregate rate."""
    with open(original_path) as f:
        spec = yaml.safe_load(f)

    spec["aggregate_rate"] = rate
    # Scale num_requests to maintain ~60s of simulated time
    spec["num_requests"] = rate * 60

    tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    yaml.dump(spec, tmp, default_flow_style=False)
    tmp.close()
    return Path(tmp.name)


def run_sim(workload_path: Path) -> dict | None:
    """Run a single simulation and return the --results-path JSON."""
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


def compute_per_class_percentiles(requests: list[dict]) -> dict:
    """Compute per-SLO-class latency percentiles from per-request records."""
    by_class: dict[str, list[float]] = defaultdict(list)
    for req in requests:
        slo_class = req.get("slo_class", "standard")
        e2e = req.get("e2e_ms", 0)
        if e2e > 0:
            by_class[slo_class].append(e2e)

    result = {}
    for cls, values in sorted(by_class.items()):
        arr = np.array(values)
        result[cls] = {
            "count": len(values),
            "p50": float(np.percentile(arr, 50)),
            "p75": float(np.percentile(arr, 75)),
            "p90": float(np.percentile(arr, 90)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(np.mean(arr)),
        }
    return result


def find_saturation(rate_data: list[dict]) -> int:
    """Find saturation rate: where P95 exceeds 3x the P95 at lowest rate."""
    if not rate_data:
        return RATE_GRID[-1]

    baseline_p95 = rate_data[0].get("aggregate_p95", 0)
    if baseline_p95 <= 0:
        return RATE_GRID[-1]

    threshold = 3 * baseline_p95
    for entry in rate_data:
        if entry.get("aggregate_p95", 0) > threshold:
            return entry["rate"]
    return rate_data[-1]["rate"]


def main():
    build_sim()

    all_results = {}

    for wl_name, wl_file in WORKLOADS:
        print(f"\n{'='*60}")
        print(f"Sweeping: {wl_name}")
        print(f"{'='*60}")

        wl_path = SCRIPT_DIR / "workloads" / wl_file
        rate_data = []

        for rate in RATE_GRID:
            print(f"\n  Rate: {rate} QPS...")
            tmp_wl = create_temp_workload(wl_path, rate)
            try:
                data = run_sim(tmp_wl)
            finally:
                os.unlink(str(tmp_wl))

            if data is None:
                print(f"  FAILED at {rate} QPS")
                continue

            completed = data.get("completed_requests", 0)
            injected = data.get("injected_requests", 0)
            requests = data.get("requests", [])
            num_requests = rate * 60

            per_class = compute_per_class_percentiles(requests)

            # Aggregate P95 across all requests
            all_e2e = [r.get("e2e_ms", 0) for r in requests if r.get("e2e_ms", 0) > 0]
            agg_p95 = float(np.percentile(all_e2e, 95)) if all_e2e else 0

            entry = {
                "rate": rate,
                "num_requests": num_requests,
                "injected": injected,
                "completed": completed,
                "rejected": num_requests - injected,
                "aggregate_p95": agg_p95,
                "aggregate_mean": float(np.mean(all_e2e)) if all_e2e else 0,
                "per_class": per_class,
            }
            rate_data.append(entry)

            # Print summary
            print(f"    Completed: {completed}/{num_requests} ({completed/num_requests*100:.0f}%)")
            print(f"    Injected:  {injected}, Rejected: {num_requests - injected}")
            print(f"    Agg P95:   {agg_p95:.0f}ms, Mean: {entry['aggregate_mean']:.0f}ms")
            for cls, stats in sorted(per_class.items()):
                print(f"    {cls:12s}: P50={stats['p50']:.0f}ms P75={stats['p75']:.0f}ms "
                      f"P95={stats['p95']:.0f}ms (n={stats['count']})")

        if not rate_data:
            print(f"\n  ERROR: All simulations failed for {wl_name}. Cannot calibrate.")
            print(f"  Check that the simulator builds and runs correctly.")
            sys.exit(1)

        # Find saturation point
        sat_rate = find_saturation(rate_data)
        overload_rate = int(sat_rate * 1.3)

        # Set SLO targets from last healthy rate (strictly below saturation point)
        capacity_data = None
        for entry in rate_data:
            if entry["rate"] < sat_rate:
                capacity_data = entry
            else:
                break

        slo_targets = {}
        if capacity_data and capacity_data.get("per_class"):
            for cls, stats in capacity_data["per_class"].items():
                if cls == "critical":
                    slo_targets[cls] = round(stats["p75"])
                elif cls == "standard":
                    slo_targets[cls] = round(stats["p90"])
                elif cls == "sheddable":
                    slo_targets[cls] = round(stats["p90"] * 2)
                elif cls == "batch":
                    slo_targets[cls] = round(stats["p90"] * 5)
                elif cls == "background":
                    slo_targets[cls] = round(stats["p90"] * 10)
                else:
                    slo_targets[cls] = round(stats["p90"] * 2)

        all_results[wl_name] = {
            "rate_sweep": rate_data,
            "saturation_rate": sat_rate,
            "overload_rate": overload_rate,
            "slo_targets": slo_targets,
        }

        print(f"\n  >> Saturation: {sat_rate} QPS")
        print(f"  >> Overload (1.3x): {overload_rate} QPS")
        print(f"  >> SLO targets: {slo_targets}")

    # Merge SLO targets (use the more conservative — higher values)
    merged_slo = {}
    for wl_data in all_results.values():
        for cls, target in wl_data.get("slo_targets", {}).items():
            if cls not in merged_slo or target > merged_slo[cls]:
                merged_slo[cls] = target

    # Compute throughput cap
    overload_factors = [d["overload_rate"] / d["saturation_rate"] for d in all_results.values()
                        if d.get("saturation_rate", 0) > 0]
    avg_overload = sum(overload_factors) / len(overload_factors) if overload_factors else 1.3
    throughput_cap = round(1 - (avg_overload - 1) / avg_overload, 2)
    throughput_cap = max(0.70, min(0.95, throughput_cap))  # Clamp to reasonable range

    calibration = {
        "slo_targets": merged_slo,
        "throughput_cap": throughput_cap,
        "workloads": {},
    }
    for wl_name, wl_data in all_results.items():
        calibration["workloads"][wl_name] = {
            "saturation_rate": wl_data["saturation_rate"],
            "overload_rate": wl_data["overload_rate"],
        }

    # Write calibration.json
    cal_path = SCRIPT_DIR / "calibration.json"
    with open(cal_path, "w") as f:
        json.dump(calibration, f, indent=2)

    print(f"\n{'='*60}")
    print("CALIBRATION COMPLETE")
    print(f"{'='*60}")
    print(f"SLO targets: {merged_slo}")
    print(f"Throughput cap: {throughput_cap}")
    for wl_name, wl_data in all_results.items():
        print(f"  {wl_name}: saturation={wl_data['saturation_rate']} QPS, "
              f"overload={wl_data['overload_rate']} QPS")
    print(f"\nWritten to: {cal_path}")
    print("\nNEXT STEPS:")
    print("  1. Update workload YAMLs with calibrated aggregate_rate and num_requests")
    print("  2. Run pilot: uv run skydiscover-run ... -s topk -i 3")


if __name__ == "__main__":
    main()
