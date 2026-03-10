#!/usr/bin/env bash
# Run all search frameworks sequentially for the Vidur router benchmark.
#
# Usage:
#   ./scripts/run_all_frameworks.sh <experiment_name> [iterations]
#
# Example:
#   ./scripts/run_all_frameworks.sh 260310_50i_baseline 50

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$(dirname "$BENCHMARK_DIR")")"

EXPERIMENT="${1:?Usage: $0 <experiment_name> [iterations]}"
ITERATIONS="${2:-50}"
SEED="${VIDUR_SEED:-42,456}"
MULTI_LLM="${VIDUR_MULTI_LLM:-1}"

FRAMEWORKS=(adaevolve evox openevolve gepa_native)
RESULTS_DIR="$PROJECT_ROOT/outputs/vidur_router/$EXPERIMENT"
FAILED=()

echo "============================================================"
echo "Vidur Router Experiment: $EXPERIMENT"
echo "Iterations: $ITERATIONS"
echo "Seed(s): $SEED"
echo "Multi-LLM: $MULTI_LLM"
echo "Results: $RESULTS_DIR"
echo "============================================================"

# Record experiment config
mkdir -p "$RESULTS_DIR"
cat > "$RESULTS_DIR/experiment_config.json" <<EOF
{
  "experiment": "$EXPERIMENT",
  "iterations": $ITERATIONS,
  "seeds": "$SEED",
  "multi_llm": "$MULTI_LLM",
  "frameworks": $(printf '%s\n' "${FRAMEWORKS[@]}" | jq -R . | jq -s .),
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

for FW in "${FRAMEWORKS[@]}"; do
    echo ""
    echo "============================================================"
    echo "Framework: $FW"
    echo "============================================================"

    FW_DIR="$RESULTS_DIR/$FW"
    mkdir -p "$FW_DIR"

    export VIDUR_OUTPUT_DIR="$FW_DIR"
    export VIDUR_SEED="$SEED"
    export VIDUR_MULTI_LLM="$MULTI_LLM"

    # Run framework
    if uv run skydiscover-run \
        "$BENCHMARK_DIR/initial_program.py" \
        "$BENCHMARK_DIR/evaluator.py" \
        -c "$BENCHMARK_DIR/config.yaml" \
        -s "$FW" \
        -i "$ITERATIONS" \
        -o "$FW_DIR" \
        -l INFO; then
        echo "  $FW: SUCCESS"
    else
        echo "  $FW: FAILED"
        FAILED+=("$FW")
    fi

    # Cleanup __pycache__ between frameworks
    find "$BENCHMARK_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    # Isolation check: no baseline_metrics.json leaked to benchmark dir
    if [ -f "$BENCHMARK_DIR/baseline_metrics.json" ]; then
        echo "  WARNING: baseline_metrics.json leaked to benchmark dir!"
        rm -f "$BENCHMARK_DIR/baseline_metrics.json"
    fi

    echo "  $FW: cleanup done"
done

echo ""
echo "============================================================"
echo "COMPLETE"
echo "============================================================"
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "Failed frameworks: ${FAILED[*]}"
else
    echo "All frameworks completed successfully"
fi
echo ""
echo "Next steps:"
echo "  python $BENCHMARK_DIR/scripts/compare_results.py $RESULTS_DIR"
echo "  python $BENCHMARK_DIR/scripts/plot_results.py $RESULTS_DIR"
echo "  python $BENCHMARK_DIR/scripts/analyze_effort.py $RESULTS_DIR"
echo "  python $BENCHMARK_DIR/scripts/analyze_diffs.py $RESULTS_DIR"
