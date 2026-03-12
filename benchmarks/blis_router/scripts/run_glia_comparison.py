#!/usr/bin/env python3
"""Run LOR vs Glia HRA comparison on workload_glia.yaml.

Copies each initial_program to routing.go, builds, runs simulator,
extracts metrics, and prints a side-by-side comparison.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # benchmarks/blis_router
INFERENCE_SIM_DIR = SCRIPT_DIR / "inference-sim"
ROUTING_GO_PATH = INFERENCE_SIM_DIR / "sim" / "routing.go"
POLICY_CONFIG_PATH = SCRIPT_DIR / "routing" / "routing_policy.yaml"
WORKLOAD_PATH = SCRIPT_DIR / "workloads" / os.environ.get("BLIS_WORKLOAD", "workload_glia.yaml")

MODEL_ID = "qwen/qwen2.5-7b-instruct"
SEEDS = ["42", "456"]
NUM_INSTANCES = "4"
SNAPSHOT_REFRESH = os.environ.get("BLIS_SNAPSHOT_REFRESH", "5000000")
EXTRA_ARGS = ["--hardware", "H100", "--tp", "1"]

PROGRAMS = {
    "LLQ": SCRIPT_DIR / "algos" / "initial_program_llq.go",
    "BLIS Default": SCRIPT_DIR / "initial_program.go",
    "Glia HRA": SCRIPT_DIR / "algos" / "initial_program_glia.go",
}


def extract_cluster_metrics(output: str) -> dict | None:
    """Extract cluster-level metrics from simulator JSON output."""
    json_pattern = re.compile(r'\{[^{}]*"instance_id"\s*:\s*"cluster"[^{}]*\}', re.DOTALL)
    for match in json_pattern.finditer(output):
        try:
            data = json.loads(match.group())
            if data.get("instance_id") == "cluster":
                return data
        except json.JSONDecodeError:
            continue
    # Also try line-by-line
    for line in output.splitlines():
        line = line.strip()
        if '"instance_id"' in line and '"cluster"' in line:
            try:
                data = json.loads(line)
                if data.get("instance_id") == "cluster":
                    return data
            except json.JSONDecodeError:
                continue
    return None


def run_simulation(program_path: Path, seed: str) -> dict | None:
    """Copy program to routing.go, build, run, extract metrics."""
    original = ROUTING_GO_PATH.read_text()
    try:
        # Copy program to routing.go
        shutil.copy2(program_path, ROUTING_GO_PATH)

        # Build
        result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=str(INFERENCE_SIM_DIR),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  BUILD FAILED: {result.stderr[:500]}")
            return None

        # Run simulator
        cmd = [
            "./simulation_worker", "run",
            "--model", MODEL_ID,
            "--num-instances", NUM_INSTANCES,
            "--policy-config", str(POLICY_CONFIG_PATH),
            "--workload-spec", str(WORKLOAD_PATH),
            "--snapshot-refresh-interval", SNAPSHOT_REFRESH,
            "--log", "info",
            "--seed", seed,
        ] + EXTRA_ARGS

        result = subprocess.run(
            cmd, cwd=str(INFERENCE_SIM_DIR),
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"  RUN FAILED (exit {result.returncode}): {result.stderr[:500]}")
            return None

        combined = result.stdout + "\n" + result.stderr
        metrics = extract_cluster_metrics(combined)
        if metrics is None:
            print(f"  No cluster metrics found in output")
            print(f"  stdout tail: {result.stdout[-300:]}")
            return None
        return metrics

    finally:
        # Always restore routing.go
        ROUTING_GO_PATH.write_text(original)


def main():
    print(f"Workload: {WORKLOAD_PATH.name}")
    print(f"Model: {MODEL_ID}")
    print(f"Seeds: {SEEDS}")
    print(f"Instances: {NUM_INSTANCES}")
    print(f"Snapshot refresh: {SNAPSHOT_REFRESH}us")
    print("=" * 70)

    all_results = {}

    for name, program_path in PROGRAMS.items():
        print(f"\n--- {name} ({program_path.name}) ---")
        if not program_path.exists():
            print(f"  MISSING: {program_path}")
            continue

        seed_metrics = []
        for seed in SEEDS:
            print(f"  Seed {seed}...", end=" ", flush=True)
            metrics = run_simulation(program_path, seed)
            if metrics:
                print(f"E2E={metrics.get('e2e_mean_ms', '?'):.1f}ms, "
                      f"P95={metrics.get('e2e_p95_ms', '?'):.1f}ms, "
                      f"TTFT={metrics.get('ttft_mean_ms', '?'):.1f}ms, "
                      f"TPS={metrics.get('tokens_per_sec', '?'):.1f}")
                seed_metrics.append(metrics)
            else:
                print("FAILED")

        if seed_metrics:
            avg = {}
            for key in ["e2e_mean_ms", "e2e_p95_ms", "ttft_mean_ms", "tokens_per_sec", "completed_requests"]:
                vals = [m.get(key, 0) for m in seed_metrics]
                avg[key] = sum(vals) / len(vals)
            all_results[name] = avg

    # Print comparison table
    if len(all_results) >= 2:
        print("\n" + "=" * 80)
        print(f"COMPARISON on {WORKLOAD_PATH.name}")
        print("=" * 80)

        names = list(all_results.keys())
        # Build header
        header = f"{'Metric':<25}"
        for n in names:
            header += f" {n:>12}"
        print(header)
        print("-" * len(header))

        metrics_keys = [
            ("e2e_mean_ms", "E2E Mean (ms)"),
            ("e2e_p95_ms", "E2E P95 (ms)"),
            ("ttft_mean_ms", "TTFT Mean (ms)"),
            ("tokens_per_sec", "Throughput (tok/s)"),
            ("completed_requests", "Completed Requests"),
        ]
        for key, label in metrics_keys:
            row = f"{label:<25}"
            for n in names:
                row += f" {all_results[n].get(key, 0):>12.1f}"
            print(row)

        # Score row
        print()
        row = f"{'BLIS Score':<25}"
        scores = {}
        for n in names:
            s = -0.5 * all_results[n]["e2e_mean_ms"] - 0.5 * all_results[n]["e2e_p95_ms"]
            scores[n] = s
            row += f" {s:>12.1f}"
        print(row)

        # % vs LLQ (if present) or first entry
        baseline_name = "LLQ" if "LLQ" in scores else names[0]
        baseline_score = scores[baseline_name]
        row = f"{'% vs ' + baseline_name:<25}"
        for n in names:
            pct = ((scores[n] - baseline_score) / abs(baseline_score)) * 100 if baseline_score != 0 else 0
            row += f" {pct:>+11.2f}%"
        print(row)


if __name__ == "__main__":
    main()
