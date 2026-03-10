"""Vidur Router Benchmark — Evaluator.

Evaluates evolved routing algorithms by:
1. Loading the schedule() function from the candidate program
2. Monkey-patching it onto the Vidur global scheduler instance
3. Running simulations across seeds × models × workloads
4. Extracting E2E and P95 latency metrics
5. Computing combined score: -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms

Environment variables:
    VIDUR_OUTPUT_DIR  — Experiment output directory (default: .eval_artifacts with warning)
    VIDUR_SEED        — Comma-separated seeds (default: "42,456")
    VIDUR_MULTI_LLM   — "1" to use both models (default), "0" for single model
    VIDUR_NUM_REPLICAS — Number of replicas (default: "4")
    VIDUR_TRACE_SCALE  — Fraction of trace to use, 0-1 (default: "1.0")
"""
import atexit
import difflib
import gc
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BENCHMARK_DIR = Path(__file__).resolve().parent
VIDUR_DIR = BENCHMARK_DIR / "vidur"
WORKLOADS_DIR = BENCHMARK_DIR / "workloads"
INITIAL_PROGRAM = BENCHMARK_DIR / "initial_program.py"

# Add Vidur to sys.path so its imports work
_vidur_path = str(VIDUR_DIR)
if _vidur_path not in sys.path:
    sys.path.insert(0, _vidur_path)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
_DEFAULT_SEEDS = ["42", "456"]
SIM_SEEDS = (
    os.environ.get("VIDUR_SEED", "").split(",")
    if os.environ.get("VIDUR_SEED")
    else _DEFAULT_SEEDS
)
SIM_SEEDS = [s.strip() for s in SIM_SEEDS if s.strip()]

MULTI_LLM = os.environ.get("VIDUR_MULTI_LLM", "1") == "1"
NUM_REPLICAS = int(os.environ.get("VIDUR_NUM_REPLICAS", "4"))
TRACE_SCALE = float(os.environ.get("VIDUR_TRACE_SCALE", "1.0"))

WORKLOADS = [
    "cache_warmup",
    "load_spikes",
    "multiturn",
]

# Model configs: (display_name, model_name_for_vidur, device, tp_size)
_MODEL_LLAMA2_7B = ("llama2_7b", "meta-llama/Llama-2-7b-hf", "a100", 1)
_MODEL_LLAMA3_8B = ("llama3_8b", "meta-llama/Meta-Llama-3-8B", "a100", 1)


def _get_models() -> list:
    models = [_MODEL_LLAMA2_7B]
    if MULTI_LLM:
        models.append(_MODEL_LLAMA3_8B)
    return models


# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
def _get_output_dir() -> Path:
    d = os.environ.get("VIDUR_OUTPUT_DIR")
    if d:
        return Path(d)
    logger.warning(
        "VIDUR_OUTPUT_DIR not set — using .eval_artifacts (not recommended for experiments)"
    )
    return BENCHMARK_DIR / ".eval_artifacts"


# ---------------------------------------------------------------------------
# Error result
# ---------------------------------------------------------------------------
def _error_result(
    error_msg: str,
    error_type: str,
    suggestion: str = "",
    **extra_artifacts: Any,
) -> dict:
    artifacts = {
        "error_type": error_type,
        "error_message": error_msg,
        "suggestion": suggestion,
    }
    artifacts.update(extra_artifacts)
    return {
        "combined_score": -100000.0,
        "avg_e2e_ms": float("inf"),
        "avg_p95_ms": float("inf"),
        "error": error_msg,
        "artifacts": artifacts,
    }


# ---------------------------------------------------------------------------
# Diff logging
# ---------------------------------------------------------------------------
def _extract_evolve_block(code: str) -> str:
    m = re.search(
        r"# EVOLVE-BLOCK-START\n(.*?)# EVOLVE-BLOCK-END",
        code,
        re.DOTALL,
    )
    return m.group(1) if m else code


