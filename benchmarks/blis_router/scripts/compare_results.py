"""
Compare results across search frameworks for the BLIS router benchmark.

Usage: python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>/
"""

import json
import sys
from pathlib import Path


def load_best_result(framework_dir: Path) -> dict | None:
    best_info = framework_dir / "best" / "best_program_info.json"
    if not best_info.exists():
        return None
    with open(best_info) as f:
        return json.load(f)


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

    print(f"\n{'Framework':<15} {'Score':>10} {'Avg E2E':>10} {'Avg P95':>10} {'Iterations':>12}")
    print("-" * 60)

    for fw in frameworks:
        result = load_best_result(results_dir / fw)
        if result is None:
            print(f"{fw:<15} {'N/A':>10}")
            continue
        metrics = result.get("metrics", result)
        score = metrics.get("combined_score", "N/A")
        avg_e2e = metrics.get("avg_e2e_ms", "N/A")
        avg_p95 = metrics.get("avg_p95_ms", "N/A")
        iteration = result.get("iteration", "N/A")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else str(score)
        e2e_str = f"{avg_e2e:.2f}" if isinstance(avg_e2e, (int, float)) else str(avg_e2e)
        p95_str = f"{avg_p95:.2f}" if isinstance(avg_p95, (int, float)) else str(avg_p95)
        print(f"{fw:<15} {score_str:>10} {e2e_str:>10} {p95_str:>10} {str(iteration):>12}")

    # Save CSV
    csv_path = results_dir / "comparison_table.csv"
    with open(csv_path, "w") as f:
        f.write("framework,combined_score,avg_e2e_ms,avg_p95_ms,iteration\n")
        for fw in frameworks:
            result = load_best_result(results_dir / fw)
            if result is None:
                continue
            metrics = result.get("metrics", result)
            f.write(f"{fw},{metrics.get('combined_score','')},{metrics.get('avg_e2e_ms','')},{metrics.get('avg_p95_ms','')},{result.get('iteration','')}\n")
    print(f"\nCSV saved to {csv_path}")


if __name__ == "__main__":
    main()
