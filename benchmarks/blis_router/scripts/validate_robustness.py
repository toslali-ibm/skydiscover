"""
Robustness Validation for BLIS Router Experiments

Re-evaluates discovered best programs across multiple seeds to detect overfitting.
Each seed changes the BLIS simulation's request ordering, so robust algorithms
should maintain their improvement across seeds.

Runs both single-LLM and multi-LLM evaluations by default, producing separate
score matrices for each mode.

Usage:
  python benchmarks/blis_router/scripts/validate_robustness.py outputs/blis_router/260306_50i_real/
  python benchmarks/blis_router/scripts/validate_robustness.py outputs/blis_router/260306_50i_real/ --seeds 42,123,456,789
  python benchmarks/blis_router/scripts/validate_robustness.py outputs/blis_router/260306_50i_real/ --single-llm-only
  python benchmarks/blis_router/scripts/validate_robustness.py outputs/blis_router/260306_50i_real/ --multi-llm-only

Output:
  <results_dir>/robustness/robustness_results.json  — full matrix (both modes)
  <results_dir>/robustness/robustness_summary.csv    — framework × seed scores (both modes)
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent  # benchmarks/blis_router/
EVALUATOR_PATH = SCRIPT_DIR / "evaluator.py"
INITIAL_PROGRAM = (
    SCRIPT_DIR / "initial_program.go"
    if (SCRIPT_DIR / "initial_program.go").exists()
    else SCRIPT_DIR / "initial_program.py"
)
WORKLOADS = ["cache_warmup", "load_spikes", "multiturn"]


def _find_best_program(fw_dir: Path) -> Path | None:
    """Find best program file (.go or .py) in a framework's best/ dir."""
    for suffix in (".go", ".py"):
        path = fw_dir / "best" / f"best_program{suffix}"
        if path.exists():
            return path
    return None


def find_frameworks(results_dir: Path) -> list[str]:
    """Find frameworks that have a best program (.go or .py)."""
    frameworks = []
    for p in sorted(results_dir.iterdir()):
        if p.is_dir() and _find_best_program(p) is not None:
            frameworks.append(p.name)
    return frameworks


