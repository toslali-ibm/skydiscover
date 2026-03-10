#!/usr/bin/env python3
"""Validate robustness of discovered algorithms across seeds and LLM modes.

Re-evaluates best programs with multiple seeds to detect overfitting.

Usage:
    python scripts/validate_robustness.py <results_dir> [--seeds 42,123,456]
        [--single-llm-only] [--multi-llm-only]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

BENCHMARK_DIR = Path(__file__).resolve().parent.parent
EVALUATOR = BENCHMARK_DIR / "evaluator.py"
INITIAL_PROGRAM = BENCHMARK_DIR / "initial_program.py"


def find_best_program(fw_dir: Path) -> Path:
    for name in ["best_program.py", "best_program.go"]:
        p = fw_dir / "best" / name
        if p.exists():
            return p
    return None


def evaluate_program(program_path: str, seed: str, multi_llm: bool, timeout: int = 1800) -> dict:
    """Evaluate a program with specific seed and LLM mode."""
    env = os.environ.copy()
    env["VIDUR_SEED"] = seed
    env["VIDUR_MULTI_LLM"] = "1" if multi_llm else "0"
    tmp_dir = tempfile.mkdtemp(prefix=f"vidur_robust_{seed}_{'multi' if multi_llm else 'single'}_")
    env["VIDUR_OUTPUT_DIR"] = tmp_dir

    try:
        result = subprocess.run(
            [sys.executable, str(EVALUATOR), program_path],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            return {"error": result.stderr[:500], "combined_score": -100000}
        # Parse JSON from stdout
        output = result.stdout.strip()
        # Find last JSON object in output
        lines = output.split("\n")
        json_start = None
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                json_start = i
                break
        if json_start is not None:
            return json.loads("\n".join(lines[json_start:]))
        return {"error": "No JSON output", "combined_score": -100000}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout", "combined_score": -100000}
    except Exception as e:
        return {"error": str(e), "combined_score": -100000}


def main():
    parser = argparse.ArgumentParser(description="Validate robustness of discovered algorithms")
    parser.add_argument("results_dir", type=str)
    parser.add_argument("--seeds", default="42,123,456", help="Comma-separated seeds")
    parser.add_argument("--single-llm-only", action="store_true")
    parser.add_argument("--multi-llm-only", action="store_true")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    seeds = [s.strip() for s in args.seeds.split(",")]

    # Determine LLM modes
    llm_modes = []
    if not args.multi_llm_only:
        llm_modes.append(("single-llm", False))
    if not args.single_llm_only:
        llm_modes.append(("multi-llm", True))

    # Find frameworks
    frameworks = sorted([
        d.name for d in results_dir.iterdir()
        if d.is_dir() and find_best_program(d) is not None
    ])

    print(f"Frameworks: {frameworks}")
    print(f"Seeds: {seeds}")
    print(f"LLM modes: {[m[0] for m in llm_modes]}")
    print()

    # Results matrix
    all_results = {}

    for mode_name, multi_llm in llm_modes:
        print(f"\n{'='*60}")
        print(f"Mode: {mode_name}")
        print(f"{'='*60}")

        mode_results = {}

        # Baseline
        print(f"\n  Evaluating baseline (LOR)...")
        baseline_scores = []
        for seed in seeds:
            r = evaluate_program(str(INITIAL_PROGRAM), seed, multi_llm)
            score = r.get("combined_score", -100000)
            baseline_scores.append(score)
            print(f"    seed={seed}: score={score:.2f}")
        mode_results["baseline"] = {
            "scores": baseline_scores,
            "mean": np.mean(baseline_scores),
            "std": np.std(baseline_scores),
        }

        # Frameworks
        for fw in frameworks:
            print(f"\n  Evaluating {fw}...")
            best_path = find_best_program(results_dir / fw)
            fw_scores = []
            fw_details = []
            for seed in seeds:
                r = evaluate_program(str(best_path), seed, multi_llm)
                score = r.get("combined_score", -100000)
                fw_scores.append(score)
                fw_details.append(r)
                print(f"    seed={seed}: score={score:.2f}")

            mean_score = np.mean(fw_scores)
            baseline_mean = mode_results["baseline"]["mean"]
            impr = ((mean_score - baseline_mean) / abs(baseline_mean) * 100) if baseline_mean != 0 else 0

            mode_results[fw] = {
                "scores": fw_scores,
                "mean": mean_score,
                "std": np.std(fw_scores),
                "improvement_pct": impr,
                "details": fw_details,
            }

        all_results[mode_name] = mode_results

        # Print summary table
        print(f"\n  {'Framework':<20} {'Mean':>10} {'Std':>8} {'Impr%':>8}")
        print(f"  {'-'*50}")
        for name, data in mode_results.items():
            impr = data.get("improvement_pct", 0)
            impr_str = f"{impr:+.2f}%" if name != "baseline" else "—"
            print(f"  {name:<20} {data['mean']:>10.2f} {data['std']:>8.2f} {impr_str:>8}")

    # Write output
    robustness_dir = results_dir / "robustness"
    robustness_dir.mkdir(exist_ok=True)

    # JSON (full results)
    json_path = robustness_dir / "robustness_results.json"
    # Convert numpy to native types
    def _convert(obj):
        if isinstance(obj, (np.floating, np.integer)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    json_str = json.dumps(all_results, indent=2, default=_convert)
    json_path.write_text(json_str)
    print(f"\nWrote {json_path}")

    # CSV summary
    import csv
    csv_path = robustness_dir / "robustness_summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "framework", "seed", "combined_score"])
        for mode_name, mode_results in all_results.items():
            for fw, data in mode_results.items():
                for i, seed in enumerate(seeds):
                    writer.writerow([mode_name, fw, seed, data["scores"][i]])
    print(f"Wrote {csv_path}")
    print("Done!")


if __name__ == "__main__":
    main()
