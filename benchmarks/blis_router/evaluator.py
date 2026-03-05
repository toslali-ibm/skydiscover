"""
BLIS Router Evaluator for SkyDiscover

Evaluates evolved routing algorithms by:
1. Extracting Go code from Python wrapper
2. Writing evolved routing.go to BLIS source (restored after evaluation)
3. Building BLIS
4. Running simulations on 3 routing-sensitive v2 workloads
5. Computing score based on average end-to-end latency

Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (higher = better)

Experiment isolation:
- routing.go is saved before and restored after every evaluation
- baseline_metrics.json is written to BLIS_OUTPUT_DIR (not benchmark dir)
- No artifacts are left in the benchmark directory between evaluations

NOTE: This evaluator is NOT safe for concurrent execution.
Multiple evaluate() calls MUST be serialized because they share routing.go.
"""

import json
import logging
import os
import re
import subprocess
import sys
import traceback
from difflib import unified_diff
from pathlib import Path

logger = logging.getLogger(__name__)

WORKLOADS = [
    ("cache_warmup", "workload_v2_cache_warmup.yaml"),
    ("load_spikes", "workload_v2_load_spikes.yaml"),
    ("multiturn", "workload_v2_multiturn.yaml"),
]

SIM_MODEL = os.environ.get("BLIS_MODEL", "meta-llama/llama-3.1-8b-instruct")
SIM_SEED = os.environ.get("BLIS_SEED", "42")


def _get_output_dir() -> Path:
    """Get the experiment output directory from env, falling back to a temp location."""
    output_dir = os.environ.get("BLIS_OUTPUT_DIR")
    if output_dir:
        return Path(output_dir)
    # Fallback for standalone testing only — warn loudly
    logger.warning(
        "BLIS_OUTPUT_DIR not set. Artifacts will be written to benchmark directory. "
        "Set BLIS_OUTPUT_DIR for experiment runs to ensure isolation."
    )
    fallback = Path(__file__).parent / ".eval_artifacts"
    fallback.mkdir(exist_ok=True)
    return fallback


def _build_sim_cmd(
    inference_sim_dir: Path, policy_config_path: Path, workload_path: Path
) -> list[str]:
    cmd = [
        "./simulation_worker", "run",
        "--model", SIM_MODEL,
        "--num-instances", "4",
        "--hardware", "H100", "--tp", "1",
        "--policy-config", str(policy_config_path),
        "--workload-spec", str(workload_path),
        "--log", "info",
        "--seed", SIM_SEED,
    ]
    if "Qwen" in SIM_MODEL:
        cmd += [
            "--hardware", "H100", "--tp", "1",
            "--alpha-coeffs", "4680.303204056608,0.0,0.0",
            "--beta-coeffs", "7051.796874715078,19.538416565504026,25.431830886933543",
            "--total-kv-blocks", "65833",
            "--max-num-running-reqs", "256",
            "--max-num-scheduled-tokens", "4096",
        ]
    return cmd


def _restore_routing_go(routing_go_path: Path, original_content: str):
    """Restore routing.go to its original content. Logs and re-raises on failure."""
    try:
        routing_go_path.write_text(original_content)
    except OSError as exc:
        logger.error(
            "CRITICAL: Failed to restore routing.go after evaluation! "
            "File may be corrupted. Path: %s, Error: %s",
            routing_go_path, exc,
        )
        raise


def extract_evolve_block(code: str) -> str:
    pattern = r"// EVOLVE-BLOCK-START(.*?)// EVOLVE-BLOCK-END"
    match = re.search(pattern, code, re.DOTALL)
    return match.group(1).strip() if match else ""


def print_diff(initial_code: str, current_code: str):
    initial_block = extract_evolve_block(initial_code)
    current_block = extract_evolve_block(current_code)
    if not initial_block or not current_block:
        return
    diff = list(unified_diff(
        initial_block.splitlines(keepends=True),
        current_block.splitlines(keepends=True),
        lineterm="",
    ))
    if not diff:
        logger.info("NO DIFF — code unchanged from initial")
        return
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    logger.info(f"Diff vs initial: -{removed} / +{added} lines")


