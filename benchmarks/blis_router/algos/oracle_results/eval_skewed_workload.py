#!/usr/bin/env python3
"""
Evaluate all baselines + oracle routers against the skewed prefix workload.

Tests whether unequal group rates (55/15/15/15%) expose the 1:1 baseline's
weakness and allow an oracle to beat it by 15%+.

Configs tested:
  - LLQ, LOR, Glia (no prefix awareness)
  - 1:1 baseline (prefix-affinity:1 + load-balance:1)
  - 3:2:2 (prefix:3, queue-depth:2, kv-util:2)
  - Oracle v4 (fresh InFlightRequests)
  - Oracle v1 (adaptive prefix weights)

Usage:
    python benchmarks/blis_router/scripts/eval_skewed_workload.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # benchmarks/blis_router/
INFERENCE_SIM_DIR = SCRIPT_DIR / "inference-sim"
ROUTING_GO_PATH = INFERENCE_SIM_DIR / "sim" / "routing.go"
POLICY_CONFIG_PATH = SCRIPT_DIR / "routing" / "routing_policy.yaml"
POLICY_3_2_2_PATH = SCRIPT_DIR / "routing" / "routing_policy_3_2_2.yaml"

# Test against the skewed workloads (can be overridden by CLI arg)
WORKLOAD_FILES = [
    ("skewed_prefix", "workload_skewed_prefix.yaml"),
    ("skewed_heavy", "workload_skewed_heavy.yaml"),
    ("skewed_moderate", "workload_skewed_moderate.yaml"),
    ("skewed_sweet_spot", "workload_skewed_sweet_spot.yaml"),
    ("skewed_optimal", "workload_skewed_optimal.yaml"),
    ("many_groups", "workload_many_groups.yaml"),
    ("composite_stress", "workload_composite_stress.yaml"),
    ("composite_v2", "workload_composite_stress_v2.yaml"),
    ("anti_glia", "workload_anti_glia.yaml"),
    ("final", "workload_final.yaml"),
]

MODELS = [
    ("qwen_7b", "qwen/qwen2.5-7b-instruct", ["--hardware", "H100", "--tp", "1"]),
    ("qwen_14b", "qwen/qwen3-14b", ["--hardware", "H100", "--tp", "1"]),
]

SEEDS = ["42", "456"]
NUM_INSTANCES = "4"
SNAPSHOT_REFRESH = "5000000"

# Configs: (name, routing_go_path, policy_yaml_path)
CONFIGS = [
    ("llq", SCRIPT_DIR / "algos" / "initial_program_llq.go", POLICY_CONFIG_PATH),
    ("lor", SCRIPT_DIR / "algos" / "initial_program_lor.go", POLICY_CONFIG_PATH),
    ("glia", SCRIPT_DIR / "algos" / "initial_program_glia.go", POLICY_CONFIG_PATH),
    ("1_1", SCRIPT_DIR / "initial_program.go", POLICY_CONFIG_PATH),
    ("3_2_2", SCRIPT_DIR / "initial_program.go", POLICY_3_2_2_PATH),
    ("oracle_v4", SCRIPT_DIR / "algos" / "oracle_router.go", POLICY_CONFIG_PATH),
]

# Also test oracle v1 (adaptive prefix) if it exists
oracle_v1_path = SCRIPT_DIR / "algos" / "oracle_v1.go"
if oracle_v1_path.exists():
    CONFIGS.append(("oracle_v1", oracle_v1_path, POLICY_CONFIG_PATH))

# Oracle v10 (load-overflow router)
oracle_v10_path = SCRIPT_DIR / "algos" / "oracle_v10.go"
if oracle_v10_path.exists():
    CONFIGS.append(("oracle_v10", oracle_v10_path, POLICY_CONFIG_PATH))

# Oracle v11 (adaptive load-overflow router)
oracle_v11_path = SCRIPT_DIR / "algos" / "oracle_v11.go"
if oracle_v11_path.exists():
    CONFIGS.append(("oracle_v11", oracle_v11_path, POLICY_CONFIG_PATH))

# Oracle v12 (prefix-spread, low prefix weight)
oracle_v12_path = SCRIPT_DIR / "algos" / "oracle_v12.go"
if oracle_v12_path.exists():
    CONFIGS.append(("oracle_v12", oracle_v12_path, POLICY_CONFIG_PATH))

# Oracle v13 (composite hypothesis oracle)
oracle_v13_path = SCRIPT_DIR / "algos" / "oracle_v13.go"
if oracle_v13_path.exists():
    CONFIGS.append(("oracle_v13", oracle_v13_path, POLICY_CONFIG_PATH))

# Oracle v14 (anti-glia tuned)
oracle_v14_path = SCRIPT_DIR / "algos" / "oracle_v14.go"
if oracle_v14_path.exists():
    CONFIGS.append(("oracle_v14", oracle_v14_path, POLICY_CONFIG_PATH))


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


def run_simulation(model_id, extra_args, workload_file, seed, policy_config):
    workload_path = SCRIPT_DIR / "workloads" / workload_file
    cmd = [
        "./simulation_worker", "run",
        "--model", model_id,
        "--num-instances", NUM_INSTANCES,
        "--policy-config", str(policy_config),
        "--workload-spec", str(workload_path),
        "--snapshot-refresh-interval", SNAPSHOT_REFRESH,
        "--log", "info",
        "--seed", seed,
    ] + extra_args
    try:
        result = subprocess.run(cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None, result.stderr[:500]
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


def evaluate_config(name, routing_go_path, policy_config_path, workload_file):
    """Evaluate a single config against a specific workload."""
    original_routing = ROUTING_GO_PATH.read_text()
    original_policy = POLICY_CONFIG_PATH.read_text()
    algo_code = Path(routing_go_path).read_text()

    # If policy is different from default, swap it
    swap_policy = (str(policy_config_path) != str(POLICY_CONFIG_PATH))
    if swap_policy:
        policy_code = Path(policy_config_path).read_text()

    try:
        ROUTING_GO_PATH.write_text(algo_code)
        if swap_policy:
            POLICY_CONFIG_PATH.write_text(policy_code)

        ok, err = build()
        if not ok:
            return {"error": f"Build failed: {err}"}

        results = {}
        for model_name, model_id, extra_args in MODELS:
            seed_results = []
            for seed in SEEDS:
                metrics, err = run_simulation(model_id, extra_args, workload_file, seed, POLICY_CONFIG_PATH)
                if metrics and "e2e_mean_ms" in metrics:
                    seed_results.append({
                        "e2e_ms": float(metrics["e2e_mean_ms"]),
                        "p95_ms": float(metrics.get("e2e_p95_ms", metrics["e2e_mean_ms"])),
                    })
                else:
                    print(f"  WARN: {name}/{model_name}/seed={seed} failed: {err}")
            if seed_results:
                avg_e2e = sum(r["e2e_ms"] for r in seed_results) / len(seed_results)
                avg_p95 = sum(r["p95_ms"] for r in seed_results) / len(seed_results)
                results[model_name] = {"e2e_ms": avg_e2e, "p95_ms": avg_p95}

        return results
    finally:
        ROUTING_GO_PATH.write_text(original_routing)
        if swap_policy:
            POLICY_CONFIG_PATH.write_text(original_policy)


def print_workload_results(wl_name, all_results, config_names):
    """Print results table for one workload."""
    baseline = all_results.get("1_1", {})
    if "error" in baseline:
        print("ERROR: 1:1 baseline failed, cannot compute scores")
        return

    print(f"\n{'='*70}")
    print(f"  RESULTS vs 1:1 BASELINE — {wl_name}")
    print(f"{'='*70}")

    print(f"\n{'Algorithm':<15} {'qwen_7b E2E':>12} {'qwen_7b P95':>12} {'qwen_14b E2E':>13} {'qwen_14b P95':>13} {'Score':>8}")
    print("-" * 80)

    for name in config_names:
        results = all_results.get(name, {})
        if "error" in results:
            print(f"{name:<15} {'ERROR':>12}")
            continue

        improvements = []
        line_parts = [f"{name:<15}"]

        for model_name in ["qwen_7b", "qwen_14b"]:
            r = results.get(model_name)
            bl = baseline.get(model_name)
            if r and bl:
                line_parts.append(f"{r['e2e_ms']:>10.2f}{'ms':>2}")
                line_parts.append(f"{r['p95_ms']:>10.2f}{'ms':>2}")
                cand_val = 0.5 * r["e2e_ms"] + 0.5 * r["p95_ms"]
                bl_val = 0.5 * bl["e2e_ms"] + 0.5 * bl["p95_ms"]
                improvements.append(1.0 - cand_val / bl_val)
            else:
                line_parts.append(f"{'FAIL':>12}")
                line_parts.append(f"{'FAIL':>12}")

        if improvements:
            score = sum(improvements) / len(improvements) * 100.0
            line_parts.append(f"{score:>+7.2f}%")
        else:
            line_parts.append(f"{'N/A':>8}")

        print(" ".join(line_parts))

    # Key comparisons
    oracle_names = [n for n in config_names if n.startswith("oracle")]
    ref_names = ["1_1", "llq", "glia", "lor"]
    if oracle_names:
        print(f"\n  Key comparisons:")
        for cand_name in oracle_names:
            cand = all_results.get(cand_name)
            if not cand or "error" in cand:
                continue
            for ref_name in ref_names:
                ref = all_results.get(ref_name)
                if not ref or "error" in ref:
                    continue
                improvements = []
                for model_name in ["qwen_7b", "qwen_14b"]:
                    cr = cand.get(model_name)
                    rr = ref.get(model_name)
                    if cr and rr:
                        cand_val = 0.5 * cr["e2e_ms"] + 0.5 * cr["p95_ms"]
                        ref_val = 0.5 * rr["e2e_ms"] + 0.5 * rr["p95_ms"]
                        improvements.append(1.0 - cand_val / ref_val)
                if improvements:
                    score = sum(improvements) / len(improvements) * 100.0
                    marker = ">>>" if score > 15 else "  >" if score > 0 else "   "
                    print(f"  {marker} {cand_name} vs {ref_name}: {score:+.2f}%")


def main():
    # Allow testing a single workload via CLI
    target_workloads = WORKLOAD_FILES
    if len(sys.argv) > 1:
        wl_name = sys.argv[1]
        for name, f in WORKLOAD_FILES:
            if name == wl_name:
                target_workloads = [(name, f)]
                break
        else:
            # Treat as a workload file name
            target_workloads = [(wl_name, wl_name if wl_name.endswith(".yaml") else f"workload_{wl_name}.yaml")]

    config_names = [c[0] for c in CONFIGS]

    grand_results = {}

    for wl_name, wl_file in target_workloads:
        print(f"\n{'#'*70}")
        print(f"  WORKLOAD: {wl_name} ({wl_file})")
        print(f"  Models: {', '.join(m[0] for m in MODELS)}")
        print(f"  Seeds: {', '.join(SEEDS)}, Instances: {NUM_INSTANCES}")
        print(f"{'#'*70}\n")

        all_results = {}
        for name, routing_go, policy_yaml in CONFIGS:
            print(f"  [{wl_name}] Evaluating {name}...")
            start = time.time()
            results = evaluate_config(name, routing_go, policy_yaml, wl_file)
            elapsed = time.time() - start
            all_results[name] = results
            if "error" in results:
                print(f"    ERROR: {results['error']}")
            else:
                for model_name in results:
                    r = results[model_name]
                    print(f"    {model_name}: e2e={r['e2e_ms']:.2f}ms, p95={r['p95_ms']:.2f}ms")
            print(f"    ({elapsed:.1f}s)")

        grand_results[wl_name] = all_results
        print_workload_results(wl_name, all_results, config_names)

    # Save all results
    out_dir = SCRIPT_DIR / "algos" / "oracle_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "skewed_workload_results.json"
    with open(out_file, "w") as f:
        json.dump(grand_results, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
