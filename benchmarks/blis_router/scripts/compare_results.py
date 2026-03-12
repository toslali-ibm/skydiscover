"""
Compare results across search frameworks for the BLIS router benchmark.

Outputs:
  - Console: summary table, per-workload table (with baseline), per-model baseline breakdown
  - CSV: comparison_table.csv (aggregate + per-workload for all frameworks and baseline)

Usage: python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>/
"""

import json
import sys
from pathlib import Path

DEFAULT_WORKLOADS = ["glia_40qps", "prefix_heavy"]
GLIA_WORKLOADS = ["glia_40qps", "prefix_heavy"]
WORKLOADS = DEFAULT_WORKLOADS  # overridden by detect_workloads() in main()


def detect_workloads(results_dir: Path, frameworks: list[str]) -> list[str]:
    """Auto-detect workload names from best_program_info.json metrics keys."""
    for fw in frameworks:
        best_info = results_dir / fw / "best" / "best_program_info.json"
        if best_info.exists():
            with open(best_info) as f:
                data = json.load(f)
            metrics = data.get("metrics", data)
            # Check for glia workload keys
            if any(k.startswith("glia_40qps") for k in metrics):
                return GLIA_WORKLOADS
            if any(k.startswith("cache_warmup") for k in metrics):
                return DEFAULT_WORKLOADS
    return DEFAULT_WORKLOADS


def load_best_result(framework_dir: Path) -> dict | None:
    best_info = framework_dir / "best" / "best_program_info.json"
    if not best_info.exists():
        return None
    with open(best_info) as f:
        return json.load(f)


def load_baseline(results_dir: Path, frameworks: list[str]) -> dict | None:
    """Load baseline metrics from the first framework's baseline_metrics.json."""
    for fw in frameworks:
        bf = results_dir / fw / "baseline_metrics.json"
        if bf.exists():
            with open(bf) as f:
                return json.load(f)
    return None


def _fmt(val, fmt=".2f"):
    return f"{val:{fmt}}" if isinstance(val, (int, float)) else "N/A"


def _pct(val, baseline):
    if not isinstance(val, (int, float)) or not isinstance(baseline, (int, float)) or baseline == 0:
        return ""
    return f"{(baseline - val) / abs(baseline) * 100:+.1f}%"


def _get_per_workload_p95(metrics: dict) -> dict:
    """Extract per-workload P95 from artifacts.workload_results if available."""
    artifacts = metrics.get("artifacts", {})
    wl_results = artifacts.get("workload_results", {})
    out = {}
    for wl in WORKLOADS:
        wl_data = wl_results.get(wl, {})
        out[f"{wl}_p95_ms"] = wl_data.get("e2e_p95_ms")
    return out


