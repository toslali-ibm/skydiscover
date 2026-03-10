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

BLIS workload rates (200/300/150 req/s) overwhelm Vidur's 4-replica cluster.
By default, traces are generated with calibrated rates (~10-17 QPS) and
reduced request counts (~400-500) so simulations complete in ~30-60s with
E2E latencies of 500-1500ms. Use `--no-calibrate` for raw BLIS rates.
