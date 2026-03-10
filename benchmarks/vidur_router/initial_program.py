"""Vidur Global Scheduler — Evolved Routing Algorithm.

This module defines a request routing function for an LLM inference cluster
simulated by Vidur (discrete-event simulator). The cluster has N replicas,
each running the same LLM model on A100 GPUs.

GOAL: Minimize end-to-end request latency and tail latency (P95).
Score = -0.5 * avg_e2e_ms - 0.5 * avg_p95_ms (higher/less negative = better)

AVAILABLE SIGNALS PER REQUEST (properties on request objects):
  - arrived_at (float): arrival timestamp in seconds
  - num_prefill_tokens (int): input/prompt tokens (larger = longer first-token time)
  - num_decode_tokens (int): output tokens to generate
  - total_tokens (int): prefill + decode
  - pd_ratio (float): prefill / decode ratio (high = compute-heavy prefill)

AVAILABLE SIGNALS PER REPLICA (via replica_schedulers dict values):
  - replica_id (int): unique replica identifier
  - num_pending_requests (int): queue depth (requests waiting + in-progress)
  - memory_usage_percent (int): 0-100% KV-cache memory utilization
  - is_empty() (bool): True if no pending work at all

CLUSTER INFO:
  - num_replicas (int): total number of replicas in the cluster

KNOWN WEAKNESS OF BASELINE (LOR):
  LOR only considers queue depth. It ignores request size — routing a 4096-token
  prefill to a replica with 1 pending request is suboptimal if another replica
  with 2 small pending requests would finish sooner. It also ignores memory
  pressure — a replica at high memory utilization may preempt requests.

SIGNAL FRESHNESS:
  All signals are real-time (simulation state). In production, queue depth and
  memory would have ~5s staleness from Prometheus scraping. Algorithms robust
  to stale signals are preferred.

NOTE ON MEMORY SIGNALS:
  Memory signals (memory_usage_percent) start at 0 for all replicas and only
  become informative after requests have been processed. Early scheduling rounds
  should rely on queue depth (num_pending_requests).

RULES:
  - Only modify code between EVOLVE-BLOCK-START and EVOLVE-BLOCK-END
  - Must be valid Python — no syntax errors
  - Must return List[Tuple[int, request]] — (replica_id, request) pairs
  - replica_id values MUST come from replica_schedulers.keys()
  - Must consume all requests from request_queue
  - Only use typing and math imports (no external packages)
  - Guard all divisions against zero
"""
from typing import List, Tuple, Dict, Any


# EVOLVE-BLOCK-START
def schedule(request_queue: list, replica_schedulers: dict, num_replicas: int) -> List[Tuple[int, Any]]:
    """Route queued requests to replicas using Least Outstanding Requests (LOR).

    Args:
        request_queue: List of Request objects waiting to be scheduled.
        replica_schedulers: Dict mapping replica_id -> ReplicaScheduler.
        num_replicas: Number of replicas in the cluster.

    Returns:
        List of (replica_id, request) tuples assigning each request to a replica.
    """
    request_queue.sort(key=lambda r: r.arrived_at)
    request_mapping = []
    pending_requests_map = {
        rs.replica_id: rs.num_pending_requests
        for rs in replica_schedulers.values()
    }
    while request_queue:
        request = request_queue.pop(0)
        replica_id = min(pending_requests_map.items(), key=lambda x: x[1])[0]
        pending_requests_map[replica_id] += 1
        request_mapping.append((replica_id, request))
    return request_mapping
# EVOLVE-BLOCK-END
