# BLIS Router Benchmark Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a BLIS router benchmark in SkyDiscover so all 5 search frameworks (AdaEvolve, EvoX, OpenEvolve, GEPA, ShinkaEvolve) can discover routing algorithms, then compare results.

**Architecture:** One evaluator + one initial program shared by all frameworks. The evaluator extracts Go code from the evolved program, writes it to `routing.go`, rebuilds BLIS via `go build`, runs 3 workloads, and returns latency metrics. SkyDiscover handles the search strategy -- just flip `--search`.

**Tech Stack:** Python (evaluator), Go (BLIS simulator), YAML (configs, workloads), bash (experiment scripts), matplotlib (analysis plots)

**Source reference:** Existing working setup at `openevolve/examples/blis_router/` -- we're porting the evaluation logic and adapting it to SkyDiscover's interface.

---

### Task 1: Create benchmark directory and add inference-sim submodule

**Files:**
- Create: `benchmarks/blis_router/` (directory)

**Step 1: Create the directory structure**

```bash
mkdir -p benchmarks/blis_router/{scripts,workloads,routing}
```

**Step 2: Add inference-sim as a git submodule**

```bash
git submodule add https://github.com/inference-sim/inference-sim.git benchmarks/blis_router/inference-sim
```

**Step 3: Verify the submodule and Go build**

```bash
cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go
```
Expected: Binary `simulation_worker` created without errors.

**Step 4: Commit**

```bash
git add .gitmodules benchmarks/blis_router/inference-sim
git commit -m "feat(blis): add inference-sim as git submodule"
```

---

### Task 2: Copy static assets (workloads, routing policy)

**Files:**
- Create: `benchmarks/blis_router/routing/routing_policy.yaml`
- Create: `benchmarks/blis_router/workloads/workload_v2_cache_warmup.yaml`
- Create: `benchmarks/blis_router/workloads/workload_v2_load_spikes.yaml`
- Create: `benchmarks/blis_router/workloads/workload_v2_multiturn.yaml`

These are direct copies from the openevolve repo -- no modifications needed.

**Step 1: Copy the files**

```bash
SRC=/Users/toslali/Desktop/work/ibm/projects/llm-inference/study/inference-llmd/nsdi/code/openevolve/examples/blis_router
DEST=benchmarks/blis_router

cp "$SRC/routing_policy.yaml" "$DEST/routing/"
cp "$SRC/workload_v2_cache_warmup.yaml" "$DEST/workloads/"
cp "$SRC/workload_v2_load_spikes.yaml" "$DEST/workloads/"
cp "$SRC/workload_v2_multiturn.yaml" "$DEST/workloads/"
```

**Step 2: Verify files exist**

```bash
ls -la benchmarks/blis_router/workloads/ benchmarks/blis_router/routing/
```
Expected: 3 workload YAML files + 1 routing policy YAML file.

**Step 3: Commit**

```bash
git add benchmarks/blis_router/workloads/ benchmarks/blis_router/routing/
git commit -m "feat(blis): add workload specs and routing policy config"
```

---

### Task 3: Copy and verify initial_program.py

**Files:**
- Create: `benchmarks/blis_router/initial_program.py`

The initial program is a direct copy -- it's a Python file containing the full `routing.go` as a string with EVOLVE-BLOCK markers. No SkyDiscover-specific imports needed.

**Step 1: Copy from openevolve**

```bash
cp "$SRC/initial_program.py" benchmarks/blis_router/
```

**Step 2: Verify EVOLVE-BLOCK markers are present**

```bash
grep -n "EVOLVE-BLOCK" benchmarks/blis_router/initial_program.py
```
Expected: Lines containing `// EVOLVE-BLOCK-START` and `// EVOLVE-BLOCK-END`.

**Step 3: Verify the Go code matches the submodule version**

Read `benchmarks/blis_router/inference-sim/sim/routing.go` and compare its struct definitions and function signatures against the Go code embedded in `initial_program.py`. They must match (same `RoutingSnapshot` fields, same `WeightedScoring.Route` signature). If they don't match, update `initial_program.py` to reflect the current `inference-sim` version.

**Step 4: Commit**

```bash
git add benchmarks/blis_router/initial_program.py
git commit -m "feat(blis): add initial program (routing.go with EVOLVE-BLOCK markers)"
```

---

### Task 4: Write the test for the evaluator

**Files:**
- Create: `tests/test_blis_evaluator.py`