def evaluate_program(program_path: Path, seed: str, multi_llm: bool) -> dict:
    """Evaluate a program by calling the evaluator in a subprocess.

    We must use a subprocess because the evaluator reads BLIS_SEED and
    BLIS_MULTI_LLM at module import time, so changing seeds requires a
    fresh Python process.
    """
    env = os.environ.copy()
    env["BLIS_SEED"] = seed
    env["BLIS_MULTI_LLM"] = "1" if multi_llm else "0"
    # Use a temp output dir for baseline isolation per seed
    tmp_output = Path(f"/tmp/blis_robustness_{seed}_{'multi' if multi_llm else 'single'}")
    tmp_output.mkdir(parents=True, exist_ok=True)
    env["BLIS_OUTPUT_DIR"] = str(tmp_output)

    # Delete cached baseline so it recomputes for this seed
    baseline_cache = tmp_output / "baseline_metrics.json"
    if baseline_cache.exists():
        baseline_cache.unlink()

    eval_code = f"""
import sys, json
sys.path.insert(0, {str(SCRIPT_DIR)!r})
from evaluator import evaluate
result = evaluate({str(program_path)!r})
print("__RESULT_JSON__")
print(json.dumps(result))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", eval_code],
            env=env, capture_output=True, text=True, timeout=300,
        )
        stdout = result.stdout
        if "__RESULT_JSON__" in stdout:
            json_str = stdout.split("__RESULT_JSON__\n", 1)[1].strip()
            return json.loads(json_str)
        return {
            "combined_score": -100000.0,
            "error": f"No result marker in output. stderr: {result.stderr[:500]}",
        }
    except subprocess.TimeoutExpired:
        return {"combined_score": -100000.0, "error": "Evaluation timed out (300s)"}
    except Exception as exc:
        return {"combined_score": -100000.0, "error": str(exc)}


def run_validation(
    results_dir: Path, seeds: list[str], llm_modes: list[tuple[str, bool]],
) -> dict:
    """Run full validation matrix: framework × seed × llm_mode."""
    frameworks = find_frameworks(results_dir)
    if not frameworks:
        print("No frameworks with best/best_program.py found.")
        sys.exit(1)

    print(f"Frameworks: {', '.join(frameworks)}")
    print(f"Seeds: {', '.join(seeds)}")
    print(f"LLM modes: {', '.join(name for name, _ in llm_modes)}")

    total_evals = (1 + len(frameworks)) * len(seeds) * len(llm_modes)
    print(f"Total evaluations: {total_evals}")
    print()

    all_results = {}
    eval_count = 0
    start_time = time.time()

    for mode_name, multi_llm in llm_modes:
        mode_results = {"baseline": {}, "frameworks": {}}

        # Evaluate baseline for each seed
        for seed in seeds:
            eval_count += 1
            elapsed = time.time() - start_time
            print(f"[{eval_count}/{total_evals}] ({elapsed:.0f}s) "
                  f"baseline / seed={seed} / {mode_name} ...", end=" ", flush=True)
            result = evaluate_program(INITIAL_PROGRAM, seed, multi_llm)
            score = result.get("combined_score", -100000.0)
            print(f"score={score:.2f}")
            mode_results["baseline"][seed] = result

        # Evaluate each framework for each seed
        for fw in frameworks:
            mode_results["frameworks"][fw] = {}
            program_path = _find_best_program(results_dir / fw)
            for seed in seeds:
                eval_count += 1
                elapsed = time.time() - start_time
                print(f"[{eval_count}/{total_evals}] ({elapsed:.0f}s) "
                      f"{fw} / seed={seed} / {mode_name} ...", end=" ", flush=True)
                result = evaluate_program(program_path, seed, multi_llm)
                score = result.get("combined_score", -100000.0)
                print(f"score={score:.2f}")
                mode_results["frameworks"][fw][seed] = result

        all_results[mode_name] = mode_results

    elapsed = time.time() - start_time
    print(f"\nAll evaluations complete in {elapsed:.1f}s")
    return all_results


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def print_tables(all_results: dict, seeds: list[str], results_dir: Path):
    """Print console tables and save CSV + JSON."""
    frameworks = []
    for mode_results in all_results.values():
        frameworks = sorted(mode_results["frameworks"].keys())
        break

    robustness_dir = results_dir / "robustness"
    robustness_dir.mkdir(parents=True, exist_ok=True)

    csv_rows = []

    for mode_name, mode_results in all_results.items():
        print(f"\n{'='*70}")
        print(f"Robustness Validation — {mode_name} (seeds: {', '.join(seeds)})")
        print(f"{'='*70}")

        baseline_data = mode_results["baseline"]
        fw_data = mode_results["frameworks"]

        # --- Table 1: Combined scores ---
        seed_cols = "".join(f"{'seed=' + s:>14}" for s in seeds)
        print(f"\nCombined Score:")
        print(f"{'Framework':<22}{seed_cols}{'Mean':>14}{'StdDev':>12}")
        print("-" * (22 + 14 * len(seeds) + 26))

        # Baseline row
        bl_scores = []
        row = f"{'baseline':<22}"
        for seed in seeds:
            s = baseline_data[seed].get("combined_score", -100000.0)
            bl_scores.append(s)
            row += f"{s:>14.2f}"
        row += f"{_mean(bl_scores):>14.2f}{_stddev(bl_scores):>12.2f}"
        print(row)

        # Framework rows
        for fw in frameworks:
            scores = []
            row = f"{fw:<22}"
            for seed in seeds:
                s = fw_data[fw][seed].get("combined_score", -100000.0)
                scores.append(s)
                row += f"{s:>14.2f}"
            row += f"{_mean(scores):>14.2f}{_stddev(scores):>12.2f}"
            print(row)

        # --- Table 2: % improvement vs per-seed baseline ---
        print(f"\n% Improvement vs per-seed baseline:")
        print(f"{'Framework':<22}{seed_cols}{'Mean':>14}{'StdDev':>12}")
        print("-" * (22 + 14 * len(seeds) + 26))

        for fw in frameworks:
            pcts = []
            row = f"{fw:<22}"
            for i, seed in enumerate(seeds):
                fw_score = fw_data[fw][seed].get("combined_score", -100000.0)
                bl_score = bl_scores[i]
                if bl_score != 0 and bl_score != -100000.0:
                    pct = (fw_score - bl_score) / abs(bl_score) * 100
                else:
                    pct = 0.0
                pcts.append(pct)
                row += f"{pct:>+13.1f}%"
            row += f"{_mean(pcts):>+13.1f}%{_stddev(pcts):>11.1f}%"
            print(row)

        # --- Table 3: Per-workload E2E (mean across seeds) ---
        print(f"\nPer-Workload E2E (mean across seeds):")
        wl_cols = "".join(f"{wl:>16}" for wl in WORKLOADS)
        print(f"{'Framework':<22}{wl_cols}")
        print("-" * (22 + 16 * len(WORKLOADS)))

        # Baseline per-workload
        row = f"{'baseline':<22}"
        for wl in WORKLOADS:
            vals = []
            for seed in seeds:
                v = baseline_data[seed].get(f"{wl}_e2e_ms")
                if v is not None:
                    vals.append(v)
            row += f"{_mean(vals):>16.1f}" if vals else f"{'N/A':>16}"
        print(row)

        for fw in frameworks:
            row = f"{fw:<22}"
            for wl in WORKLOADS:
                vals = []
                for seed in seeds:
                    v = fw_data[fw][seed].get(f"{wl}_e2e_ms")
                    if v is not None:
                        vals.append(v)
                row += f"{_mean(vals):>16.1f}" if vals else f"{'N/A':>16}"
            print(row)

        # --- Table 4: Per-workload P95 (mean across seeds) ---
        print(f"\nPer-Workload P95 (mean across seeds):")
        print(f"{'Framework':<22}{wl_cols}")
        print("-" * (22 + 16 * len(WORKLOADS)))

        row = f"{'baseline':<22}"
        for wl in WORKLOADS:
            vals = []
            for seed in seeds:
                v = baseline_data[seed].get(f"avg_p95_ms")
                # P95 per-workload isn't in the top-level evaluate return;
                # check artifacts.workload_results
                artifacts = baseline_data[seed].get("artifacts", {})
                wl_results = artifacts.get("workload_results", {})
                wl_data = wl_results.get(wl, {})
                p95 = wl_data.get("e2e_p95_ms")
                if p95 is not None:
                    vals.append(p95)
            row += f"{_mean(vals):>16.1f}" if vals else f"{'N/A':>16}"
        print(row)

        for fw in frameworks:
            row = f"{fw:<22}"
            for wl in WORKLOADS:
                vals = []
                for seed in seeds:
                    artifacts = fw_data[fw][seed].get("artifacts", {})
                    wl_results = artifacts.get("workload_results", {})
                    wl_data = wl_results.get(wl, {})
                    p95 = wl_data.get("e2e_p95_ms")
                    if p95 is not None:
                        vals.append(p95)
                row += f"{_mean(vals):>16.1f}" if vals else f"{'N/A':>16}"
            print(row)

        # Collect CSV rows for this mode
        for fw_name in ["baseline"] + frameworks:
            for seed in seeds:
                if fw_name == "baseline":
                    result = baseline_data[seed]
                else:
                    result = fw_data[fw_name][seed]
                csv_rows.append({
                    "mode": mode_name,
                    "framework": fw_name,
                    "seed": seed,
                    "combined_score": result.get("combined_score"),
                    "avg_e2e_ms": result.get("avg_e2e_ms"),
                    "avg_p95_ms": result.get("avg_p95_ms"),
                    **{f"{wl}_e2e_ms": result.get(f"{wl}_e2e_ms") for wl in WORKLOADS},
                })

    # Save JSON
    json_path = robustness_dir / "robustness_results.json"
    with open(json_path, "w") as f:
        # Convert to serializable format (replace any non-serializable values)
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nJSON saved to {json_path}")

    # Save CSV
    csv_path = robustness_dir / "robustness_summary.csv"
    if csv_rows:
        fieldnames = list(csv_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
    print(f"CSV saved to {csv_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate robustness of BLIS router experiments across seeds"
    )
    parser.add_argument("results_dir", type=Path, help="Experiment results directory")
    parser.add_argument(
        "--seeds", type=str, default="42,123,456",
        help="Comma-separated seeds (default: 42,123,456)",
    )
    parser.add_argument(
        "--single-llm-only", action="store_true",
        help="Only run single-LLM evaluation",
    )
    parser.add_argument(
        "--multi-llm-only", action="store_true",
        help="Only run multi-LLM evaluation",
    )
    args = parser.parse_args()

    if not args.results_dir.is_dir():
        print(f"Directory not found: {args.results_dir}")
        sys.exit(1)

    seeds = [s.strip() for s in args.seeds.split(",")]

    # Determine LLM modes
    if args.single_llm_only and args.multi_llm_only:
        print("Cannot specify both --single-llm-only and --multi-llm-only")
        sys.exit(1)
    elif args.single_llm_only:
        llm_modes = [("single_llm", False)]
    elif args.multi_llm_only:
        llm_modes = [("multi_llm", True)]
    else:
        # Default: both modes
        llm_modes = [("single_llm", False), ("multi_llm", True)]

    all_results = run_validation(args.results_dir, seeds, llm_modes)
    print_tables(all_results, seeds, args.results_dir)


if __name__ == "__main__":
    main()