def _parse_cluster_metrics(output_text: str) -> dict | None:
    json_blocks = []
    in_json = False
    json_buffer = ""
    brace_count = 0
    max_buffer = 1_000_000  # 1MB safety limit
    for line in output_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            in_json = True
            brace_count = 0
        if in_json:
            json_buffer += line + "\n"
            if len(json_buffer) > max_buffer:
                logger.warning("JSON buffer exceeded 1MB limit, resetting")
                json_buffer = ""
                in_json = False
                continue
            brace_count += stripped.count("{") - stripped.count("}")
            if brace_count == 0 and json_buffer.strip():
                try:
                    json_blocks.append(json.loads(json_buffer))
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "Skipped non-JSON block in simulator output (%s): %.200s",
                        exc, json_buffer.strip(),
                    )
                json_buffer = ""
                in_json = False
    for block in json_blocks:
        if block.get("instance_id") == "cluster":
            return block
    return None


def _error_result(error_msg: str, error_type: str, suggestion: str = "", **extra_artifacts) -> dict:
    return {
        "combined_score": -100000.0,
        "avg_e2e_ms": float("inf"),
        "error": error_msg,
        "artifacts": {
            "error_type": error_type,
            "error_message": error_msg,
            "suggestion": suggestion,
            **extra_artifacts,
        },
    }


def get_or_compute_baseline(
    script_dir: Path, inference_sim_dir: Path, policy_config_path: Path
) -> dict:
    """Compute baseline metrics, caching in the experiment output directory."""
    output_dir = _get_output_dir()
    cache_path = output_dir / "baseline_metrics.json"

    # Try to load from cache (no TOCTOU — rely on exception handling)
    try:
        with open(cache_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        pass  # No cache yet, will compute below
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read baseline cache, recomputing: %s", exc)

    initial_program_path = script_dir / "initial_program.py"
    if not initial_program_path.exists():
        logger.warning("initial_program.py not found; cannot compute baseline")
        return {}

    with open(initial_program_path, "r") as f:
        initial_text = f.read()

    go_code = extract_go_code(initial_text)
    if not go_code:
        logger.warning("Could not extract Go code from initial program for baseline")
        return {}

    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    if not routing_go_path.exists():
        logger.warning("routing.go not found at %s; initialize inference-sim submodule", routing_go_path)
        return {}

    # Save original routing.go, restore after baseline computation
    original_routing = routing_go_path.read_text()
    try:
        routing_go_path.write_text(go_code)

        build_result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=inference_sim_dir, capture_output=True, text=True, timeout=60,
        )
        if build_result.returncode != 0:
            logger.warning("Baseline build failed: %s", build_result.stderr[:300])
            return {}

        baseline = {}
        latencies = []
        tail_latencies = []

        for workload_name, workload_file in WORKLOADS:
            workload_path = script_dir / "workloads" / workload_file
            cmd = _build_sim_cmd(inference_sim_dir, policy_config_path, workload_path)
            try:
                sim_result = subprocess.run(
                    cmd, cwd=inference_sim_dir, capture_output=True, text=True, timeout=120,
                )
                if sim_result.returncode != 0:
                    logger.warning("Baseline %s failed: %s", workload_name, sim_result.stderr[:300])
                    continue
                output_text = sim_result.stdout + (sim_result.stderr or "")
                cluster_metrics = _parse_cluster_metrics(output_text)
                if cluster_metrics and "e2e_mean_ms" in cluster_metrics:
                    e2e_ms = float(cluster_metrics["e2e_mean_ms"])
                    e2e_p95_ms = float(cluster_metrics.get("e2e_p95_ms", e2e_ms))
                    baseline[f"{workload_name}_e2e_ms"] = e2e_ms
                    latencies.append(e2e_ms)
                    tail_latencies.append(e2e_p95_ms)
                else:
                    logger.warning("Baseline %s: no cluster metrics found", workload_name)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("Baseline %s error: %s", workload_name, exc)

        if latencies:
            avg_e2e = sum(latencies) / len(latencies)
            avg_p95 = sum(tail_latencies) / len(tail_latencies)
            baseline["avg_e2e_ms"] = avg_e2e
            baseline["avg_p95_ms"] = avg_p95
            baseline["combined_score"] = -0.5 * avg_e2e - 0.5 * avg_p95

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(baseline, f, indent=2)
            logger.info("Cached baseline metrics to %s", cache_path)
        except OSError as exc:
            logger.warning("Failed to cache baseline metrics: %s", exc)

        return baseline

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Baseline computation error: %s", exc)
        return {}
    finally:
        _restore_routing_go(routing_go_path, original_routing)


