#!/usr/bin/env python3
"""
Compare BLIS routing baselines against glia workloads.

Evaluates multiple routing programs against multiple workloads, reporting
avg E2E, P95 latency, and % gain vs LLQ baseline.

Each "config" is a (program.go, policy.yaml) pair. The script:
1. Replaces routing.go with the program
2. Swaps routing_policy.yaml if needed
3. Builds the simulator
4. Runs all (workload × seed × model) combinations
5. Restores original files

Prints three tables: per-model (qwen_7b, qwen_14b) and combined.

Usage:
    python benchmarks/blis_router/scripts/compare_baselines.py [--output FILE]

    Default output: prints table to stdout + saves JSON to
    outputs/blis_router/baseline_comparison.json
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # benchmarks/blis_router/
INFERENCE_SIM_DIR = SCRIPT_DIR / "inference-sim"
ROUTING_GO_PATH = INFERENCE_SIM_DIR / "sim" / "routing.go"
POLICY_CONFIG_PATH = SCRIPT_DIR / "routing" / "routing_policy.yaml"

# ── Routing configs: (name, program_path, policy_yaml_path) ──
CONFIGS = [
    ("llq", SCRIPT_DIR / "algos" / "initial_program_llq.go", POLICY_CONFIG_PATH),
    ("glia", SCRIPT_DIR / "algos" / "initial_program_glia.go", POLICY_CONFIG_PATH),
    ("1_1", SCRIPT_DIR / "initial_program.go", POLICY_CONFIG_PATH),
]

# ── Workloads ──
WORKLOADS = [
    ("glia_40qps", "workload_glia_40qps.yaml"),
    ("glia_prefix_heavy", "workload_glia_prefix_heavy.yaml"),
]

# ── Models and seeds ──
_MODEL_QWEN_7B = ("qwen_7b", "qwen/qwen2.5-7b-instruct", ["--hardware", "H100", "--tp", "1"])
_MODEL_QWEN_14B = ("qwen_14b", "qwen/qwen3-14b", ["--hardware", "H100", "--tp", "1"])

_DEFAULT_SEEDS = ["42", "456"]
SIM_SEEDS = (
    os.environ.get("BLIS_SEED", "").split(",")
    if os.environ.get("BLIS_SEED")
    else _DEFAULT_SEEDS
)
SIM_NUM_INSTANCES = os.environ.get("BLIS_NUM_INSTANCES", "4")
MULTI_LLM = os.environ.get("BLIS_MULTI_LLM", "1") == "1"
SIM_SNAPSHOT_REFRESH = os.environ.get("BLIS_SNAPSHOT_REFRESH", "5000000")


def get_models():
    if MULTI_LLM:
        return [_MODEL_QWEN_7B, _MODEL_QWEN_14B]
    return [_MODEL_QWEN_7B]


def parse_cluster_metrics(output_text: str) -> dict | None:
    """Extract cluster-level metrics from simulation stdout."""
    json_blocks = []
    in_json = False
    json_buffer = ""
    brace_count = 0
    for line in output_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            in_json = True
            brace_count = 0
        if in_json:
            json_buffer += line + "\n"
            if len(json_buffer) > 1_000_000:
                json_buffer = ""
                in_json = False
                continue
            brace_count += stripped.count("{") - stripped.count("}")
            if brace_count == 0 and json_buffer.strip():
                try:
                    json_blocks.append(json.loads(json_buffer))
                except json.JSONDecodeError:
                    pass
                json_buffer = ""
                in_json = False
    for block in json_blocks:
        if block.get("instance_id") == "cluster":
            return block
    return None


def run_config(name: str, program_path: Path, policy_path: Path) -> dict:
    """Run a single routing config against all workloads.

    Returns nested dict: {workload: {model: {"e2e_ms": ..., "p95_ms": ..., "n_runs": ...}}}
    """
    logger.info("=== Evaluating config: %s ===", name)

    if not program_path.exists():
        logger.error("Program not found: %s", program_path)
        return {}

    # Save originals
    original_routing = ROUTING_GO_PATH.read_text()
    original_policy = POLICY_CONFIG_PATH.read_text()

    try:
        # Swap routing.go
        ROUTING_GO_PATH.write_text(program_path.read_text())

        # Swap policy config if different
        if policy_path != POLICY_CONFIG_PATH:
            POLICY_CONFIG_PATH.write_text(policy_path.read_text())

        # Build
        build = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            logger.error("[%s] Build failed:\n%s", name, build.stderr[:500])
            return {}
        logger.info("[%s] Build OK", name)

        models = get_models()
        # results[workload][model_name] = {"e2e_ms": avg, "p95_ms": avg, "n_runs": N}
        results: dict[str, dict[str, dict]] = {}

        for wl_name, wl_file in WORKLOADS:
            workload_path = SCRIPT_DIR / "workloads" / wl_file
            if not workload_path.exists():
                logger.warning("[%s] Workload not found: %s", name, workload_path)
                continue

            results[wl_name] = {}

            for model_name, model_id, extra_args in models:
                e2e_vals = []
                p95_vals = []

                for seed in SIM_SEEDS:
                    cmd = [
                        "./simulation_worker", "run",
                        "--model", model_id,
                        "--num-instances", SIM_NUM_INSTANCES,
                        "--policy-config", str(POLICY_CONFIG_PATH),
                        "--workload-spec", str(workload_path),
                        "--snapshot-refresh-interval", SIM_SNAPSHOT_REFRESH,
                        "--log", "info",
                        "--seed", seed,
                    ] + extra_args

                    tag = f"{name}/{wl_name}/{model_name}/seed={seed}"
                    try:
                        result = subprocess.run(
                            cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120,
                        )
                        if result.returncode != 0:
                            logger.warning("[%s] failed: %s", tag, result.stderr[:300])
                            continue

                        cluster = parse_cluster_metrics(result.stdout + (result.stderr or ""))
                        if cluster and "e2e_mean_ms" in cluster:
                            e2e = float(cluster["e2e_mean_ms"])
                            p95 = float(cluster.get("e2e_p95_ms", e2e))
                            e2e_vals.append(e2e)
                            p95_vals.append(p95)
                            logger.info("[%s] e2e=%.1fms p95=%.1fms", tag, e2e, p95)
                        else:
                            logger.warning("[%s] no cluster metrics", tag)
                    except (subprocess.TimeoutExpired, OSError) as exc:
                        logger.warning("[%s] error: %s", tag, exc)

                if e2e_vals:
                    results[wl_name][model_name] = {
                        "e2e_ms": sum(e2e_vals) / len(e2e_vals),
                        "p95_ms": sum(p95_vals) / len(p95_vals),
                        "n_runs": len(e2e_vals),
                    }
                else:
                    results[wl_name][model_name] = {"e2e_ms": None, "p95_ms": None, "error": "all runs failed"}

        return results

    finally:
        ROUTING_GO_PATH.write_text(original_routing)
        POLICY_CONFIG_PATH.write_text(original_policy)
        logger.info("[%s] Restored original routing.go and policy config", name)


def aggregate_workload(wl_data: dict[str, dict], model_filter: list[str] | None = None) -> dict:
    """Aggregate per-model workload data into a single {e2e_ms, p95_ms}.

    model_filter: list of model names to include. None = all models.
    """
    e2e_vals = []
    p95_vals = []
    for model_name, mdata in wl_data.items():
        if model_filter and model_name not in model_filter:
            continue
        if mdata.get("e2e_ms") is not None:
            n = mdata.get("n_runs", 1)
            e2e_vals.extend([mdata["e2e_ms"]] * n)
            p95_vals.extend([mdata["p95_ms"]] * n)
    if not e2e_vals:
        return {"e2e_ms": None, "p95_ms": None}
    return {
        "e2e_ms": sum(e2e_vals) / len(e2e_vals),
        "p95_ms": sum(p95_vals) / len(p95_vals),
    }


def pct_gain(baseline: float, candidate: float) -> str:
    """Positive = candidate is better (lower latency)."""
    if baseline is None or candidate is None or baseline == 0:
        return "N/A"
    gain = (baseline - candidate) / abs(baseline) * 100
    return f"+{gain:.1f}%" if gain >= 0 else f"{gain:.1f}%"


def print_metric_table(title: str, metric_key: str, metric_label: str, all_results: dict, model_filter: list[str] | None = None):
    """Print a comparison table for a single metric (e2e_ms or p95_ms)."""
    W = 80
    wl_names = [wl for wl, _ in WORKLOADS]

    print(f"\n{'='*W}")
    print(title)
    print(f"{'='*W}")
    filter_desc = ", ".join(model_filter) if model_filter else "all models"
    print(f"Seeds: {sorted(SIM_SEEDS)} | Models: {filter_desc} | Instances: {SIM_NUM_INSTANCES}")
    print(f"{'='*W}\n")

    # Build aggregated view per config
    agg: dict[str, dict[str, dict]] = {}
    for cfg_name in [n for n, _, _ in CONFIGS]:
        agg[cfg_name] = {}
        cfg_data = all_results.get(cfg_name, {})
        for wl in wl_names:
            wl_data = cfg_data.get(wl, {})
            agg[cfg_name][wl] = aggregate_workload(wl_data, model_filter)

    llq = agg.get("llq", {})

    # Header
    print(f"{'':12}", end="")
    for wl in wl_names:
        short = wl.replace("glia_", "")
        print(f" | {short:^20}", end="")
    print()
    header = f"{'Program':<12}"
    for _ in wl_names:
        header += f" | {metric_label:>10} {'%gain':>7}"
    print(header)
    print(f"{'-'*W}")

    for cfg_name, _, _ in CONFIGS:
        row = f"{cfg_name:<12}"
        for wl in wl_names:
            wl_data = agg[cfg_name].get(wl, {})
            llq_data = llq.get(wl, {})
            val = wl_data.get(metric_key)
            llq_val = llq_data.get(metric_key)

            if val is not None:
                gain = pct_gain(llq_val, val)
                row += f" | {val:>10.1f} {gain:>7}"
            else:
                row += f" | {'ERR':>10} {'N/A':>7}"
        print(row)

    print(f"{'='*W}")


def print_combined_score_table(title: str, all_results: dict, model_filter: list[str] | None = None):
    """Print combined score table: score = 0.5*e2e + 0.5*p95, with % gain vs LLQ."""
    W = 80
    wl_names = [wl for wl, _ in WORKLOADS]

    print(f"\n{'='*W}")
    print(title)
    print(f"{'='*W}")
    filter_desc = ", ".join(model_filter) if model_filter else "all models"
    print(f"Seeds: {sorted(SIM_SEEDS)} | Models: {filter_desc} | Instances: {SIM_NUM_INSTANCES}")
    print(f"Score = 0.5 * E2E + 0.5 * P95  (lower is better)")
    print(f"{'='*W}\n")

    # Build aggregated view per config
    agg: dict[str, dict[str, dict]] = {}
    for cfg_name in [n for n, _, _ in CONFIGS]:
        agg[cfg_name] = {}
        cfg_data = all_results.get(cfg_name, {})
        for wl in wl_names:
            wl_data = cfg_data.get(wl, {})
            agg[cfg_name][wl] = aggregate_workload(wl_data, model_filter)

    llq = agg.get("llq", {})

    # Header
    print(f"{'':12}", end="")
    for wl in wl_names:
        short = wl.replace("glia_", "")
        print(f" | {short:^20}", end="")
    print(f" | {'OVERALL':^20}")
    header = f"{'Program':<12}"
    for _ in wl_names:
        header += f" | {'Score':>10} {'%gain':>7}"
    header += f" | {'Score':>10} {'%gain':>7}"
    print(header)
    print(f"{'-'*(W+24)}")

    for cfg_name, _, _ in CONFIGS:
        row = f"{cfg_name:<12}"
        cfg_scores = []
        llq_scores = []
        for wl in wl_names:
            wl_data = agg[cfg_name].get(wl, {})
            llq_data = llq.get(wl, {})
            e2e = wl_data.get("e2e_ms")
            p95 = wl_data.get("p95_ms")
            llq_e2e = llq_data.get("e2e_ms")
            llq_p95 = llq_data.get("p95_ms")

            if e2e is not None and p95 is not None:
                score = 0.5 * e2e + 0.5 * p95
                cfg_scores.append(score)
            else:
                score = None

            if llq_e2e is not None and llq_p95 is not None:
                llq_score = 0.5 * llq_e2e + 0.5 * llq_p95
                llq_scores.append(llq_score)
            else:
                llq_score = None

            if score is not None:
                gain = pct_gain(llq_score, score)
                row += f" | {score:>10.1f} {gain:>7}"
            else:
                row += f" | {'ERR':>10} {'N/A':>7}"

        # Overall: average of per-workload % improvements (so each workload
        # contributes equally, matching the evaluator's scoring approach)
        if cfg_scores and llq_scores and len(cfg_scores) == len(llq_scores):
            per_wl_gains = []
            for cs, ls in zip(cfg_scores, llq_scores):
                if ls > 0:
                    per_wl_gains.append((ls - cs) / abs(ls) * 100)
            if per_wl_gains:
                avg_gain = sum(per_wl_gains) / len(per_wl_gains)
                gain_str = f"+{avg_gain:.1f}%" if avg_gain >= 0 else f"{avg_gain:.1f}%"
            else:
                gain_str = "N/A"
            avg_score = sum(cfg_scores) / len(cfg_scores)
            row += f" | {avg_score:>10.1f} {gain_str:>7}"
        else:
            row += f" | {'N/A':>10} {'N/A':>7}"

        print(row)

    print(f"{'='*(W+24)}")


def print_tables_for_model(model_label: str, all_results: dict, model_filter: list[str] | None = None):
    """Print E2E and P95 tables for a given model filter."""
    print_metric_table(
        f"BASELINE COMPARISON: {model_label} — Avg E2E Latency (ms)",
        "e2e_ms", "E2E",
        all_results, model_filter,
    )
    print_metric_table(
        f"BASELINE COMPARISON: {model_label} — P95 Latency (ms)",
        "p95_ms", "P95",
        all_results, model_filter,
    )
    print_combined_score_table(
        f"BASELINE COMPARISON: {model_label} — Combined Score (0.5*E2E + 0.5*P95)",
        all_results, model_filter,
    )


def main():
    parser = argparse.ArgumentParser(description="Compare BLIS routing baselines")
    parser.add_argument("--output", type=str, default=None, help="JSON output path")
    args = parser.parse_args()

    # Run all configs
    all_results = {}
    for name, program, policy in CONFIGS:
        all_results[name] = run_config(name, program, policy)

    # ── Print tables ──
    models = get_models()
    model_names = [m[0] for m in models]

    for model_name in model_names:
        model_label = {"qwen_7b": "Qwen-2.5-7B", "qwen_14b": "Qwen3-14B"}.get(model_name, model_name)
        print_tables_for_model(model_label, all_results, model_filter=[model_name])

    if len(model_names) > 1:
        print_tables_for_model("Combined (all models)", all_results, model_filter=None)

    # ── Save JSON ──
    output_path = Path(args.output) if args.output else Path("outputs/blis_router/baseline_comparison.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
