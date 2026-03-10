#!/usr/bin/env python3
"""Analyze search effort and efficiency for Vidur router experiments.

Produces effort metrics, convergence curves, and efficiency charts.

Usage:
    python scripts/analyze_effort.py <results_dir>
"""
import csv
import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

WORKLOADS = ["cache_warmup", "load_spikes", "multiturn"]


def extract_iteration_times_from_log(fw_dir: Path) -> list:
    """Extract per-iteration durations from log files."""
    log_dir = fw_dir / "logs"
    if not log_dir.exists():
        log_dir = fw_dir
    log_files = sorted(log_dir.glob("*.log")) + sorted(fw_dir.glob("run.log"))
    if not log_files:
        return []

    times = []
    for log_file in log_files:
        text = log_file.read_text(errors="replace")
        # Pattern: "Iteration N: ... completed in Xs"
        for m in re.finditer(r"Iteration\s+(\d+):.*completed in\s+([\d.]+)s", text):
            times.append(float(m.group(2)))
        if times:
            break

        # Fallback: timestamp-based
        timestamps = []
        for m in re.finditer(
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),\d+\s+.*Iteration\s+(\d+):", text
        ):
            from datetime import datetime
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            timestamps.append((int(m.group(2)), ts))
        if len(timestamps) >= 2:
            timestamps.sort()
            for i in range(1, len(timestamps)):
                dt = (timestamps[i][1] - timestamps[i - 1][1]).total_seconds()
                if 0 < dt < 3600:
                    times.append(dt)

    return times


def extract_score_trajectory(fw_dir: Path) -> list:
    """Extract best-score-so-far trajectory from logs."""
    log_files = sorted((fw_dir / "logs").glob("*.log")) if (fw_dir / "logs").exists() else []
    log_files += sorted(fw_dir.glob("run.log"))
    if not log_files:
        return []

    scores = []
    for log_file in log_files:
        text = log_file.read_text(errors="replace")
        for m in re.finditer(r"best program score[:\s]+([-\d.]+)", text, re.IGNORECASE):
            try:
                scores.append(float(m.group(1)))
            except ValueError:
                pass
        if scores:
            break

    # Convert to best-so-far
    trajectory = []
    best = float("-inf")
    for s in scores:
        best = max(best, s)
        trajectory.append(best)
    return trajectory


def load_checkpoint_programs(fw_dir: Path) -> int:
    """Count unique programs in latest checkpoint."""
    cp_dir = fw_dir / "checkpoints"
    if not cp_dir.exists():
        return 0
    checkpoints = sorted(cp_dir.iterdir())
    if not checkpoints:
        return 0
    prog_dir = checkpoints[-1] / "programs"
    if not prog_dir.exists():
        return 0
    return len(list(prog_dir.glob("*.json")))


