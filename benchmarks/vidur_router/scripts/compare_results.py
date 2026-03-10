#!/usr/bin/env python3
"""Compare results across frameworks for Vidur router experiments.

Reads best_program_info.json and baseline_metrics.json from each framework
directory and produces a comparison table.

Usage:
    python scripts/compare_results.py <results_dir>
"""
import csv
import json
import sys
from pathlib import Path

WORKLOADS = ["cache_warmup", "load_spikes", "multiturn"]


def load_best_result(framework_dir: Path) -> dict:
    best_info = framework_dir / "best" / "best_program_info.json"
    if not best_info.exists():
        return {}
    return json.loads(best_info.read_text())


def load_baseline(results_dir: Path, frameworks: list) -> dict:
    for fw in frameworks:
        p = results_dir / fw / "baseline_metrics.json"
        if p.exists():
            return json.loads(p.read_text())
    return {}


def _get_per_workload_p95(metrics: dict) -> dict:
    """Extract per-workload P95 from metrics or artifacts."""
    p95 = {}
    for wl in WORKLOADS:
        # Try flat keys first
        key = f"{wl}_p95_ms"
        if key in metrics:
            p95[wl] = metrics[key]
        # Try artifacts.workload_results
        elif "artifacts" in metrics:
            wr = metrics["artifacts"].get("workload_results", {})
            if wl in wr:
                p95[wl] = wr[wl].get("e2e_p95_ms", None)
    return p95


def main():
    if len(sys.argv) < 2:
        print("Usage: compare_results.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.exists():
        print(f"Error: {results_dir} not found")
        sys.exit(1)

    # Find frameworks
    frameworks = sorted([
        d.name for d in results_dir.iterdir()
        if d.is_dir() and (d / "best" / "best_program_info.json").exists()
    ])

    if not frameworks:
        print("No framework results found")
        sys.exit(1)

    print(f"Found frameworks: {frameworks}")

    # Load baseline
    baseline = load_baseline(results_dir, frameworks)

    # Load results
    results = {}
    for fw in frameworks:
        info = load_best_result(results_dir / fw)
        if info:
            results[fw] = info.get("metrics", info)

    # --- Table 1: Aggregate Scores ---
    print("\n" + "=" * 80)
    print("AGGREGATE SCORES")
    print("=" * 80)
    header = f"{'Framework':<20} {'Score':>10} {'Avg E2E':>10} {'Avg P95':>10} {'Impr%':>8}"
    print(header)
    print("-" * len(header))

    if baseline:
        bs = baseline.get("combined_score", 0)
        be = baseline.get("avg_e2e_ms", 0)
        bp = baseline.get("avg_p95_ms", 0)
        print(f"{'baseline (LOR)':<20} {bs:>10.2f} {be:>10.1f} {bp:>10.1f} {'—':>8}")

    for fw, metrics in results.items():
        score = metrics.get("combined_score", 0)
        e2e = metrics.get("avg_e2e_ms", 0)
        p95 = metrics.get("avg_p95_ms", 0)
        if baseline and baseline.get("combined_score"):
            bs = baseline["combined_score"]
            impr = ((score - bs) / abs(bs)) * 100
            print(f"{fw:<20} {score:>10.2f} {e2e:>10.1f} {p95:>10.1f} {impr:>+7.2f}%")
        else:
            print(f"{fw:<20} {score:>10.2f} {e2e:>10.1f} {p95:>10.1f} {'—':>8}")

    # --- Table 2: Per-Workload E2E ---
    print("\n" + "=" * 80)
    print("PER-WORKLOAD E2E LATENCY (ms)")
    print("=" * 80)
    wl_header = f"{'Framework':<20}" + "".join(f" {wl:>15}" for wl in WORKLOADS)
    print(wl_header)
    print("-" * len(wl_header))

    if baseline:
        vals = "".join(
            f" {baseline.get(f'{wl}_e2e_ms', 0):>15.1f}" for wl in WORKLOADS
        )
        print(f"{'baseline (LOR)':<20}{vals}")

    for fw, metrics in results.items():
        vals = "".join(
            f" {metrics.get(f'{wl}_e2e_ms', 0):>15.1f}" for wl in WORKLOADS
        )
        print(f"{fw:<20}{vals}")

    # --- Table 3: Per-Workload P95 ---
    print("\n" + "=" * 80)
    print("PER-WORKLOAD P95 LATENCY (ms)")
    print("=" * 80)
    print(wl_header)
    print("-" * len(wl_header))

    if baseline:
        bp95 = {}
        for wl in WORKLOADS:
            bp95[wl] = baseline.get(f"{wl}_p95_ms", 0)
        vals = "".join(f" {bp95.get(wl, 0):>15.1f}" for wl in WORKLOADS)
        print(f"{'baseline (LOR)':<20}{vals}")

    for fw, metrics in results.items():
        p95 = _get_per_workload_p95(metrics)
        vals = "".join(f" {p95.get(wl, 0):>15.1f}" for wl in WORKLOADS)
        print(f"{fw:<20}{vals}")

    # --- Write CSV ---
    csv_path = results_dir / "comparison_table.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        cols = [
            "framework", "combined_score", "avg_e2e_ms", "avg_p95_ms",
            "improvement_pct",
        ]
        for wl in WORKLOADS:
            cols.extend([f"{wl}_e2e_ms", f"{wl}_p95_ms"])
        writer.writerow(cols)

        # Baseline row
        if baseline:
            row = [
                "baseline",
                baseline.get("combined_score", ""),
                baseline.get("avg_e2e_ms", ""),
                baseline.get("avg_p95_ms", ""),
                0.0,
            ]
            for wl in WORKLOADS:
                row.extend([
                    baseline.get(f"{wl}_e2e_ms", ""),
                    baseline.get(f"{wl}_p95_ms", ""),
                ])
            writer.writerow(row)

        # Framework rows
        for fw, metrics in results.items():
            score = metrics.get("combined_score", 0)
            bs = baseline.get("combined_score", score) if baseline else score
            impr = ((score - bs) / abs(bs)) * 100 if bs != 0 else 0
            p95 = _get_per_workload_p95(metrics)

            row = [
                fw,
                score,
                metrics.get("avg_e2e_ms", ""),
                metrics.get("avg_p95_ms", ""),
                round(impr, 4),
            ]
            for wl in WORKLOADS:
                row.extend([
                    metrics.get(f"{wl}_e2e_ms", ""),
                    p95.get(wl, ""),
                ])
            writer.writerow(row)

    print(f"\nWrote {csv_path}")


if __name__ == "__main__":
    main()