def print_diff(initial_code: str, current_code: str) -> None:
    initial_block = _extract_evolve_block(initial_code)
    current_block = _extract_evolve_block(current_code)
    diff = list(
        difflib.unified_diff(
            initial_block.splitlines(keepends=True),
            current_block.splitlines(keepends=True),
            fromfile="initial",
            tofile="evolved",
        )
    )
    if not diff:
        logger.info("NO DIFF — code unchanged from initial")
        return
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    logger.info("Eval diff: -%d/+%d lines vs initial", removed, added)


# ---------------------------------------------------------------------------
# Load schedule function from program file
# ---------------------------------------------------------------------------
def _load_schedule_fn(program_path: str):
    """Dynamically load the schedule() function from a program file."""
    spec = importlib.util.spec_from_file_location("evolved_program", program_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {program_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "schedule"):
        raise AttributeError(f"No schedule() function found in {program_path}")
    return module.schedule


# ---------------------------------------------------------------------------
# Vidur imports (deferred to avoid import-time issues)
# ---------------------------------------------------------------------------
_vidur_imported = False


def _ensure_vidur_imports():
    global _vidur_imported
    if _vidur_imported:
        return
    # Import Vidur components
    global Simulator, SimulationConfig, MetricsConfig, ClusterConfig, ReplicaConfig
    global TraceRequestGeneratorConfig, LORGlobalSchedulerConfig, SarathiSchedulerConfig
    global RandomForrestExecutionTimePredictorConfig
    global RequestMetricsTimeDistributions
    global BaseEntity, Request, Batch, BatchStage, Replica, Cluster, ExecutionTime

    from vidur.simulator import Simulator as _Sim
    from vidur.config.config import (
        SimulationConfig as _SimConfig,
        MetricsConfig as _MetConfig,
        ClusterConfig as _ClusterConfig,
        ReplicaConfig as _ReplicaConfig,
        TraceRequestGeneratorConfig as _TraceConfig,
        LORGlobalSchedulerConfig as _LORConfig,
        SarathiSchedulerConfig as _SarathiConfig,
        RandomForrestExecutionTimePredictorConfig as _RFConfig,
    )
    from vidur.metrics.constants import (
        RequestMetricsTimeDistributions as _RMTD,
    )
    from vidur.entities.base_entity import BaseEntity as _BE
    from vidur.entities.request import Request as _Req
    from vidur.entities.batch import Batch as _Batch
    from vidur.entities.batch_stage import BatchStage as _BS
    from vidur.entities.replica import Replica as _Rep
    from vidur.entities.cluster import Cluster as _Clu
    from vidur.entities.execution_time import ExecutionTime as _ET

    Simulator = _Sim
    SimulationConfig = _SimConfig
    MetricsConfig = _MetConfig
    ClusterConfig = _ClusterConfig
    ReplicaConfig = _ReplicaConfig
    TraceRequestGeneratorConfig = _TraceConfig
    LORGlobalSchedulerConfig = _LORConfig
    SarathiSchedulerConfig = _SarathiConfig
    RandomForrestExecutionTimePredictorConfig = _RFConfig
    RequestMetricsTimeDistributions = _RMTD
    BaseEntity = _BE
    Request = _Req
    Batch = _Batch
    BatchStage = _BS
    Replica = _Rep
    Cluster = _Clu
    ExecutionTime = _ET

    _vidur_imported = True


# ---------------------------------------------------------------------------
# Entity ID reset
# ---------------------------------------------------------------------------
def _reset_entity_ids():
    """Reset all Vidur entity class-level ID counters between simulations."""
    for cls in BaseEntity.__subclasses__():
        cls._id = -1


# ---------------------------------------------------------------------------
# Build simulation config
# ---------------------------------------------------------------------------
def _build_sim_config(
    model_name: str,
    device: str,
    tp_size: int,
    trace_file: str,
    num_replicas: int,
    temp_output_dir: str,
) -> "SimulationConfig":
    """Build a Vidur SimulationConfig for one simulation run."""
    _ensure_vidur_imports()

    replica_config = ReplicaConfig(
        model_name=model_name,
        num_pipeline_stages=1,
        tensor_parallel_size=tp_size,
        device=device,
        network_device=f"{device}_pairwise_nvlink",
    )

    cluster_config = ClusterConfig(
        num_replicas=num_replicas,
        replica_config=replica_config,
        global_scheduler_config=LORGlobalSchedulerConfig(),
        replica_scheduler_config=SarathiSchedulerConfig(),
    )

    trace_config = TraceRequestGeneratorConfig(
        trace_file=trace_file,
        time_scale_factor=1.0,
        # Both Llama-2-7B and Llama-3-8B have max_position_embeddings=4096.
        # Plan specifies 8192 but the model cannot handle prefill > 4096.
        # Trace replay clips total tokens (prefill+decode) to stay within limit.
        max_tokens=4096,
    )

    metrics_config = MetricsConfig(
        write_metrics=True,
        store_plots=False,
        enable_chrome_trace=False,
        write_json_trace=False,
        wandb_project=None,
        store_request_metrics=True,
        store_operation_metrics=False,
        store_utilization_metrics=False,
        store_batch_metrics=False,
        output_dir=temp_output_dir,
    )

    # Set absolute paths for profiling data (Vidur defaults are relative to CWD)
    vidur_data_dir = str(VIDUR_DIR / "data")
    exec_time_config = RandomForrestExecutionTimePredictorConfig(
        compute_input_file=f"{vidur_data_dir}/profiling/compute/{{DEVICE}}/{{MODEL}}/mlp.csv",
        attention_input_file=f"{vidur_data_dir}/profiling/compute/{{DEVICE}}/{{MODEL}}/attention.csv",
        all_reduce_input_file=f"{vidur_data_dir}/profiling/network/{{NETWORK_DEVICE}}/all_reduce.csv",
        send_recv_input_file=f"{vidur_data_dir}/profiling/network/{{NETWORK_DEVICE}}/send_recv.csv",
        cpu_overhead_input_file=f"{vidur_data_dir}/profiling/cpu_overhead/{{NETWORK_DEVICE}}/{{MODEL}}/cpu_overheads.csv",
    )

    sim_config = SimulationConfig(
        cluster_config=cluster_config,
        request_generator_config=trace_config,
        metrics_config=metrics_config,
        execution_time_predictor_config=exec_time_config,
        time_limit=300,
    )

    return sim_config


# ---------------------------------------------------------------------------
# Monkey-patch scheduler
# ---------------------------------------------------------------------------
def _patch_scheduler(simulator, schedule_fn):
    """Patch the global scheduler's schedule() method with our evolved function."""
    original = simulator._scheduler
    valid_ids = set(original._replica_schedulers.keys())

    def patched_schedule():
        queue_copy = list(original._request_queue)
        result = schedule_fn(
            queue_copy,
            original._replica_schedulers,
            original._num_replicas,
        )
        # Validate replica IDs
        for replica_id, req in result:
            if replica_id not in valid_ids:
                raise ValueError(
                    f"Invalid replica_id {replica_id}, valid: {valid_ids}"
                )
        # Apply: clear original queue and dispatch via the original add mechanism
        original._request_queue.clear()
        return result

    original.schedule = patched_schedule


# ---------------------------------------------------------------------------
# Extract metrics from a simulation
# ---------------------------------------------------------------------------
def _extract_metrics(simulator) -> dict:
    """Extract E2E and TTFT metrics from a completed simulation."""
    _ensure_vidur_imports()
    store = simulator._metric_store

    # E2E time
    e2e_series = store._request_metrics_time_distributions.get(
        RequestMetricsTimeDistributions.REQUEST_E2E_TIME
    )
    if e2e_series is None or len(e2e_series._data_series) == 0:
        return {"error": "No E2E metrics recorded"}

    df_e2e = e2e_series._to_df()
    y_col = e2e_series._y_name
    e2e_values = df_e2e[y_col]
    mean_e2e_s = float(e2e_values.mean())
    p95_e2e_s = float(e2e_values.quantile(0.95))

    # TTFT (prefill E2E time)
    ttft_ms = None
    ttft_series = store._request_metrics_time_distributions.get(
        RequestMetricsTimeDistributions.PREFILL_TIME_E2E
    )
    if ttft_series is not None and len(ttft_series._data_series) > 0:
        df_ttft = ttft_series._to_df()
        ttft_ms = float(df_ttft[ttft_series._y_name].mean()) * 1000

    return {
        "e2e_ms": mean_e2e_s * 1000,
        "p95_ms": p95_e2e_s * 1000,
        "ttft_ms": ttft_ms,
        "completed_requests": len(e2e_series._data_series),
    }


# ---------------------------------------------------------------------------
# Run a single simulation
# ---------------------------------------------------------------------------
def _run_single_sim(
    schedule_fn,
    model_name: str,
    device: str,
    tp_size: int,
    trace_file: str,
    num_replicas: int,
    log_key: str,
) -> Optional[dict]:
    """Run one Vidur simulation and return metrics or None on failure."""
    _ensure_vidur_imports()

    temp_dir = tempfile.mkdtemp(prefix="vidur_eval_")
    simulator = None
    try:
        config = _build_sim_config(
            model_name, device, tp_size, trace_file, num_replicas, temp_dir
        )
        simulator = Simulator(config)
        _patch_scheduler(simulator, schedule_fn)

        try:
            simulator.run()
        except Exception as e:
            logger.warning("%s simulation crashed: %s", log_key, str(e)[:300])
            return None

        metrics = _extract_metrics(simulator)
        if "error" in metrics:
            logger.warning("%s metrics error: %s", log_key, metrics["error"])
            return None

        logger.info(
            "%s: E2E=%.1fms P95=%.1fms completed=%d",
            log_key,
            metrics["e2e_ms"],
            metrics["p95_ms"],
            metrics["completed_requests"],
        )
        return metrics

    except Exception as e:
        logger.warning("%s failed: %s", log_key, str(e)[:300])
        return None
    finally:
        if simulator is not None:
            try:
                atexit.unregister(simulator._write_output)
            except Exception:
                pass
            del simulator
        gc.collect()
        _reset_entity_ids()
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Scale traces (optional)
# ---------------------------------------------------------------------------
def _get_trace_path(workload: str, seed: str) -> str:
    """Get trace CSV path, optionally creating a scaled version."""
    base_path = WORKLOADS_DIR / f"{workload}_seed{seed}.csv"
    if TRACE_SCALE >= 1.0:
        return str(base_path)

    # Create scaled trace in temp dir (never write to benchmark workloads dir)
    import pandas as pd
    df = pd.read_csv(base_path)
    n = max(1, int(len(df) * TRACE_SCALE))
    df_scaled = df.head(n)
    output_dir = _get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    scaled_path = output_dir / f"{workload}_seed{seed}_scale{int(TRACE_SCALE*100)}.csv"
    df_scaled.to_csv(scaled_path, index=False)
    return str(scaled_path)


# ---------------------------------------------------------------------------
# Run all workloads
# ---------------------------------------------------------------------------
def _run_workloads(schedule_fn) -> Tuple[dict, dict]:
    """Run all seed × model × workload combinations.

    Returns (aggregate_metrics, workload_details).
    """
    models = _get_models()
    all_e2e = []
    all_p95 = []
    workload_results = {}
    num_successful = 0
    num_failed = 0

    for seed in SIM_SEEDS:
        for model_display, model_name, device, tp_size in models:
            for workload in WORKLOADS:
                log_key = f"{workload}/{model_display}/seed{seed}"
                trace_path = _get_trace_path(workload, seed)

                if not Path(trace_path).exists():
                    logger.warning("%s: trace file not found: %s", log_key, trace_path)
                    num_failed += 1
                    continue

                metrics = _run_single_sim(
                    schedule_fn,
                    model_name,
                    device,
                    tp_size,
                    trace_path,
                    NUM_REPLICAS,
                    log_key,
                )

                if metrics is None:
                    num_failed += 1
                    continue

                num_successful += 1
                all_e2e.append(metrics["e2e_ms"])
                all_p95.append(metrics["p95_ms"])

                # Accumulate per-workload
                if workload not in workload_results:
                    workload_results[workload] = {"e2e": [], "p95": [], "ttft": [], "completed": []}
                workload_results[workload]["e2e"].append(metrics["e2e_ms"])
                workload_results[workload]["p95"].append(metrics["p95_ms"])
                if metrics.get("ttft_ms") is not None:
                    workload_results[workload]["ttft"].append(metrics["ttft_ms"])
                workload_results[workload]["completed"].append(metrics["completed_requests"])

    if not all_e2e:
        return {}, {}

    avg_e2e = sum(all_e2e) / len(all_e2e)
    avg_p95 = sum(all_p95) / len(all_p95)

    # Per-workload averages
    per_workload = {}
    for wl, data in workload_results.items():
        per_workload[wl] = {
            "e2e_ms": sum(data["e2e"]) / len(data["e2e"]),
            "e2e_p95_ms": sum(data["p95"]) / len(data["p95"]),
            "ttft_mean_ms": sum(data["ttft"]) / len(data["ttft"]) if data["ttft"] else None,
            "completed_requests": sum(data["completed"]),
        }

    aggregate = {
        "avg_e2e_ms": avg_e2e,
        "avg_p95_ms": avg_p95,
        "num_successful": num_successful,
        "num_failed": num_failed,
    }

    return aggregate, per_workload


# ---------------------------------------------------------------------------
# Baseline caching
# ---------------------------------------------------------------------------
def get_or_compute_baseline() -> dict:
    """Load cached baseline or compute from initial_program.py."""
    output_dir = _get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "baseline_metrics.json"

    # Try loading cached baseline
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("seeds") == sorted(SIM_SEEDS):
                models = [m[0] for m in _get_models()]
                if cached.get("models") == sorted(models):
                    logger.info("Loaded cached baseline (seeds=%s, models=%s)", SIM_SEEDS, models)
                    return cached
                logger.info("Baseline cache model mismatch, recomputing")
            else:
                logger.info("Baseline cache seed mismatch, recomputing")
        except Exception as e:
            logger.warning("Failed to load baseline cache: %s", e)

    # Compute baseline
    logger.info("Computing baseline from %s", INITIAL_PROGRAM)
    if not INITIAL_PROGRAM.exists():
        logger.warning("initial_program.py not found, cannot compute baseline")
        return {}

    try:
        schedule_fn = _load_schedule_fn(str(INITIAL_PROGRAM))
    except Exception as e:
        logger.warning("Failed to load initial program: %s", e)
        return {}

    aggregate, per_workload = _run_workloads(schedule_fn)
    if not aggregate:
        logger.warning("Baseline computation failed — all simulations failed")
        return {}

    avg_e2e = aggregate["avg_e2e_ms"]
    avg_p95 = aggregate["avg_p95_ms"]
    score = -0.5 * avg_e2e - 0.5 * avg_p95

    baseline = {
        "seeds": sorted(SIM_SEEDS),
        "models": sorted(m[0] for m in _get_models()),
        "avg_e2e_ms": avg_e2e,
        "avg_p95_ms": avg_p95,
        "combined_score": score,
        "num_successful": aggregate["num_successful"],
        "num_failed": aggregate["num_failed"],
    }

    # Per-workload metrics
    for wl, data in per_workload.items():
        baseline[f"{wl}_e2e_ms"] = data["e2e_ms"]
        baseline[f"{wl}_p95_ms"] = data["e2e_p95_ms"]

    # Per-model breakdown
    # (simplified — would need per-model tracking for full breakdown)

    try:
        cache_path.write_text(json.dumps(baseline, indent=2))
        logger.info("Cached baseline to %s (score=%.2f)", cache_path, score)
    except Exception as e:
        logger.warning("Failed to cache baseline: %s", e)

    return baseline


# ---------------------------------------------------------------------------
# Main evaluate function
# ---------------------------------------------------------------------------
def evaluate(program_path: str) -> dict:
    """Evaluate a candidate routing algorithm.

    Args:
        program_path: Path to a Python file containing a schedule() function.

    Returns:
        Dict with combined_score, per-workload metrics, and artifacts.
    """
    _ensure_vidur_imports()

    # Read program
    try:
        program_text = Path(program_path).read_text()
    except Exception as e:
        return _error_result(str(e), "FileReadError", "Check file path")

    # Diff logging
    try:
        initial_text = INITIAL_PROGRAM.read_text()
        print_diff(initial_text, program_text)
    except Exception as e:
        logger.warning("Diff logging failed: %s", e)

    # Load schedule function
    try:
        schedule_fn = _load_schedule_fn(program_path)
    except Exception as e:
        return _error_result(
            str(e), "LoadError",
            "Ensure file has a schedule(request_queue, replica_schedulers, num_replicas) function"
        )

    # Compute or load baseline
    baseline = get_or_compute_baseline()

    # Run workloads
    logger.info(
        "Running %d seeds × %d models × %d workloads = %d simulations",
        len(SIM_SEEDS), len(_get_models()), len(WORKLOADS),
        len(SIM_SEEDS) * len(_get_models()) * len(WORKLOADS),
    )

    aggregate, per_workload = _run_workloads(schedule_fn)

    if not aggregate:
        return _error_result(
            "All simulations failed",
            "AllWorkloadsFailed",
            "Check for SyntaxError/TypeError in schedule(). Ensure valid replica_ids.",
        )

    avg_e2e = aggregate["avg_e2e_ms"]
    avg_p95 = aggregate["avg_p95_ms"]
    score = -0.5 * avg_e2e - 0.5 * avg_p95

    # Build result
    result = {
        "combined_score": score,
        "avg_e2e_ms": avg_e2e,
        "avg_p95_ms": avg_p95,
        "success_rate": aggregate["num_successful"] / max(1, aggregate["num_successful"] + aggregate["num_failed"]),
        "num_successful": aggregate["num_successful"],
        "num_failed": aggregate["num_failed"],
    }

    # Per-workload flat keys
    for wl, data in per_workload.items():
        result[f"{wl}_e2e_ms"] = data["e2e_ms"]
        result[f"{wl}_p95_ms"] = data["e2e_p95_ms"]

    # Average TTFT across workloads
    ttft_values = [data["ttft_mean_ms"] for data in per_workload.values() if data.get("ttft_mean_ms") is not None]
    result["avg_ttft_ms"] = sum(ttft_values) / len(ttft_values) if ttft_values else None

    # Artifacts
    result["artifacts"] = {
        "workload_results": per_workload,
        "successful_workloads": aggregate["num_successful"],
        "failed_workloads": aggregate["num_failed"],
        "success_rate": f"{result['success_rate']*100:.0f}%",
        "seeds": SIM_SEEDS,
        "models": [m[0] for m in _get_models()],
        "num_replicas": NUM_REPLICAS,
        "trace_scale": TRACE_SCALE,
    }

    if baseline:
        result["artifacts"]["baseline_score"] = baseline.get("combined_score")
        baseline_score = baseline.get("combined_score", score)
        if baseline_score != 0:
            improvement = ((score - baseline_score) / abs(baseline_score)) * 100
            result["artifacts"]["improvement_vs_baseline"] = f"{improvement:+.2f}%"

    # Warn on partial failure
    total = aggregate["num_successful"] + aggregate["num_failed"]
    if aggregate["num_failed"] > 0:
        result["artifacts"]["warning"] = (
            f"{aggregate['num_failed']}/{total} simulations failed"
        )

    return result


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    program = sys.argv[1] if len(sys.argv) > 1 else str(INITIAL_PROGRAM)
    print(f"Evaluating: {program}")
    result = evaluate(program)
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["combined_score"] > -100000 else 1)
