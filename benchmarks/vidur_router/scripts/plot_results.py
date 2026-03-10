#!/usr/bin/env python3
"""Plot comparison charts for Vidur router experiments.

Produces 3 charts: combined_scores.png, latency_comparison.png, per_workload_latency.png.

Usage:
    python scripts/plot_results.py <results_dir>
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

WORKLOADS = ["cache_warmup", "load_spikes", "multiturn"]
FRAMEWORK_COLORS = {
    "adaevolve": "#2196F3",
    "evox": "#FF9800",
    "openevolve": "#4CAF50",
    "gepa_native": "#9C27B0",
    "topk": "#F44336",
    "best_of_n": "#795548",
    "beam_search": "#607D8B",
    "baseline": "#9E9E9E",
}
DEFAULT_COLOR = "#00BCD4"


def _color(name: str) -> str:
    return FRAMEWORK_COLORS.get(name, DEFAULT_COLOR)


def load_data(results_dir: Path):
    frameworks = sorted([
        d.name for d in results_dir.iterdir()
        if d.is_dir() and (d / "best" / "best_program_info.json").exists()
    ])

    baseline = None
    for fw in frameworks:
        p = results_dir / fw / "baseline_metrics.json"
        if p.exists():
            baseline = json.loads(p.read_text())
            break

    results = {}
    for fw in frameworks:
        info_path = results_dir / fw / "best" / "best_program_info.json"
        info = json.loads(info_path.read_text())
        results[fw] = info.get("metrics", info)

    return frameworks, baseline, results


def plot_combined_scores(frameworks, baseline, results, output_dir):
    fig, ax = plt.subplots(figsize=(10, 6))

    names = []
    scores = []
    colors = []

    if baseline:
        names.append("baseline\n(LOR)")
        scores.append(-baseline["combined_score"])  # Negate so bars go up
        colors.append(_color("baseline"))

    for fw in frameworks:
        names.append(fw)
        scores.append(-results[fw]["combined_score"])
        colors.append(_color(fw))

    bars = ax.bar(names, scores, color=colors, edgecolor="white", linewidth=0.5)

    # Add % improvement labels
    if baseline:
        bs = baseline["combined_score"]
        for i, fw in enumerate(frameworks):
            idx = i + 1  # offset by baseline bar
            fs = results[fw]["combined_score"]
            impr = ((fs - bs) / abs(bs)) * 100
            ax.text(bars[idx].get_x() + bars[idx].get_width() / 2,
                    bars[idx].get_height() + max(scores) * 0.01,
                    f"{impr:+.1f}%", ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("Combined Latency Score (lower = better)")
    ax.set_title("Vidur Router: Combined Scores by Framework")
    plt.tight_layout()
    plt.savefig(output_dir / "combined_scores.png", dpi=150)
    plt.close()
    print(f"  Wrote {output_dir / 'combined_scores.png'}")


def plot_latency_comparison(frameworks, baseline, results, output_dir):
    fig, ax = plt.subplots(figsize=(10, 6))

    all_names = []
    if baseline:
        all_names.append("baseline")
    all_names.extend(frameworks)

    x = np.arange(len(all_names))
    width = 0.35

    e2e_vals = []
    p95_vals = []
    for name in all_names:
        if name == "baseline" and baseline:
            e2e_vals.append(baseline["avg_e2e_ms"])
            p95_vals.append(baseline["avg_p95_ms"])
        else:
            e2e_vals.append(results[name]["avg_e2e_ms"])
            p95_vals.append(results[name]["avg_p95_ms"])

    ax.bar(x - width / 2, e2e_vals, width, label="Avg E2E", color="#2196F3", alpha=0.8)
    ax.bar(x + width / 2, p95_vals, width, label="Avg P95", color="#FF5722", alpha=0.8)

    ax.set_ylabel("Latency (ms)")
    ax.set_title("Vidur Router: E2E vs P95 Latency")
    ax.set_xticks(x)
    ax.set_xticklabels(all_names, rotation=15, ha="right")
    ax.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "latency_comparison.png", dpi=150)
    plt.close()
    print(f"  Wrote {output_dir / 'latency_comparison.png'}")


def plot_per_workload(frameworks, baseline, results, output_dir):
    fig, axes = plt.subplots(1, len(WORKLOADS), figsize=(5 * len(WORKLOADS), 6))
    if len(WORKLOADS) == 1:
        axes = [axes]

    all_names = []
    if baseline:
        all_names.append("baseline")
    all_names.extend(frameworks)

    for ax, wl in zip(axes, WORKLOADS):
        vals = []
        colors = []
        for name in all_names:
            if name == "baseline" and baseline:
                vals.append(baseline.get(f"{wl}_e2e_ms", 0))
            else:
                vals.append(results[name].get(f"{wl}_e2e_ms", 0))
            colors.append(_color(name))

        ax.bar(all_names, vals, color=colors, edgecolor="white", linewidth=0.5)
        ax.set_title(wl.replace("_", " ").title())
        ax.set_ylabel("E2E Latency (ms)")
        ax.tick_params(axis="x", rotation=30)

    plt.suptitle("Vidur Router: Per-Workload E2E Latency", fontsize=14)
    plt.tight_layout()
    plt.savefig(output_dir / "per_workload_latency.png", dpi=150)
    plt.close()
    print(f"  Wrote {output_dir / 'per_workload_latency.png'}")


def main():
    if len(sys.argv) < 2:
        print("Usage: plot_results.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    frameworks, baseline, results = load_data(results_dir)

    plots_dir = results_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    print("Generating plots...")
    plot_combined_scores(frameworks, baseline, results, plots_dir)
    plot_latency_comparison(frameworks, baseline, results, plots_dir)
    plot_per_workload(frameworks, baseline, results, plots_dir)
    print("Done!")


if __name__ == "__main__":
    main()