def _get_baseline_per_workload_p95(baseline: dict) -> dict:
    """Compute per-workload P95 from baseline per_model data."""
    per_model = baseline.get("per_model")
    if not per_model:
        return {}
    out = {}
    for wl in WORKLOADS:
        vals = []
        for model_data in per_model.values():
            wl_data = model_data.get("workloads", {}).get(wl, {})
            p95 = wl_data.get("e2e_p95_ms")
            if p95 is not None:
                vals.append(p95)
        out[f"{wl}_p95_ms"] = sum(vals) / len(vals) if vals else None
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python compare_results.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Directory not found: {results_dir}")
        sys.exit(1)

    frameworks = sorted(
        p.name for p in results_dir.iterdir()
        if p.is_dir() and (p / "best").is_dir()
    )

    if not frameworks:
        print("No framework results found.")
        sys.exit(1)

    global WORKLOADS
    WORKLOADS = detect_workloads(results_dir, frameworks)
    baseline = load_baseline(results_dir, frameworks)

    # --- Table 1: Aggregate scores ---
    print(f"\n{'Framework':<20} {'Score':>12} {'Avg E2E':>10} {'Avg P95':>10} {'% vs BL':>10} {'Iter':>6}")
    print("-" * 72)

    if baseline:
        bl_score = baseline.get("combined_score")
        print(f"{'*baseline*':<20} {_fmt(bl_score):>12} {_fmt(baseline.get('avg_e2e_ms')):>10} "
              f"{_fmt(baseline.get('avg_p95_ms')):>10} {'---':>10} {'---':>6}")

    for fw in frameworks:
        result = load_best_result(results_dir / fw)
        if result is None:
            print(f"{fw:<20} {'N/A':>12}")
            continue
        metrics = result.get("metrics", result)
        score = metrics.get("combined_score")
        # The evaluator's combined_score for evolved programs is already a percentage
        # improvement vs baseline (0% = same as baseline, +X% = better).
        # Display it directly as the "% vs BL" column.
        pct = f"{score:+.1f}%" if isinstance(score, (int, float)) else ""
        print(f"{fw:<20} {_fmt(score):>12} {_fmt(metrics.get('avg_e2e_ms')):>10} "
              f"{_fmt(metrics.get('avg_p95_ms')):>10} {pct:>10} {str(result.get('iteration', 'N/A')):>6}")

    # --- Table 2: Per-workload E2E latency ---
    print(f"\nPer-Workload E2E Latency (ms):")
    hdr = f"{'Framework':<20}"
    for wl in WORKLOADS:
        hdr += f" {wl:>16}"
    print(hdr)
    print("-" * (20 + 17 * len(WORKLOADS)))

    bl_wl = {}
    if baseline:
        row = f"{'*baseline*':<20}"
        for wl in WORKLOADS:
            val = baseline.get(f"{wl}_e2e_ms")
            bl_wl[wl] = val
            row += f" {_fmt(val):>16}"
        print(row)

    for fw in frameworks:
        result = load_best_result(results_dir / fw)
        if result is None:
            continue
        metrics = result.get("metrics", result)
        row = f"{fw:<20}"
        for wl in WORKLOADS:
            val = metrics.get(f"{wl}_e2e_ms")
            pct = _pct(val, bl_wl.get(wl))
            cell = _fmt(val)
            if pct:
                cell += f" ({pct})"
            row += f" {cell:>16}"
        print(row)

    # --- Table 3: Per-workload P95 latency ---
    bl_p95 = _get_baseline_per_workload_p95(baseline) if baseline else {}
    if bl_p95 or any(
        _get_per_workload_p95(load_best_result(results_dir / fw).get("metrics", load_best_result(results_dir / fw)) or {})
        for fw in frameworks if load_best_result(results_dir / fw)
    ):
        print(f"\nPer-Workload P95 Latency (ms):")
        hdr = f"{'Framework':<20}"
        for wl in WORKLOADS:
            hdr += f" {wl:>16}"
        print(hdr)
        print("-" * (20 + 17 * len(WORKLOADS)))

        if bl_p95:
            row = f"{'*baseline*':<20}"
            for wl in WORKLOADS:
                row += f" {_fmt(bl_p95.get(f'{wl}_p95_ms')):>16}"
            print(row)

        for fw in frameworks:
            result = load_best_result(results_dir / fw)
            if result is None:
                continue
            metrics = result.get("metrics", result)
            p95_data = _get_per_workload_p95(metrics)
            row = f"{fw:<20}"
            for wl in WORKLOADS:
                val = p95_data.get(f"{wl}_p95_ms")
                pct = _pct(val, bl_p95.get(f"{wl}_p95_ms"))
                cell = _fmt(val)
                if pct:
                    cell += f" ({pct})"
                row += f" {cell:>16}"
            print(row)

    # --- Per-model baseline breakdown ---
    if baseline and "per_model" in baseline:
        print(f"\nMulti-LLM baseline breakdown:")
        print(f"  {'Model':<20} {'Avg E2E':>10} {'Avg P95':>10}")
        print(f"  {'-'*42}")
        for model_name, model_data in baseline["per_model"].items():
            e2e = model_data.get("avg_e2e_ms")
            p95 = model_data.get("avg_p95_ms")
            print(f"  {model_name:<20} {_fmt(e2e):>10} {_fmt(p95):>10}")

    # --- Save CSV (all data in one file) ---
    csv_path = results_dir / "comparison_table.csv"
    wl_e2e_cols = [f"{wl}_e2e_ms" for wl in WORKLOADS]
    wl_p95_cols = [f"{wl}_p95_ms" for wl in WORKLOADS]
    header = ["framework", "combined_score", "avg_e2e_ms", "avg_p95_ms", "iteration"] + wl_e2e_cols + wl_p95_cols

    with open(csv_path, "w") as f:
        f.write(",".join(header) + "\n")

        # Baseline row
        if baseline:
            bl_p95_data = _get_baseline_per_workload_p95(baseline)
            vals = ["*baseline*",
                    str(baseline.get("combined_score", "")),
                    str(baseline.get("avg_e2e_ms", "")),
                    str(baseline.get("avg_p95_ms", "")),
                    ""]
            for wl in WORKLOADS:
                vals.append(str(baseline.get(f"{wl}_e2e_ms", "")))
            for wl in WORKLOADS:
                vals.append(str(bl_p95_data.get(f"{wl}_p95_ms", "")))
            f.write(",".join(vals) + "\n")

        # Framework rows
        for fw in frameworks:
            result = load_best_result(results_dir / fw)
            if result is None:
                continue
            metrics = result.get("metrics", result)
            p95_data = _get_per_workload_p95(metrics)
            vals = [fw,
                    str(metrics.get("combined_score", "")),
                    str(metrics.get("avg_e2e_ms", "")),
                    str(metrics.get("avg_p95_ms", "")),
                    str(result.get("iteration", ""))]
            for wl in WORKLOADS:
                vals.append(str(metrics.get(f"{wl}_e2e_ms", "")))
            for wl in WORKLOADS:
                vals.append(str(p95_data.get(f"{wl}_p95_ms", "")))
            f.write(",".join(vals) + "\n")

    print(f"\nCSV saved to {csv_path}")


if __name__ == "__main__":
    main()