def extract_go_code(program_text: str) -> str:
    match = re.search(r'GO_ROUTING_CODE\s*=\s*"""(.*?)"""', program_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    if "package sim" in program_text:
        return program_text
    return ""


def evaluate(program_path: str) -> dict:
    try:
        with open(program_path, "r") as f:
            program_text = f.read()
    except (OSError, IOError) as exc:
        return _error_result(
            f"Cannot read program file: {exc}", "FileReadError",
            f"Check that {program_path} exists and is readable",
        )

    script_dir = Path(__file__).parent
    inference_sim_dir = script_dir / "inference-sim"
    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    policy_config_path = script_dir / "routing" / "routing_policy.yaml"

    # Verify routing.go exists (submodule must be initialized)
    if not routing_go_path.exists():
        return _error_result(
            f"routing.go not found at {routing_go_path}",
            "SetupError",
            "Initialize the inference-sim submodule: git submodule update --init",
        )

    go_code = extract_go_code(program_text)
    if not go_code:
        return _error_result(
            "Failed to extract Go code", "ExtractionError",
            'Ensure program contains GO_ROUTING_CODE = """...""" or starts with "package sim"',
        )

    logger.info(f"Extracted Go code: {len(go_code)} chars")

    try:
        initial_program_path = script_dir / "initial_program.py"
        if initial_program_path.exists():
            with open(initial_program_path, "r") as f:
                initial_go_code = extract_go_code(f.read())
            if initial_go_code:
                print_diff(initial_go_code, go_code)
    except Exception as exc:
        logger.warning("Failed to compute diff against initial program (non-fatal): %s", exc)

    baseline = get_or_compute_baseline(script_dir, inference_sim_dir, policy_config_path)
    if not baseline:
        logger.warning("Baseline metrics unavailable; score comparison will not be possible")

    # Save original routing.go before mutation
    original_routing = routing_go_path.read_text()

    try:
        try:
            routing_go_path.write_text(go_code)
        except Exception as e:
            return _error_result(
                f"Failed to write file: {e}", "FileWriteError",
                full_traceback=traceback.format_exc(),
            )

        try:
            result = subprocess.run(
                ["go", "build", "-o", "simulation_worker", "main.go"],
                cwd=inference_sim_dir, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                error_summary = result.stderr.strip()[:500] if result.stderr else "Unknown build error"
                return _error_result(
                    f"Build failed: {error_summary}", "BuildError",
                    "Check for Go syntax errors in the evolved EVOLVE-BLOCK section",
                    build_stderr=result.stderr,
                )
        except subprocess.TimeoutExpired:
            return _error_result("Build timeout (60s)", "BuildTimeout")
        except Exception as e:
            return _error_result(
                f"Build error: {e}", type(e).__name__,
                full_traceback=traceback.format_exc(),
            )

        latencies = []
        tail_latencies = []
        request_counts = []
        workload_results = {}
        failed_workloads = []

        for workload_name, workload_file in WORKLOADS:
            try:
                workload_path = script_dir / "workloads" / workload_file
                cmd = _build_sim_cmd(inference_sim_dir, policy_config_path, workload_path)
                result = subprocess.run(
                    cmd, cwd=inference_sim_dir, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    failed_workloads.append(workload_name)
                    workload_results[workload_name] = {"e2e_ms": None, "error": "Simulation failed", "stderr": result.stderr[:500]}
                    continue

                output_text = result.stdout + (result.stderr or "")
                cluster_metrics = _parse_cluster_metrics(output_text)

                if cluster_metrics and "e2e_mean_ms" in cluster_metrics:
                    e2e_ms = float(cluster_metrics["e2e_mean_ms"])
                    e2e_p95_ms = float(cluster_metrics.get("e2e_p95_ms", e2e_ms))
                    latencies.append(e2e_ms)
                    tail_latencies.append(e2e_p95_ms)
                    request_counts.append(int(cluster_metrics.get("completed_requests", 1)))
                    workload_results[workload_name] = {
                        "e2e_ms": e2e_ms, "e2e_p95_ms": e2e_p95_ms,
                        "completed_requests": cluster_metrics.get("completed_requests"),
                        "ttft_mean_ms": cluster_metrics.get("ttft_mean_ms"),
                        "tokens_per_sec": cluster_metrics.get("tokens_per_sec"),
                    }
                    logger.info(f"{workload_name}: e2e_mean={e2e_ms:.2f}ms, p95={e2e_p95_ms:.2f}ms")
                else:
                    failed_workloads.append(workload_name)
                    workload_results[workload_name] = {"e2e_ms": None, "error": "No cluster metrics found"}

            except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
                failed_workloads.append(workload_name)
                workload_results[workload_name] = {"e2e_ms": None, "error": str(e)}
                logger.warning("Workload %s failed: %s", workload_name, e)
            except Exception as e:
                logger.error("Unexpected error in workload %s: %s", workload_name, e, exc_info=True)
                failed_workloads.append(workload_name)
                workload_results[workload_name] = {"e2e_ms": None, "error": f"Unexpected: {e}"}

        if len(latencies) == 0:
            return _error_result(
                "All workloads failed", "AllWorkloadsFailed",
                "Check BLIS simulation errors. May be routing logic causing crashes or timeouts.",
                failed_workloads=failed_workloads, workload_results=workload_results,
            )

        avg_latency = sum(latencies) / len(latencies)
        avg_tail_latency = sum(tail_latencies) / len(tail_latencies)

        score = -0.5 * avg_latency - 0.5 * avg_tail_latency
        success_rate = len(latencies) / len(WORKLOADS)

        return {
            "combined_score": score,
            "avg_e2e_ms": avg_latency,
            "avg_p95_ms": avg_tail_latency,
            "cache_warmup_e2e_ms": workload_results.get("cache_warmup", {}).get("e2e_ms"),
            "load_spikes_e2e_ms": workload_results.get("load_spikes", {}).get("e2e_ms"),
            "multiturn_e2e_ms": workload_results.get("multiturn", {}).get("e2e_ms"),
            "success_rate": success_rate,
            "num_successful": len(latencies),
            "num_failed": len(failed_workloads),
            "artifacts": {
                "workload_results": workload_results,
                "successful_workloads": len(latencies),
                "failed_workloads": len(failed_workloads),
                "success_rate": f"{success_rate:.0%}",
                **({"warning": f"Some workloads failed: {', '.join(failed_workloads)}",
                    "suggestion": "Check if evolved routing logic causes crashes or extreme slowdowns"}
                   if failed_workloads else {}),
            },
        }

    finally:
        # Always restore original routing.go — no state leaks between evaluations
        _restore_routing_go(routing_go_path, original_routing)


if __name__ == "__main__":
    print("Testing evaluator with initial program...")
    script_dir = Path(__file__).parent
    result = evaluate(str(script_dir / "initial_program.py"))
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        print(f"  Suggestion: {result.get('artifacts', {}).get('suggestion', 'N/A')}")
        sys.exit(1)
    score = result.get("combined_score")
    avg_e2e = result.get("avg_e2e_ms")
    print(f"  Score: {score:.2f}" if score is not None else "  Score: N/A")
    print(f"  Avg E2E: {avg_e2e:.2f}ms" if avg_e2e and avg_e2e != float("inf") else "  Avg E2E: N/A")