def analyze_framework(fw_dir: Path, baseline_score: float) -> dict:
    """Analyze a single framework's search effort."""
    # Load best result
    best_info_path = fw_dir / "best" / "best_program_info.json"
    if not best_info_path.exists():
        return {}
    best_info = json.loads(best_info_path.read_text())
    metrics = best_info.get("metrics", best_info)
    score = metrics.get("combined_score", 0)
    iteration = best_info.get("iteration", 0)

    # Iteration times
    iter_times = extract_iteration_times_from_log(fw_dir)
    total_time = sum(iter_times) if iter_times else 0
    n_iters = len(iter_times) if iter_times else iteration

    # Score trajectory
    trajectory = extract_score_trajectory(fw_dir)

    # Population
    pop_size = load_checkpoint_programs(fw_dir)

    # Improvement
    improvement = ((score - baseline_score) / abs(baseline_score) * 100) if baseline_score != 0 else 0

    return {
        "framework": fw_dir.name,
        "best_score": score,
        "best_iteration": iteration,
        "total_iterations": n_iters,
        "wall_time_s": total_time,
        "avg_iter_time_s": np.mean(iter_times) if iter_times else 0,
        "median_iter_time_s": np.median(iter_times) if iter_times else 0,
        "min_iter_time_s": min(iter_times) if iter_times else 0,
        "max_iter_time_s": max(iter_times) if iter_times else 0,
        "improvement_pct": improvement,
        "population_size": pop_size,
        "trajectory": trajectory,
        "iter_times": iter_times,
        "improvement_per_min": improvement / (total_time / 60) if total_time > 0 else 0,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: analyze_effort.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    frameworks = sorted([
        d.name for d in results_dir.iterdir()
        if d.is_dir() and (d / "best" / "best_program_info.json").exists()
    ])

    if not frameworks:
        print("No framework results found")
        sys.exit(1)

    # Load baseline
    baseline_score = 0
    for fw in frameworks:
        p = results_dir / fw / "baseline_metrics.json"
        if p.exists():
            baseline_score = json.loads(p.read_text()).get("combined_score", 0)
            break

    # Analyze each framework
    analyses = {}
    for fw in frameworks:
        result = analyze_framework(results_dir / fw, baseline_score)
        if result:
            analyses[fw] = result

    # --- Print Table ---
    print("\n" + "=" * 100)
    print("SEARCH EFFORT ANALYSIS")
    print("=" * 100)
    header = (
        f"{'Framework':<18} {'Iters':>6} {'Wall(s)':>8} {'Avg(s)':>7} "
        f"{'Med(s)':>7} {'Pop':>5} {'Impr%':>8} {'Impr/min':>9} {'BestIter':>9}"
    )
    print(header)
    print("-" * len(header))
    for fw, a in analyses.items():
        print(
            f"{fw:<18} {a['total_iterations']:>6} {a['wall_time_s']:>8.0f} "
            f"{a['avg_iter_time_s']:>7.1f} {a['median_iter_time_s']:>7.1f} "
            f"{a['population_size']:>5} {a['improvement_pct']:>+7.2f}% "
            f"{a['improvement_per_min']:>8.4f} {a['best_iteration']:>9}"
        )

    # --- Write CSV ---
    csv_path = results_dir / "effort_analysis.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        cols = [
            "framework", "total_iterations", "wall_time_s",
            "avg_iter_time_s", "median_iter_time_s", "min_iter_time_s", "max_iter_time_s",
            "population_size", "improvement_pct", "improvement_per_min", "best_iteration",
        ]
        writer.writerow(cols)
        for fw, a in analyses.items():
            writer.writerow([a.get(c, "") for c in cols])
    print(f"\nWrote {csv_path}")

    # --- Write JSON ---
    json_path = results_dir / "effort_analysis.json"
    # Remove non-serializable fields for JSON
    json_data = {}
    for fw, a in analyses.items():
        entry = {k: v for k, v in a.items() if k not in ("iter_times", "trajectory")}
        entry["trajectory"] = a.get("trajectory", [])
        entry["iter_times"] = a.get("iter_times", [])
        json_data[fw] = entry
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"Wrote {json_path}")

    # --- Plots ---
    plots_dir = results_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    # 1. Iteration duration boxplot
    fig, ax = plt.subplots(figsize=(10, 6))
    data = [a["iter_times"] for a in analyses.values() if a["iter_times"]]
    labels = [fw for fw, a in analyses.items() if a["iter_times"]]
    if data:
        ax.boxplot(data, labels=labels)
        ax.set_ylabel("Iteration Duration (s)")
        ax.set_title("Vidur Router: Iteration Duration Distribution")
        plt.tight_layout()
        plt.savefig(plots_dir / "iteration_duration_boxplot.png", dpi=150)
    plt.close()

    # 2. Convergence curves
    fig, ax = plt.subplots(figsize=(10, 6))
    for fw, a in analyses.items():
        if a["trajectory"]:
            ax.plot(a["trajectory"], label=fw, linewidth=2)
    if baseline_score:
        ax.axhline(y=baseline_score, color="gray", linestyle="--", label="baseline")
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Best Score So Far")
    ax.set_title("Vidur Router: Convergence Curves")
    ax.legend()
    plt.tight_layout()
    plt.savefig(plots_dir / "convergence_curves.png", dpi=150)
    plt.close()

    # 3. Effort vs improvement
    fig, ax = plt.subplots(figsize=(10, 6))
    for fw, a in analyses.items():
        ax.scatter(
            a["wall_time_s"] / 60,
            a["improvement_pct"],
            s=max(50, a["population_size"] * 10),
            label=fw,
            alpha=0.7,
        )
    ax.set_xlabel("Wall Time (min)")
    ax.set_ylabel("Improvement (%)")
    ax.set_title("Vidur Router: Effort vs Improvement (bubble = population)")
    ax.legend()
    plt.tight_layout()
    plt.savefig(plots_dir / "effort_vs_improvement.png", dpi=150)
    plt.close()

    # 4. Search efficiency
    fig, ax = plt.subplots(figsize=(10, 6))
    fws = list(analyses.keys())
    efficiencies = [analyses[fw]["improvement_per_min"] for fw in fws]
    ax.bar(fws, efficiencies, color=["#2196F3", "#FF9800", "#4CAF50", "#9C27B0"][:len(fws)])
    ax.set_ylabel("Improvement % per Minute")
    ax.set_title("Vidur Router: Search Efficiency")
    plt.tight_layout()
    plt.savefig(plots_dir / "search_efficiency.png", dpi=150)
    plt.close()

    print(f"Wrote 4 plots to {plots_dir}/")
    print("Done!")


if __name__ == "__main__":
    main()
