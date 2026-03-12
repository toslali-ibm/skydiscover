#!/usr/bin/env python3
"""
Run LOR (Least Outstanding Requests) baseline against BLIS workloads.

This script evaluates a pure load-balancing router (no prefix affinity)
against the same workloads used in BLIS experiments. It reuses the
evaluator's simulation infrastructure but with the LOR routing code.

Output: lor_metrics.json in the specified output directory.

Usage:
    python benchmarks/blis_router/scripts/run_lor_baseline.py [--output-dir DIR]

    Default output: outputs/blis_router/lor_baseline/
    Uses same seeds/models as BLIS experiments (BLIS_SEED, BLIS_MULTI_LLM env vars).
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
LOR_PROGRAM_PATH = SCRIPT_DIR / "algos" / "initial_program_lor.go"

DEFAULT_WORKLOADS = [
    ("cache_warmup", "workload_v2_cache_warmup.yaml"),
    ("load_spikes", "workload_v2_load_spikes.yaml"),
    ("multiturn", "workload_v2_multiturn.yaml"),
]

GLIA_WORKLOADS = [
    ("glia_40qps", "workload_glia_40qps.yaml"),
    ("prefix_heavy", "workload_glia_prefix_heavy.yaml"),
]


def _detect_workloads(output_dir: Path) -> list[tuple[str, str]]:
    """Auto-detect workloads from experiment directory or evaluator config."""
    # Check if output_dir is inside an experiment that used glia workloads
    parent = output_dir.parent
    for fw_dir in parent.iterdir():
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


WORKLOADS = DEFAULT_WORKLOADS  # overridden in run_lor()

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


def run_lor(output_dir: Path) -> dict:
    """Run LOR routing code against all workloads and return metrics."""
    global WORKLOADS
    WORKLOADS = _detect_workloads(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Read LOR routing code
    if not LOR_PROGRAM_PATH.exists():
        logger.error("LOR program not found: %s", LOR_PROGRAM_PATH)
        sys.exit(1)

    lor_code = LOR_PROGRAM_PATH.read_text()

    # Save original routing.go
    if not ROUTING_GO_PATH.exists():
        logger.error("routing.go not found. Initialize inference-sim submodule.")
        sys.exit(1)

    original_routing = ROUTING_GO_PATH.read_text()

    try:
        # Write LOR code
        ROUTING_GO_PATH.write_text(lor_code)
        logger.info("Wrote LOR routing code to %s", ROUTING_GO_PATH)

        # Build
        logger.info("Building simulation_worker with LOR routing...")
        build = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            logger.error("Build failed:\n%s", build.stderr[:500])
            sys.exit(1)
        logger.info("Build successful")

        # Run all workloads
        models = get_models()
        all_latencies = []
        all_tail_latencies = []
        per_model = {}
        workload_agg: dict[str, list[dict]] = {wl: [] for wl, _ in WORKLOADS}

        for model_name, model_id, extra_args in models:
            model_latencies = []
            model_tail_latencies = []
            model_workloads: dict[str, list[dict]] = {wl: [] for wl, _ in WORKLOADS}

            for seed in SIM_SEEDS:
                for workload_name, workload_file in WORKLOADS:
                    workload_path = SCRIPT_DIR / "workloads" / workload_file
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

                    seed_tag = f"[seed={seed}]" if len(SIM_SEEDS) > 1 else ""
                    model_tag = f"{model_name}/" if len(models) > 1 else ""
                    log_key = f"LOR {model_tag}{workload_name}{seed_tag}"

                    try:
                        result = subprocess.run(
                            cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120,
                        )
                        if result.returncode != 0:
                            logger.warning("%s failed: %s", log_key, result.stderr[:300])
                            continue

                        output_text = result.stdout + (result.stderr or "")
                        cluster = parse_cluster_metrics(output_text)
                        if cluster and "e2e_mean_ms" in cluster:
                            e2e = float(cluster["e2e_mean_ms"])
                            p95 = float(cluster.get("e2e_p95_ms", e2e))
                            model_latencies.append(e2e)
                            model_tail_latencies.append(p95)
                            entry = {
                                "e2e_ms": e2e, "e2e_p95_ms": p95,
                                "completed_requests": cluster.get("completed_requests"),
                                "ttft_mean_ms": cluster.get("ttft_mean_ms"),
                                "tokens_per_sec": cluster.get("tokens_per_sec"),
                            }
                            model_workloads[workload_name].append(entry)
                            workload_agg[workload_name].append(entry)
                            logger.info("%s: e2e=%.2fms, p95=%.2fms", log_key, e2e, p95)
                        else:
                            logger.warning("%s: no cluster metrics found", log_key)
                    except (subprocess.TimeoutExpired, OSError) as exc:
                        logger.warning("%s failed: %s", log_key, exc)

            # Per-model summary
            per_model_entry = {"model_id": model_id, "workloads": {}}
            for wl_name, entries in model_workloads.items():
                if entries:
                    per_model_entry["workloads"][wl_name] = {
                        "e2e_ms": sum(e["e2e_ms"] for e in entries) / len(entries),
                        "e2e_p95_ms": sum(e["e2e_p95_ms"] for e in entries) / len(entries),
                    }
            if model_latencies:
                per_model_entry["avg_e2e_ms"] = sum(model_latencies) / len(model_latencies)
                per_model_entry["avg_p95_ms"] = sum(model_tail_latencies) / len(model_tail_latencies)
            per_model[model_name] = per_model_entry
            all_latencies.extend(model_latencies)
            all_tail_latencies.extend(model_tail_latencies)

        # Build results
        workload_results = {}
        for wl_name, entries in workload_agg.items():
            if entries:
                workload_results[wl_name] = {
                    "e2e_ms": sum(e["e2e_ms"] for e in entries) / len(entries),
                    "e2e_p95_ms": sum(e["e2e_p95_ms"] for e in entries) / len(entries),
                }
            else:
                workload_results[wl_name] = {"e2e_ms": None, "error": "All seeds/models failed"}

        if not all_latencies:
            logger.error("All simulations failed!")
            sys.exit(1)

        avg_e2e = sum(all_latencies) / len(all_latencies)
        avg_p95 = sum(all_tail_latencies) / len(all_tail_latencies)
        combined_score = -0.5 * avg_e2e - 0.5 * avg_p95

        metrics = {
            "router": "lor",
            "description": "Least Outstanding Requests — pure load balancing, no prefix affinity",
            "seeds": sorted(SIM_SEEDS),
            "multi_llm": MULTI_LLM,
            "num_instances": int(SIM_NUM_INSTANCES),
            "avg_e2e_ms": avg_e2e,
            "avg_p95_ms": avg_p95,
            "combined_score": combined_score,
            "workloads": workload_results,
            "per_model": per_model,
        }

        # Save
        metrics_path = output_dir / "lor_metrics.json"
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        logger.info("Saved LOR metrics to %s", metrics_path)

        # Print summary
        print(f"\n{'='*60}")
        print(f"LOR Baseline Results")
        print(f"{'='*60}")
        print(f"Combined score: {combined_score:.2f}")
        print(f"Avg E2E:        {avg_e2e:.2f} ms")
        print(f"Avg P95:        {avg_p95:.2f} ms")
        print(f"Seeds:          {sorted(SIM_SEEDS)}")
        print(f"Multi-LLM:      {MULTI_LLM}")
        print(f"\nPer-workload E2E (ms):")
        for wl_name, wl_data in workload_results.items():
            if wl_data.get("e2e_ms") is not None:
                print(f"  {wl_name:20s}: {wl_data['e2e_ms']:10.2f}")
        print(f"\nPer-workload P95 (ms):")
        for wl_name, wl_data in workload_results.items():
            if wl_data.get("e2e_p95_ms") is not None:
                print(f"  {wl_name:20s}: {wl_data['e2e_p95_ms']:10.2f}")
        print(f"\nMetrics saved to: {metrics_path}")

        return metrics

    finally:
        # Always restore original routing.go
        ROUTING_GO_PATH.write_text(original_routing)
        logger.info("Restored original routing.go")


def main():
    parser = argparse.ArgumentParser(description="Run LOR baseline against BLIS workloads")
    parser.add_argument(
        "--output-dir", type=str,
        default="outputs/blis_router/lor_baseline",
        help="Directory to save lor_metrics.json (default: outputs/blis_router/lor_baseline/)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    logger.info("Running LOR baseline (seeds=%s, multi_llm=%s)", SIM_SEEDS, MULTI_LLM)
    run_lor(output_dir)


if __name__ == "__main__":
    main()
