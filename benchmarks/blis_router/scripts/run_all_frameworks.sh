#!/usr/bin/env bash
# Run BLIS router benchmark across all search frameworks
#
# Usage: bash benchmarks/blis_router/scripts/run_all_frameworks.sh [iterations] [seed]
#
# Experiment isolation guarantees:
# - Each framework gets its own output directory
# - BLIS_OUTPUT_DIR env var directs all artifacts (baseline, etc.) to that directory
# - routing.go is restored by the evaluator after every evaluation call
# - __pycache__ and .eval_artifacts are cleaned between frameworks
# - No data leaks between experiments
set -euo pipefail

ITERATIONS=${1:-50}
SEED="${2:-42}"
MODEL="${BLIS_LLM_MODEL:-aws/claude-sonnet-4-5}"
BASE_DIR="benchmarks/blis_router"
OUTPUT_BASE="outputs/blis_router"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "BLIS Router Benchmark — ${ITERATIONS} iterations per framework"
echo "Model: $MODEL"
echo "Seed: $SEED"
echo "Output: ${OUTPUT_BASE}/${TIMESTAMP}/"
echo ""

# Ensure deps
uv sync --extra external

# Verify Go + BLIS build
echo "Verifying BLIS build..."
(cd "$BASE_DIR/inference-sim" && go build -o simulation_worker main.go) || {
  echo "FATAL: BLIS build failed. Install Go and check inference-sim submodule."
  exit 1
}
echo "Build OK"
echo ""

# Portable file hash (works on macOS and Linux)
file_hash() {
  if command -v md5sum &>/dev/null; then
    md5sum "$1" | cut -d' ' -f1
  else
    md5 -q "$1"
  fi
}

# Save original routing.go hash for verification
ORIGINAL_ROUTING_HASH=$(file_hash "$BASE_DIR/inference-sim/sim/routing.go")

# Clean any leftover artifacts from previous runs
cleanup_between_experiments() {
  # Remove Python cache from benchmark dir
  find "$BASE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  # Remove fallback eval artifacts (should not exist if BLIS_OUTPUT_DIR is set)
  rm -rf "$BASE_DIR/.eval_artifacts"
  # Verify routing.go is unchanged
  local current_hash
  current_hash=$(file_hash "$BASE_DIR/inference-sim/sim/routing.go")
  if [[ "$current_hash" != "$ORIGINAL_ROUTING_HASH" ]]; then
    echo "WARNING: routing.go was modified! Restoring from git..."
    (cd "$BASE_DIR/inference-sim" && git checkout sim/routing.go)
  fi
}

# Always clean up on exit (even if a framework fails)
trap cleanup_between_experiments EXIT

# Save experiment metadata
METADATA_DIR="${OUTPUT_BASE}/${TIMESTAMP}"
mkdir -p "$METADATA_DIR"
cat > "${METADATA_DIR}/experiment_config.json" <<JSONEOF
{
  "iterations": ${ITERATIONS},
  "seed": ${SEED},
  "model": "${MODEL}",
  "timestamp": "${TIMESTAMP}",
  "inference_sim_commit": "$(cd "$BASE_DIR/inference-sim" && git rev-parse HEAD)",
  "skydiscover_commit": "$(git rev-parse HEAD)"
}
JSONEOF

# Run each framework sequentially (they share the same routing.go file)
FAILED_FRAMEWORKS=()
for SEARCH in adaevolve evox openevolve gepa shinkaevolve; do
  echo "=========================================="
  echo "Running: $SEARCH ($ITERATIONS iterations)"
  echo "=========================================="

  OUTPUT_DIR="${OUTPUT_BASE}/${TIMESTAMP}/${SEARCH}"
  mkdir -p "$OUTPUT_DIR"

  # Use framework-specific config if it exists, else base config
  CONFIG="$BASE_DIR/config.yaml"
  [[ -f "$BASE_DIR/config_${SEARCH}.yaml" ]] && CONFIG="$BASE_DIR/config_${SEARCH}.yaml"

  # Clean state between experiments
  cleanup_between_experiments

  # Set output dir and seed so evaluator writes artifacts there (not in benchmark dir)
  export BLIS_OUTPUT_DIR="$OUTPUT_DIR"
  export BLIS_SEED="$SEED"

  if uv run skydiscover-run \
    "$BASE_DIR/initial_program.py" \
    "$BASE_DIR/evaluator.py" \
    -c "$CONFIG" \
    -s "$SEARCH" \
    -m "$MODEL" \
    -i "$ITERATIONS" \
    -o "$OUTPUT_DIR" \
    -l INFO \
    2>&1 | tee "${OUTPUT_DIR}/run.log"; then
    echo "$SEARCH complete. Results in $OUTPUT_DIR"
  else
    echo "WARNING: $SEARCH failed. See ${OUTPUT_DIR}/run.log"
    FAILED_FRAMEWORKS+=("$SEARCH")
  fi

  echo ""
done

echo "All frameworks complete. Results in ${OUTPUT_BASE}/${TIMESTAMP}/"
if [[ ${#FAILED_FRAMEWORKS[@]} -gt 0 ]]; then
  echo "WARNING: Failed frameworks: ${FAILED_FRAMEWORKS[*]}"
fi
echo "Run: python $BASE_DIR/scripts/compare_results.py ${OUTPUT_BASE}/${TIMESTAMP}"
