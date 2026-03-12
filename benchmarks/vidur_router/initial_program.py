from typing import List, Tuple, Dict, Any


def schedule(request_queue: list, replica_schedulers: dict, num_replicas: int) -> List[Tuple[int, Any]]:
    """Route queued requests to replicas. Returns list of (replica_id, request) tuples."""
    # EVOLVE-BLOCK-START
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
    # EVOLVE-BLOCK-END
    return request_mapping
