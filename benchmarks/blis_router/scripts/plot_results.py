"""
Plot BLIS router experiment results.

Reads comparison_table.csv (or best_program_info.json per framework) and generates:
  1. Bar chart: combined score by framework (with baseline reference line)
  2. Grouped bar chart: avg E2E and avg P95 latency by framework (includes baseline)
  3. Per-workload breakdown: E2E latency per workload per framework (includes baseline)

All charts annotate bars with percentage improvement vs baseline.

Usage:
    python benchmarks/blis_router/scripts/plot_results.py <results_dir>

Outputs saved to <results_dir>/plots/
"""

import json
import sys
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
except ImportError:
    print("matplotlib is required: pip install matplotlib")
    sys.exit(1)

DEFAULT_WORKLOADS = ["glia_40qps", "prefix_heavy"]
GLIA_WORKLOADS = ["glia_40qps", "prefix_heavy"]
WORKLOADS = DEFAULT_WORKLOADS  # overridden by detect_workloads() in main()
BASELINE_COLOR = "#999999"
FRAMEWORK_COLORS = {
    "adaevolve": "#4C72B0",
    "openevolve": "#55A868",
    "gepa_native": "#DD8452",
    "evox": "#C44E52",
}
DEFAULT_COLOR = "#8172B2"


def detect_workloads(results_dir: Path) -> list[str]:
    """Auto-detect workloads from best_program_info.json files."""
    for fw_dir in sorted(results_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        best_info = fw_dir / "best" / "best_program_info.json"
        if best_info.exists():
            with open(best_info) as f:
                data = json.load(f)
            metrics = data.get("metrics", data)
            if any(k.startswith("glia_40qps") for k in metrics):
                return GLIA_WORKLOADS
            if any(k.startswith("cache_warmup") for k in metrics):
                return DEFAULT_WORKLOADS
    return DEFAULT_WORKLOADS


def _fw_color(name: str) -> str:
    return FRAMEWORK_COLORS.get(name, DEFAULT_COLOR)


def _pct_improvement(val, baseline):
    """Percentage improvement (positive = better = lower latency)."""
    if baseline == 0:
        return 0.0
    return (baseline - val) / abs(baseline) * 100


def _pct_improvement_score(val, baseline):
    """Percentage improvement for scores (positive = better = higher score)."""
    if baseline == 0:
        return 0.0
    return (val - baseline) / abs(baseline) * 100


def load_framework_data(results_dir: Path) -> list[dict]:
    """Load per-framework results from best_program_info.json files."""
    rows = []
    for fw_dir in sorted(results_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        best_info = fw_dir / "best" / "best_program_info.json"
        if not best_info.exists():
            continue
        with open(best_info) as f:
            data = json.load(f)
        metrics = data.get("metrics", data)
        row = {
            "framework": fw_dir.name,
            "combined_score": metrics.get("combined_score"),
            "avg_e2e_ms": metrics.get("avg_e2e_ms"),
            "avg_p95_ms": metrics.get("avg_p95_ms"),
            "iteration": data.get("iteration"),
        }
        for wl in WORKLOADS:
            row[f"{wl}_e2e_ms"] = metrics.get(f"{wl}_e2e_ms")
        rows.append(row)
    return rows


def load_baseline(results_dir: Path) -> dict | None:
    """Load baseline metrics from the first framework's baseline_metrics.json."""
    for fw_dir in sorted(results_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        baseline_file = fw_dir / "baseline_metrics.json"
        if baseline_file.exists():
            with open(baseline_file) as f:
                return json.load(f)
    return None


def plot_combined_scores(rows: list[dict], baseline: dict | None, out_dir: Path):
    """Bar chart of % improvement vs baseline per framework.

    The evaluator's combined_score for evolved programs is already a percentage
    improvement (mean per-workload improvement × 100). The baseline's combined_score
    is in different units (raw -0.5*e2e - 0.5*p95). So we plot the baseline as 0%
    and each framework as its combined_score (which IS the % improvement).
    """
    # Build data: baseline at 0%, then frameworks by their % improvement score
    names = []
    pct_improvements = []
    colors = []
    if baseline is not None:
        names.append("baseline")
        pct_improvements.append(0.0)
        colors.append(BASELINE_COLOR)
    for r in sorted(rows, key=lambda x: x["combined_score"], reverse=True):
        names.append(r["framework"])
        pct_improvements.append(r["combined_score"])  # already a percentage
        colors.append(_fw_color(r["framework"]))

    fig, ax = plt.subplots(figsize=(max(7, len(names) * 1.4), 5.5))
    bars = ax.bar(names, pct_improvements, color=colors, edgecolor="white", width=0.6)

    # Annotate with score value
    for bar, pct, name in zip(bars, pct_improvements, names):
        if name == "baseline":
            label = "0% (ref)"
        else:
            label = f"+{pct:.1f}%"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                label, ha="center", va="bottom", fontsize=9, fontweight="bold")

    ax.set_ylabel("Improvement vs Baseline (%)")
    ax.set_title("BLIS Router: Latency Improvement vs Baseline")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "combined_scores.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'combined_scores.png'}")


def plot_latency_comparison(rows: list[dict], baseline: dict | None, out_dir: Path):
    """Grouped bar chart: avg E2E vs avg P95 per framework, with baseline and % annotations."""
    # Build data: baseline first, then frameworks
    names = []
    e2e = []
    p95 = []
    if baseline:
        names.append("baseline")
        e2e.append(baseline.get("avg_e2e_ms", 0))
        p95.append(baseline.get("avg_p95_ms", 0))
    for r in rows:
        names.append(r["framework"])
        e2e.append(r["avg_e2e_ms"])
        p95.append(r["avg_p95_ms"])

    baseline_e2e = e2e[0] if baseline else None
    baseline_p95 = p95[0] if baseline else None

    x = range(len(names))
    width = 0.35
    fig, ax = plt.subplots(figsize=(max(7, len(names) * 1.6), 5.5))
    bars1 = ax.bar([i - width / 2 for i in x], e2e, width, label="Avg E2E (ms)",
                   color="#4C72B0", edgecolor="white")
    bars2 = ax.bar([i + width / 2 for i in x], p95, width, label="Avg P95 (ms)",
                   color="#DD8452", edgecolor="white")

    # Annotate with % improvement vs baseline
    for i, name in enumerate(names):
        if name == "baseline" or baseline_e2e is None:
            continue
        pct_e2e = _pct_improvement(e2e[i], baseline_e2e)
        pct_p95 = _pct_improvement(p95[i], baseline_p95)
        if abs(pct_e2e) > 0.05:
            sign = "+" if pct_e2e >= 0 else ""
            ax.text(i - width / 2, e2e[i], f"{sign}{pct_e2e:.1f}%",
                    ha="center", va="bottom", fontsize=7.5, color="#4C72B0", fontweight="bold")
        if abs(pct_p95) > 0.05:
            sign = "+" if pct_p95 >= 0 else ""
            ax.text(i + width / 2, p95[i], f"{sign}{pct_p95:.1f}%",
                    ha="center", va="bottom", fontsize=7.5, color="#DD8452", fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels(names)
    ax.set_ylabel("Latency (ms)")
    ax.set_title("BLIS Router: Latency by Framework (% = improvement vs baseline)")
    ax.legend()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "latency_comparison.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'latency_comparison.png'}")


def plot_per_workload(rows: list[dict], baseline: dict | None, out_dir: Path):
    """Grouped bar chart: per-workload E2E latency for each framework, with baseline and %."""
    # Build data: baseline first, then frameworks
    all_entries = []
    if baseline:
        entry = {"framework": "baseline"}
        for wl in WORKLOADS:
            entry[f"{wl}_e2e_ms"] = baseline.get(f"{wl}_e2e_ms", 0)
        all_entries.append(entry)
    all_entries.extend(rows)

    names = [e["framework"] for e in all_entries]
    n = len(names)
    n_wl = len(WORKLOADS)
    width = 0.8 / n_wl
    all_colors = ["#4C72B0", "#55A868", "#DD8452"]
    colors = all_colors[:n_wl]

    # Baseline values per workload
    baseline_wl = {}
    if baseline:
        for wl in WORKLOADS:
            baseline_wl[wl] = baseline.get(f"{wl}_e2e_ms", 0)

    fig, ax = plt.subplots(figsize=(max(8, n * 2.2), 5.5))
    for j, wl in enumerate(WORKLOADS):
        vals = [e.get(f"{wl}_e2e_ms") or 0 for e in all_entries]
        offsets = [i + (j - n_wl / 2 + 0.5) * width for i in range(n)]
        bars = ax.bar(offsets, vals, width, label=wl.replace("_", " ").title(),
                      color=colors[j], edgecolor="white")

        # Annotate with % improvement
        if baseline_wl:
            bv = baseline_wl.get(wl, 0)
            for k, (bar, val) in enumerate(zip(bars, vals)):
                if names[k] == "baseline" or bv == 0:
                    continue
                pct = _pct_improvement(val, bv)
                if abs(pct) > 0.05:
                    sign = "+" if pct >= 0 else ""
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                            f"{sign}{pct:.1f}%", ha="center", va="bottom",
                            fontsize=6.5, fontweight="bold", color=colors[j])

    ax.set_xticks(list(range(n)))
    ax.set_xticklabels(names)
    ax.set_ylabel("E2E Latency (ms)")
    ax.set_title("BLIS Router: Per-Workload Latency (% = improvement vs baseline)")
    ax.legend()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "per_workload_latency.png", dpi=150)
    plt.close(fig)
    print(f"  Saved {out_dir / 'per_workload_latency.png'}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python plot_results.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Directory not found: {results_dir}")
        sys.exit(1)

    global WORKLOADS
    WORKLOADS = detect_workloads(results_dir)
    print(f"Detected workloads: {WORKLOADS}")

    rows = load_framework_data(results_dir)
    if not rows:
        print("No framework results found.")
        sys.exit(1)

    baseline = load_baseline(results_dir)
    if baseline:
        print(f"Baseline loaded: combined_score={baseline.get('combined_score', 'N/A')}")
    else:
        print("Warning: No baseline_metrics.json found, plots will not include baseline.")

    out_dir = results_dir / "plots"
    out_dir.mkdir(exist_ok=True)

    print(f"Plotting results for {len(rows)} frameworks...")
    plot_combined_scores(rows, baseline, out_dir)
    plot_latency_comparison(rows, baseline, out_dir)
    plot_per_workload(rows, baseline, out_dir)
    print(f"\nAll plots saved to {out_dir}/")


if __name__ == "__main__":
    main()
