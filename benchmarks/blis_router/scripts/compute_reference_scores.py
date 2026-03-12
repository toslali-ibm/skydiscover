#!/usr/bin/env python3
"""Compute reference scores for baseline configs against the 1_1 baseline.

Uses the EXACT evaluator scoring formula:
  - Per workload: candidate_val = 0.5 * e2e + 0.5 * p95
  - Per workload: baseline_val  = 0.5 * bl_e2e + 0.5 * bl_p95
  - Improvement per (workload, model) = 1 - candidate_val / baseline_val
  - Per workload improvement = mean(per-model improvements)
  - Score = mean(per-workload improvements) * 100

Reads:  outputs/blis_router/baseline_comparison.json
Writes: outputs/blis_router/reference_scores.json
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

INPUT_PATH = os.path.join(REPO_ROOT, "outputs", "blis_router", "baseline_comparison.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "outputs", "blis_router", "reference_scores.json")

# Workload name mapping: JSON key -> evaluator name
WORKLOAD_MAP = {
    "glia_prefix_heavy": "prefix_heavy",
    "glia_40qps": "glia_40qps",
}

BASELINE_CONFIG = "1_1"
CANDIDATE_CONFIGS = ["llq", "lor", "glia", "3_2_2"]


def compute_scores(data: dict) -> dict:
    """Compute evaluator-style scores for each candidate config vs 1_1 baseline."""
    baseline_data = data[BASELINE_CONFIG]
    evaluator_workloads = list(WORKLOAD_MAP.keys())

    scores = {}
    for config_name in CANDIDATE_CONFIGS:
        candidate_data = data[config_name]
        per_workload = {}

        for wl_json_name in evaluator_workloads:
            wl_display = WORKLOAD_MAP[wl_json_name]

            # Collect per-model improvements
            bl_wl = baseline_data[wl_json_name]
            cd_wl = candidate_data[wl_json_name]

            # Use all models present in both baseline and candidate
            models = sorted(set(bl_wl.keys()) & set(cd_wl.keys()))
            model_improvements = []

            for model in models:
                bl_e2e = bl_wl[model]["e2e_ms"]
                bl_p95 = bl_wl[model]["p95_ms"]
                cd_e2e = cd_wl[model]["e2e_ms"]
                cd_p95 = cd_wl[model]["p95_ms"]

                baseline_val = 0.5 * bl_e2e + 0.5 * bl_p95
                candidate_val = 0.5 * cd_e2e + 0.5 * cd_p95

                improvement = 1.0 - candidate_val / baseline_val
                model_improvements.append(improvement)

            # Average across models (each model is a separate data point)
            wl_improvement = sum(model_improvements) / len(model_improvements)
            per_workload[wl_display] = round(wl_improvement * 100, 4)

        # Score = mean of per-workload improvements * 100
        overall_score = sum(per_workload.values()) / len(per_workload)
        scores[config_name] = {
            "score": round(overall_score, 4),
            "per_workload": per_workload,
        }

    return scores


def print_summary(scores: dict) -> None:
    """Print a human-readable summary table."""
    workloads = list(WORKLOAD_MAP.values())

    # Header
    wl_headers = "".join(f"{wl:>18s}" for wl in workloads)
    print(f"\n{'Config':>10s}{wl_headers}{'Overall':>12s}")
    print("-" * (10 + 18 * len(workloads) + 12))

    # Baseline row
    print(f"{'1_1 (bl)':>10s}" + "".join(f"{'0.0000':>18s}" for _ in workloads) + f"{'0.0000':>12s}")

    # Candidate rows
    for config_name in CANDIDATE_CONFIGS:
        s = scores[config_name]
        wl_vals = "".join(f"{s['per_workload'][wl]:>18.4f}" for wl in workloads)
        print(f"{config_name:>10s}{wl_vals}{s['score']:>12.4f}")

    print()
    print("Values are % improvement vs 1_1 baseline (positive = better, negative = worse).")
    print()


def main():
    if not os.path.exists(INPUT_PATH):
        print(f"ERROR: Input file not found: {INPUT_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(INPUT_PATH, "r") as f:
        data = json.load(f)

    # Validate expected structure
    for config in [BASELINE_CONFIG] + CANDIDATE_CONFIGS:
        if config not in data:
            print(f"ERROR: Config '{config}' not found in {INPUT_PATH}", file=sys.stderr)
            sys.exit(1)

    scores = compute_scores(data)
    print_summary(scores)

    result = {
        "baseline": BASELINE_CONFIG,
        "workloads": list(WORKLOAD_MAP.values()),
        "scores": scores,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Written: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
