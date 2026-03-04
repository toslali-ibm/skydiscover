#!/usr/bin/env bash
# Run BLIS router benchmark across all search frameworks
set -euo pipefail

ITERATIONS=${1:-50}
MODEL="${BLIS_LLM_MODEL:-aws/claude-sonnet-4-5}"
BASE_DIR="benchmarks/blis_router"
OUTPUT_BASE="outputs/blis_router"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "BLIS Router Benchmark — ${ITERATIONS} iterations per framework"
echo "Model: $MODEL"
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

# Run each framework sequentially (they share the same routing.go file)
for SEARCH in adaevolve evox openevolve gepa shinkaevolve; do
  echo "=========================================="
  echo "Running: $SEARCH ($ITERATIONS iterations)"
  echo "=========================================="

  OUTPUT_DIR="${OUTPUT_BASE}/${TIMESTAMP}/${SEARCH}"
  mkdir -p "$OUTPUT_DIR"

  # Use framework-specific config if it exists, else base config
  CONFIG="$BASE_DIR/config.yaml"
  [[ -f "$BASE_DIR/config_${SEARCH}.yaml" ]] && CONFIG="$BASE_DIR/config_${SEARCH}.yaml"

  # Clean baseline cache so each framework starts fresh
  rm -f "$BASE_DIR/baseline_metrics.json"

  uv run skydiscover-run \
    "$BASE_DIR/initial_program.py" \
    "$BASE_DIR/evaluator.py" \
    -c "$CONFIG" \
    -s "$SEARCH" \
    -m "$MODEL" \
    -i "$ITERATIONS" \
    -o "$OUTPUT_DIR" \
    -l INFO \
    2>&1 | tee "${OUTPUT_DIR}/run.log"

  echo "$SEARCH complete. Results in $OUTPUT_DIR"
  echo ""
done

echo "All frameworks complete. Results in ${OUTPUT_BASE}/${TIMESTAMP}/"
echo "Run: python $BASE_DIR/scripts/compare_results.py ${OUTPUT_BASE}/${TIMESTAMP}"
