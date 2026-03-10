#!/usr/bin/env python3
"""Vidur Router Benchmark — Pilot Study.

Validates the entire pipeline: imports, traces, evaluation, scoring, isolation.
Run this before any experiment to ensure the benchmark is working correctly.

Usage:
    python benchmarks/vidur_router/scripts/pilot_study.py
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent
VIDUR_DIR = BENCHMARK_DIR / "vidur"
WORKLOADS_DIR = BENCHMARK_DIR / "workloads"
INITIAL_PROGRAM = BENCHMARK_DIR / "initial_program.py"

# Add vidur and benchmark to path
sys.path.insert(0, str(VIDUR_DIR))
sys.path.insert(0, str(BENCHMARK_DIR))

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))
        failed += 1
    return condition


def test_vidur_imports():
    """Test 1: Verify Vidur core imports work."""
    print("\n=== Test 1: Vidur Imports ===")
    try:
        from vidur.simulator import Simulator
        check("Import Simulator", True)
    except Exception as e:
        check("Import Simulator", False, str(e))
        return False

    try:
        from vidur.config.config import (
            SimulationConfig, MetricsConfig, ClusterConfig,
            ReplicaConfig, TraceRequestGeneratorConfig,
            LORGlobalSchedulerConfig,
        )
        check("Import config classes", True)
    except Exception as e:
        check("Import config classes", False, str(e))
        return False

    try:
        from vidur.metrics.constants import RequestMetricsTimeDistributions
        check("Import metrics constants", True)
    except Exception as e:
        check("Import metrics constants", False, str(e))
        return False

    try:
        from vidur.entities.base_entity import BaseEntity
        from vidur.entities.request import Request
        check("Import entity classes", True)
    except Exception as e:
        check("Import entity classes", False, str(e))

    return True


def test_profiling_data():
    """Test 2: Verify profiling data exists for both models."""
    print("\n=== Test 2: Profiling Data ===")
    profiling_base = VIDUR_DIR / "data" / "profiling" / "compute" / "a100" / "meta-llama"

    models = [
        ("Llama-2-7b-hf", "meta-llama/Llama-2-7b-hf"),
        ("Meta-Llama-3-8B", "meta-llama/Meta-Llama-3-8B"),
    ]
    all_ok = True
    for dirname, display in models:
        model_dir = profiling_base / dirname
        has_attn = (model_dir / "attention.csv").exists()
        has_mlp = (model_dir / "mlp.csv").exists()
        ok = has_attn and has_mlp
        check(f"Profiling data for {display}", ok,
              f"dir={model_dir}, attention={has_attn}, mlp={has_mlp}")
        if not ok:
            all_ok = False
    return all_ok


def test_trace_files():
    """Test 3: Verify trace CSVs exist and parse correctly."""
    print("\n=== Test 3: Trace Files ===")
    import pandas as pd

    workloads = ["cache_warmup", "load_spikes", "multiturn"]
    seeds = ["42", "456"]
    all_ok = True

    for wl in workloads:
        for seed in seeds:
            csv_path = WORKLOADS_DIR / f"{wl}_seed{seed}.csv"
            exists = csv_path.exists()
            check(f"Trace exists: {wl}_seed{seed}.csv", exists)
            if not exists:
                all_ok = False
                continue

            df = pd.read_csv(csv_path)
            has_cols = all(
                c in df.columns
                for c in ["arrived_at", "num_prefill_tokens", "num_decode_tokens"]
            )
            check(f"  Columns correct", has_cols, f"got: {list(df.columns)}")

            n = len(df)
            check(f"  Non-empty ({n} rows)", n > 0)

            if n > 0:
                check(f"  arrived_at monotonic", df["arrived_at"].is_monotonic_increasing)
                check(f"  prefill_tokens > 0", (df["num_prefill_tokens"] > 0).all())
                check(f"  decode_tokens > 0", (df["num_decode_tokens"] > 0).all())

    return all_ok


def test_evolve_block():
    """Test 4: Verify EVOLVE-BLOCK markers in initial_program.py."""
    print("\n=== Test 4: EVOLVE-BLOCK Markers ===")
    code = INITIAL_PROGRAM.read_text()
    check("initial_program.py exists", INITIAL_PROGRAM.exists())
    check("EVOLVE-BLOCK-START present", "# EVOLVE-BLOCK-START" in code)
    check("EVOLVE-BLOCK-END present", "# EVOLVE-BLOCK-END" in code)
    check("schedule() function defined", "def schedule(" in code)
    check("Code > 500 chars", len(code) > 500, f"got {len(code)}")
    return True


def test_baseline_evaluation():
    """Test 5: Run baseline (LOR) on 1 workload × 1 seed × 1 model."""
    print("\n=== Test 5: Baseline Evaluation (single workload) ===")

    # Use temp output dir
    temp_dir = tempfile.mkdtemp(prefix="vidur_pilot_")
    os.environ["VIDUR_OUTPUT_DIR"] = temp_dir
    os.environ["VIDUR_SEED"] = "42"
    os.environ["VIDUR_MULTI_LLM"] = "0"

    print(f"  Output dir: {temp_dir}")
    print(f"  Running evaluation (this may take 30-120s)...")

    start = time.time()
    try:
        # Import evaluator
        from evaluator import evaluate
        result = evaluate(str(INITIAL_PROGRAM))
        elapsed = time.time() - start
        print(f"  Evaluation completed in {elapsed:.1f}s")
    except Exception as e:
        check("Evaluation runs", False, str(e))
        return False
    finally:
        # Restore env
        os.environ.pop("VIDUR_OUTPUT_DIR", None)
        os.environ.pop("VIDUR_SEED", None)
        os.environ.pop("VIDUR_MULTI_LLM", None)

    # Check result
    score = result.get("combined_score", -999999)
    # Error sentinel is exactly -100000.0; any other value means evaluation succeeded
    check("Score is not error sentinel", score != -100000.0, f"got {score}")
    check("Score is negative", score < 0, f"got {score} (latency-based scores should be negative)")

    avg_e2e = result.get("avg_e2e_ms", 0)
    avg_p95 = result.get("avg_p95_ms", 0)
    check("avg_e2e_ms > 0", avg_e2e > 0, f"got {avg_e2e}")
    check("avg_p95_ms >= avg_e2e_ms", avg_p95 >= avg_e2e * 0.95,
          f"P95={avg_p95:.1f}ms, E2E={avg_e2e:.1f}ms")

    success_rate = result.get("success_rate", 0)
    check("success_rate > 0", success_rate > 0, f"got {success_rate}")

    num_failed = result.get("num_failed", -1)
    num_success = result.get("num_successful", 0)
    total = num_success + max(0, num_failed)
    if num_failed > 0:
        print(f"  WARNING: {num_failed}/{total} simulations failed (may need load calibration)")
    check("At least one simulation succeeded", num_success > 0, f"{num_success} succeeded, {num_failed} failed")

    # Check baseline cached in output dir (not benchmark dir)
    baseline_in_output = Path(temp_dir) / "baseline_metrics.json"
    check("Baseline cached in output dir", baseline_in_output.exists())
    baseline_in_benchmark = BENCHMARK_DIR / "baseline_metrics.json"
    check("No baseline leaked to benchmark dir", not baseline_in_benchmark.exists())

    print(f"\n  Score: {score:.2f}")
    print(f"  Avg E2E: {avg_e2e:.1f}ms")
    print(f"  Avg P95: {avg_p95:.1f}ms")
    print(f"  Wall time per eval: {elapsed:.1f}s")

    # Estimate full experiment time
    # Full eval = 2 seeds × 2 models × 3 workloads = 12 sims
    # This was 1 seed × 1 model × 3 workloads = 3 sims
    ratio = (2 * 2 * 3) / (1 * 1 * 3)
    est_full = elapsed * ratio
    print(f"  Estimated full eval (2 seeds × 2 models × 3 workloads): {est_full:.0f}s")
    print(f"  Estimated 50-iteration experiment: {est_full * 50 / 3600:.1f}h")

    return score > -100000


def test_isolation():
    """Test 6: Verify no artifacts leaked to benchmark directory."""
    print("\n=== Test 6: Isolation Check ===")
    artifacts = [
        BENCHMARK_DIR / "baseline_metrics.json",
        BENCHMARK_DIR / ".eval_artifacts",
    ]
    all_ok = True
    for artifact in artifacts:
        leaked = artifact.exists()
        check(f"No leaked {artifact.name}", not leaked)
        if leaked:
            all_ok = False
    return all_ok


def main():
    print("=" * 60)
    print("Vidur Router Benchmark — Pilot Study")
    print("=" * 60)

    # Run tests in order
    ok1 = test_vidur_imports()
    ok2 = test_profiling_data()
    ok3 = test_trace_files()
    ok4 = test_evolve_block()

    if not (ok1 and ok2 and ok3):
        print("\n" + "=" * 60)
        print(f"STOPPING: Prerequisites failed ({failed} failures)")
        print("Fix the above issues before running baseline evaluation.")
        print("=" * 60)
        sys.exit(1)

    ok5 = test_baseline_evaluation()
    ok6 = test_isolation()

    # Summary
    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL CHECKS PASSED — benchmark is ready for experiments")
    else:
        print("SOME CHECKS FAILED — fix issues before running experiments")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
