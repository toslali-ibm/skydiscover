#!/usr/bin/env python3
"""
One-off script: Compare Baseline (1:1), LOR, and best discovered algorithms
on v3 workloads (Vidur-rate: ~17 QPS, 400-500 requests).

Tests whether strategies discovered at high load (v2: 200-300 QPS) transfer
to low load (v3: ~17 QPS).

Usage:
    python benchmarks/blis_router/scripts/compare_v3_workloads.py

Output: outputs/blis_router/v3_comparison/
"""

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

V3_WORKLOADS = [
    ("cache_warmup", "workload_v3_cache_warmup.yaml"),
    ("load_spikes", "workload_v3_load_spikes.yaml"),
    ("multiturn", "workload_v3_multiturn.yaml"),
]

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

EXPERIMENT_DIR = Path("outputs/blis_router/260309_50i_twoseed")

# Routers to test: (name, path_to_go_file)
ROUTERS = {
    "baseline_1to1": SCRIPT_DIR / "initial_program.go",
    "lor": SCRIPT_DIR / "algos" / "initial_program_lor.go",
    "vidur_best": SCRIPT_DIR / "algos" / "initial_program_vidur.go",
}

# Add best discovered algorithms
for fw in ["openevolve", "evox", "gepa_native", "adaevolve"]:
    best_path = EXPERIMENT_DIR / fw / "best" / "best_program.go"
    if best_path.exists():
        ROUTERS[fw] = best_path


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


def run_router(name: str, go_file: Path) -> dict | None:
    """Run a router against v3 workloads, return metrics."""
    logger.info("Running %s (%s)...", name, go_file.name)

    go_code = go_file.read_text()
    original_routing = ROUTING_GO_PATH.read_text()

    try:
        ROUTING_GO_PATH.write_text(go_code)

        # Build
        build = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            logger.error("%s build failed:\n%s", name, build.stderr[:500])
            return None

        models = get_models()
        all_e2e = []
        all_p95 = []
        workload_results = {}

        for wl_name, wl_file in V3_WORKLOADS:
            wl_path = SCRIPT_DIR / "workloads" / wl_file
            wl_entries = []

            for model_name, model_id, extra_args in models:
                for seed in SIM_SEEDS:
                    cmd = [
                        "./simulation_worker", "run",
                        "--model", model_id,
                        "--num-instances", SIM_NUM_INSTANCES,
                        "--policy-config", str(POLICY_CONFIG_PATH),
                        "--workload-spec", str(wl_path),
                        "--snapshot-refresh-interval", SIM_SNAPSHOT_REFRESH,
                        "--log", "info",
                        "--seed", seed,
                    ] + extra_args

                    try:
                        result = subprocess.run(
                            cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120,
                        )
                        if result.returncode != 0:
                            continue
                        output = result.stdout + (result.stderr or "")
                        cluster = parse_cluster_metrics(output)
                        if cluster and "e2e_mean_ms" in cluster:
                            e2e = float(cluster["e2e_mean_ms"])
                            p95 = float(cluster.get("e2e_p95_ms", e2e))
                            wl_entries.append({"e2e_ms": e2e, "e2e_p95_ms": p95})
                            all_e2e.append(e2e)
                            all_p95.append(p95)
                    except (subprocess.TimeoutExpired, OSError):
                        pass

            if wl_entries:
                workload_results[wl_name] = {
                    "e2e_ms": sum(e["e2e_ms"] for e in wl_entries) / len(wl_entries),
                    "e2e_p95_ms": sum(e["e2e_p95_ms"] for e in wl_entries) / len(wl_entries),
                }
            else:
                workload_results[wl_name] = {"e2e_ms": None, "e2e_p95_ms": None}

        if not all_e2e:
            logger.error("%s: all simulations failed", name)
            return None

        avg_e2e = sum(all_e2e) / len(all_e2e)
        avg_p95 = sum(all_p95) / len(all_p95)
        score = -0.5 * avg_e2e - 0.5 * avg_p95

        logger.info("%s: score=%.2f, e2e=%.2f, p95=%.2f", name, score, avg_e2e, avg_p95)
        return {
            "combined_score": score,
            "avg_e2e_ms": avg_e2e,
            "avg_p95_ms": avg_p95,
            "workloads": workload_results,
        }

    finally:
        ROUTING_GO_PATH.write_text(original_routing)


def pct_imp(old, new):
    if old == 0:
        return "N/A"
    imp = (old - new) / abs(old) * 100
    return f"+{imp:.1f}%" if imp >= 0 else f"{imp:.1f}%"


