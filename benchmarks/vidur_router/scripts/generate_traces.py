#!/usr/bin/env python3
"""Generate Vidur trace CSV files from BLIS workload YAML specifications.

Reads the three BLIS workload YAMLs and produces per-seed Vidur-compatible
CSV files with columns: arrived_at, num_prefill_tokens, num_decode_tokens.

Usage:
    python scripts/generate_traces.py [--seeds 42,456] [--output-dir workloads]
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import yaml


BLIS_WORKLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "blis_router" / "workloads"

WORKLOAD_FILES = [
    ("cache_warmup", "workload_v3_cache_warmup.yaml"),
    ("load_spikes", "workload_v3_load_spikes.yaml"),
    ("multiturn", "workload_v3_multiturn.yaml"),
]

DEFAULT_SEEDS = [42, 456]

# Calibrated parameters for Vidur's 4-replica Llama-2-7B-on-A100 cluster.
# V3 workloads already use Vidur-calibrated rates (~12-17 QPS), so rate_scale=1.0.
# (V2 used high BLIS rates of 200-300 QPS and needed rate_scale=12-18x.)
VIDUR_CALIBRATION = {
    "cache_warmup": {"num_requests": 500, "rate_scale": 1.0},  # 16.7 QPS as-is
    "load_spikes":  {"num_requests": 500, "rate_scale": 1.0},  # 16.7 QPS as-is
    "multiturn":    {"num_requests": 1000, "rate_scale": 1.0},  # 25 QPS, same as BLIS
}

# Max position embeddings for target models (Llama-2-7B, Llama-3-8B)
MAX_MODEL_TOKENS = 4096


def _sample_distribution(rng: np.random.Generator, dist: dict, n: int) -> np.ndarray:
    """Sample n values from a BLIS token distribution spec."""
    dtype = dist["type"]
    params = dist["params"]

    if dtype == "gaussian":
        vals = rng.normal(params["mean"], params["std_dev"], size=n)
        lo = params.get("min", 1)
        hi = params.get("max", float("inf"))
        vals = np.clip(vals, lo, hi)
    elif dtype == "exponential":
        vals = rng.exponential(params["mean"], size=n)
    elif dtype == "constant":
        vals = np.full(n, params["value"])
    else:
        raise ValueError(f"Unsupported distribution type: {dtype}")

    return np.maximum(vals, 1).astype(int)


def _generate_arrivals(
    rng: np.random.Generator,
    process: str,
    rate: float,
    n: int,
    cv: float = 1.0,
) -> np.ndarray:
    """Generate n arrival timestamps (seconds) for a client group."""
    mean_iat = 1.0 / rate if rate > 0 else 1.0

    if process == "poisson":
        iats = rng.exponential(mean_iat, size=n)
    elif process == "gamma":
        shape = 1.0 / (cv * cv) if cv > 0 else 1.0
        scale = mean_iat * (cv * cv) if cv > 0 else mean_iat
        iats = rng.gamma(shape, scale, size=n)
    elif process == "constant":
        iats = np.full(n, mean_iat)
    else:
        raise ValueError(f"Unsupported arrival process: {process}")

    return np.cumsum(iats)


def _generate_multiturn_requests(
    rng: np.random.Generator,
    client: dict,
    arrivals: np.ndarray,
    prefix_length: int,
) -> list:
    """Flatten multi-turn sessions into individual requests.

    Each session start time comes from `arrivals`. Each session generates
    max_rounds requests with accumulated context.
    """
    mt = client["reasoning"]["multi_turn"]
    max_rounds = mt["max_rounds"]
    think_time_s = mt["think_time_us"] / 1e6

    input_dist = client["input_distribution"]
    output_dist = client["output_distribution"]

    rows = []
    for session_start in arrivals:
        accumulated_context = 0
        prev_arrival = session_start
        prev_decode_time_s = 0.0

        prev_output_tokens = 0
        for round_idx in range(max_rounds):
            new_input = int(max(1, _sample_distribution(rng, input_dist, 1)[0]))
            new_output = int(max(1, _sample_distribution(rng, output_dist, 1)[0]))

            if round_idx == 0:
                arrived_at = session_start
            else:
                # Next round arrives after think time + decode time of previous round
                # Estimate: ~7ms per decode token (A100 throughput)
                prev_decode_time_s = 0.007 * prev_output_tokens
                arrived_at = prev_arrival + think_time_s + prev_decode_time_s

            prefill_tokens = prefix_length + accumulated_context + new_input
            decode_tokens = new_output

            # Clip to model context limit (prefill + decode must fit)
            if prefill_tokens + decode_tokens > MAX_MODEL_TOKENS:
                prefill_tokens = min(prefill_tokens, MAX_MODEL_TOKENS - decode_tokens)
                if prefill_tokens < 1:
                    prefill_tokens = 1
                    decode_tokens = min(decode_tokens, MAX_MODEL_TOKENS - 1)

            rows.append({
                "arrived_at": arrived_at,
                "num_prefill_tokens": max(1, prefill_tokens),
                "num_decode_tokens": max(1, decode_tokens),
            })

            # Use clipped values for context accumulation (not raw sampled values)
            actual_input = prefill_tokens - prefix_length - accumulated_context
            accumulated_context += max(0, actual_input) + decode_tokens
            prev_output_tokens = decode_tokens
            prev_arrival = arrived_at

    return rows


def generate_trace_for_workload(
    workload_name: str,
    workload_path: Path,
    seed: int,
    calibrate: bool = True,
) -> list:
    """Generate Vidur trace rows from a single BLIS workload YAML.

    If calibrate=True (default), applies per-workload rate scaling and request
    count limits from VIDUR_CALIBRATION to produce traces that give reasonable
    E2E latencies on Vidur's 4-replica cluster.
    """
    with open(workload_path) as f:
        spec = yaml.safe_load(f)

    aggregate_rate = spec["aggregate_rate"]
    num_requests = spec["num_requests"]
    clients = spec["clients"]
    is_multiturn = spec.get("category") == "reasoning"

    # Apply Vidur calibration: scale down rate, cap request count
    cal = VIDUR_CALIBRATION.get(workload_name, {}) if calibrate else {}
    rate_scale = cal.get("rate_scale", 1.0)
    aggregate_rate = aggregate_rate / rate_scale
    num_requests = cal.get("num_requests", num_requests)

    all_rows = []
    master_rng = np.random.default_rng(seed)

    for client in clients:
        # Each client gets an isolated RNG for determinism
        client_seed = int(master_rng.integers(0, 2**31))
        client_rng = np.random.default_rng(client_seed)

        rate_fraction = client["rate_fraction"]
        client_rate = aggregate_rate * rate_fraction

        prefix_length = client.get("prefix_length", 0)

        arrival_spec = client.get("arrival", {"process": "poisson"})
        process = arrival_spec["process"]
        cv = arrival_spec.get("cv", 1.0)

        has_multiturn = "reasoning" in client and "multi_turn" in client.get("reasoning", {})

        if has_multiturn:
            mt = client["reasoning"]["multi_turn"]
            max_rounds = mt["max_rounds"]
            # Number of sessions: total requests from this group / rounds per session
            n_client_requests = int(num_requests * rate_fraction)
            n_sessions = max(1, math.ceil(n_client_requests / max_rounds))
            session_rate = client_rate / max_rounds

            session_arrivals = _generate_arrivals(
                client_rng, process, session_rate, n_sessions, cv
            )
            rows = _generate_multiturn_requests(
                client_rng, client, session_arrivals, prefix_length
            )
        else:
            n_client_requests = int(num_requests * rate_fraction)
            arrivals = _generate_arrivals(
                client_rng, process, client_rate, n_client_requests, cv
            )

            input_tokens = _sample_distribution(
                client_rng, client["input_distribution"], n_client_requests
            )
            output_tokens = _sample_distribution(
                client_rng, client["output_distribution"], n_client_requests
            )

            rows = []
            for i in range(n_client_requests):
                prefill = prefix_length + int(input_tokens[i])
                decode = max(1, int(output_tokens[i]))
                # Clip to model context limit
                if prefill + decode > MAX_MODEL_TOKENS:
                    prefill = min(prefill, MAX_MODEL_TOKENS - decode)
                    if prefill < 1:
                        prefill = 1
                        decode = min(decode, MAX_MODEL_TOKENS - 1)
                rows.append({
                    "arrived_at": float(arrivals[i]),
                    "num_prefill_tokens": max(1, prefill),
                    "num_decode_tokens": decode,
                })

        all_rows.extend(rows)

    # Sort by arrival and truncate to num_requests
    all_rows.sort(key=lambda r: r["arrived_at"])
    if len(all_rows) > num_requests:
        all_rows = all_rows[:num_requests]

    return all_rows


def write_trace_csv(rows: list, output_path: Path) -> None:
    """Write trace rows to a Vidur-compatible CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("arrived_at,num_prefill_tokens,num_decode_tokens\n")
        for row in rows:
            f.write(f"{row['arrived_at']:.6f},{row['num_prefill_tokens']},{row['num_decode_tokens']}\n")


