# Vidur Workload Traces

Generated from BLIS workload YAML specifications by `scripts/generate_traces.py`.

## CSV Format

| Column | Type | Description |
|--------|------|-------------|
| `arrived_at` | float | Arrival timestamp in seconds |
| `num_prefill_tokens` | int | Input/prompt tokens |
| `num_decode_tokens` | int | Output/completion tokens |

## Mapping from BLIS

| BLIS concept | Vidur mapping | Notes |
|---|---|---|
| `prefix_length + input_tokens` | `num_prefill_tokens` | Prefix = extra prefill (no caching in Vidur) |
| `output_distribution` | `num_decode_tokens` | Direct mapping |
| Arrival process | `arrived_at` timestamps | Direct generation |
| `slo_class`, `prefix_group`, `streaming`, `multi_turn` | Not mapped | Vidur lacks these concepts |

## Caveats

- Vidur does not model prefix caching, SLO classes, or sessions
- This reduces the optimization surface vs BLIS
- Smaller improvements are expected and consistent with the reduced signal surface
- Multi-turn sessions are flattened into independent requests with accumulated context

## Load Calibration

BLIS workload rates (200/300/150 req/s) may not be appropriate for Vidur's execution model.
Use `time_scale_factor` in the evaluator to calibrate per-workload utilization.
Target ~70-80% utilization so routing decisions matter.