def main():
    output_dir = Path("outputs/blis_router/v3_comparison")
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("V3 workload comparison (Vidur-rate, seeds=%s, multi_llm=%s)", SIM_SEEDS, MULTI_LLM)
    logger.info("Routers: %s", list(ROUTERS.keys()))

    results = {}
    for name, go_file in ROUTERS.items():
        metrics = run_router(name, go_file)
        if metrics:
            results[name] = metrics

    if not results:
        logger.error("No routers completed successfully")
        sys.exit(1)

    # Save raw results
    raw_path = output_dir / "v3_results.json"
    with open(raw_path, "w") as f:
        json.dump(results, f, indent=2)

    # ── Print comparison ──
    W = 95
    print(f"\n{'='*W}")
    print("V3 WORKLOAD COMPARISON (Vidur-rate: ~17 QPS, 400-500 requests)")
    print(f"{'='*W}")
    print(f"Seeds: {sorted(SIM_SEEDS)} | Multi-LLM: {MULTI_LLM} | Instances: {SIM_NUM_INSTANCES}")

    # Sort: baseline first, lor second, then frameworks by score
    order = []
    if "baseline_1to1" in results:
        order.append("baseline_1to1")
    if "lor" in results:
        order.append("lor")
    fws = sorted(
        [k for k in results if k not in ("baseline_1to1", "lor")],
        key=lambda k: results[k]["combined_score"],
        reverse=True,
    )
    order.extend(fws)

    lor_score = results.get("lor", {}).get("combined_score")
    bl_score = results.get("baseline_1to1", {}).get("combined_score")

    # Aggregate table
    print(f"\n{'AGGREGATE SCORES':^{W}}")
    print(f"{'-'*W}")
    print(f"{'Router':<20} {'Score':>12} {'Avg E2E':>12} {'Avg P95':>12} {'vs LOR':>10} {'vs Baseline':>12}")
    print(f"{'-'*W}")

    for name in order:
        r = results[name]
        vs_lor = pct_imp(lor_score, r["combined_score"]) if lor_score else "---"
        vs_bl = pct_imp(bl_score, r["combined_score"]) if bl_score else "---"
        if name == "baseline_1to1":
            vs_bl = "---"
        if name == "lor":
            vs_lor = "---"
        if name == "baseline_1to1":
            vs_lor = pct_imp(lor_score, r["combined_score"]) if lor_score else "---"
        print(f"{name:<20} {r['combined_score']:>12.2f} {r['avg_e2e_ms']:>12.2f} {r['avg_p95_ms']:>12.2f} {vs_lor:>10} {vs_bl:>12}")

    # Per-workload E2E
    workloads = ["cache_warmup", "load_spikes", "multiturn"]
    print(f"\n{'PER-WORKLOAD E2E (ms)':^{W}}")
    print(f"{'-'*W}")
    hdr = f"{'Workload':<18}"
    for name in order:
        hdr += f" {name:>14}"
    print(hdr)
    print(f"{'-'*W}")
    for wl in workloads:
        row = f"{wl:<18}"
        for name in order:
            val = results[name].get("workloads", {}).get(wl, {}).get("e2e_ms")
            row += f" {val:>14.1f}" if val else f" {'N/A':>14}"
        print(row)

    # Per-workload P95
    print(f"\n{'PER-WORKLOAD P95 (ms)':^{W}}")
    print(f"{'-'*W}")
    print(hdr)
    print(f"{'-'*W}")
    for wl in workloads:
        row = f"{wl:<18}"
        for name in order:
            val = results[name].get("workloads", {}).get(wl, {}).get("e2e_p95_ms")
            row += f" {val:>14.1f}" if val else f" {'N/A':>14}"
        print(row)

    # V2 vs V3 comparison (if v2 LOR results exist)
    lor_v2_path = Path("outputs/blis_router/lor_baseline/lor_metrics.json")
    if lor_v2_path.exists() and "lor" in results:
        lor_v2 = json.loads(lor_v2_path.read_text())
        print(f"\n{'V2 vs V3 RATE SENSITIVITY (LOR router)':^{W}}")
        print(f"{'-'*W}")
        print(f"{'Metric':<25} {'V2 (high rate)':>18} {'V3 (Vidur rate)':>18} {'Change':>12}")
        print(f"{'-'*W}")
        for metric, label in [("combined_score", "Score"), ("avg_e2e_ms", "Avg E2E (ms)"), ("avg_p95_ms", "Avg P95 (ms)")]:
            v2_val = lor_v2.get(metric)
            v3_val = results["lor"].get(metric)
            if v2_val is not None and v3_val is not None:
                change = pct_imp(v2_val, v3_val) if metric == "combined_score" else f"{((v3_val - v2_val) / v2_val * 100):+.1f}%"
                print(f"{label:<25} {v2_val:>18.2f} {v3_val:>18.2f} {change:>12}")

    # Value decomposition
    if "lor" in results and "baseline_1to1" in results:
        best_name = fws[0] if fws else None
        if best_name:
            best_score = results[best_name]["combined_score"]
            print(f"\n{'VALUE DECOMPOSITION (V3)':^{W}}")
            print(f"{'-'*W}")
            prefix_val = bl_score - lor_score
            evo_val = best_score - bl_score
            total_val = best_score - lor_score
            print(f"Prefix-affinity over LOR:  {prefix_val:>+10.2f} score")
            print(f"Evolution over baseline:    {evo_val:>+10.2f} score")
            print(f"Total (LOR → {best_name}): {total_val:>+10.2f} score")
            if abs(lor_score) > 0:
                print(f"LOR → best improvement:    {pct_imp(lor_score, best_score)}")

    print(f"\n{'='*W}")
    print(f"Results saved to: {raw_path}")
    print(f"{'='*W}\n")


if __name__ == "__main__":
    main()
