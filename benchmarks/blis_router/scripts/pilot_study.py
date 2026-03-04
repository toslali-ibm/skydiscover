#!/usr/bin/env python3
"""
BLIS Router Pilot Study

End-to-end validation of the BLIS router benchmark pipeline.
Run this after any important code changes to verify nothing is broken.

Tests:
  1. Go code extraction from initial_program.py
  2. EVOLVE-BLOCK marker detection
  3. Go build with extracted code
  4. Full evaluation (3 workloads) with baseline program
  5. Score sanity checks (finite, negative, all workloads succeed)

Usage:
  python benchmarks/blis_router/scripts/pilot_study.py
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

# Resolve paths relative to benchmark root
SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_DIR = SCRIPT_DIR.parent
INFERENCE_SIM_DIR = BENCHMARK_DIR / "inference-sim"

# Add benchmark dir to path for imports
sys.path.insert(0, str(BENCHMARK_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def check(condition: bool, msg: str):
    """Assert with clear pass/fail output."""
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {msg}")
    if not condition:
        raise AssertionError(msg)


def test_go_code_extraction():
    """Test 1: Extract Go code from initial_program.py."""
    print("\n--- Test 1: Go Code Extraction ---")
    from evaluator import extract_go_code

    with open(BENCHMARK_DIR / "initial_program.py") as f:
        text = f.read()

    go_code = extract_go_code(text)
    check(len(go_code) > 1000, f"Go code extracted ({len(go_code)} chars)")
    check(go_code.startswith("package sim"), "Starts with 'package sim'")
    check("InFlightRequests" in go_code, "Contains InFlightRequests field")
    check("PendingRequests" not in go_code, "Does NOT contain PendingRequests (old field name)")
    check("WeightedScoring" in go_code, "Contains WeightedScoring struct")
    return go_code


def test_evolve_block(go_code: str):
    """Test 2: EVOLVE-BLOCK markers are present and well-formed."""
    print("\n--- Test 2: EVOLVE-BLOCK Markers ---")
    from evaluator import extract_evolve_block

    block = extract_evolve_block(go_code)
    check(len(block) > 100, f"Evolve block extracted ({len(block)} chars)")
    check("scores[snap.ID]" in block, "Block contains scoring logic")
    check("bestIdx" in block, "Block contains bestIdx selection")
    check("bestScore" in block, "Block contains bestScore tracking")

    # Verify observers are OUTSIDE the block
    start_idx = go_code.index("// EVOLVE-BLOCK-START")
    end_idx = go_code.index("// EVOLVE-BLOCK-END")
    inside_block = go_code[start_idx:end_idx]
    check("observers" not in inside_block, "Observers are outside EVOLVE-BLOCK (correct)")


def test_go_build(go_code: str):
    """Test 3: Extracted Go code compiles."""
    print("\n--- Test 3: Go Build ---")
    routing_go = INFERENCE_SIM_DIR / "sim" / "routing.go"

    # Save original
    original = routing_go.read_text()

    try:
        routing_go.write_text(go_code)
        result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=INFERENCE_SIM_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(result.returncode == 0, "Go build succeeds")
        if result.returncode != 0:
            print(f"    stderr: {result.stderr[:300]}")
    finally:
        # Restore original
        routing_go.write_text(original)


def test_full_evaluation():
    """Test 4 & 5: Full evaluation pipeline and score sanity."""
    print("\n--- Test 4: Full Evaluation ---")
    from evaluator import evaluate

    # Remove cached baseline to force fresh computation
    cache_path = BENCHMARK_DIR / "baseline_metrics.json"
    if cache_path.exists():
        cache_path.unlink()

    result = evaluate(str(BENCHMARK_DIR / "initial_program.py"))

    score = result.get("combined_score")
    avg_e2e = result.get("avg_e2e_ms")
    avg_p95 = result.get("avg_p95_ms")
    success_rate = result.get("success_rate")
    error = result.get("error")

    check(error is None, f"No error (got: {error})")
    check(isinstance(score, (int, float)), f"Score is numeric: {score}")
    check(score != float("-inf") and score != float("inf"), "Score is finite")
    check(score < 0, f"Score is negative (latency-based): {score:.2f}")
    check(score > -100000, f"Score is not error sentinel: {score:.2f}")

    print("\n--- Test 5: Score Sanity ---")
    check(success_rate == 1.0, f"All 3 workloads succeeded (rate={success_rate})")
    check(isinstance(avg_e2e, (int, float)) and avg_e2e > 0, f"Avg E2E positive: {avg_e2e:.2f}ms")
    check(isinstance(avg_p95, (int, float)) and avg_p95 > 0, f"Avg P95 positive: {avg_p95:.2f}ms")
    check(avg_p95 >= avg_e2e, f"P95 >= mean (tail >= avg): {avg_p95:.2f} >= {avg_e2e:.2f}")

    # Check per-workload results
    for wl in ["cache_warmup", "load_spikes", "multiturn"]:
        wl_e2e = result.get(f"{wl}_e2e_ms")
        check(wl_e2e is not None and wl_e2e > 0, f"{wl} e2e_ms: {wl_e2e:.2f}ms")

    # Check baseline was cached
    check(cache_path.exists(), "Baseline metrics cached to disk")

    print(f"\n  Baseline score: {score:.2f}")
    print(f"  Avg E2E: {avg_e2e:.2f}ms")
    print(f"  Avg P95: {avg_p95:.2f}ms")

    return result


def main():
    print("=" * 60)
    print("BLIS Router Pilot Study")
    print("=" * 60)

    try:
        go_code = test_go_code_extraction()
        test_evolve_block(go_code)
        test_go_build(go_code)
        result = test_full_evaluation()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
        print(f"\nBaseline score: {result['combined_score']:.2f}")
        print("Pipeline is healthy. Ready for full experiments.")
        return 0

    except AssertionError as e:
        print(f"\n{'=' * 60}")
        print(f"PILOT STUDY FAILED: {e}")
        print(f"{'=' * 60}")
        return 1

    except Exception as e:
        print(f"\n{'=' * 60}")
        print(f"UNEXPECTED ERROR: {e}")
        print(f"{'=' * 60}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
