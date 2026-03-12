#!/usr/bin/env python3
"""
Compare LOR baseline vs prefix-affinity baseline vs discovered algorithms.

Produces a 3-way comparison table showing:
  LOR (dumb) → Baseline (prefix-affinity + load-balance) → Discovered (best per framework)

This answers: how much does prefix-affinity buy over LOR, and how much
does evolution buy on top of prefix-affinity?

Usage:
    python benchmarks/blis_router/scripts/compare_lor.py \
        --lor outputs/blis_router/lor_baseline/lor_metrics.json \
        --experiment outputs/blis_router/260309_50i_twoseed

    If --lor is omitted, looks in outputs/blis_router/lor_baseline/lor_metrics.json by default.
"""

import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def pct_change(old: float, new: float) -> str:
    if old == 0:
        return "N/A"
    change = (new - old) / abs(old) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"


def pct_improvement(old: float, new: float) -> str:
    """Positive = better (lower latency)."""
    if old == 0:
        return "N/A"
    improvement = (old - new) / abs(old) * 100
    return f"+{improvement:.1f}%" if improvement >= 0 else f"{improvement:.1f}%"


def main():
    parser = argparse.ArgumentParser(description="Compare LOR vs baseline vs discovered algorithms")
    parser.add_argument(
        "--lor", type=str,
        default="outputs/blis_router/lor_baseline/lor_metrics.json",
        help="Path to lor_metrics.json",
    )
    parser.add_argument(
        "--experiment", type=str, required=True,
        help="Path to experiment directory (e.g., outputs/blis_router/260309_50i_twoseed)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Path to save comparison JSON (default: <experiment>/lor_comparison.json)",
    )
    args = parser.parse_args()

    lor_path = Path(args.lor)
    exp_dir = Path(args.experiment)

    if not lor_path.exists():
        print(f"ERROR: LOR metrics not found: {lor_path}")
        print("Run: python benchmarks/blis_router/scripts/run_lor_baseline.py first")
        sys.exit(1)

    if not exp_dir.exists():
        print(f"ERROR: Experiment directory not found: {exp_dir}")
        sys.exit(1)

    # Load LOR metrics
    lor = load_json(lor_path)

    # Load baseline from any framework's baseline_metrics.json
    baseline = None
    frameworks = {}
    for fw_dir in sorted(exp_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        bl_path = fw_dir / "baseline_metrics.json"
        bp_path = fw_dir / "best" / "best_program_info.json"
        if bl_path.exists() and baseline is None:
            baseline = load_json(bl_path)
        if bp_path.exists():
            info = load_json(bp_path)
            metrics = info.get("metrics", {})
            frameworks[fw_dir.name] = {
                "combined_score": metrics.get("combined_score"),
                "avg_e2e_ms": metrics.get("avg_e2e_ms"),
                "avg_p95_ms": metrics.get("avg_p95_ms"),
                "workloads": metrics.get("artifacts", {}).get("workload_results", {}),
            }

    if baseline is None:
        print("ERROR: No baseline_metrics.json found in any framework directory")
        sys.exit(1)

    if not frameworks:
        print("ERROR: No best_program_info.json found in any framework directory")
        sys.exit(1)

    # ── Print comparison ──
    W = 80
    print(f"\n{'='*W}")
    print("3-WAY COMPARISON: LOR vs Baseline (Prefix-Affinity) vs Discovered")
    print(f"{'='*W}")

    # Aggregate scores
    lor_score = lor["combined_score"]
    bl_score = baseline["combined_score"]
    lor_e2e = lor["avg_e2e_ms"]
    lor_p95 = lor["avg_p95_ms"]
    bl_e2e = baseline["avg_e2e_ms"]
    bl_p95 = baseline["avg_p95_ms"]

    print(f"\n{'AGGREGATE SCORES':^{W}}")
    print(f"{'-'*W}")
    header = f"{'Router':<25} {'Score':>12} {'Avg E2E':>12} {'Avg P95':>12} {'vs LOR':>10} {'vs Baseline':>12}"
    print(header)
    print(f"{'-'*W}")

    # LOR row
    print(f"{'LOR (load-balance only)':<25} {lor_score:>12.2f} {lor_e2e:>12.2f} {lor_p95:>12.2f} {'---':>10} {'---':>12}")

    # Baseline row
    print(f"{'Baseline (prefix+LB)':<25} {bl_score:>12.2f} {bl_e2e:>12.2f} {bl_p95:>12.2f} {pct_improvement(lor_score, bl_score):>10} {'---':>12}")

    # Framework rows (sorted by score descending = less negative)
    sorted_fws = sorted(frameworks.items(), key=lambda x: x[1]["combined_score"] or -999999, reverse=True)
    for fw_name, fw_data in sorted_fws:
        fw_score = fw_data["combined_score"]
        fw_e2e = fw_data["avg_e2e_ms"]
        fw_p95 = fw_data["avg_p95_ms"]
        vs_lor = pct_improvement(lor_score, fw_score)
        vs_bl = pct_improvement(bl_score, fw_score)
        print(f"{fw_name:<25} {fw_score:>12.2f} {fw_e2e:>12.2f} {fw_p95:>12.2f} {vs_lor:>10} {vs_bl:>12}")

    # ── Per-workload E2E comparison ──
    # Auto-detect workloads from LOR metrics or framework data
    lor_wl_keys = list(lor.get("workloads", {}).keys())
    workloads = lor_wl_keys if lor_wl_keys else ["cache_warmup", "load_spikes", "multiturn"]
    print(f"\n{'PER-WORKLOAD E2E LATENCY (ms)':^{W}}")
    print(f"{'-'*W}")
    wl_header = f"{'Workload':<18} {'LOR':>10} {'Baseline':>10}"
    for fw_name, _ in sorted_fws:
        wl_header += f" {fw_name:>12}"
    print(wl_header)
    print(f"{'-'*W}")

    for wl in workloads:
        lor_wl = lor.get("workloads", {}).get(wl, {}).get("e2e_ms")
        bl_wl = baseline.get(f"{wl}_e2e_ms")
        row = f"{wl:<18} {lor_wl:>10.1f} {bl_wl:>10.1f}" if lor_wl and bl_wl else f"{wl:<18} {'N/A':>10} {'N/A':>10}"
        for fw_name, fw_data in sorted_fws:
            fw_wl = fw_data.get("workloads", {}).get(wl, {}).get("e2e_ms")
            row += f" {fw_wl:>12.1f}" if fw_wl else f" {'N/A':>12}"
        print(row)

    # ── Per-workload P95 comparison ──
    print(f"\n{'PER-WORKLOAD P95 LATENCY (ms)':^{W}}")
    print(f"{'-'*W}")
    print(wl_header)
    print(f"{'-'*W}")

    for wl in workloads:
        lor_wl = lor.get("workloads", {}).get(wl, {}).get("e2e_p95_ms")
        # Baseline p95 needs to be derived from per_model data
        bl_wl = None
        if "per_model" in baseline:
            p95_vals = []
            for model_data in baseline["per_model"].values():
                wl_data = model_data.get("workloads", {}).get(wl, {})
                if wl_data.get("e2e_p95_ms") is not None:
                    p95_vals.append(wl_data["e2e_p95_ms"])
            if p95_vals:
                bl_wl = sum(p95_vals) / len(p95_vals)

        row = f"{wl:<18}"
        row += f" {lor_wl:>10.1f}" if lor_wl else f" {'N/A':>10}"
        row += f" {bl_wl:>10.1f}" if bl_wl else f" {'N/A':>10}"
        for fw_name, fw_data in sorted_fws:
            fw_wl = fw_data.get("workloads", {}).get(wl, {}).get("e2e_p95_ms")
            row += f" {fw_wl:>12.1f}" if fw_wl else f" {'N/A':>12}"
        print(row)

    # ── Value decomposition ──
    print(f"\n{'VALUE DECOMPOSITION':^{W}}")
    print(f"{'-'*W}")
    prefix_value = bl_score - lor_score  # Score improvement from prefix-affinity
    print(f"Value of prefix-affinity over LOR:    {prefix_value:>+.2f} score ({pct_improvement(lor_score, bl_score)} improvement)")
    print()

    best_fw_name, best_fw = sorted_fws[0]
    best_score = best_fw["combined_score"]
    evolution_value = best_score - bl_score
    total_value = best_score - lor_score

    print(f"Value of evolution over baseline:      {evolution_value:>+.2f} score ({pct_improvement(bl_score, best_score)} improvement)")
    print(f"Total value (LOR → best discovered):   {total_value:>+.2f} score ({pct_improvement(lor_score, best_score)} improvement)")
    print()

    if total_value != 0:
        prefix_pct = abs(prefix_value) / abs(total_value) * 100
        evolution_pct = abs(evolution_value) / abs(total_value) * 100
        print(f"Attribution:  {prefix_pct:.1f}% from prefix-affinity  |  {evolution_pct:.1f}% from evolution")
    print(f"Best discovered algorithm: {best_fw_name}")
    print(f"{'='*W}\n")

    # ── Save comparison JSON ──
    output_path = Path(args.output) if args.output else exp_dir / "lor_comparison.json"
    comparison = {
        "lor": {
            "combined_score": lor_score,
            "avg_e2e_ms": lor_e2e,
            "avg_p95_ms": lor_p95,
            "workloads": lor.get("workloads", {}),
        },
        "baseline": {
            "combined_score": bl_score,
            "avg_e2e_ms": bl_e2e,
            "avg_p95_ms": bl_p95,
        },
        "frameworks": {
            fw_name: {
                "combined_score": fw_data["combined_score"],
                "avg_e2e_ms": fw_data["avg_e2e_ms"],
                "avg_p95_ms": fw_data["avg_p95_ms"],
            }
            for fw_name, fw_data in sorted_fws
        },
        "decomposition": {
            "lor_to_baseline": prefix_value,
            "baseline_to_best": evolution_value,
            "lor_to_best": total_value,
            "prefix_affinity_attribution_pct": round(abs(prefix_value) / abs(total_value) * 100, 1) if total_value != 0 else None,
            "evolution_attribution_pct": round(abs(evolution_value) / abs(total_value) * 100, 1) if total_value != 0 else None,
            "best_framework": best_fw_name,
        },
    }
    with open(output_path, "w") as f:
        json.dump(comparison, f, indent=2)
    print(f"Comparison saved to: {output_path}")


if __name__ == "__main__":
    main()
