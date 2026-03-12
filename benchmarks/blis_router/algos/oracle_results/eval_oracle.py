#!/usr/bin/env python3
"""
Standalone oracle router evaluator.
Tests a routing.go file against both workloads, both models, 2 seeds.
Computes scores vs the 1:1 baseline.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent
INFERENCE_SIM_DIR = SCRIPT_DIR / "inference-sim"
ROUTING_GO_PATH = INFERENCE_SIM_DIR / "sim" / "routing.go"
POLICY_CONFIG = SCRIPT_DIR / "routing" / "routing_policy.yaml"

WORKLOADS = [
    ("prefix_heavy", "workload_glia_prefix_heavy.yaml"),
    ("glia_40qps", "workload_glia_40qps.yaml"),
]

MODELS = [
    ("qwen_7b", "qwen/qwen2.5-7b-instruct", ["--hardware", "H100", "--tp", "1"]),
    ("qwen_14b", "qwen/qwen3-14b", ["--hardware", "H100", "--tp", "1"]),
]

SEEDS = ["42", "456"]
NUM_INSTANCES = "4"
SNAPSHOT_REFRESH = "5000000"

# 1:1 baseline reference values (from baseline_comparison.json)
BASELINE_1_1 = {
    "prefix_heavy": {
        "qwen_7b": {"e2e_ms": 56.956054, "p95_ms": 140.59950},
        "qwen_14b": {"e2e_ms": 131.544989, "p95_ms": 330.67013},
    },
    "glia_40qps": {
        "qwen_7b": {"e2e_ms": 4314.379941, "p95_ms": 17241.3084},
        "qwen_14b": {"e2e_ms": 9054.555143, "p95_ms": 33122.76375},
    },
}


def parse_cluster_metrics(output_text):
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


def run_simulation(model_id, extra_args, workload_file, seed):
    workload_path = SCRIPT_DIR / "workloads" / workload_file
    cmd = [
        "./simulation_worker", "run",
        "--model", model_id,
        "--num-instances", NUM_INSTANCES,
        "--policy-config", str(POLICY_CONFIG),
        "--workload-spec", str(workload_path),
        "--snapshot-refresh-interval", SNAPSHOT_REFRESH,
        "--log", "info",
        "--seed", seed,
    ] + extra_args
    try:
        result = subprocess.run(cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None, result.stderr[:200]
        return parse_cluster_metrics(result.stdout + (result.stderr or "")), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def build():
    result = subprocess.run(
        ["go", "build", "-o", "simulation_worker", "main.go"],
        cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return False, result.stderr[:500]
    return True, ""


def evaluate_routing_go(algo_path):
    """Evaluate a routing.go file. Returns results dict."""
    original = ROUTING_GO_PATH.read_text()
    algo_code = Path(algo_path).read_text()

    try:
        ROUTING_GO_PATH.write_text(algo_code)
        ok, err = build()
        if not ok:
            return {"error": f"Build failed: {err}"}

        results = {}
        for wl_name, wl_file in WORKLOADS:
            results[wl_name] = {}
            for model_name, model_id, extra_args in MODELS:
                seed_results = []
                for seed in SEEDS:
                    metrics, err = run_simulation(model_id, extra_args, wl_file, seed)
                    if metrics and "e2e_mean_ms" in metrics:
                        seed_results.append({
                            "e2e_ms": float(metrics["e2e_mean_ms"]),
                            "p95_ms": float(metrics.get("e2e_p95_ms", metrics["e2e_mean_ms"])),
                        })
                    else:
                        print(f"  WARN: {wl_name}/{model_name}/seed={seed} failed: {err}")
                if seed_results:
                    avg_e2e = sum(r["e2e_ms"] for r in seed_results) / len(seed_results)
                    avg_p95 = sum(r["p95_ms"] for r in seed_results) / len(seed_results)
                    results[wl_name][model_name] = {"e2e_ms": avg_e2e, "p95_ms": avg_p95}

        return results
    finally:
        ROUTING_GO_PATH.write_text(original)


def compute_score(results):
    """Compute score vs 1:1 baseline using evaluator formula."""
    improvements = []
    for wl_name in ["prefix_heavy", "glia_40qps"]:
        wl_data = results.get(wl_name, {})
        if not wl_data:
            continue
        # Average across models
        cand_e2e_vals = [wl_data[m]["e2e_ms"] for m in wl_data]
        cand_p95_vals = [wl_data[m]["p95_ms"] for m in wl_data]
        bl_e2e_vals = [BASELINE_1_1[wl_name][m]["e2e_ms"] for m in wl_data]
        bl_p95_vals = [BASELINE_1_1[wl_name][m]["p95_ms"] for m in wl_data]

        cand_e2e = sum(cand_e2e_vals) / len(cand_e2e_vals)
        cand_p95 = sum(cand_p95_vals) / len(cand_p95_vals)
        bl_e2e = sum(bl_e2e_vals) / len(bl_e2e_vals)
        bl_p95 = sum(bl_p95_vals) / len(bl_p95_vals)

        cand_val = 0.5 * cand_e2e + 0.5 * cand_p95
        bl_val = 0.5 * bl_e2e + 0.5 * bl_p95
        improvements.append(1.0 - cand_val / bl_val)

    if improvements:
        return sum(improvements) / len(improvements) * 100.0
    return None


def print_results(name, results, score):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    if "error" in results:
        print(f"  ERROR: {results['error']}")
        return

    for wl_name in ["prefix_heavy", "glia_40qps"]:
        wl = results.get(wl_name, {})
        print(f"\n  {wl_name}:")
        for model_name in ["qwen_7b", "qwen_14b"]:
            m = wl.get(model_name, {})
            bl = BASELINE_1_1[wl_name][model_name]
            if m:
                e2e_pct = (1 - m["e2e_ms"] / bl["e2e_ms"]) * 100
                p95_pct = (1 - m["p95_ms"] / bl["p95_ms"]) * 100
                print(f"    {model_name}: e2e={m['e2e_ms']:.2f}ms ({e2e_pct:+.1f}%), "
                      f"p95={m['p95_ms']:.2f}ms ({p95_pct:+.1f}%)")
            else:
                print(f"    {model_name}: FAILED")

    if score is not None:
        print(f"\n  COMBINED SCORE vs 1:1 baseline: {score:+.2f}%")
        if score > 0:
            print(f"  >>> BEATS 1:1 baseline by {score:.2f}% <<<")
        else:
            print(f"  --- Below 1:1 baseline by {abs(score):.2f}% ---")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python eval_oracle.py <routing.go> [name]")
        sys.exit(1)

    algo_path = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else Path(algo_path).stem

    print(f"Evaluating {name} from {algo_path}...")
    start = time.time()
    results = evaluate_routing_go(algo_path)
    elapsed = time.time() - start

    if "error" not in results:
        score = compute_score(results)
        print_results(name, results, score)
        print(f"\n  Time: {elapsed:.1f}s")

        # Save results
        out_dir = SCRIPT_DIR / "algos" / "oracle_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{name}_results.json"
        with open(out_file, "w") as f:
            json.dump({"name": name, "results": results, "score": score, "elapsed_s": elapsed}, f, indent=2)
        print(f"  Results saved to {out_file}")
    else:
        print_results(name, results, None)
        print(f"\n  Time: {elapsed:.1f}s")