def main():
    parser = argparse.ArgumentParser(description="Generate Vidur traces from BLIS workloads")
    parser.add_argument(
        "--seeds", default=",".join(str(s) for s in DEFAULT_SEEDS),
        help="Comma-separated seeds (default: 42,456)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent.parent / "workloads"),
        help="Output directory for trace CSVs",
    )
    parser.add_argument(
        "--no-calibrate", action="store_true",
        help="Disable Vidur calibration (use raw BLIS rates)",
    )
    args = parser.parse_args()

    seeds = [int(s.strip()) for s in args.seeds.split(",")]
    output_dir = Path(args.output_dir)
    calibrate = not args.no_calibrate

    mode = "calibrated for Vidur" if calibrate else "raw BLIS rates"
    print(f"Generating traces for seeds {seeds} → {output_dir} ({mode})")
    print(f"BLIS workloads dir: {BLIS_WORKLOADS_DIR}")
    if calibrate:
        print(f"Calibration: {VIDUR_CALIBRATION}")

    for workload_name, yaml_file in WORKLOAD_FILES:
        yaml_path = BLIS_WORKLOADS_DIR / yaml_file
        if not yaml_path.exists():
            print(f"  ERROR: {yaml_path} not found", file=sys.stderr)
            sys.exit(1)

        for seed in seeds:
            rows = generate_trace_for_workload(workload_name, yaml_path, seed, calibrate=calibrate)
            csv_name = f"{workload_name}_seed{seed}.csv"
            csv_path = output_dir / csv_name
            write_trace_csv(rows, csv_path)

            # Summary stats
            prefills = [r["num_prefill_tokens"] for r in rows]
            decodes = [r["num_decode_tokens"] for r in rows]
            duration = rows[-1]["arrived_at"] - rows[0]["arrived_at"] if len(rows) > 1 else 0
            qps = len(rows) / duration if duration > 0 else 0

            print(f"  {csv_name}: {len(rows)} requests, "
                  f"duration={duration:.1f}s, QPS={qps:.1f}, "
                  f"prefill=[{min(prefills)}-{max(prefills)}], "
                  f"decode=[{min(decodes)}-{max(decodes)}]")

    # Write README
    readme_path = output_dir / "README.md"
    readme_path.write_text(
        "# Vidur Workload Traces\n\n"
        "Generated from BLIS workload YAML specifications by `scripts/generate_traces.py`.\n\n"
        "## CSV Format\n\n"
        "| Column | Type | Description |\n"
        "|--------|------|-------------|\n"
        "| `arrived_at` | float | Arrival timestamp in seconds |\n"
        "| `num_prefill_tokens` | int | Input/prompt tokens |\n"
        "| `num_decode_tokens` | int | Output/completion tokens |\n\n"
        "## Mapping from BLIS\n\n"
        "| BLIS concept | Vidur mapping | Notes |\n"
        "|---|---|---|\n"
        "| `prefix_length + input_tokens` | `num_prefill_tokens` | Prefix = extra prefill (no caching in Vidur) |\n"
        "| `output_distribution` | `num_decode_tokens` | Direct mapping |\n"
        "| Arrival process | `arrived_at` timestamps | Direct generation |\n"
        "| `slo_class`, `prefix_group`, `streaming`, `multi_turn` | Not mapped | Vidur lacks these concepts |\n\n"
        "## Caveats\n\n"
        "- Vidur does not model prefix caching, SLO classes, or sessions\n"
        "- This reduces the optimization surface vs BLIS\n"
        "- Smaller improvements are expected and consistent with the reduced signal surface\n"
        "- Multi-turn sessions are flattened into independent requests with accumulated context\n\n"
        "## Load Calibration\n\n"
        "BLIS workload rates (200/300/150 req/s) overwhelm Vidur's 4-replica cluster.\n"
        "By default, traces are generated with calibrated rates (~10-17 QPS) and\n"
        "reduced request counts (~400-500) so simulations complete in ~30-60s with\n"
        "E2E latencies of 500-1500ms. Use `--no-calibrate` for raw BLIS rates.\n"
    )
    print(f"\nWrote README to {readme_path}")
    print("Done!")


if __name__ == "__main__":
    main()