We test the evaluator's pure-Python logic (Go code extraction, JSON parsing, score computation) without requiring BLIS or Go installed.

**Step 1: Write the test file**

```python
"""Tests for BLIS router evaluator — pure-Python logic only (no Go/BLIS required)."""

import json
import textwrap

import pytest

# We'll import these after the evaluator is created in Task 5
# For now this file defines what the evaluator MUST satisfy.


def test_extract_go_code_from_python_wrapper():
    """extract_go_code finds GO_ROUTING_CODE triple-quoted string."""
    from benchmarks.blis_router.evaluator import extract_go_code

    program_text = textwrap.dedent('''\
        GO_ROUTING_CODE = """package sim

        import "fmt"

        func main() {}
        """
    ''')
    result = extract_go_code(program_text)
    assert result.startswith("package sim")
    assert 'import "fmt"' in result


def test_extract_go_code_raw_go():
    """extract_go_code accepts raw Go code (no Python wrapper)."""
    from benchmarks.blis_router.evaluator import extract_go_code

    raw_go = "package sim\n\nfunc Route() {}\n"
    result = extract_go_code(raw_go)
    assert result == raw_go


def test_extract_go_code_empty():
    """extract_go_code returns empty string on garbage input."""
    from benchmarks.blis_router.evaluator import extract_go_code

    assert extract_go_code("no go code here") == ""
    assert extract_go_code("") == ""


def test_parse_cluster_metrics_valid():
    """_parse_cluster_metrics finds the cluster JSON block."""
    from benchmarks.blis_router.evaluator import _parse_cluster_metrics

    output = textwrap.dedent("""\
        === Simulation Metrics ===
        {"instance_id": "inst-0", "e2e_mean_ms": 100.0}
        {"instance_id": "cluster", "e2e_mean_ms": 4523.17, "e2e_p95_ms": 8234.56, "completed_requests": 1189}
    """)
    result = _parse_cluster_metrics(output)
    assert result is not None
    assert result["instance_id"] == "cluster"
    assert result["e2e_mean_ms"] == 4523.17
    assert result["e2e_p95_ms"] == 8234.56


def test_parse_cluster_metrics_missing():
    """_parse_cluster_metrics returns None when no cluster block exists."""
    from benchmarks.blis_router.evaluator import _parse_cluster_metrics

    assert _parse_cluster_metrics("no json here") is None
    assert _parse_cluster_metrics('{"instance_id": "inst-0"}') is None


def test_extract_evolve_block():
    """extract_evolve_block pulls content between markers."""
    from benchmarks.blis_router.evaluator import extract_evolve_block

    code = "before\n// EVOLVE-BLOCK-START\nchanged code\n// EVOLVE-BLOCK-END\nafter"
    assert extract_evolve_block(code) == "changed code"


def test_extract_evolve_block_missing():
    """extract_evolve_block returns empty string if no markers."""
    from benchmarks.blis_router.evaluator import extract_evolve_block

    assert extract_evolve_block("no markers here") == ""
```

**Step 2: Run test to verify it fails (evaluator doesn't exist yet)**

```bash
uv run pytest tests/test_blis_evaluator.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.blis_router.evaluator'`

**Step 3: Commit**

```bash
git add tests/test_blis_evaluator.py
git commit -m "test(blis): add evaluator unit tests (pure-Python logic)"
```

---

### Task 5: Write the evaluator

**Files:**
- Create: `benchmarks/blis_router/__init__.py` (empty, for importability)
- Create: `benchmarks/blis_router/evaluator.py`

This is the core file. Port from `openevolve/examples/blis_router/evaluator.py` with these changes:
1. Remove `from openevolve.evaluation_result import EvaluationResult` — return plain `dict`
2. All paths relative to `Path(__file__).parent`
3. Baseline cache goes in the benchmark directory (not `openevolve_output/`)
4. `__main__` block uses plain dict instead of `EvaluationResult` attributes

**Step 1: Create empty `__init__.py`**

```bash
touch benchmarks/blis_router/__init__.py
```

**Step 2: Write `evaluator.py`**

The full evaluator. Key changes from the openevolve version are marked with `# CHANGED`:

```python
"""
BLIS Router Evaluator for SkyDiscover

Evaluates evolved routing algorithms by:
1. Extracting Go code from Python wrapper
2. Writing evolved routing.go to BLIS source
3. Building BLIS
4. Running simulations on 3 routing-sensitive v2 workloads
5. Computing score based on average end-to-end latency

Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (higher = better)
"""

import json
import logging
import os
import re
import subprocess
import traceback
from difflib import unified_diff
from pathlib import Path

# CHANGED: no openevolve import — return plain dict

logger = logging.getLogger(__name__)

WORKLOADS = [
    ("cache_warmup", "workload_v2_cache_warmup.yaml"),
    ("load_spikes", "workload_v2_load_spikes.yaml"),
    ("multiturn", "workload_v2_multiturn.yaml"),
]

SIM_MODEL = os.environ.get("BLIS_MODEL", "meta-llama/llama-3.1-8b-instruct")


def _build_sim_cmd(
    inference_sim_dir: Path, policy_config_path: Path, workload_path: Path
) -> list[str]:
    """Return the simulation command list for a single workload run."""
    cmd = [
        "./simulation_worker",
        "run",
        "--model",
        SIM_MODEL,
        "--num-instances",
        "4",
        "--policy-config",
        str(policy_config_path),
        "--workload-spec",
        str(workload_path),
        "--log",
        "info",
    ]
    if "Qwen" in SIM_MODEL:
        cmd += [
            "--hardware", "H100",
            "--tp", "1",
            "--alpha-coeffs", "4680.303204056608,0.0,0.0",
            "--beta-coeffs", "7051.796874715078,19.538416565504026,25.431830886933543",
            "--total-kv-blocks", "65833",
            "--max-num-running-reqs", "256",
            "--max-num-scheduled-tokens", "4096",
        ]
    return cmd


def extract_evolve_block(code: str) -> str:
    """Extract only EVOLVE-BLOCK section from Go code."""
    pattern = r"// EVOLVE-BLOCK-START(.*?)// EVOLVE-BLOCK-END"
    match = re.search(pattern, code, re.DOTALL)
    return match.group(1).strip() if match else ""


def print_diff(initial_code: str, current_code: str):
    """Print compact diff between initial and current EVOLVE-BLOCK."""
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
    """Parse cluster-wide metrics from simulation output JSON blocks."""
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


def _error_result(error_msg: str, error_type: str, suggestion: str = "", **extra_artifacts) -> dict:
    """Return a standardized error dict."""
    result = {
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
    return result


def get_or_compute_baseline(
    script_dir: Path, inference_sim_dir: Path, policy_config_path: Path
) -> dict:
    """Get baseline metrics from cache or compute by running the initial program."""
    cache_path = script_dir / "baseline_metrics.json"  # CHANGED: cache in benchmark dir
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
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
    try:
        with open(routing_go_path, "w") as f:
            f.write(go_code)
    except OSError as exc:
        logger.warning("Failed to write routing.go for baseline: %s", exc)
        return {}

    try:
        build_result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=inference_sim_dir, capture_output=True, text=True, timeout=60,
        )
        if build_result.returncode != 0:
            logger.warning("Baseline build failed: %s", build_result.stderr[:300])
            return {}
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Baseline build error: %s", exc)
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
        with open(cache_path, "w") as f:
            json.dump(baseline, f, indent=2)
        logger.info("Cached baseline metrics to %s", cache_path)
    except OSError as exc:
        logger.warning("Failed to cache baseline metrics: %s", exc)

    return baseline


def extract_go_code(program_text: str) -> str:
    """Extract Go code from Python program text."""
    match = re.search(r'GO_ROUTING_CODE\s*=\s*"""(.*?)"""', program_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    if "package sim" in program_text:
        return program_text
    return ""


def evaluate(program_path: str) -> dict:  # CHANGED: returns dict, not EvaluationResult
    """Evaluate the evolved routing algorithm.

    Args:
        program_path: Path to the program file containing routing.go code

    Returns:
        dict with combined_score, per-workload metrics, and artifacts
    """
    with open(program_path, "r") as f:
        program_text = f.read()

    script_dir = Path(__file__).parent
    inference_sim_dir = script_dir / "inference-sim"
    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    policy_config_path = script_dir / "routing" / "routing_policy.yaml"

    # Step 1: Extract Go code
    go_code = extract_go_code(program_text)
    if not go_code:
        return _error_result(
            "Failed to extract Go code",
            "ExtractionError",
            'Ensure program contains GO_ROUTING_CODE = """...""" or starts with "package sim"',
        )

    logger.info(f"Extracted Go code: {len(go_code)} chars")

    # Diff vs initial
    try:
        initial_program_path = script_dir / "initial_program.py"
        if initial_program_path.exists():
            with open(initial_program_path, "r") as f:
                initial_go_code = extract_go_code(f.read())
            if initial_go_code:
                print_diff(initial_go_code, go_code)
    except Exception:
        pass

    # Compute/load baseline
    get_or_compute_baseline(script_dir, inference_sim_dir, policy_config_path)

    # Step 2: Write evolved routing.go
    try:
        with open(routing_go_path, "w") as f:
            f.write(go_code)
    except Exception as e:
        return _error_result(
            f"Failed to write file: {e}", "FileWriteError",
            full_traceback=traceback.format_exc(),
        )

    # Step 3: Build BLIS
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

    # Step 4: Run simulations
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

        except subprocess.TimeoutExpired:
            failed_workloads.append(workload_name)
            workload_results[workload_name] = {"e2e_ms": None, "error": "Timeout (120s)"}
        except Exception as e:
            failed_workloads.append(workload_name)
            workload_results[workload_name] = {"e2e_ms": None, "error": str(e)}

    # Step 5: Compute score
    if len(latencies) == 0:
        return _error_result(
            "All workloads failed", "AllWorkloadsFailed",
            "Check BLIS simulation errors. May be routing logic causing crashes or timeouts.",
            failed_workloads=failed_workloads, workload_results=workload_results,
        )

    use_weighted = os.environ.get("WEIGHTED_LATENCY", "false").lower() == "true"
    if use_weighted:
        total_requests = sum(request_counts)
        avg_latency = sum(l * c for l, c in zip(latencies, request_counts)) / total_requests
        avg_tail_latency = sum(l * c for l, c in zip(tail_latencies, request_counts)) / total_requests
    else:
        avg_latency = sum(latencies) / len(latencies)
        avg_tail_latency = sum(tail_latencies) / len(tail_latencies)

    score = -0.5 * avg_latency - 0.5 * avg_tail_latency
    success_rate = len(latencies) / len(WORKLOADS)

    # CHANGED: return plain dict instead of EvaluationResult
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


if __name__ == "__main__":
    print("Testing evaluator with initial program...")
    script_dir = Path(__file__).parent
    result = evaluate(str(script_dir / "initial_program.py"))
    score = result.get("combined_score")
    avg_e2e = result.get("avg_e2e_ms")
    print(f"  Score: {score:.2f}" if score is not None else "  Score: N/A")
    print(f"  Avg E2E: {avg_e2e:.2f}ms" if avg_e2e and avg_e2e != float("inf") else "  Avg E2E: N/A")
```

**Step 3: Run tests to verify they pass**

```bash
uv run pytest tests/test_blis_evaluator.py -v
```
Expected: All 7 tests PASS.

**Step 4: Commit**

```bash
git add benchmarks/blis_router/__init__.py benchmarks/blis_router/evaluator.py
git commit -m "feat(blis): add evaluator (returns dict, no openevolve dependency)"
```

---

### Task 6: Write the SkyDiscover config

**Files:**
- Create: `benchmarks/blis_router/config.yaml`

**Step 1: Write config.yaml**

```yaml
language: python
diff_based_generation: true
max_iterations: 50
checkpoint_interval: 5
max_solution_length: 40000

llm:
  models:
    - name: "aws/claude-sonnet-4-5"
      weight: 0.7
    - name: "aws/claude-opus-4-6"
      weight: 0.3
  api_base: "https://ete-litellm.ai-models.vpc-int.res.ibm.com"
  temperature: 1.0
  top_p: null
  max_tokens: 32000
  timeout: 120

prompt:
  system_message: |
    You are optimizing a request router for a 4-instance LLM inference cluster.

    GOAL: Minimize end-to-end latency by improving how requests are routed to instances.

    BASELINE: Fixed equal weights [1.0, 1.0] on two scorers (prefix-affinity, load-balance).
    The weights never adapt to request or system state.

    AVAILABLE SIGNALS — from request (req):
      - len(req.InputTokens): input size
      - req.SLOClass: "realtime", "interactive", or "batch"
      - req.SessionID: non-empty for multi-turn sessions

    AVAILABLE SIGNALS — from instance snapshots (snap):
      - scores[snap.ID]: weighted scorer output (after the scorer loop)
      - snap.QueueDepth, snap.BatchSize, snap.PendingRequests
      - snap.EffectiveLoad(): QueueDepth + BatchSize + PendingRequests
      - snap.CacheHitRate: historical cache hit rate (0.0-1.0)
      - snap.KVUtilization: memory pressure (may be stale under high load)
      - snap.FreeKVBlocks: available memory blocks

    You can modify scores[snap.ID] after the scorer loop, add conditionals, or change
    the selection logic. The initial scorer loop already populates scores[] with the
    weighted combination of prefix-affinity and load-balance.

    RULES (compilation failures waste an iteration):
    1. Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
    2. Must be valid Go — guard all divisions to avoid NaN/Inf
    3. bestIdx must be valid: 0 <= bestIdx < len(snapshots)
    4. Don't zero scores — use a small multiplier instead
    5. Don't add new keys to scores[] — only modify existing instance IDs

    EVALUATION: Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (lower latency = higher score)
    Tested on 3 workloads: cache_warmup, load_spikes, multiturn.

search:
  type: "adaevolve"
  num_context_programs: 4

evaluator:
  timeout: 300
  cascade_evaluation: false
```

**Step 2: Validate YAML parses correctly**

```bash
python -c "import yaml; yaml.safe_load(open('benchmarks/blis_router/config.yaml'))"
```
Expected: No errors.

**Step 3: Commit**

```bash
git add benchmarks/blis_router/config.yaml
git commit -m "feat(blis): add SkyDiscover config with routing-optimization system prompt"
```

---

### Task 7: Write the experiment runner script

**Files:**
- Create: `benchmarks/blis_router/scripts/run_all_frameworks.sh`

**Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Run BLIS router discovery across all 5 search frameworks.
# Usage: bash benchmarks/blis_router/scripts/run_all_frameworks.sh [ITERATIONS]
set -euo pipefail

ITERATIONS=${1:-50}
BASE_DIR="benchmarks/blis_router"
OUTPUT_BASE="outputs/blis_router"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "BLIS Router Experiment — $ITERATIONS iterations per framework"
echo "Output: ${OUTPUT_BASE}/${TIMESTAMP}/"
echo ""

# Ensure external backends installed
uv sync --extra external

# Verify Go + BLIS build
echo "Verifying BLIS build..."
(cd "$BASE_DIR/inference-sim" && go build -o simulation_worker main.go) || {
    echo "FATAL: BLIS build failed. Install Go and check inference-sim submodule."
    exit 1
}
echo "BLIS build OK."
echo ""

# Run each framework sequentially (they share routing.go)
for SEARCH in adaevolve evox openevolve gepa shinkaevolve; do
    echo "=========================================="
    echo "  $SEARCH — $ITERATIONS iterations"
    echo "=========================================="

    OUTPUT_DIR="${OUTPUT_BASE}/${TIMESTAMP}/${SEARCH}"
    mkdir -p "$OUTPUT_DIR"

    CONFIG="$BASE_DIR/config.yaml"
    [[ -f "$BASE_DIR/config_${SEARCH}.yaml" ]] && CONFIG="$BASE_DIR/config_${SEARCH}.yaml"

    uv run skydiscover-run \
        "$BASE_DIR/initial_program.py" \
        "$BASE_DIR/evaluator.py" \
        -c "$CONFIG" \
        -s "$SEARCH" \
        -i "$ITERATIONS" \
        -o "$OUTPUT_DIR" \
        -l INFO \
        2>&1 | tee "${OUTPUT_DIR}/run.log"

    echo ""
    echo "$SEARCH complete. Results: $OUTPUT_DIR"
    echo ""
done

echo "=========================================="
echo "All frameworks complete."
echo "Results: ${OUTPUT_BASE}/${TIMESTAMP}/"
echo ""
echo "Next: python $BASE_DIR/scripts/compare_results.py ${OUTPUT_BASE}/${TIMESTAMP}"
```

**Step 2: Make executable**

```bash
chmod +x benchmarks/blis_router/scripts/run_all_frameworks.sh
```

**Step 3: Commit**

```bash
git add benchmarks/blis_router/scripts/run_all_frameworks.sh
git commit -m "feat(blis): add master experiment runner script for all 5 frameworks"
```

---

### Task 8: Write the cross-framework comparison script

**Files:**
- Create: `benchmarks/blis_router/scripts/compare_results.py`

**Step 1: Write the comparison script**

```python
#!/usr/bin/env python3
"""Compare BLIS router results across search frameworks.

Usage:
    python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>

Reads best_program_info.json from each framework's output directory.
Outputs: comparison_table.csv, comparison_chart.png, winner_summary.txt
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

FRAMEWORKS = ["adaevolve", "evox", "openevolve", "gepa", "shinkaevolve"]

METRICS = [
    ("combined_score", "Combined Score", True),   # (key, label, higher_is_better)
    ("avg_e2e_ms", "Avg E2E (ms)", False),
    ("avg_p95_ms", "Avg P95 (ms)", False),
    ("cache_warmup_e2e_ms", "Cache Warmup E2E (ms)", False),
    ("load_spikes_e2e_ms", "Load Spikes E2E (ms)", False),
    ("multiturn_e2e_ms", "Multiturn E2E (ms)", False),
]


def load_best_metrics(experiment_dir: Path) -> dict[str, dict]:
    """Load best program metrics for each framework."""
    results = {}
    for fw in FRAMEWORKS:
        info_path = experiment_dir / fw / "best" / "best_program_info.json"
        if not info_path.exists():
            print(f"  SKIP {fw}: {info_path} not found")
            continue
        with open(info_path) as f:
            data = json.load(f)
        results[fw] = data.get("metrics", data)
    return results


def write_csv(results: dict[str, dict], output_path: Path):
    """Write framework x metric comparison CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        header = ["Framework"] + [label for _, label, _ in METRICS]
        writer.writerow(header)
        for fw in FRAMEWORKS:
            if fw not in results:
                continue
            row = [fw] + [results[fw].get(key, "N/A") for key, _, _ in METRICS]
            writer.writerow(row)
    print(f"  Saved: {output_path}")


def write_summary(results: dict[str, dict], output_path: Path):
    """Write winner summary for each metric."""
    lines = ["BLIS Router — Framework Comparison Summary", "=" * 50, ""]
    for key, label, higher_is_better in METRICS:
        values = {}
        for fw, m in results.items():
            v = m.get(key)
            if v is not None and v != float("inf"):
                values[fw] = v
        if not values:
            lines.append(f"{label}: no data")
            continue
        if higher_is_better:
            winner = max(values, key=values.get)
        else:
            winner = min(values, key=values.get)
        lines.append(f"{label}:")
        for fw in FRAMEWORKS:
            if fw in values:
                marker = " <-- BEST" if fw == winner else ""
                lines.append(f"  {fw:15s} {values[fw]:>12.2f}{marker}")
        lines.append("")
    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Saved: {output_path}")
    print()
    print("\n".join(lines))


def plot_comparison(results: dict[str, dict], output_path: Path):
    """Generate grouped bar chart comparing frameworks."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("  SKIP chart: matplotlib not installed")
        return

    fws = [fw for fw in FRAMEWORKS if fw in results]
    plot_metrics = [(k, l, h) for k, l, h in METRICS if k != "combined_score"]

    x = np.arange(len(plot_metrics))
    width = 0.8 / len(fws)

    fig, ax = plt.subplots(figsize=(14, 7))
    for i, fw in enumerate(fws):
        vals = [results[fw].get(k, 0) or 0 for k, _, _ in plot_metrics]
        ax.bar(x + i * width, vals, width, label=fw)

    ax.set_xticks(x + width * (len(fws) - 1) / 2)
    ax.set_xticklabels([l for _, l, _ in plot_metrics], rotation=20, ha="right")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("BLIS Router — Per-Workload Latency by Framework")
    ax.legend()
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    print(f"  Saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Compare BLIS router results across frameworks")
    parser.add_argument("experiment_dir", help="Path to experiment output directory")
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir)
    if not experiment_dir.is_dir():
        sys.exit(f"Error: {experiment_dir} is not a directory")

    print(f"Loading results from {experiment_dir}")
    results = load_best_metrics(experiment_dir)
    if not results:
        sys.exit("No framework results found")

    print(f"Found results for: {', '.join(results.keys())}")
    print()

    write_csv(results, experiment_dir / "comparison_table.csv")
    write_summary(results, experiment_dir / "winner_summary.txt")
    plot_comparison(results, experiment_dir / "comparison_chart.png")


if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add benchmarks/blis_router/scripts/compare_results.py
git commit -m "feat(blis): add cross-framework comparison script"
```

---

### Task 9: Write the README

**Files:**
- Create: `benchmarks/blis_router/README.md`

**Step 1: Write README.md**

```markdown
# BLIS Router Optimization

Evolve adaptive routing logic for a BLIS multi-instance LLM inference cluster.

**Goal:** Minimize end-to-end latency by making `WeightedScoring.Route()` adaptive,
beating the static-weight baseline across 3 workloads.

## Prerequisites

- Python >= 3.10, [uv](https://docs.astral.sh/uv/)
- Go (for building BLIS simulator)
- `OPENAI_API_KEY` or equivalent LLM API key

## Setup

```bash
# Install SkyDiscover + external backends
uv sync --extra external

# Initialize submodule and verify Go build
git submodule update --init benchmarks/blis_router/inference-sim
cd benchmarks/blis_router/inference-sim && go build -o simulation_worker main.go && cd -
```

## Run a Single Framework

```bash
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  --search adaevolve \
  --iterations 50
```

## Run All 5 Frameworks

```bash
bash benchmarks/blis_router/scripts/run_all_frameworks.sh 50
```

## Compare Results

```bash
python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>
```

## Scoring

`score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms` (higher = better, less negative)

## Workloads

| Workload | Rate | Duration | Tests |
|----------|------|----------|-------|
| cache_warmup | 1000 req/s | 5s | Load-aware vs prefix-affinity tension |
| load_spikes | 1000 req/s | 5s | Heavy-hitter dominance |
| multiturn | 150 req/s | 10s | Session affinity benefits |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BLIS_MODEL` | `meta-llama/llama-3.1-8b-instruct` | Simulation model |
| `WEIGHTED_LATENCY` | `false` | Weight workloads by request count |
```

**Step 2: Commit**

```bash
git add benchmarks/blis_router/README.md
git commit -m "docs(blis): add benchmark README"
```

---

### Task 10: Smoke test with SkyDiscover (2 iterations, topk)

This is the end-to-end verification. Requires Go installed and inference-sim submodule initialized.

**Step 1: Run a 2-iteration smoke test**

```bash
uv run skydiscover-run \
  benchmarks/blis_router/initial_program.py \
  benchmarks/blis_router/evaluator.py \
  -c benchmarks/blis_router/config.yaml \
  --search topk \
  --iterations 2 \
  --output outputs/blis_router/smoke_test \
  -l DEBUG
```

**Step 2: Verify output**

```bash
ls outputs/blis_router/smoke_test/best/
cat outputs/blis_router/smoke_test/best/best_program_info.json
```
Expected: `best_program.py` exists, `best_program_info.json` contains `combined_score` (negative number, not -100000).

**Step 3: If smoke test passes, commit any adjustments**

```bash
git add -A benchmarks/blis_router/
git commit -m "feat(blis): BLIS router benchmark complete — smoke test passes"
```

---

### Task 11: Update CLAUDE.md and docs

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/running-experiments.md`

**Step 1: Add BLIS to CLAUDE.md doc index**

Already done (design doc reference). Verify the entry is present.

**Step 2: Add BLIS section to `docs/running-experiments.md`**

Add a section under "Reproducing Paper Results":

```markdown
## BLIS Router (All 5 Frameworks)

```bash
# Setup
git submodule update --init benchmarks/blis_router/inference-sim
uv sync --extra external

# Run all 5 frameworks (sequential, ~3 hours for 50 iterations)
bash benchmarks/blis_router/scripts/run_all_frameworks.sh 50

# Compare results
python benchmarks/blis_router/scripts/compare_results.py outputs/blis_router/<timestamp>
```
```

**Step 3: Commit**

```bash
git add CLAUDE.md docs/running-experiments.md
git commit -m "docs: add BLIS router to experiment guides"
```

---

## Checkpoint Summary

| After Task | What you have | Can verify |
|-----------|---------------|------------|
| 1-3 | Directory structure, submodule, static assets, initial program | `go build` works, EVOLVE-BLOCK markers present |
| 4-5 | Tests + evaluator | `pytest tests/test_blis_evaluator.py` passes |
| 6 | Config | YAML parses correctly |
| 7-8 | Experiment + comparison scripts | Scripts exist, are executable |
| 9 | README | Documentation complete |
| 10 | **Full end-to-end smoke test** | `skydiscover-run --search topk -i 2` produces valid output |
| 11 | Updated docs | CLAUDE.md and running-experiments.md reference BLIS |
